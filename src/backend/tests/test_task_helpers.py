"""Tests fuer isolierte Helper-Funktionen aus dem Task-Router und Recurring-Modul.

Prüft:
- _resolve_assignee_input: 'me' → UUID, 'agent' → 'agent', None → None
- _sanitize_text: XSS-Schutz via bleach (HTML-Tags entfernen)
- Cron-Expression-Validierung und Scheduling via croniter
"""

import uuid
from datetime import date, datetime, timezone

import pytest
from croniter import croniter

from app.routers.tasks import _resolve_assignee_input, _sanitize_text
from tests.conftest import FakeUser, OWNER_ID, MEMBER_ID


# ---------------------------------------------------------------------------
# _resolve_assignee_input
# ---------------------------------------------------------------------------

class TestResolveAssigneeInput:
    """Prüft die Assignee-Auflösung (me → UUID, agent, None)."""

    def test_me_resolves_to_user_id(self, owner_user):
        result = _resolve_assignee_input("me", owner_user)
        assert result == str(OWNER_ID)

    def test_me_resolves_to_member_id(self, member_user):
        result = _resolve_assignee_input("me", member_user)
        assert result == str(MEMBER_ID)

    def test_agent_passes_through(self, owner_user):
        result = _resolve_assignee_input("agent", owner_user)
        assert result == "agent"

    def test_none_returns_none(self, owner_user):
        result = _resolve_assignee_input(None, owner_user)
        assert result is None

    def test_uuid_string_passes_through(self, owner_user):
        """Explizite UUID wird unverändert durchgereicht."""
        some_id = str(uuid.uuid4())
        result = _resolve_assignee_input(some_id, owner_user)
        assert result == some_id

    def test_empty_string_passes_through(self, owner_user):
        result = _resolve_assignee_input("", owner_user)
        assert result == ""


# ---------------------------------------------------------------------------
# _sanitize_text
# ---------------------------------------------------------------------------

class TestSanitizeText:
    """Prüft XSS-Schutz durch HTML-Tag-Entfernung."""

    def test_script_tag_stripped(self):
        result = _sanitize_text("<script>alert('xss')</script>Hello")
        assert "<script>" not in result
        assert "</script>" not in result
        assert "Hello" in result

    def test_plain_text_unchanged(self):
        text = "Normaler Text ohne HTML"
        assert _sanitize_text(text) == text

    def test_none_returns_none(self):
        assert _sanitize_text(None) is None

    def test_html_tags_stripped(self):
        result = _sanitize_text("<b>Fett</b> und <i>kursiv</i>")
        assert "<b>" not in result
        assert "<i>" not in result
        assert "Fett" in result
        assert "kursiv" in result

    def test_nested_tags_stripped(self):
        result = _sanitize_text("<div><p>Paragraph</p></div>")
        assert "<div>" not in result
        assert "<p>" not in result
        assert "Paragraph" in result

    def test_img_tag_stripped(self):
        result = _sanitize_text('<img src="x" onerror="alert(1)">')
        assert "<img" not in result
        assert "onerror" not in result

    def test_link_tag_stripped(self):
        result = _sanitize_text('<a href="https://evil.com">Klick</a>')
        assert "<a" not in result
        assert "Klick" in result

    def test_umlauts_preserved(self):
        text = "Prüfung der Lösung für Übungen"
        assert _sanitize_text(text) == text

    def test_empty_string(self):
        assert _sanitize_text("") == ""

    def test_style_tag_stripped(self):
        result = _sanitize_text("<style>body{display:none}</style>Sichtbar")
        assert "<style>" not in result
        assert "Sichtbar" in result

    def test_ampersand_preserved(self):
        """'&' bleibt literal erhalten (nicht als &amp; gespeichert)."""
        assert _sanitize_text("Tom & Jerry") == "Tom & Jerry"

    def test_special_chars_preserved(self):
        """Literale Sonderzeichen bleiben erhalten, werden nicht escaped."""
        assert _sanitize_text("a < b > c & d") == "a < b > c & d"
        assert _sanitize_text("R&D & A&B") == "R&D & A&B"

    def test_no_entity_encoding(self):
        """Ergebnis enthält keine HTML-Entities für eingegebene Sonderzeichen."""
        result = _sanitize_text("Preis & Wert")
        assert "&amp;" not in result
        assert "&" in result


