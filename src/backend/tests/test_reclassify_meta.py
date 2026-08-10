"""Tests für die Korrektur-Job-Metadaten bei Triage-Reklassifikation.

Prüft die reine Funktion ``_build_corrective_meta`` (kein DB-Zugriff):
- Klont Original-Job-Metadaten und ergänzt forced_class/correction_reason
- Fallback-Rekonstruktion aus dem EmailTriage-Eintrag ohne Original-Job
- Entfernung irreführender Reste des letzten Laufs (draft_id, trace, …)
"""

from types import SimpleNamespace

from app.routers.triage import _build_corrective_meta


def _make_item(message_id="AAMk999", subject="Re: Angebot", from_address="kunde@firma.ch", from_name="Frau Müller"):
    return SimpleNamespace(
        message_id=message_id,
        subject=subject,
        from_address=from_address,
        from_name=from_name,
    )


def test_clones_original_meta_and_sets_forced_class():
    item = _make_item()
    orig = {
        "email_message_id": "AAMk999",
        "message_id": "AAMk999",
        "subject": "Re: Angebot",
        "from_address": "kunde@firma.ch",
        "from_name": "Frau Müller",
        "conversation_id": "conv-xyz",
        "body_preview": "Guten Tag …",
    }
    meta = _build_corrective_meta(orig, item, forced_class="auto_reply", reason="Kunde erwartet Antwort")

    assert meta["forced_class"] == "auto_reply"
    assert meta["correction_reason"] == "Kunde erwartet Antwort"
    assert meta["is_correction"] is True
    # Original-Felder bleiben erhalten
    assert meta["conversation_id"] == "conv-xyz"
    assert meta["email_message_id"] == "AAMk999"


def test_fallback_reconstruction_without_original_meta():
    item = _make_item()
    meta = _build_corrective_meta(None, item, forced_class="task", reason=None)

    assert meta["email_message_id"] == "AAMk999"
    assert meta["message_id"] == "AAMk999"
    assert meta["subject"] == "Re: Angebot"
    assert meta["from_address"] == "kunde@firma.ch"
    assert meta["from_name"] == "Frau Müller"
    assert meta["forced_class"] == "task"
    assert meta["correction_reason"] == ""
    assert meta["is_correction"] is True


def test_strips_stale_run_artifacts():
    item = _make_item()
    orig = {
        "email_message_id": "AAMk999",
        "draft_id": "old-draft-123",
        "original_draft_html": "<p>alt</p>",
        "trace": [{"step": 1}],
        "tools_used": ["create_draft"],
        "self_grade": {"score": 0.4},
        "feedback_captured": True,
    }
    meta = _build_corrective_meta(orig, item, forced_class="task", reason="Lieber eine Aufgabe")

    for stale in ("draft_id", "original_draft_html", "trace", "tools_used", "self_grade", "feedback_captured"):
        assert stale not in meta
    assert meta["forced_class"] == "task"


def test_does_not_mutate_original_meta():
    item = _make_item()
    orig = {"email_message_id": "AAMk999", "draft_id": "keep-me"}
    _build_corrective_meta(orig, item, forced_class="task", reason=None)
    # Die übergebenen Original-Metadaten dürfen nicht verändert werden.
    assert orig["draft_id"] == "keep-me"
    assert "forced_class" not in orig


def test_forced_label_is_carried_into_the_job():
    """Eine gleichzeitige Label-Korrektur muss den Job erreichen.

    Sonst setzt ``_apply_label_correction`` die Kategorie, und der unmittelbar
    folgende Korrektur-Job überschreibt sie mit dem geratenen LLM-Label.
    """
    item = _make_item()
    meta = _build_corrective_meta(
        None, item, forced_class="task", reason=None, forced_label="Wichtig"
    )
    assert meta["forced_label"] == "Wichtig"


def test_without_label_correction_no_forced_label_key():
    """Reine Klassen-Korrektur lässt dem Modell die Label-Entscheidung."""
    item = _make_item()
    meta = _build_corrective_meta(None, item, forced_class="task", reason=None)
    assert "forced_label" not in meta
