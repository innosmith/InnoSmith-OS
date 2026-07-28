"""Unit-Tests fuer die deterministische Mustererkennung des Reflexions-Jobs.

Testet ``_build_proposals`` rein (ohne DB): aus Korrektursignalen werden bei
wiederkehrenden Mustern Regel-Vorschlaege abgeleitet.
"""

from types import SimpleNamespace

import pytest

from app.services.reflection import _build_proposals, _existing_rule_signatures


def _fb(feedback_type, *, sender=None, original=None, corrected=None):
    return SimpleNamespace(
        feedback_type=feedback_type,
        sender_email=sender,
        original=original or {},
        corrected=corrected or {},
    )


class TestBuildProposals:
    def test_recurring_reclass_creates_triage_rule(self):
        fb = [
            _fb("triage_reclass", sender="kunde@firma.ch",
                original={"triage_class": "fyi"}, corrected={"triage_class": "task"}),
            _fb("triage_reclass", sender="Kunde@Firma.ch",
                original={"triage_class": "fyi"}, corrected={"triage_class": "task"}),
        ]
        proposals = _build_proposals(fb, min_occurrences=2)
        assert len(proposals) == 1
        scope, text, evidence, hint = proposals[0]
        assert scope == "triage"
        assert "task" in text
        assert evidence["count"] == 2
        assert evidence["to_class"] == "task"
        assert hint == "L1"

    def test_below_threshold_no_proposal(self):
        fb = [
            _fb("triage_reclass", sender="a@b.ch",
                original={"triage_class": "fyi"}, corrected={"triage_class": "task"}),
        ]
        assert _build_proposals(fb, min_occurrences=2) == []

    def test_same_class_is_ignored(self):
        fb = [
            _fb("triage_reclass", sender="a@b.ch",
                original={"triage_class": "task"}, corrected={"triage_class": "task"}),
            _fb("triage_reclass", sender="a@b.ch",
                original={"triage_class": "task"}, corrected={"triage_class": "task"}),
        ]
        assert _build_proposals(fb, min_occurrences=2) == []

    def test_recurring_draft_edits_create_draft_rule(self):
        fb = [
            _fb("draft_edit", sender="vip@kunde.ch"),
            _fb("draft_edit", sender="vip@kunde.ch"),
            _fb("draft_edit", sender="vip@kunde.ch"),
        ]
        proposals = _build_proposals(fb, min_occurrences=2)
        assert len(proposals) == 1
        scope, text, evidence, _hint = proposals[0]
        assert scope == "draft"
        assert "search_my_replies" in text
        assert evidence["count"] == 3

    def test_case_insensitive_sender_grouping(self):
        fb = [
            _fb("draft_edit", sender="X@Y.ch"),
            _fb("draft_edit", sender="x@y.ch"),
        ]
        proposals = _build_proposals(fb, min_occurrences=2)
        assert len(proposals) == 1
        assert proposals[0][2]["sender"] == "x@y.ch"

    def test_feedback_without_sender_ignored(self):
        fb = [
            _fb("draft_edit", sender=None),
            _fb("draft_edit", sender=None),
        ]
        assert _build_proposals(fb, min_occurrences=2) == []

    def test_discarded_task_suggestions_create_triage_rule(self):
        # Haeufigstes Realbetrieb-Signal: wiederholt verworfene Task-Vorschlaege
        # desselben Absenders -> zurueckhaltende Triage-Leitregel.
        fb = [
            _fb("task_deleted", sender="alerts@system.ch"),
            _fb("task_deleted", sender="alerts@system.ch"),
            _fb("rejected", sender="alerts@system.ch"),
        ]
        proposals = _build_proposals(fb, min_occurrences=3)
        assert len(proposals) == 1
        scope, text, evidence, hint = proposals[0]
        assert scope == "triage"
        assert "fyi" in text.lower()
        assert evidence["signal"] == "discarded_suggestions"
        assert evidence["count"] == 3
        assert hint == "L1"

    def test_discarded_below_threshold_no_proposal(self):
        fb = [
            _fb("task_deleted", sender="alerts@system.ch"),
            _fb("task_deleted", sender="alerts@system.ch"),
        ]
        assert _build_proposals(fb, min_occurrences=3) == []

    def test_own_correspondent_gets_no_blanket_rule(self):
        """Kein Pauschal-fyi fuer Adressen, an die Anthony selbst schreibt.

        Genau hier vergiftete sich die Lernschleife: aus abgelehnten Task-Vorschlaegen
        entstanden Regeln, die Swiss Bankers, BFH, T&R und Anthonys eigene Adresse
        kuenftig zurueckhaltend behandeln sollten -- also genau die wichtigen Absender.
        """
        fb = [
            _fb("task_deleted", sender="angela.parenta@swissbankers.ch"),
            _fb("task_deleted", sender="angela.parenta@swissbankers.ch"),
            _fb("task_deleted", sender="angela.parenta@swissbankers.ch"),
        ]
        proposals = _build_proposals(
            fb, min_occurrences=3,
            correspondents={"angela.parenta@swissbankers.ch"},
        )
        assert proposals == []

    def test_machine_sender_still_gets_rule(self):
        """Echtes Rauschen wird weiterhin gelernt -- die Schleife bleibt wirksam.

        Leadinfo, Toggl und LinkedIn sind die drei zu Recht aktiven Regeln; an diese
        Adressen hat Anthony nie geschrieben.
        """
        fb = [
            _fb("task_deleted", sender="izabela@leadinfo.com"),
            _fb("task_deleted", sender="izabela@leadinfo.com"),
            _fb("rejected", sender="izabela@leadinfo.com"),
        ]
        proposals = _build_proposals(
            fb, min_occurrences=3,
            correspondents={"angela.parenta@swissbankers.ch"},
        )
        assert len(proposals) == 1
        assert proposals[0][2]["sender"] == "izabela@leadinfo.com"

    def test_correspondent_exemption_is_case_insensitive(self):
        fb = [
            _fb("task_deleted", sender="Angela.Parenta@SwissBankers.ch"),
            _fb("task_deleted", sender="angela.parenta@swissbankers.ch"),
            _fb("rejected", sender="ANGELA.PARENTA@swissbankers.ch"),
        ]
        proposals = _build_proposals(
            fb, min_occurrences=3,
            correspondents={"Angela.Parenta@swissbankers.CH"},
        )
        assert proposals == []

    def test_reclass_rule_still_created_for_correspondents(self):
        """Die Gegenrichtung bleibt offen: explizite Umklassifikation wird gelernt.

        Nur die Pauschal-Daempfung ist ausgenommen, nicht das Lernen an sich -- sonst
        koennte das System 'Finanzen' fuer T&R nie lernen.
        """
        fb = [
            _fb("triage_reclass", sender="dominique.chuard@t-r.ch",
                original={"triage_class": "fyi"}, corrected={"triage_class": "task"}),
            _fb("triage_reclass", sender="dominique.chuard@t-r.ch",
                original={"triage_class": "fyi"}, corrected={"triage_class": "task"}),
        ]
        proposals = _build_proposals(
            fb, min_occurrences=2,
            correspondents={"dominique.chuard@t-r.ch"},
        )
        assert len(proposals) == 1
        assert proposals[0][2]["key"] == "triage:dominique.chuard@t-r.ch:fyi->task"