# ---------------------------------------------------------------------------
# Cron-Validierung (croniter) — isolierte Logik aus recurring.py
# ---------------------------------------------------------------------------

class TestCronValidation:
    """Prüft Cron-Expression-Parsing wie es im Recurring-Scheduler verwendet wird."""

    @pytest.mark.parametrize("expression", [
        "0 9 * * 1",       # Montag 09:00
        "0 8 * * 1-5",     # Werktags 08:00
        "30 14 1 * *",     # Monatlich am 1. um 14:30
        "0 0 * * *",       # Täglich Mitternacht
        "*/15 * * * *",    # Alle 15 Minuten
    ])
    def test_valid_cron_expressions(self, expression):
        assert croniter.is_valid(expression)

    @pytest.mark.parametrize("expression", [
        "",
        "invalid",
        "60 * * * *",       # Minute > 59
        "* 25 * * *",       # Stunde > 23
        "* * 32 * *",       # Tag > 31
        "not a cron",
    ])
    def test_invalid_cron_expressions(self, expression):
        assert not croniter.is_valid(expression)

    def test_next_occurrence_calculation(self):
        """Nächster Lauf nach einem Zeitpunkt wird korrekt berechnet."""
        base = datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc)  # Montag
        cron = croniter("0 9 * * 1", base)  # Montags 09:00
        next_run = cron.get_next(datetime)

        assert next_run.hour == 9
        assert next_run.minute == 0
        assert next_run.weekday() == 0  # Montag

    def test_daily_cron_next_day(self):
        """Tägliche Cron-Expression berechnet nächsten Tag."""
        base = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
        cron = croniter("0 9 * * *", base)  # Täglich 09:00
        next_run = cron.get_next(datetime)

        assert next_run.day == 13
        assert next_run.hour == 9

    def test_monthly_cron(self):
        """Monatliche Cron-Expression am 1. des Monats."""
        base = datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)
        cron = croniter("0 8 1 * *", base)  # Am 1. um 08:00
        next_run = cron.get_next(datetime)

        assert next_run.month == 6
        assert next_run.day == 1
        assert next_run.hour == 8

    def test_weekday_only_cron(self):
        """Werktags-Cron überspringt Wochenende."""
        base = datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)  # Freitag Abend
        cron = croniter("0 9 * * 1-5", base)
        next_run = cron.get_next(datetime)

        assert next_run.weekday() == 0  # Montag
        assert next_run.day == 11

    def test_timezone_naive_result(self):
        """croniter gibt standardmässig timezone-naive Datetimes zurück."""
        base = datetime(2026, 5, 12, 0, 0)
        cron = croniter("0 9 * * *", base)
        next_run = cron.get_next(datetime)
        assert next_run.tzinfo is None

    def test_gate_check_prevents_early_spawn(self):
        """Monatlicher Task: next_run in der Zukunft darf nicht gespawnt werden.

        Simuliert den Fix in recurring.py: `if next_run.date() > now.date(): continue`
        base_time nutzt datetime.max.time() (Tagesende), damit get_next()
        zur NAECHSTEN Cron-Occurrence springt, nicht zur gleichen.
        """
        last_due = datetime.combine(date(2026, 5, 1), datetime.max.time(), tzinfo=timezone.utc)
        cron = croniter("0 8 1 * *", last_due)
        next_run = cron.get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)

        now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)

        assert next_run.date() == date(2026, 6, 1)
        assert next_run.date() > now.date(), "Spawn darf nicht vor Fälligkeit erfolgen"

    def test_gate_check_allows_due_spawn(self):
        """Am Fälligkeitstag wird korrekt gespawnt."""
        last_due = datetime.combine(date(2026, 5, 1), datetime.max.time(), tzinfo=timezone.utc)
        cron = croniter("0 8 1 * *", last_due)
        next_run = cron.get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)

        now = datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)

        assert next_run.date() == date(2026, 6, 1)
        assert next_run.date() <= now.date(), "Spawn muss am Fälligkeitstag erlaubt sein"

    def test_due_date_based_next_run(self):
        """next_run basierend auf due_date (nicht created_at) ergibt korrekten Termin.

        Wenn created_at vom due_date abweicht (z.B. durch Scheduler-Vorlauf),
        muss due_date als Basis für croniter verwendet werden.
        """
        due_date = datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc)
        cron = croniter("0 9 * * 1", due_date)  # Montags 09:00
        next_run = cron.get_next(datetime)

        assert next_run.date() == date(2026, 5, 25)  # Nächster Montag
        assert next_run.weekday() == 0


