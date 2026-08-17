"""Tests für die Triage-Prompt-Qualität.

Prüft:
- PFLICHT-Block enthalten ("MUSS aufgerufen werden" / "PFLICHT")
- Thread-Hint bei vorhandener conversation_id
- Keine ASCII-Umlaute (ue, ae, oe als Umlaut-Ersatz) im generierten Prompt
"""

import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest


def _make_fake_job(
    message_id="AAMk123",
    subject="Testbetreff",
    from_address="test@example.com",
    from_name="Test Sender",
    conversation_id="conv-abc",
    body_preview="Dies ist eine Test-E-Mail",
    inference_classification="focused",
    recipient_type="to",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        metadata_json={
            "email_message_id": message_id,
            "subject": subject,
            "from_address": from_address,
            "from_name": from_name,
            "conversation_id": conversation_id,
            "body_preview": body_preview,
            "inference_classification": inference_classification,
            "recipient_type": recipient_type,
        },
    )


@pytest.fixture
def fake_job():
    return _make_fake_job()


@pytest.fixture
def fake_job_no_conversation():
    return _make_fake_job(conversation_id="")


class TestTriagePrompt:

    @pytest.mark.asyncio
    async def test_pflicht_block_present(self, fake_job):
        prompt = await self._build_prompt(fake_job)
        assert "PFLICHT" in prompt, "PFLICHT-Block fehlt im Prompt"
        assert "MUSS" in prompt or "muss" in prompt.lower()

    @pytest.mark.asyncio
    async def test_thread_hint_with_conversation_id(self, fake_job):
        prompt = await self._build_prompt(fake_job)
        assert "conv-abc" in prompt, "conversation_id sollte im Thread-Hint erscheinen"
        assert "get_thread" in prompt

    @pytest.mark.asyncio
    async def test_thread_hint_absent_without_conversation_id(self, fake_job_no_conversation):
        prompt = await self._build_prompt(fake_job_no_conversation)
        assert 'get_thread("")' in prompt or "get_thread" in prompt

    @pytest.mark.asyncio
    async def test_no_ascii_umlaut_replacements(self, fake_job):
        """Prüft, dass keine ue/ae/oe als Umlaut-Ersatz im Prompt vorkommen.

        Wir suchen nach typischen Mustern wie 'fuer', 'muessen', 'ueber' etc.,
        die auf ASCII-Umlaute hindeuten.
        """
        prompt = await self._build_prompt(fake_job)
        ascii_umlaut_patterns = [
            r"\bfuer\b",
            r"\bueber\b",
            r"\bmuessen\b",
            r"\bkoennen\b",
            r"\bmoechte\b",
            r"\bGruesse\b",
            r"\bAendern\b",
            r"\bOeffnen\b",
        ]
        for pattern in ascii_umlaut_patterns:
            matches = re.findall(pattern, prompt, re.IGNORECASE)
            assert len(matches) == 0, (
                f"ASCII-Umlaut-Pattern '{pattern}' gefunden im Prompt: {matches}"
            )

    @pytest.mark.asyncio
    async def test_contains_sender_info(self, fake_job):
        prompt = await self._build_prompt(fake_job)
        assert "test@example.com" in prompt
        assert "Test Sender" in prompt

    @pytest.mark.asyncio
    async def test_contains_subject(self, fake_job):
        prompt = await self._build_prompt(fake_job)
        assert "Testbetreff" in prompt

    @pytest.mark.asyncio
    async def test_recipient_type_to_in_prompt(self, fake_job):
        """Prompt zeigt recipient_type=to korrekt an."""
        prompt = await self._build_prompt(fake_job)
        assert "Empfänger-Typ:" in prompt
        assert "to" in prompt

    @pytest.mark.asyncio
    async def test_recipient_type_cc_shows_warning(self):
        """Bei CC-Mails erscheint eine deutliche Warnung im Prompt."""
        job = _make_fake_job(recipient_type="cc")
        prompt = await self._build_prompt(job)
        assert "NUR im CC" in prompt
        assert "fyi" in prompt
        assert "KEIN auto_reply" in prompt

    @pytest.mark.asyncio
    async def test_recipient_type_to_no_cc_warning(self, fake_job):
        """Bei TO-Mails erscheint KEINE dynamische CC-Warnung im Job-Block."""
        prompt = await self._build_prompt(fake_job)
        assert "⚠️ **ACHTUNG: Anthony ist bei dieser E-Mail NUR im CC" not in prompt

    @pytest.mark.asyncio
    async def test_style_skill_view_native(self, fake_job):
        """Im Normalfall (nativer Skill vorhanden) wird skill_view(email-style) angewiesen."""
        prompt = await self._build_prompt(fake_job, style_native=True)
        assert "SCHREIBSTIL" in prompt, "SCHREIBSTIL-Block fehlt im Prompt"
        assert "skill_view(name='email-style')" in prompt

    @pytest.mark.asyncio
    async def test_triage_skill_view_native(self, fake_job):
        """Im Normalfall (nativer Skill vorhanden) wird skill_view(email-triage) angewiesen."""
        prompt = await self._build_prompt(fake_job, skill_native=True)
        assert "skill_view(name='email-triage')" in prompt

    @pytest.mark.asyncio
    async def test_style_block_injected_fallback(self, fake_job):
        """Fallback: ohne nativen Skill wird der Schreibstil-Kanon injiziert."""
        sentinel = "Knapp, klar, kollegial, lösungsorientiert (STIL-SENTINEL)"
        prompt = await self._build_prompt(fake_job, style_text=sentinel, style_native=False)
        assert "SCHREIBSTIL" in prompt, "SCHREIBSTIL-Block fehlt im Prompt"
        assert sentinel in prompt, "Schreibstil-Kanon-Inhalt fehlt im Prompt"

    @pytest.mark.asyncio
    async def test_style_block_absent_when_empty(self, fake_job):
        """Ohne nativen Skill und ohne Kanon-Text wird kein SCHREIBSTIL-Block eingefügt."""
        prompt = await self._build_prompt(fake_job, style_text="", style_native=False)
        assert "SCHREIBSTIL" not in prompt

    @pytest.mark.asyncio
    async def test_two_pass_omits_style_and_draft_step(self, fake_job):
        """Zwei-Pass: Klassifikations-Prompt ohne Stil-Block; Draft-Schritt sagt 'KEINEN'."""
        prompt = await self._build_prompt(fake_job, style_native=True, two_pass=True)
        assert "SCHREIBSTIL" not in prompt
        assert "Erstelle KEINEN Antwort-Entwurf" in prompt
        assert "separaten" in prompt

    @pytest.mark.asyncio
    async def test_single_pass_keeps_draft_step(self, fake_job):
        """Einpass: der Draft-Schritt im selben Loop bleibt erhalten."""
        prompt = await self._build_prompt(fake_job, style_native=True, two_pass=False)
        assert "Erstelle Draft falls auto_reply" in prompt

    def test_style_canon_swiss_spelling(self):
        """Der echte Schreibstil-Kanon nutzt Schweizer Schreibweise (kein ß, keine ue/ae/oe-Ersatzformen)."""
        from app.services.hermes_worker import STYLE_PROFILE

        if not STYLE_PROFILE.exists():
            pytest.skip(f"Schreibstil-Kanon nicht vorhanden: {STYLE_PROFILE}")

        text = STYLE_PROFILE.read_text(encoding="utf-8")
        assert "ß" not in text, "Schreibstil-Kanon darf kein scharfes S enthalten"

        ascii_umlaut_patterns = [
            r"\bfuer\b", r"\bueber\b", r"\bmuessen\b", r"\bkoennen\b",
            r"\bmoechte\b", r"\bGruesse\b", r"\bAendern\b", r"\bOeffnen\b",
        ]
        for pattern in ascii_umlaut_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            assert len(matches) == 0, (
                f"ASCII-Umlaut-Pattern '{pattern}' im Schreibstil-Kanon: {matches}"
            )

    async def _build_prompt(
        self,
        job,
        style_text="(Schreibstil-Kanon Platzhalter)",
        skill_text="(Triage-Skill Platzhalter)",
        skill_native=True,
        style_native=True,
        two_pass=False,
    ):
        """Importiert und ruft _build_triage_prompt auf, mit gemockten DB-Calls.

        Skill-Verfügbarkeit und -Inhalt werden gemockt, damit der Test unabhängig
        vom Dateisystem (~/.hermes/skills/...) ist. Default: native Skills vorhanden.
        ``two_pass`` steuert den Zwei-Pass-Modus (Default aus -> Stil im Klassifikations-
        Prompt); im Zwei-Pass entfällt der Stil-Block (separater Schreib-Pass).
        """
        from app.services.hermes_worker import get_settings
        with patch("app.services.hermes_worker._load_projects_context", new_callable=AsyncMock) as mock_projects, \
             patch("app.services.hermes_worker._load_style_profile", return_value=style_text), \
             patch("app.services.hermes_worker._load_triage_skill", return_value=skill_text), \
             patch("app.services.hermes_worker._triage_skill_available", return_value=skill_native), \
             patch("app.services.hermes_worker._style_skill_available", return_value=style_native), \
             patch.object(get_settings(), "two_pass_draft", two_pass):
            mock_projects.return_value = "## VERFÜGBARE PROJEKTE\n- \"TestProjekt\" (id: 123)"

            from app.services.hermes_worker import _build_triage_prompt
            return await _build_triage_prompt(job)


