"""Tests für die proaktiven Assistenzfunktionen.

Abgedeckt (rein deterministisch, ohne DB/Netz):
- Briefing-Scheduler: Soll-Zeitpunkt-Berechnung (Daily/Weekly/Monthly)
- Meeting-Pipeline: VTT-Parser, Chunking, Anonymisierung (Regex + Pseudonyme)
- Follow-up-Erkennung: Arbeitstage, Antwort-Erkennung, Empfänger-Extraktion
"""

from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

from app.services.briefing import _parse_time, _scheduled_for
from app.services.followup import (
    _first_recipient,
    _has_reply,
    _workdays_since,
)
from app.services.meetings import (
    _letter_label,
    apply_pseudonyms,
    chunk_transcript,
    mask_deterministic,
    parse_vtt,
)

_TZ = ZoneInfo("Europe/Zurich")


# ── Briefing-Scheduler ────────────────────────────────────────────────────────

class TestScheduledFor:
    def test_daily_werktag_nach_sollzeit(self):
        # Donnerstag 2026-07-02 08:00 -> Soll heute 06:30
        now = datetime(2026, 7, 2, 8, 0, tzinfo=_TZ)
        result = _scheduled_for("daily_briefing", {}, now)
        assert result == datetime(2026, 7, 2, 6, 30, tzinfo=_TZ)

    def test_daily_vor_sollzeit_nimmt_vortag(self):
        # Donnerstag 05:00 -> jüngster Werktag-Slot ist Mittwoch 06:30
        now = datetime(2026, 7, 2, 5, 0, tzinfo=_TZ)
        result = _scheduled_for("daily_briefing", {}, now)
        assert result == datetime(2026, 7, 1, 6, 30, tzinfo=_TZ)

    def test_daily_wochenende_springt_auf_freitag(self):
        # Sonntag 2026-07-05 10:00 -> jüngster Werktag ist Freitag 03.07.
        now = datetime(2026, 7, 5, 10, 0, tzinfo=_TZ)
        result = _scheduled_for("daily_briefing", {}, now)
        assert result == datetime(2026, 7, 3, 6, 30, tzinfo=_TZ)

    def test_daily_deaktiviert(self):
        now = datetime(2026, 7, 2, 8, 0, tzinfo=_TZ)
        assert _scheduled_for("daily_briefing", {"briefing_daily_enabled": False}, now) is None

    def test_daily_eigene_uhrzeit(self):
        now = datetime(2026, 7, 2, 9, 0, tzinfo=_TZ)
        result = _scheduled_for("daily_briefing", {"briefing_daily_time": "08:15"}, now)
        assert result == datetime(2026, 7, 2, 8, 15, tzinfo=_TZ)

    def test_weekly_default_sonntag(self):
        # Montag 2026-07-06 09:00 -> jüngster Sonntag-Slot war 05.07. 17:00
        now = datetime(2026, 7, 6, 9, 0, tzinfo=_TZ)
        result = _scheduled_for("weekly_briefing", {}, now)
        assert result == datetime(2026, 7, 5, 17, 0, tzinfo=_TZ)

    def test_weekly_slot_heute_noch_nicht_erreicht(self):
        # Sonntag 10:00, Slot 17:00 -> Vorwoche
        now = datetime(2026, 7, 5, 10, 0, tzinfo=_TZ)
        result = _scheduled_for("weekly_briefing", {}, now)
        assert result == datetime(2026, 6, 28, 17, 0, tzinfo=_TZ)

    def test_weekly_eigener_tag(self):
        # Freitag als Weekly-Tag; Samstag 10:00 -> gestriger Freitag-Slot
        now = datetime(2026, 7, 4, 10, 0, tzinfo=_TZ)
        result = _scheduled_for("weekly_briefing", {"briefing_weekly_day": 4}, now)
        assert result == datetime(2026, 7, 3, 17, 0, tzinfo=_TZ)

    def test_monthly_letzter_arbeitstag(self):
        # 31.07.2026 ist ein Freitag -> Slot heute 17:00
        now = datetime(2026, 7, 31, 18, 0, tzinfo=_TZ)
        result = _scheduled_for("monthly_briefing", {}, now)
        assert result == datetime(2026, 7, 31, 17, 0, tzinfo=_TZ)

    def test_monthly_mitte_monat_nimmt_vormonat(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=_TZ)
        result = _scheduled_for("monthly_briefing", {}, now)
        assert result == datetime(2026, 6, 30, 17, 0, tzinfo=_TZ)

    def test_monthly_ueberspringt_wochenende(self):
        """31.05.2026 ist ein Sonntag — das Briefing gehört auf Freitag den 29.

        Vorher lief das Monatsbriefing am letzten Kalendertag. Fiel der auf ein
        Wochenende, erschien die Planung für den nächsten Monat erst, wenn
        dieser bereits begonnen hatte.
        """
        now = datetime(2026, 5, 31, 20, 0, tzinfo=_TZ)
        result = _scheduled_for("monthly_briefing", {}, now)
        assert result == datetime(2026, 5, 29, 17, 0, tzinfo=_TZ)

    def test_monthly_ueberspringt_abwesenheit(self):
        """Ferien am Monatsende verschieben das Briefing auf davor.

        Ein Monatsbriefing, das während der Ferien erscheint, ist bei der
        Rückkehr veraltet — die Planung findet vor der Abwesenheit statt.
        """
        now = datetime(2026, 7, 31, 18, 0, tzinfo=_TZ)
        off = {date(2026, 7, d) for d in (29, 30, 31)}
        result = _scheduled_for("monthly_briefing", {}, now, off)
        assert result == datetime(2026, 7, 28, 17, 0, tzinfo=_TZ)

    def test_unbekannter_typ(self):
        now = datetime(2026, 7, 2, 8, 0, tzinfo=_TZ)
        assert _scheduled_for("quarterly_briefing", {}, now) is None