class TestSemanticKey:
    """Der semantische Schluessel darf NICHT vom Beleg-Zaehler abhaengen -- sonst
    wird eine verworfene Regel bei steigendem Zaehler erneut vorgeschlagen."""

    def test_reclass_key_present_and_stable(self):
        def _key_for(count_pairs):
            fb = [
                _fb("triage_reclass", sender="kunde@firma.ch",
                    original={"triage_class": "fyi"}, corrected={"triage_class": "task"})
                for _ in range(count_pairs)
            ]
            (_scope, text, evidence, _hint), = _build_proposals(fb, min_occurrences=2)
            return evidence["key"], text

        key2, text2 = _key_for(2)
        key3, text3 = _key_for(3)
        assert key2 == "triage:kunde@firma.ch:fyi->task"
        assert key2 == key3          # Schluessel stabil ueber Zaehler hinweg
        assert text2 != text3        # rule_text enthaelt den Zaehler -> aendert sich

    def test_discard_and_draft_keys(self):
        fb = [
            _fb("task_deleted", sender="alerts@system.ch"),
            _fb("rejected", sender="alerts@system.ch"),
            _fb("draft_edit", sender="vip@kunde.ch"),
            _fb("draft_edit", sender="vip@kunde.ch"),
        ]
        proposals = _build_proposals(fb, min_occurrences=2)
        keys = {p[2]["key"] for p in proposals}
        assert "triage:alerts@system.ch:discard" in keys
        assert "draft:vip@kunde.ch:style" in keys

    def test_reclass_key_without_from_class(self):
        fb = [
            _fb("triage_reclass", sender="a@b.ch", corrected={"triage_class": "task"}),
            _fb("triage_reclass", sender="a@b.ch", corrected={"triage_class": "task"}),
        ]
        (_s, _t, evidence, _h), = _build_proposals(fb, min_occurrences=2)
        assert evidence["key"] == "triage:a@b.ch:*->task"


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


class TestExistingRuleSignatures:
    @pytest.mark.asyncio
    async def test_collects_keys_and_texts(self):
        rows = [
            ("E-Mails von a@b.ch als 'task' triagieren. Belegt durch 2 manuelle Korrekturen.",
             {"key": "triage:a@b.ch:fyi->task", "count": 2}),
            ("Altregel ohne Key", None),
        ]
        sigs = await _existing_rule_signatures(_FakeDB(rows))
        # Semantischer Key vorhanden (auch wenn Regel rejected wurde)
        assert "triage:a@b.ch:fyi->task" in sigs
        # rule_text-Fallback fuer Altdaten ohne Key
        assert "Altregel ohne Key" in sigs

    @pytest.mark.asyncio
    async def test_rejected_rule_blocks_higher_count_proposal(self):
        # Verworfene Regel (Zaehler 2) liegt in der DB -> neuer Vorschlag mit
        # Zaehler 3 (anderer rule_text, gleicher Key) muss geblockt werden.
        rejected_text = ("E-Mails von x@y.ch als 'task' triagieren. "
                         "Belegt durch 2 manuelle Korrekturen.")
        sigs = await _existing_rule_signatures(
            _FakeDB([(rejected_text, {"key": "triage:x@y.ch:fyi->task", "count": 2})])
        )
        fb = [
            _fb("triage_reclass", sender="x@y.ch",
                original={"triage_class": "fyi"}, corrected={"triage_class": "task"})
            for _ in range(3)
        ]
        (_scope, rule_text, evidence, _hint), = _build_proposals(fb, min_occurrences=2)
        key = evidence["key"]
        # Skip-Bedingung aus run_reflection nachgebildet:
        skipped = (key and key in sigs) or rule_text in sigs
        assert skipped is True