# ---------------------------------------------------------------------------
# _select_target_occurrence — Catch-up & Lookahead (recurring.py)
# ---------------------------------------------------------------------------

class TestSelectTargetOccurrence:
    """Prüft die Okkurrenz-Auswahl des Recurring-Schedulers.

    Deckt Fix B (Catch-up der aktuellen Periode) und das Lookahead-Verhalten ab.
    """

    def _fn(self):
        from app.services.recurring import _select_target_occurrence
        return _select_target_occurrence

    def test_catchup_same_day_created_after_cron_time(self):
        """Vorlage am selben Tag NACH der Cron-Uhrzeit erstellt → Instanz heute.

        Weekly InnoSmith: Cron Montag 08:00, Vorlage Montag 10:07 erstellt.
        Erwartung: Catch-up auf heute statt nächste Woche.
        """
        select = self._fn()
        now = datetime(2026, 6, 1, 8, 50, tzinfo=timezone.utc)  # Montag
        created = datetime(2026, 6, 1, 8, 7, tzinfo=timezone.utc)
        target = select("0 8 * * MON", now, None, created)
        assert target.date() == date(2026, 6, 1)

    def test_catchup_monthly_first_of_month(self):
        """Monatliche Vorlage am 1. nach der Cron-Zeit erstellt → Instanz heute."""
        select = self._fn()
        now = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
        created = datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
        target = select("0 16 1 * *", now, None, created)
        assert target.date() == date(2026, 6, 1)

    def test_no_backfill_when_occurrence_before_creation(self):
        """Keine Rück-Befüllung: Vorlage erst nach der jüngsten Okkurrenz erstellt.

        Monatlich am 1., Vorlage aber am 15. erstellt → nächste Okkurrenz
        ist der 1. des Folgemonats, NICHT der vergangene 1.
        """
        select = self._fn()
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        created = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
        target = select("0 8 1 * *", now, None, created)
        assert target.date() == date(2026, 7, 1)

    def test_weekly_created_after_weekday_waits_next_week(self):
        """Wöchentlich Montag, Vorlage Dienstag erstellt → wartet bis nächsten Montag."""
        select = self._fn()
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)  # Dienstag
        created = datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)
        target = select("0 8 * * MON", now, None, created)
        assert target.date() == date(2026, 6, 8)  # nächster Montag
        assert target.weekday() == 0

    def test_existing_instance_uses_next_after_last(self):
        """Mit bestehender Instanz wird die nächste Okkurrenz nach last_ts gewählt."""
        select = self._fn()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        last_ts = datetime.combine(date(2026, 5, 1), datetime.max.time(), tzinfo=timezone.utc)
        created = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
        target = select("0 8 1 * *", now, last_ts, created)
        assert target.date() == date(2026, 6, 1)

    def test_existing_instance_no_same_day_respawn(self):
        """Nach Abschluss am Fälligkeitstag: keine erneute Okkurrenz am selben Tag.

        last_ts (Tagesende) sorgt dafür, dass get_next zur NÄCHSTEN Periode springt.
        """
        select = self._fn()
        now = datetime(2026, 5, 19, 15, 0, tzinfo=timezone.utc)
        last_ts = datetime.combine(date(2026, 5, 19), datetime.max.time(), tzinfo=timezone.utc)
        created = datetime(2026, 5, 14, 8, 0, tzinfo=timezone.utc)
        target = select("0 16 19 * *", now, last_ts, created)
        assert target.date() == date(2026, 6, 19)  # nicht erneut am 19. Mai

    def test_result_is_timezone_aware(self):
        """Ergebnis ist immer timezone-aware (UTC-normalisiert)."""
        select = self._fn()
        now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        created = datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)
        target = select("0 8 * * MON", now, None, created)
        assert target.tzinfo is not None