class TestBriefingAnweisungsKonsistenz:
    """Prompt und Datensammlung müssen dasselbe Briefing beschreiben.

    Vorfall: Das Monatsbriefing nannte Umsatzzahlen, die um Grössenordnungen
    falsch waren. Ursache war nicht das Modell, sondern die Kombination aus
    einem Prompt, der eine Monatsbilanz verlangte, und einer Datenlage, die
    Fakturierung vor Monatsende erwartete. Ein Widerspruch zwischen den beiden
    Seiten fällt keinem Test auf, der nur eine Seite liest — deshalb hier
    beide gemeinsam.
    """

    def _quellen(self, builder_name: str) -> str:
        import inspect

        from app.services import briefing_data

        return inspect.getsource(getattr(briefing_data, builder_name))

    def test_monatsbriefing_ohne_finanzsektionen(self):
        src = self._quellen("build_monthly_context")
        for begriff in ("umsatz_sollist", "coverage", "kreditoren", "revenue"):
            assert begriff not in src, f"Finanz-Sektion «{begriff}» wieder im Monatsbriefing"

    def test_monatsprompt_verbietet_finanzzahlen(self):
        from app.services.hermes_worker import _BRIEFING_INSTRUCTIONS

        prompt = _BRIEFING_INSTRUCTIONS["monthly_briefing"]
        assert "Umsatz" in prompt and "NICHT" in prompt
        assert "Monatsbilanz" not in prompt

    def test_tagesbriefing_verlangt_keine_priorisierung(self):
        """Das Tagesbriefing sammelt keine Aufgabenlisten — also darf der Prompt
        auch keine Top-3-Auswahl verlangen, sonst erfindet das Modell sie."""
        from app.services.hermes_worker import _BRIEFING_INSTRUCTIONS

        prompt = _BRIEFING_INSTRUCTIONS["daily_briefing"]
        assert "KEINE Top-3-Liste" in prompt
        src = self._quellen("build_daily_context")
        assert "_sec_focus_tasks" not in src
        assert "_sec_tasks_due_today" not in src

    def test_alle_briefings_verbieten_tabellen(self):
        from app.services.hermes_worker import _build_briefing_prompt

        import inspect

        src = inspect.getsource(_build_briefing_prompt)
        assert "KEINE Markdown-Tabelle" in src


class TestParseTime:
    def test_gueltig(self):
        t = _parse_time("07:45", "06:30")
        assert (t.hour, t.minute) == (7, 45)

    def test_ungueltig_faellt_auf_default(self):
        t = _parse_time("kaputt", "06:30")
        assert (t.hour, t.minute) == (6, 30)

    def test_none_faellt_auf_default(self):
        t = _parse_time(None, "17:00")
        assert (t.hour, t.minute) == (17, 0)


# ── VTT-Parser + Chunking ─────────────────────────────────────────────────────