class TestForcedClassCorrection:
    """Tests für den Berater-Korrektur-Block (forced_class) im Triage-Prompt."""

    async def _build_prompt(
        self,
        job,
        style_text="(Schreibstil-Kanon Platzhalter)",
        skill_text="(Triage-Skill Platzhalter)",
        skill_native=True,
        style_native=True,
        two_pass=False,
    ):
        from app.services.hermes_worker import get_settings
        with patch("app.services.hermes_worker._load_projects_context", new_callable=AsyncMock) as mock_projects, \
             patch("app.services.hermes_worker._load_style_profile", return_value=style_text), \
             patch("app.services.hermes_worker._load_triage_skill", return_value=skill_text), \
             patch("app.services.hermes_worker._triage_skill_available", return_value=skill_native), \
             patch("app.services.hermes_worker._style_skill_available", return_value=style_native), \
             patch.object(get_settings(), "two_pass_draft", two_pass):
            mock_projects.return_value = "## VERFÜGBARE PROJEKTE\n- \"TestProjekt\" (id: 123)"
            from app.services.hermes_worker import _build_triage_prompt
            return await _build_triage_prompt(job)

    @pytest.mark.asyncio
    async def test_no_correction_block_without_forced_class(self, fake_job):
        prompt = await self._build_prompt(fake_job)
        assert "KORREKTUR DES BERATERS" not in prompt

    @pytest.mark.asyncio
    async def test_correction_block_for_forced_task(self):
        job = _make_fake_job()
        job.metadata_json["forced_class"] = "task"
        job.metadata_json["correction_reason"] = "Das ist klar eine Aufgabe"
        prompt = await self._build_prompt(job)
        assert "KORREKTUR DES BERATERS" in prompt
        assert "task" in prompt
        assert "Das ist klar eine Aufgabe" in prompt
        assert "Aufgabe (task)" in prompt

    @pytest.mark.asyncio
    async def test_correction_block_for_forced_auto_reply(self):
        job = _make_fake_job()
        job.metadata_json["forced_class"] = "auto_reply"
        prompt = await self._build_prompt(job)
        assert "KORREKTUR DES BERATERS" in prompt
        assert "Antwort-Entwurf (auto_reply)" in prompt
        # Korrektur-Block soll ganz oben stehen (vor der Skill-Sektion).
        assert prompt.index("KORREKTUR DES BERATERS") < prompt.index("TRIAGE-SKILL")

    @pytest.mark.asyncio
    async def test_correction_block_before_skill_fallback(self):
        """Auch im Datei-Fallback steht der Korrektur-Block vor den Instruktionen."""
        job = _make_fake_job()
        job.metadata_json["forced_class"] = "auto_reply"
        prompt = await self._build_prompt(job, skill_native=False)
        assert prompt.index("KORREKTUR DES BERATERS") < prompt.index("TRIAGE-INSTRUKTIONEN")