# ---------------------------------------------------------------------------
# _resolve_last_spawn_ts — Persistierter Merker vs. Instanz-Ableitung
# ---------------------------------------------------------------------------

class TestResolveLastSpawnTs:
    """Prüft, dass der Spawn-Merker Instanz-Löschungen überlebt.

    Simuliert den Phantom-Bug: Instanz gelöscht → Ableitung fällt zurück,
    Merker bleibt und verhindert den Respawn der gleichen Okkurrenz.
    """

    def _fn(self):
        from app.services.recurring import _resolve_last_spawn_ts
        return _resolve_last_spawn_ts

    def test_marker_alone(self):
        """Nur Merker gesetzt → Merker als Tagesende-UTC."""
        resolve = self._fn()
        result = resolve(date(2026, 8, 1), None)
        assert result == datetime.combine(
            date(2026, 8, 1), datetime.max.time(), tzinfo=timezone.utc
        )

    def test_instance_alone(self):
        """Nur Instanz-Ableitung → wird unverändert (aware) zurückgegeben."""
        resolve = self._fn()
        latest = datetime.combine(
            date(2026, 7, 1), datetime.max.time(), tzinfo=timezone.utc
        )
        result = resolve(None, latest)
        assert result == latest

    def test_both_none(self):
        resolve = self._fn()
        assert resolve(None, None) is None

    def test_marker_wins_over_older_instance(self):
        """Merker in der Zukunft der Instanzen verhindert Respawn nach Löschung.

        Phantom-Szenario: Instanz 1.8. gelöscht, letzte verbleibende Instanz
        ist 1.7., Merker steht auf 1.8. → Merker gewinnt, nächste Okkurrenz
        wird der 1.9. statt erneut der 1.8.
        """
        resolve = self._fn()
        latest = datetime.combine(
            date(2026, 7, 1), datetime.max.time(), tzinfo=timezone.utc
        )
        result = resolve(date(2026, 8, 1), latest)
        assert result.date() == date(2026, 8, 1)

        from app.services.recurring import _select_target_occurrence
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        created = datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc)
        target = _select_target_occurrence("0 8 1 * *", now, result, created)
        assert target.date() == date(2026, 9, 1)

    def test_instance_wins_when_newer(self):
        """Neuere Instanz ohne aktualisierten Merker (Altbestand) gewinnt."""
        resolve = self._fn()
        latest = datetime.combine(
            date(2026, 8, 1), datetime.max.time(), tzinfo=timezone.utc
        )
        result = resolve(date(2026, 7, 1), latest)
        assert result.date() == date(2026, 8, 1)

    def test_naive_instance_ts_normalized(self):
        """Timezone-naive Instanz-Timestamps werden auf UTC normalisiert."""
        resolve = self._fn()
        latest = datetime.combine(date(2026, 7, 1), datetime.max.time())
        result = resolve(None, latest)
        assert result is not None
        assert result.tzinfo is not None
        assert result.date() == date(2026, 7, 1)