SAMPLE_VTT = """WEBVTT

a1b2c3d4-0001/1-0
00:00:01.000 --> 00:00:04.000
<v Anthony Smith>Guten Morgen zusammen.</v>

a1b2c3d4-0002/2-0
00:00:04.500 --> 00:00:08.000
<v Anthony Smith>Fangen wir mit dem Status an.</v>

a1b2c3d4-0003/3-0
00:00:08.500 --> 00:00:12.000
<v Petra Muster>Das Backend ist bereit für den Test.</v>
"""


class TestParseVtt:
    def test_sprecher_attribuiert(self):
        text = parse_vtt(SAMPLE_VTT)
        assert "**Anthony Smith:**" in text
        assert "**Petra Muster:**" in text
        assert "Backend ist bereit" in text

    def test_caption_merge_gleicher_sprecher(self):
        text = parse_vtt(SAMPLE_VTT)
        # Beide Anthony-Captions verschmelzen zu einem Absatz
        assert text.count("**Anthony Smith:**") == 1
        assert "Guten Morgen zusammen. Fangen wir mit dem Status an." in text

    def test_header_und_identifier_verworfen(self):
        text = parse_vtt(SAMPLE_VTT)
        assert "WEBVTT" not in text
        assert "a1b2c3d4" not in text
        assert "-->" not in text

    def test_leerer_input(self):
        assert parse_vtt("") == ""
        assert parse_vtt("WEBVTT\n") == ""


class TestChunkTranscript:
    def test_kurzer_text_ein_chunk(self):
        assert chunk_transcript("Hallo Welt", max_chars=100) == ["Hallo Welt"]

    def test_langer_text_mehrere_chunks(self):
        paras = [f"**Sprecher {i}:** " + ("bla " * 50) for i in range(20)]
        text = "\n\n".join(paras)
        chunks = chunk_transcript(text, max_chars=1000)
        assert len(chunks) > 1
        # Kein Absatz wird zerteilt: Zusammensetzen ergibt den Originaltext
        assert "\n\n".join(chunks) == text

    def test_chunkgrenzen_respektieren_max(self):
        paras = ["x" * 400 for _ in range(10)]
        chunks = chunk_transcript("\n\n".join(paras), max_chars=1000)
        assert all(len(c) <= 1000 for c in chunks)


# ── Anonymisierung ────────────────────────────────────────────────────────────

class TestMaskDeterministic:
    def test_email_maskiert(self):
        mapping: dict[str, str] = {}
        out = mask_deterministic("Bitte an anthony@innosmith.ch senden.", mapping)
        assert "anthony@innosmith.ch" not in out
        assert "[E-Mail 1]" in out
        assert mapping["anthony@innosmith.ch"] == "[E-Mail 1]"

    def test_telefon_maskiert(self):
        mapping: dict[str, str] = {}
        out = mask_deterministic("Erreichbar unter +41 79 123 45 67 heute.", mapping)
        assert "79 123 45 67" not in out
        assert "[Telefon 1]" in out

    def test_konsistenz_bei_wiederholung(self):
        mapping: dict[str, str] = {}
        out = mask_deterministic("a@b.ch schreibt an a@b.ch und c@d.ch", mapping)
        assert out.count("[E-Mail 1]") == 2
        assert "[E-Mail 2]" in out

    def test_mapping_wird_fortgefuehrt(self):
        # Re-Run mit bestehendem Mapping vergibt keine doppelten Nummern
        mapping = {"alt@firma.ch": "[E-Mail 1]"}
        out = mask_deterministic("neu@firma.ch", mapping)
        assert "[E-Mail 2]" in out