class TestDetermineRecipientType:
    """Tests für die recipient_type-Ableitung aus TO/CC-Feldern."""

    def test_to_recipient(self):
        from app.services.triage import _determine_recipient_type
        email = {
            "toRecipients": [{"emailAddress": {"address": "anthony@innosmith.ch"}}],
            "ccRecipients": [],
        }
        assert _determine_recipient_type(email) == "to"

    def test_cc_recipient(self):
        from app.services.triage import _determine_recipient_type
        email = {
            "toRecipients": [{"emailAddress": {"address": "other@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "anthony@gerbersmith.ch"}}],
        }
        assert _determine_recipient_type(email) == "cc"

    def test_bfh_address_recognized(self):
        from app.services.triage import _determine_recipient_type
        email = {
            "toRecipients": [{"emailAddress": {"address": "anthony.smith@bfh.ch"}}],
            "ccRecipients": [],
        }
        assert _determine_recipient_type(email) == "to"

    def test_to_takes_precedence_over_cc(self):
        """Wenn Owner in TO und CC steht, gewinnt TO."""
        from app.services.triage import _determine_recipient_type
        email = {
            "toRecipients": [{"emailAddress": {"address": "anthony@innosmith.ch"}}],
            "ccRecipients": [{"emailAddress": {"address": "anthony@gerbersmith.ch"}}],
        }
        assert _determine_recipient_type(email) == "to"

    def test_unknown_when_not_in_either(self):
        from app.services.triage import _determine_recipient_type
        email = {
            "toRecipients": [{"emailAddress": {"address": "someone@example.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "other@example.com"}}],
        }
        assert _determine_recipient_type(email) == "unknown"

    def test_case_insensitive(self):
        from app.services.triage import _determine_recipient_type
        email = {
            "toRecipients": [],
            "ccRecipients": [{"emailAddress": {"address": "Anthony@InnoSmith.ch"}}],
        }
        assert _determine_recipient_type(email) == "cc"

    def test_empty_recipients(self):
        from app.services.triage import _determine_recipient_type
        email = {}
        assert _determine_recipient_type(email) == "unknown"


class TestExtractJsonBlock:
    """Contract-Tests fuer den robusten Triage-JSON-Parser.

    Hintergrund: ~11% der Prod-Jobs fielen frueher still durch, weil die alte
    enge Regex nur einen ```json-Fence ODER ein flaches Objekt mit BEIDEN Feldern
    ``label`` UND ``triage_class`` (ohne Verschachtelung) akzeptierte. Diese Tests
    fixieren die toleranten Faelle, die lokale Modelle real produzieren.
    """

    def test_fenced_json_block(self):
        from app.services.hermes_worker import _extract_json_block
        content = (
            "Analyse ...\n\n```json\n"
            '{"label": "System", "triage_class": "fyi", "reply_expected": false}\n'
            "```\n"
        )
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "fyi"

    def test_bare_fence_without_json_tag(self):
        from app.services.hermes_worker import _extract_json_block
        content = '```\n{"triage_class": "task", "task_title": "X"}\n```'
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "task"

    def test_no_fence_object_in_prose(self):
        from app.services.hermes_worker import _extract_json_block
        content = 'Entscheid: {"triage_class": "auto_reply", "label": "Wichtig"} -- fertig.'
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "auto_reply"

    def test_nested_object_with_array(self):
        """Verschachtelte Felder (categories-Array, Sub-Objekt) duerfen nicht abbrechen."""
        from app.services.hermes_worker import _extract_json_block
        content = (
            '{"triage_class": "fyi", "label": "System", '
            '"categories": ["System", "Newsletter"], '
            '"meta": {"move_folder": "System"}}'
        )
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "fyi"
        assert parsed["categories"] == ["System", "Newsletter"]

    def test_trailing_comma_tolerated(self):
        from app.services.hermes_worker import _extract_json_block
        content = '{"triage_class": "task", "task_title": "Y",}'
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "task"

    def test_single_quotes_python_dict(self):
        from app.services.hermes_worker import _extract_json_block
        content = "{'triage_class': 'fyi', 'reply_expected': false}"
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "fyi"
        assert parsed["reply_expected"] is False

    def test_prose_only_returns_none(self):
        from app.services.hermes_worker import _extract_json_block
        content = "Ich habe die E-Mail eingeordnet und einen Task erstellt. Keine Aktion noetig."
        assert _extract_json_block(content) is None

    def test_empty_returns_none(self):
        from app.services.hermes_worker import _extract_json_block
        assert _extract_json_block("") is None

    def test_last_object_with_triage_class_wins(self):
        """Mehrere Objekte: das letzte mit triage_class (Abschlussblock) gewinnt."""
        from app.services.hermes_worker import _extract_json_block
        content = (
            '{"some": "tool_args"}\n'
            '{"triage_class": "task", "label": "erste"}\n'
            'Korrektur:\n'
            '{"triage_class": "fyi", "label": "finale"}'
        )
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["triage_class"] == "fyi"
        assert parsed["label"] == "finale"

    def test_json_true_false_null(self):
        from app.services.hermes_worker import _extract_json_block
        content = '{"triage_class": "task", "reply_expected": true, "deadline": null}'
        parsed = _extract_json_block(content)
        assert parsed is not None
        assert parsed["reply_expected"] is True
        assert parsed["deadline"] is None


class TestStripInternalNotes:
    """Interne API-/Fehler-Diagnosen duerfen nicht in nutzersichtbaren Text gelangen."""

    def test_removes_graph_404_sentence(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = (
            "Rahel sendet die IT-Checkliste und will einen Termin. "
            "Die E-Mail liess sich via Graph API nicht laden (404)."
        )
        cleaned = _strip_internal_notes(text)
        assert "404" not in cleaned
        assert "Graph" not in cleaned
        assert "IT-Checkliste" in cleaned

    def test_removes_httpstatuserror(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = "Kurzes Briefing. HTTPStatusError: 400 Bad Request bei createReplyAll."
        cleaned = _strip_internal_notes(text)
        assert "HTTPStatusError" not in cleaned
        assert "createReplyAll" not in cleaned
        assert "Briefing" in cleaned

    def test_keeps_clean_text(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = "Bitte den Vertrag bis Freitag pruefen und an den Kunden antworten."
        assert _strip_internal_notes(text) == text

    def test_all_noise_returns_none(self):
        from app.services.hermes_worker import _strip_internal_notes
        assert _strip_internal_notes("404 Not Found. HTTPStatusError createReply.") is None

    def test_none_passthrough(self):
        from app.services.hermes_worker import _strip_internal_notes
        assert _strip_internal_notes(None) is None


class TestStripInternalNotesKeepsStructure:
    """Regression: die Reinigung darf die Markdown-Struktur nicht einebnen.

    Vorfall (03.08.2026): Jede E-Mail-Task erschien im Cockpit als eine einzige
    Textwand -- die Aufzählung des Agenten stand ohne Zeilenumbruch hintereinander
    («... folgender Punkte: 1. **A:** ... 2. **B:** ...»). Ursache war diese
    Funktion: sie splittete an ``\\n+`` und fügte mit ``" ".join(...)`` zusammen,
    kollabierte also jeden Zeilenumbruch zu einem Leerzeichen. Kein Test sah das,
    weil alle bestehenden Fälle mit einzeiligem Text arbeiteten.
    """

    def test_markdown_list_keeps_line_breaks(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = (
            "Valentin bittet um vier Punkte bis Ende Woche.\n"
            "\n"
            "- **Produktivübertragung:** Anpassungen übertragen.\n"
            "- **Assembly-Startseite:** Vorschau aufsetzen."
        )
        assert _strip_internal_notes(text) == text

    def test_blank_line_separates_paragraphs(self):
        from app.services.hermes_worker import _strip_internal_notes
        cleaned = _strip_internal_notes("Erster Absatz.\n\nZweiter Absatz.")
        assert cleaned == "Erster Absatz.\n\nZweiter Absatz."

    def test_indentation_of_nested_list_survives(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = "- Oberpunkt\n  - Unterpunkt"
        assert _strip_internal_notes(text) == text

    def test_noise_line_dropped_without_flattening_the_rest(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = (
            "- Vertrag prüfen\n"
            "- Die Mail liess sich via Graph API nicht laden (404).\n"
            "- Termin bestätigen"
        )
        cleaned = _strip_internal_notes(text)
        assert cleaned == "- Vertrag prüfen\n- Termin bestätigen"

    def test_noise_removed_inside_a_line_keeps_neighbouring_lines(self):
        from app.services.hermes_worker import _strip_internal_notes
        text = "Erste Zeile. HTTPStatusError: 400 Bad Request.\nZweite Zeile."
        assert _strip_internal_notes(text) == "Erste Zeile.\nZweite Zeile."

    def test_excess_blank_lines_are_collapsed(self):
        from app.services.hermes_worker import _strip_internal_notes
        assert _strip_internal_notes("A.\n\n\n\nB.") == "A.\n\nB."


class TestNoSourceEmailBlockInDescription:
    """Die Beschreibung enthält keine Herkunftsangaben mehr.

    Vorfall (03.08.2026): Jede E-Mail-Task trug am Ende der Beschreibung einen
    «Quell-E-Mail»-Block mit Absender, Betreff und Outlook-Link. Dieselben Angaben
    standen schon im Sidebar-Badge und im Thread-Panel; der Block kostete rund acht
    Zeilen in der schmalen Spalte und schob die eigentliche Aufgabe aus dem Blick.
    Absender und Betreff kommen jetzt aus den Task-Feldern, der Link aus
    ``TaskOut.source_email_web_link``.
    """

    def test_helpers_are_gone(self):
        import app.services.hermes_worker as hw
        assert not hasattr(hw, "_email_reference_block")
        assert not hasattr(hw, "_outlook_deeplink")

    def test_worker_never_appends_outlook_url(self):
        """Kein Codepfad im Worker darf eine Outlook-URL in Text schreiben."""
        import app.services.hermes_worker as hw
        source = Path(hw.__file__).read_text(encoding="utf-8")
        assert "outlook.office.com" not in source
        # Die Markdown-Überschrift des alten Blocks (Kommentare dürfen ihn erwähnen).
        assert "**Quell-E-Mail**" not in source


class TestTaskDescriptionSkillContract:
    """Konsistenz zwischen ausgerolltem Skill und dem, was das Backend wirklich tut.

    Vorfall (03.08.2026): ``references/triage-rules.md`` behauptete,
    ``task_description`` werde «vom Backend NICHT ausgewertet» und sei «dekorativ»
    -- im Widerspruch zu ``SKILL.md`` und zu ``_post_process_triage``, das den Wert
    an ``_create_email_task`` weiterreicht. Das Modell hatte damit keinen Anlass,
    die Beschreibung sauber zu formatieren. Beide Seiten wurden nirgends gemeinsam
    gelesen, deshalb fiel der Widerspruch niemandem auf.
    """

    SKILL_DIR = Path.home() / ".hermes" / "skills" / "email-triage"

    def _texts(self) -> dict[str, str]:
        if not self.SKILL_DIR.is_dir():
            return {}
        files = [
            self.SKILL_DIR / "SKILL.md",
            *sorted((self.SKILL_DIR / "references").glob("*.md")),
        ]
        return {f.name: f.read_text(encoding="utf-8") for f in files if f.is_file()}

    def test_backend_really_consumes_task_description(self):
        """Gegenprobe im Code: ohne sie waere die Skill-Forderung unbegruendet."""
        import inspect
        from app.services import hermes_worker

        source = inspect.getsource(hermes_worker._post_process_triage)
        assert 'parsed.get("task_description")' in source
        assert "task_description=task_description" in source

    def test_skill_does_not_call_task_description_decorative(self):
        texts = self._texts()
        if not texts:
            pytest.skip("Triage-Skill nicht ausgerollt")

        offenders: list[str] = []
        for name, text in texts.items():
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "task_description" not in line:
                    continue
                if re.search(r"dekorativ|NICHT ausgewertet", line, re.IGNORECASE):
                    offenders.append(f"{name}:{lineno}: {line.strip()[:110]}")
        assert not offenders, (
            "Der Skill bezeichnet task_description als dekorativ, obwohl das Backend "
            "den Wert in die Aufgabe uebernimmt:\n" + "\n".join(offenders)
        )

    def test_skill_specifies_markdown_format_for_task_description(self):
        """Positivprobe: die Formatvorgabe muss auffindbar sein."""
        texts = self._texts()
        if not texts:
            pytest.skip("Triage-Skill nicht ausgerollt")

        combined = "\n".join(texts.values())
        assert "Format von `task_description`" in combined
        assert "Markdown-Liste" in combined
        # Der Quell-Link kommt deterministisch vom Backend, nicht vom Modell.
        assert "Quell-E-Mail" in combined


class TestSelfGradeStyleAnchor:
    """Self-Grade erkennt spaete Tools (create_draft/search_my_replies) korrekt."""

    def test_style_anchor_detected_with_prefixed_tool(self):
        from app.services.hermes_worker import _compute_self_grade
        meta = {"conversation_id": "conv-1"}
        tools = [
            "mcp_graph_get_thread",
            "mcp_graph_search_sender_history",
            "mcp_taskpilot_get_sender_profile",
            "mcp_graph_search_my_replies",
        ]
        grade = _compute_self_grade(meta, {"draft_id": "d1"}, tools)
        assert grade["missing"] == []
        assert grade["score"] == 1.0

    def test_style_anchor_missing_when_not_called(self):
        from app.services.hermes_worker import _compute_self_grade
        meta = {"conversation_id": "conv-1"}
        tools = [
            "mcp_graph_get_thread",
            "mcp_graph_search_sender_history",
            "mcp_taskpilot_get_sender_profile",
        ]
        grade = _compute_self_grade(meta, {"draft_id": "d1"}, tools)
        assert "style_anchor_used" in grade["missing"]


class TestExtractNewIdFromMove:
    """Post-Move-ID wird zuverlaessig aus dem Tool-Ergebnis erfasst."""

    def test_parses_wrapped_new_id(self):
        import json
        from app.services.hermes_worker import _extract_new_id_from_move_result
        inner = json.dumps(
            {"status": "moved", "message_id": "OLD", "folder": "System", "new_id": "NEWID123"}
        )
        # Hermes wrappt das MCP-Ergebnis als {"result": "<json-string>"}.
        assert _extract_new_id_from_move_result({"result": inner}) == "NEWID123"

    def test_parses_plain_dict(self):
        from app.services.hermes_worker import _extract_new_id_from_move_result
        assert _extract_new_id_from_move_result({"new_id": "X"}) == "X"

    def test_none_when_absent(self):
        from app.services.hermes_worker import _extract_new_id_from_move_result
        assert _extract_new_id_from_move_result({"status": "moved"}) is None

    def test_regex_fallback_double_escaped(self):
        from app.services.hermes_worker import _extract_new_id_from_move_result
        text = '{\\"status\\": \\"moved\\", \\"new_id\\": \\"ESCAPED42\\"}'
        assert _extract_new_id_from_move_result(text) == "ESCAPED42"


class TestFinalizeEmailState:
    """Deterministische Outlook-Finalisierung: Kategorie-Gating + immer ungelesen."""

    def _client(self, categories=None, own_mails=None):
        from unittest.mock import AsyncMock
        client = AsyncMock()
        client.get_email_categories.return_value = {"categories": categories or []}
        # Standard: an diese Adresse hat Anthony nie geschrieben -- ein Move ist also
        # erlaubt. Ohne diese Vorgabe liefert der AsyncMock ein wahrheitswertiges
        # Objekt zurueck und jeder Move waere stillschweigend blockiert.
        client.search_my_replies_to.return_value = own_mails or []
        return client

    @pytest.mark.asyncio
    async def test_category_set_when_missing_and_unread_is_last(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.services import hermes_worker as hw

        client = self._client(categories=[])
        manager = MagicMock()
        manager.attach_mock(client.get_email_categories, "get_cat")
        manager.attach_mock(client.set_categories, "set_cat")
        manager.attach_mock(client.mark_as_unread, "unread")

        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state({"email_message_id": "M1"}, "Wichtig", None)

        client.set_categories.assert_awaited_once_with("M1", ["Wichtig"])
        client.mark_as_unread.assert_awaited_once_with("M1")
        # ungelesen MUSS der letzte Graph-Schritt sein (set_categories kippt isRead).
        assert [c[0] for c in manager.mock_calls][-1] == "unread"

    @pytest.mark.asyncio
    async def test_category_always_set_even_when_one_exists(self):
        """Die Kategorie ist kein Luecken-Fueller mehr, sondern die Wahrheit.

        Vorher wurde eine bestehende Kategorie nie ueberschrieben -- weil sie
        typischerweise vom LLM selbst stammte. Genau daher kamen die 80 erfundenen
        Kategorien: das Backend validierte die Meldung statt die Tat. Jetzt schreibt
        ausschliesslich das Backend, und zwar das validierte Label.
        """
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client(categories=["Irgendwas Erfundenes"])
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state({"email_message_id": "M1"}, "Finanzen", None)

        client.set_categories.assert_awaited_once_with("M1", ["Finanzen"])
        client.mark_as_unread.assert_awaited_once_with("M1")

    @pytest.mark.asyncio
    async def test_moved_id_takes_precedence(self):
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client(categories=[])
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state({"email_message_id": "OLD"}, "Wichtig", "NEW")

        client.set_categories.assert_awaited_once_with("NEW", ["Wichtig"])
        client.mark_as_unread.assert_awaited_once_with("NEW")

    @pytest.mark.asyncio
    async def test_unklassifiziert_skips_category_but_marks_unread(self):
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client(categories=[])
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state({"email_message_id": "M1"}, "Unklassifiziert", None)

        client.get_email_categories.assert_not_awaited()
        client.set_categories.assert_not_awaited()
        client.mark_as_unread.assert_awaited_once_with("M1")

    @pytest.mark.asyncio
    async def test_404_is_tolerated(self):
        from unittest.mock import AsyncMock, patch
        import httpx
        from app.services import hermes_worker as hw

        req = httpx.Request("GET", "http://x")
        err = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        client = self._client(categories=[])
        client.get_email_categories.side_effect = err
        client.mark_as_unread.side_effect = err

        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            # Darf NICHT werfen -- CC-only/veraltete IDs sind erwartbar.
            await hw._finalize_email_state({"email_message_id": "M1"}, "Wichtig", None)

        client.mark_as_unread.assert_awaited_once_with("M1")

    @pytest.mark.asyncio
    async def test_no_message_id_skips_client(self):
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        build = AsyncMock()
        with patch.object(hw, "_build_graph_client", build):
            await hw._finalize_email_state({}, "Wichtig", None)

        build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_move_happens_before_category_on_new_id(self):
        """Move zuerst, dann Kategorie auf der NEUEN ID, ungelesen zuletzt.

        Ein Move aendert die Graph-Message-ID. Genau diese Reihenfolge war fruher die
        Aufgabe des LLM und die Hauptquelle stillschweigend fehlender Kategorien
        (404 auf der alten ID).
        """
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        client.move_to_folder.return_value = {"id": "NEU"}
        manager = MagicMock()
        manager.attach_mock(client.move_to_folder, "move")
        manager.attach_mock(client.set_categories, "set_cat")
        manager.attach_mock(client.mark_as_unread, "unread")

        meta = {
            "email_message_id": "ALT",
            "inference_classification": "other",
            "from_address": "daily@updates.miro.com",
            "subject": "Neuigkeiten auf deinem Board",
        }
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state(meta, "Newsletter", None, triage_class="fyi")

        client.move_to_folder.assert_awaited_once_with("ALT", "Newsletter")
        client.set_categories.assert_awaited_once_with("NEU", ["Newsletter"])
        client.mark_as_unread.assert_awaited_once_with("NEU")
        assert [c[0] for c in manager.mock_calls] == ["move", "set_cat", "unread"]

    @pytest.mark.asyncio
    async def test_focused_no_longer_blocks_the_move(self):
        """``inferenceClassification`` steuert den Move nicht mehr -- Absicht, kein Regress.

        Die Bedingung ``== 'other'`` stand hier von Juli bis August 2026 als Schutz
        gegen Fehlmoves echter Korrespondenz (Affolter, Springer, Streit, Haemmerli,
        Almonte, von Lanthen -- alle trugen ``focused``). Sie wirkte, aber viel zu
        breit: Outlooks Fokus-Heuristik stufte in Anthonys Postfach 1126 von 1426
        Mails als ``focused`` ein, darunter LinkedIn, Synology und Leadinfo. Gemessen
        blieben dadurch in 30 Tagen 99 ``System``- und 12 ``Newsletter``-Mails in der
        Inbox liegen, waehrend nur 46 bzw. 17 verschoben wurden -- das Gate filterte
        die Mehrheit statt der Ausnahmen, und die Unterordner blieben faktisch
        unbenutzt.

        Die beklagten Fehlmoves waren in Wahrheit Label-Fehler des Modells (eine
        Kundenmail als ``System``). Der Ersatz war zunaechst der Skill (Label und
        Klasse sind getrennte Fragen) plus ``needs_review`` -- das genuegte nicht:
        in den folgenden 25 Tagen wurden 49 Mails namentlicher Absender weiterhin
        als ``System`` verschoben, mit Confidence bis 1.0. Seit August 2026 fragt
        darum ``move_target`` den Korrespondenz-Nachweis -- siehe
        ``test_own_correspondence_is_never_moved``. Das ist keine Rueckkehr zum
        ``other``-Gate: geprueft wird eigene gesendete Post, nicht Outlooks
        Fokus-Heuristik.
        """
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        client.move_to_folder.return_value = {"id": "NEU"}
        meta = {
            "email_message_id": "M1",
            "inference_classification": "focused",
            "from_address": "wordpress@innosmith.ch",
            "subject": "Wordfence activity for 17.08.2026",
        }
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state(meta, "System", None, triage_class="fyi")

        client.move_to_folder.assert_awaited_once_with("M1", "System")
        client.set_categories.assert_awaited_once_with("NEU", ["System"])
        client.mark_as_unread.assert_awaited_once_with("NEU")

    @pytest.mark.asyncio
    async def test_finanzen_sets_category_but_never_moves(self):
        """``Finanzen`` ist eine Sichtmarke in der Inbox, kein Ordner.

        Verschoben hiesse aus dem Blick -- und genau das war der Hauptkritikpunkt.
        Selbst unter den sonst move-tauglichen Bedingungen (fyi + other) bleibt die
        Mail liegen.
        """
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        meta = {"email_message_id": "M1", "inference_classification": "other"}
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state(meta, "Finanzen", None, triage_class="fyi")

        client.move_to_folder.assert_not_awaited()
        client.set_categories.assert_awaited_once_with("M1", ["Finanzen"])

    @pytest.mark.asyncio
    async def test_task_is_never_moved(self):
        """Alles mit Handlungsbedarf bleibt sichtbar, unabhaengig vom Label."""
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        meta = {"email_message_id": "M1", "inference_classification": "other"}
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state(meta, "System", None, triage_class="task")

        client.move_to_folder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_needs_review_blocks_move(self):
        """Was das System nicht verstanden hat, raeumt es nicht weg."""
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        meta = {"email_message_id": "M1", "inference_classification": "other"}
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state(
                meta, "Newsletter", None, triage_class="fyi", needs_review=True
            )

        client.move_to_folder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_own_correspondence_blocks_move_and_is_reported(self):
        """Eine Adresse, an die Anthony geschrieben hat, wird nicht weggeraeumt.

        Die Kategorie wird trotzdem gesetzt und die Mail bleibt ungelesen sichtbar.
        Der Grund geht als Klartext an den Aufrufer, damit im Cockpit nicht bloss
        eine unverschobene Mail steht.
        """
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client(own_mails=[{"id": "S1", "subject": "AW: SSH public keys"}])
        meta = {
            "email_message_id": "M1",
            "from_address": "justin.springer@swissbankers.ch",
        }
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            grund = await hw._finalize_email_state(meta, "System", None, triage_class="fyi")

        client.move_to_folder.assert_not_awaited()
        client.set_categories.assert_awaited_once_with("M1", ["System"])
        client.mark_as_unread.assert_awaited_once_with("M1")
        assert grund == "eigene Korrespondenz mit dieser Adresse"

    @pytest.mark.asyncio
    async def test_correspondence_is_only_checked_when_a_move_is_possible(self):
        """Der Nachweis kostet eine Graph-Suche -- sie laeuft nur bei moeglichem Move.

        Bei ``Finanzen`` (kein Zielordner), bei ``task`` und bei ``needs_review`` ist
        die Frage bereits entschieden; eine Suche waere reine Netzlast pro Mail.
        """
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        faelle = (
            ("Finanzen", "fyi", False),
            ("System", "task", False),
            ("System", "fyi", True),
        )
        for label, klasse, review in faelle:
            client = self._client()
            with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
                await hw._finalize_email_state(
                    {"email_message_id": "M1", "from_address": "a@b.ch"},
                    label, None, triage_class=klasse, needs_review=review,
                )
            client.search_my_replies_to.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failing_correspondence_check_blocks_the_move(self):
        """Faellt die Graph-Suche aus, bleibt die Mail liegen -- fail-closed."""
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        client.search_my_replies_to.side_effect = RuntimeError("Graph down")
        meta = {"email_message_id": "M1", "from_address": "wordpress@innosmith.ch"}
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            grund = await hw._finalize_email_state(meta, "System", None, triage_class="fyi")

        client.move_to_folder.assert_not_awaited()
        client.mark_as_unread.assert_awaited_once_with("M1")
        assert grund == "Korrespondenz nicht pruefbar"

    @pytest.mark.asyncio
    async def test_missing_folder_does_not_stop_finalization(self):
        """Fehlt der Zielordner, wird nicht verschoben -- Kategorie/ungelesen laufen weiter.

        ``get_or_create_folder`` erstellt bewusst keine Ordner und wirft ValueError.
        """
        from unittest.mock import AsyncMock, patch
        from app.services import hermes_worker as hw

        client = self._client()
        client.move_to_folder.side_effect = ValueError("Ordner 'Junk' existiert nicht")
        meta = {"email_message_id": "M1", "inference_classification": "other"}
        with patch.object(hw, "_build_graph_client", AsyncMock(return_value=client)):
            await hw._finalize_email_state(meta, "Junk", None, triage_class="fyi")

        client.set_categories.assert_awaited_once_with("M1", ["Junk"])
        client.mark_as_unread.assert_awaited_once_with("M1")


class TestAutoSubmittedContext:
    """Der Auto-Submitted-Header als Fakt im Prompt -- nicht als Regel im Code.

    Messung am Postfach: nur 2 von 22 Abwesenheitsnotizen tragen ``auto-replied``, die
    anderen 20 tragen ``auto-generated`` -- genauso wie Ticketsysteme, Confluence und
    eine Lieferantenrechnung. Der Header trennt Autoresponder also NICHT von
    handlungsrelevanter Maschinenpost. Deshalb wandert er als Tatsache in den Prompt,
    statt als deterministische Klassifikation ins Backend.
    """

    @pytest.mark.asyncio
    async def test_header_is_extracted_into_metadata(self):
        from unittest.mock import AsyncMock
        from app.services.triage import _enrich_auto_submitted

        client = AsyncMock()
        client.get_message_headers.return_value = [
            {"name": "Received", "value": "from mx.example.com"},
            {"name": "Auto-Submitted", "value": "auto-replied (vacation)"},
        ]
        mails = [{"id": "M1"}]
        await _enrich_auto_submitted(client, mails)
        assert mails[0]["autoSubmitted"] == "auto-replied (vacation)"

    @pytest.mark.asyncio
    async def test_no_header_leaves_mail_untouched(self):
        from unittest.mock import AsyncMock
        from app.services.triage import _enrich_auto_submitted

        client = AsyncMock()
        client.get_message_headers.return_value = [{"name": "Received", "value": "x"}]
        mails = [{"id": "M1"}]
        await _enrich_auto_submitted(client, mails)
        assert "autoSubmitted" not in mails[0]

    @pytest.mark.asyncio
    async def test_explicit_no_is_not_treated_as_machine(self):
        """``Auto-Submitted: no`` heisst laut RFC 3834 ausdruecklich «von Hand»."""
        from unittest.mock import AsyncMock
        from app.services.triage import _enrich_auto_submitted

        client = AsyncMock()
        client.get_message_headers.return_value = [{"name": "Auto-Submitted", "value": "no"}]
        mails = [{"id": "M1"}]
        await _enrich_auto_submitted(client, mails)
        assert "autoSubmitted" not in mails[0]

    @pytest.mark.asyncio
    async def test_graph_error_does_not_break_the_cycle(self):
        from unittest.mock import AsyncMock
        from app.services.triage import _enrich_auto_submitted

        client = AsyncMock()
        client.get_message_headers.side_effect = RuntimeError("Graph weg")
        mails = [{"id": "M1"}]
        await _enrich_auto_submitted(client, mails)  # darf nicht werfen
        assert "autoSubmitted" not in mails[0]


class TestAbsenceContext:
    """Eigene Abwesenheit im Entwurfs-Prompt.

    Die Ferien vom 13.-24.07.2026 standen vollstaendig in ``capacity_time_off`` --
    der Draft-Prompt hat die Tabelle nur nie gelesen. Daher entstanden Antworten, die
    die Ferien nicht erwaehnten und Termine hineinlegten.
    """

    @staticmethod
    def _ferien_zwei_wochen():
        """Die echten Ferientage: zwei Arbeitswochen, Wochenende nicht erfasst."""
        return [date(2026, 7, 13) + timedelta(days=i) for i in range(5)] + [
            date(2026, 7, 20) + timedelta(days=i) for i in range(5)
        ]

    def test_weekend_gap_is_bridged(self):
        """Zwei Arbeitswochen sind EINE Abwesenheit, nicht zwei.

        Ohne Bruecke ueber das Wochenende wuerde der Entwurf «bis Freitag, 17.07.»
        sagen, obwohl Anthony noch eine weitere Woche weg ist.
        """
        from app.services.hermes_worker import _group_absence_ranges

        assert _group_absence_ranges(self._ferien_zwei_wochen()) == [
            (date(2026, 7, 13), date(2026, 7, 24))
        ]

    def test_real_gap_stays_separate(self):
        from app.services.hermes_worker import _group_absence_ranges

        tage = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 9, 21)]
        assert _group_absence_ranges(tage) == [
            (date(2026, 8, 10), date(2026, 8, 11)),
            (date(2026, 9, 21), date(2026, 9, 21)),
        ]

    def test_first_available_day_skips_weekend(self):
        """Rueckkehr ist der erste Arbeitstag, nicht der Kalendertag danach."""
        from app.services.hermes_worker import _first_available_day, _group_absence_ranges

        ranges = _group_absence_ranges(self._ferien_zwei_wochen())
        # Ferien enden Freitag 24.07. -> Rueckkehr Montag 27.07., nicht Samstag 25.07.
        assert _first_available_day(ranges, date(2026, 7, 17)) == date(2026, 7, 27)

    def test_first_available_day_none_when_present(self):
        from app.services.hermes_worker import _first_available_day, _group_absence_ranges

        ranges = _group_absence_ranges(self._ferien_zwei_wochen())
        assert _first_available_day(ranges, date(2026, 7, 27)) is None

    def test_block_names_the_return_date_during_absence(self):
        from app.services.hermes_worker import _build_absence_block, _group_absence_ranges

        block = _build_absence_block(
            _group_absence_ranges(self._ferien_zwei_wochen()), date(2026, 7, 17)
        )
        assert "HEUTE abwesend" in block
        assert "24.07.2026" in block
        assert "Termine NIE in die Abwesenheit legen" in block

    def test_block_warns_about_upcoming_absence(self):
        from app.services.hermes_worker import _build_absence_block, _group_absence_ranges

        block = _build_absence_block(
            _group_absence_ranges([date(2026, 8, 10) + timedelta(days=i) for i in range(5)]),
            date(2026, 7, 27),
        )
        assert "aktuell verfügbar" in block
        assert "10.08." in block

    def test_no_absence_leaves_prompt_untouched(self):
        from app.services.hermes_worker import _build_absence_block

        assert _build_absence_block([], date(2026, 7, 27)) == ""

    def test_holidays_are_not_absences(self):
        """Ein Feiertag wird in einer Antwort nicht als Abwesenheit erwaehnt."""
        from app.services.hermes_worker import _absence_ranges

        rows = [(date(2026, 8, 1), "feiertag")]

        class _Res:
            def all(self):
                return rows

        class _DB:
            async def execute(self, *_a, **_k):
                return _Res()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        import asyncio
        from unittest.mock import patch
        from app.services import hermes_worker as hw

        with patch.object(hw, "async_session", lambda: _DB()):
            assert asyncio.run(_absence_ranges()) == []

    def test_scheduling_window_starts_after_absence(self):
        """Freie Slots erst nach der Rueckkehr suchen.

        ``find_free_slots`` liest den Kalender; Ferien stehen aber in
        ``capacity_time_off`` und nicht zwingend als Termin. Ohne verschobenes Fenster
        haelt Graph die Ferientage fuer frei.

        Das Rueckkehrdatum ist relativ zu heute, weil das Fenster nur nach vorne
        verschoben wird -- ein festes Datum liesse den Test mit der Zeit ablaufen.
        """
        from app.services.hermes_worker import _build_calendar_draft_step

        rueckkehr = date.today() + timedelta(days=5)
        step = _build_calendar_draft_step(
            "Terminvorschlag Meeting", "Wann hätten Sie Zeit für einen Termin?",
            available_from=rueckkehr,
        )
        assert f'start="{rueckkehr.isoformat()}T08:00:00"' in step

    def test_scheduling_step_absent_for_non_scheduling_mail(self):
        from app.services.hermes_worker import _build_calendar_draft_step

        assert _build_calendar_draft_step("Rechnung 4711", "Beleg im Anhang") == ""


class TestTriageLabels:
    """Kanonisches Label-Vokabular: fail-closed statt Zurechtbiegen."""

    def test_labels_match_real_outlook_categories(self):
        """Die zehn Labels sind gegen ``/outlook/masterCategories`` verifiziert."""
        from app.services.triage_labels import TRIAGE_LABELS

        assert TRIAGE_LABELS == (
            "Signale", "System", "Wichtig", "Offerten/Verträge", "Networking/Leads",
            "Finanzen", "Kalender", "Newsletter", "Junk", "Unklar",
        )

    def test_agent_vocabulary_excludes_the_fallback(self):
        """Der Agent darf ``Unklar`` nicht waehlen -- es ist die Sichtungsmarke.

        Regression zum Vorfall vom 28.07.-17.08.2026: ``Unklar`` stand im
        Agenten-Vokabular und der Skill empfahl es ausdruecklich bei Unsicherheit.
        Gemessene Folge: 20 % aller kategorisierten Mails trugen ``Unklar``, davon
        praktisch keine ``needs_review`` -- die Faelle lagen ohne Aufgabe und ohne
        Sichtungsmarke da, darunter Kundenthreads wie "AW: Offerte
        KI-Basisschulungen". Gleichzeitig wurden ``Offerten/Verträge``,
        ``Networking/Leads`` und ``Signale`` drei Wochen lang nie vergeben.
        """
        from app.services.triage_labels import AGENT_LABELS, FALLBACK_LABEL, TRIAGE_LABELS

        assert FALLBACK_LABEL not in AGENT_LABELS
        assert set(AGENT_LABELS) == set(TRIAGE_LABELS) - {FALLBACK_LABEL}
        assert len(AGENT_LABELS) == 9

    def test_agent_unklar_counts_as_invalid(self):
        """Ein vom Modell geliefertes ``Unklar`` laeuft in den fail-closed-Pfad."""
        from app.services.triage_labels import normalize_agent_label

        assert normalize_agent_label("Unklar") is None
        assert normalize_agent_label("unklar") is None
        assert normalize_agent_label("Wichtig") == "Wichtig"

    def test_only_case_and_whitespace_are_tolerated(self):
        from app.services.triage_labels import normalize_agent_label, normalize_label

        assert normalize_label("finanzen") == "Finanzen"
        assert normalize_label("  Newsletter  ") == "Newsletter"
        assert normalize_label("Offerten/Verträge") == "Offerten/Verträge"
        # Die Menschen-Variante akzeptiert ``Unklar`` weiter (manuelle Korrektur).
        assert normalize_label("Unklar") == "Unklar"
        assert normalize_agent_label("  wichtig ") == "Wichtig"

    def test_invented_labels_are_rejected(self):
        """Keine Synonymtabelle: erfundene Labels werden abgewiesen, nicht geraten.

        Das sind echte Beobachtungen aus den 80 Kategorien, die in Outlook landeten.
        """
        from app.services.triage_labels import normalize_label

        for erfunden in (
            "Rechnung", "Termin", "System-Info", "Wichtig/Finanzen",
            "Finance", "Kunde", "", None, 42,
        ):
            assert normalize_label(erfunden) is None, erfunden

    def test_only_three_labels_have_folders(self):
        """Nur drei Ziele stehen dem LLM offen -- Finanzen und Kalender bewusst nicht."""
        from app.services.triage_labels import LABEL_FOLDERS

        assert set(LABEL_FOLDERS) == {"System", "Newsletter", "Junk"}
        assert "Finanzen" not in LABEL_FOLDERS
        assert "Kalender" not in LABEL_FOLDERS

    def test_calendar_label_never_triggers_a_move(self):
        """Termine bleiben sichtbar, auch wenn das Modell sie fuer reine Info haelt.

        Eine Einladung, eine Absage des Veranstalters oder die Terminkonflikt-Meldung
        eines Kunden verlangt eine Reaktion. Nach ``Inbox/Kalender`` verschiebt darum
        ausschliesslich der deterministische Pfad fuer echte Terminantworten.
        """
        from app.services.triage_labels import move_target

        assert move_target("Kalender", "fyi", known_correspondent=False) is None

    def test_own_correspondence_is_never_moved(self):
        """Ein falsches ``System`` auf einer Kundenmail raeumt sie nicht mehr weg.

        Regression zu 49 Fehlmoves in 25 Tagen (Juli/August 2026). Der Skill sagte
        bereits "Hat ein Mensch die Mail geschrieben, ist sie NIE System" -- eine
        Prompt-Bitte, die das Modell mit Confidence 0.9 bis 1.0 ueberging. Betroffen
        waren u. a. "Projekt NITL -- Bitte um Rueckmeldung" von rahel.frey@be.ch und
        zweimal "AW: DRINGEND: PRDAI01 -- Archiv-Mount" von Swiss Bankers. An allen
        gemessenen Schadensfaellen war der Absender eine Adresse, an die Anthony
        selbst schon geschrieben hatte.
        """
        from app.services.triage_labels import move_target

        for label in ("System", "Newsletter", "Junk"):
            assert move_target(label, "fyi", known_correspondent=True) is None, label

    def test_machines_still_move(self):
        """Der Schutz darf das Rauschen nicht in der Inbox stauen.

        ``support@track.toggl.com`` allein kam in 30 Tagen zwoelfmal, GitHub-Meldungen
        sechzehnmal. An keine dieser Adressen hat Anthony je geschrieben.
        """
        from app.services.triage_labels import move_target

        assert move_target("System", "fyi", known_correspondent=False) == "System"
        assert move_target("Newsletter", "fyi", known_correspondent=False) == "Newsletter"

    def test_cold_outreach_still_moves_to_junk(self):
        """Unaufgeforderte Verkaufsanfragen brauchen keinen Label-Sonderfall.

        Sie kommen von Adressen, an die Anthony nie geschrieben hat, und fallen damit
        schon durch die allgemeine Regel. Phishing, das die Adresse eines echten
        Kontakts faelscht, bleibt umgekehrt sichtbar.
        """
        from app.services.triage_labels import move_target

        assert move_target("Junk", "fyi", known_correspondent=False) == "Junk"

    def test_unverifiable_correspondence_blocks_the_move(self):
        """Faellt die Graph-Suche aus, wird nichts verschoben -- fail-closed.

        ``None`` heisst "nicht ermittelt", nicht "kein Kontakt". Ein Ausfall darf
        keine Post wegraeumen, und ein Replay mit lueckenhaften Metadaten soll nicht
        ausgerechnet den Schutz aushebeln.
        """
        from app.services.triage_labels import move_suppressed_reason, move_target

        assert move_target("System", "fyi") is None
        assert move_target("System", "fyi", known_correspondent=None) is None
        assert move_suppressed_reason("System", "fyi") == "Korrespondenz nicht pruefbar"

    def test_move_gate_carries_no_pattern_lists(self):
        """Das Gate darf keine Adress- oder Betreffmuster mehr enthalten.

        Zuvor entschieden vier Listen mit Local-Part-Fragmenten, Rollenpostfach-Namen,
        Domain-Fragmenten und Antwort-Praefixen (``re:``, ``aw:``, ``wg:``) darueber,
        ob Post weggeraeumt wird. Sie waren sprach-, client- und anbieterabhaengig und
        haetten fuer jede weitere Sprache wachsen muessen. Ersetzt durch einen Fakt aus
        dem Postfach. Nicht wieder einfuehren.
        """
        import inspect

        from app.services import triage_labels
        from app.services.triage_labels import move_target

        # Kein Umschlag-Text mehr im Gate: nur noch der Nachweis und die Bremse.
        params = set(inspect.signature(move_target).parameters)
        assert params == {"label", "triage_class", "needs_review", "known_correspondent"}

        # Und keine Muster-Konstanten, die still wieder einwandern koennten.
        entfernt = {
            "_MACHINE_LOCAL_SUBSTRINGS",
            "_MACHINE_DOMAIN_SUBSTRINGS",
            "_ROLE_MAILBOX_SEGMENTS",
            "_REPLY_PREFIXES",
            "is_named_person",
            "is_reply_subject",
        }
        assert entfernt.isdisjoint(vars(triage_labels))

    def test_junk_targets_the_review_subfolder(self):
        """``Junk`` zeigt auf Anthonys Sichtungsordner, nicht auf Outlooks Quarantaene."""
        from app.services.triage_labels import LABEL_FOLDERS

        assert LABEL_FOLDERS["Junk"] == "Junk"

    def test_triage_labels_frontend_in_sync(self):
        """Frontend-Spiegel darf nicht abdriften.

        Die Auswahlliste im Cockpit muss genau die Labels anbieten, die das Backend
        akzeptiert -- sonst laufen Korrekturen in einen 400er oder es fehlt eine
        Kategorie in der UI.
        """
        import re
        from pathlib import Path
        from app.services.triage_labels import TRIAGE_LABELS

        ts = Path(__file__).resolve().parents[2] / "frontend/src/lib/triageLabels.ts"
        assert ts.is_file(), f"Frontend-Spiegel fehlt: {ts}"
        block = re.search(
            r"export const TRIAGE_LABELS = \[(.*?)\] as const;", ts.read_text("utf-8"), re.S
        )
        assert block, "TRIAGE_LABELS-Block im Frontend nicht gefunden"
        assert tuple(re.findall(r"'([^']+)'", block.group(1))) == TRIAGE_LABELS
