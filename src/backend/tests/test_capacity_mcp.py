"""Tests des Kapazitäts-MCP-Servers: Argumentauswertung und Leer-Antwort.

Der Server ist die Antwort auf einen konkreten Produktionsfehler: Am 03.08.2026
nannte ein E-Mail-Entwurf «14h Budget für Juli» als heutigen Stand -- die Zahl
stammte aus einer Mail vom 02.07.2026, weil der Agent kein Fachsystem hatte, das
ihm den Augustwert nennen konnte.

Geprüft wird hier, was ohne Datenbank prüfbar ist: die Zeitraum-Auflösung (inklusive
der Normierung von Teilzeiträumen) und die Fehlermeldungen, die ein Modell lesen und
selbst korrigieren muss.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC / "capacity"))
sys.path.insert(0, str(_SRC / "toggl"))


def _load_capacity_server():
    """Unter eigenem Namen laden, nicht als ``server``.

    Jeder MCP-Server heisst ``server.py``. Ein Import ueber ``sys.path`` belegt den
    Namen prozessweit und verdeckt die anderen Server -- ``test_mcp_handlers.py``
    bekam so den Kapazitaets- statt den Graph-Server.
    """
    path = _SRC / "mcp-capacity" / "server.py"
    spec = importlib.util.spec_from_file_location("cap_server", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cap_server"] = module
    spec.loader.exec_module(module)
    return module


cap_server = _load_capacity_server()


# ── Zeitraum-Auflösung ───────────────────────────────────────────────────────


def test_month_resolves_to_the_full_calendar_month():
    start, end, label, weeks = cap_server._resolve_period({"month": "2026-08"})

    assert (start, end) == (date(2026, 8, 1), date(2026, 8, 31))
    assert label == "2026-08"
    assert weeks is None  # Monatssemantik: 52/12 wie im Cockpit


def test_february_end_is_correct_in_a_leap_year():
    start, end, _, _ = cap_server._resolve_period({"month": "2028-02"})

    assert (start, end) == (date(2028, 2, 1), date(2028, 2, 29))


def test_missing_month_falls_back_to_today():
    _, _, label, _ = cap_server._resolve_period({})

    assert label == date.today().strftime("%Y-%m")


def test_explicit_range_carries_its_own_week_factor():
    """Sonst liefert eine Zweiwochen-Anfrage die Monatszahl -- eine falsche Zusage."""
    start, end, label, weeks = cap_server._resolve_period(
        {"from": "2026-08-03", "to": "2026-08-16"}
    )

    assert (start, end) == (date(2026, 8, 3), date(2026, 8, 16))
    assert label == "2026-08-03..2026-08-16"
    assert weeks == 2.0


def test_a_single_bound_is_ignored_in_favour_of_the_month():
    """Nur ``from`` ohne ``to`` ist kein Zeitraum -- dann gilt der Monat."""
    _, _, label, weeks = cap_server._resolve_period({"from": "2026-08-03", "month": "2026-08"})

    assert label == "2026-08"
    assert weeks is None


# ── Fehlermeldungen für das Modell ───────────────────────────────────────────


def test_malformed_month_yields_a_correctable_message():
    """Vorher stand hier «invalid literal for int() with base 10: 'kapu'».

    Aus so einer Meldung kann ein Modell keine Korrektur ableiten; es wiederholt den
    Fehlaufruf und verbrennt eine Recherche-Runde.
    """
    with pytest.raises(cap_server.BadArgument) as exc:
        cap_server._resolve_period({"month": "kaputt"})

    assert "YYYY-MM" in str(exc.value)
    assert "kaputt" in str(exc.value)


def test_malformed_date_names_the_offending_field():
    with pytest.raises(cap_server.BadArgument) as exc:
        cap_server._resolve_period({"from": "gestern", "to": "2026-08-16"})

    assert "from" in str(exc.value)
    assert "YYYY-MM-DD" in str(exc.value)


def test_reversed_range_is_rejected():
    with pytest.raises(cap_server.BadArgument) as exc:
        cap_server._resolve_period({"from": "2026-08-16", "to": "2026-08-03"})

    assert "liegt vor" in str(exc.value)


# ── Leer-Antwort ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_period_states_that_nothing_is_planned(monkeypatch):
    """Ein leeres ``projects`` schweigt -- und dieses Schweigen füllte das Modell im
    Fehlerfall mit einem Archivfund.

    Für August 2026 war nichts geplant; der Entwurf nannte trotzdem 14h. Die leere
    Antwort muss darum ausdrücklich sagen, dass sie ein Befund ist.
    """
    proj = cap_server.CapacityProjectRef(id="p1", name="Cheetah", toggl_project_id=901)
    monkeypatch.setattr(cap_server, "_load_projects", _async_return([proj]))
    monkeypatch.setattr(cap_server, "_load_allocations", _async_return([]))
    monkeypatch.setattr(cap_server, "_toggl_actuals", _async_return(({}, {})))

    result = await cap_server._capacity_overview(None, {"month": "2026-08"})

    assert result["projects"] == []
    assert "keine Kapazität geplant" in result["antwort"]
    assert "keine Datenlücke" in result["antwort"]


@pytest.mark.asyncio
async def test_populated_period_has_no_empty_notice(monkeypatch):
    """Der Hinweis darf nur erscheinen, wenn wirklich nichts geplant ist."""
    proj = cap_server.CapacityProjectRef(id="p1", name="Cheetah", toggl_project_id=901)
    alloc = cap_server.Allocation("p1", date(2026, 8, 3), 600, "day")
    monkeypatch.setattr(cap_server, "_load_projects", _async_return([proj]))
    monkeypatch.setattr(cap_server, "_load_allocations", _async_return([alloc]))
    monkeypatch.setattr(cap_server, "_toggl_actuals", _async_return(({}, {})))

    result = await cap_server._capacity_overview(None, {"month": "2026-08"})

    assert result["projects"][0]["planned_hours"] == 10.0
    assert "antwort" not in result


@pytest.mark.asyncio
async def test_missing_toggl_still_returns_the_plan(monkeypatch):
    """Ohne Toggl-Token gibt es keine Ist-Zahlen. Der Plan allein ist nützlicher als
    ein Fehler -- der Aufrufer erkennt die Lage an ``actual_available``."""
    proj = cap_server.CapacityProjectRef(id="p1", name="Cheetah", toggl_project_id=901)
    alloc = cap_server.Allocation("p1", date(2026, 8, 3), 600, "day")
    monkeypatch.setattr(cap_server, "_load_projects", _async_return([proj]))
    monkeypatch.setattr(cap_server, "_load_allocations", _async_return([alloc]))
    monkeypatch.setattr(cap_server, "_toggl_actuals", _async_return(({}, {})))

    result = await cap_server._capacity_overview(None, {"month": "2026-08"})

    assert result["actual_available"] is False
    assert result["projects"][0]["actual_hours"] == 0.0


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


# ── Werkzeug-Beschreibungen ──────────────────────────────────────────────────


def test_tools_warn_against_reading_a_budget_entitlement():
    """Der fehlerhafte Entwurf sagte «14h verfügbar» zu. Planungsstunden sind aber
    kein Vertragskontingent -- das muss schon in der Werkzeugbeschreibung stehen,
    weil der Agent sie liest, bevor er entscheidet."""
    overview = next(t for t in cap_server.TOOLS if t.name == "get_capacity_overview")

    assert "Vertragskontingent" in overview.description


def test_absence_tool_points_away_from_the_mail_archive():
    absences = next(t for t in cap_server.TOOLS if t.name == "get_absences")

    assert "nicht Angaben aus älteren E-Mails" in absences.description