class TestApplyPseudonyms:
    def test_personen_und_firmen(self):
        mapping: dict[str, str] = {}
        names = [
            {"name": "Petra Muster", "type": "person"},
            {"name": "InnoSmith", "type": "org"},
        ]
        out = apply_pseudonyms("Petra Muster arbeitet bei InnoSmith.", names, mapping)
        assert out == "Person A arbeitet bei Firma A."

    def test_laengste_namen_zuerst(self):
        # "Petra Muster" darf nicht durch ein separates "Petra" zerteilt werden
        mapping: dict[str, str] = {}
        names = [
            {"name": "Petra", "type": "person"},
            {"name": "Petra Muster", "type": "person"},
        ]
        out = apply_pseudonyms("Petra Muster und Petra.", names, mapping)
        assert mapping["Petra Muster"] != mapping["Petra"]
        assert "Muster" not in out

    def test_wortgrenzen(self):
        mapping: dict[str, str] = {}
        names = [{"name": "Max", "type": "person"}]
        out = apply_pseudonyms("Max nutzt Maximal viel.", names, mapping)
        assert "Person A nutzt Maximal" in out

    def test_mapping_konsistent_ueber_reruns(self):
        mapping: dict[str, str] = {}
        names = [{"name": "Petra", "type": "person"}]
        apply_pseudonyms("Petra", names, mapping)
        first = mapping["Petra"]
        # Zweiter Lauf mit zusätzlichem Namen: bestehendes Mapping bleibt
        names2 = [{"name": "Petra", "type": "person"}, {"name": "Hans", "type": "person"}]
        apply_pseudonyms("Petra und Hans", names2, mapping)
        assert mapping["Petra"] == first
        assert mapping["Hans"] != first


class TestLetterLabel:
    def test_erste_buchstaben(self):
        assert _letter_label(0) == "A"
        assert _letter_label(1) == "B"
        assert _letter_label(25) == "Z"

    def test_ueberlauf_excel_stil(self):
        assert _letter_label(26) == "AA"
        assert _letter_label(27) == "AB"


# ── Follow-up-Erkennung ───────────────────────────────────────────────────────

class TestWorkdaysSince:
    def test_gleiche_woche(self):
        # Mo 29.06. -> Fr 03.07. = 4 Arbeitstage (Di, Mi, Do, Fr)
        assert _workdays_since(date(2026, 6, 29), date(2026, 7, 3)) == 4

    def test_wochenende_zaehlt_nicht(self):
        # Fr 03.07. -> Mo 06.07. = 1 Arbeitstag (nur Montag)
        assert _workdays_since(date(2026, 7, 3), date(2026, 7, 6)) == 1

    def test_gleicher_tag(self):
        assert _workdays_since(date(2026, 7, 3), date(2026, 7, 3)) == 0

    def test_ende_vor_start(self):
        assert _workdays_since(date(2026, 7, 6), date(2026, 7, 3)) == 0


class TestHasReply:
    SENT_AT = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)

    @staticmethod
    def _msg(sender: str, received: str) -> dict:
        return {
            "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": received,
        }

    def test_antwort_von_anderem_absender(self):
        msgs = [self._msg("kunde@firma.ch", "2026-06-26T09:00:00Z")]
        assert _has_reply(msgs, "anthony@innosmith.ch", self.SENT_AT) is True

    def test_eigene_nachricht_zaehlt_nicht(self):
        msgs = [self._msg("anthony@innosmith.ch", "2026-06-26T09:00:00Z")]
        assert _has_reply(msgs, "anthony@innosmith.ch", self.SENT_AT) is False

    def test_aeltere_nachricht_zaehlt_nicht(self):
        msgs = [self._msg("kunde@firma.ch", "2026-06-24T09:00:00Z")]
        assert _has_reply(msgs, "anthony@innosmith.ch", self.SENT_AT) is False

    def test_leerer_thread(self):
        assert _has_reply([], "anthony@innosmith.ch", self.SENT_AT) is False


class TestFirstRecipient:
    def test_normale_adresse(self):
        msg = {"toRecipients": [{"emailAddress": {"address": "Kunde@Firma.CH"}}]}
        assert _first_recipient(msg) == "kunde@firma.ch"

    def test_keine_empfaenger(self):
        assert _first_recipient({}) == ""
        assert _first_recipient({"toRecipients": []}) == ""


class TestKeinAdressmusterFilter:
    """Der Nachfass-Pfad filtert nicht mehr nach Adressmustern.

    Zuvor sortierte ein Regex (``noreply``/``donotreply``/``newsletter``/
    ``notification``) Empfaenger aus, an die Nachfassen sinnlos schien. Gemessen an
    199 tatsaechlich gesendeten Mails traf er KEINE einzige -- Anthony schreibt nicht
    an Automaten, und ein Nachfass-Vorschlag entsteht nur zu selbst gesendeter Post.
    Alleiniger inhaltlicher Entscheider ist das LLM-Gate. Nicht wieder einfuehren.
    """

    def test_kein_regex_mehr_im_modul(self):
        from app.services import followup

        assert not hasattr(followup, "_NOREPLY_RE")
