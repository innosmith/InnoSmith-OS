"""Tests der geteilten Kapazitaets-Rechenlogik (``src/capacity/capacity_report.py``).

Diese Zahlen landen in Kundenmails. Die Tests halten die beiden Stellen fest, an
denen sich eine Abweichung einschleichen koennte: die Monatsnormierung der
Wochen-Zuweisungen und die Billable-Sekunden-Auswertung des Toggl-Reports.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capacity"))

from capacity_report import (  # noqa: E402
    WEEKS_PER_MONTH,
    Allocation,
    CapacityProjectRef,
    actual_minutes_by_toggl_project,
    build_report,
    planned_minutes,
    resolve_aggregated_toggl_ids,
    weeks_in_period,
)

JULY_START, JULY_END = date(2026, 7, 1), date(2026, 7, 31)
AUG_START, AUG_END = date(2026, 8, 1), date(2026, 8, 31)


def _week(project: str, week_start: date, minutes: int) -> Allocation:
    return Allocation(project_id=project, week_start=week_start, minutes=minutes)


# ── Monatsnormierung ─────────────────────────────────────────────────────────


def test_weekly_allocations_are_normalized_to_a_month_not_summed():
    """Vier erfasste Wochen à 10h ergeben nicht 40h, sondern das 52/12-fache.

    Die Planung fuehrt Wochen; ein Monat hat aber keine ganze Zahl davon. Waere hier
    summiert, wuerde ein Monat mit fuenf erfassten Wochen 25 % teurer erscheinen als
    einer mit vier -- allein wegen des Kalenders.
    """
    allocs = [_week("p1", date(2026, 7, d), 600) for d in (6, 13, 20, 27)]

    result = planned_minutes(allocs, JULY_START, JULY_END)

    assert result["p1"] == int(round(600 * WEEKS_PER_MONTH))
    assert result["p1"] != 4 * 600


def test_five_recorded_weeks_yield_the_same_month_as_four_at_equal_load():
    """Gleiche Wochenlast, gleiche Monatszahl -- unabhaengig von der Wochenzahl."""
    four = [_week("p1", date(2026, 7, d), 600) for d in (6, 13, 20, 27)]
    five = four + [_week("p1", date(2026, 7, 30), 600)]

    assert planned_minutes(four, JULY_START, JULY_END) == planned_minutes(
        five, JULY_START, JULY_END
    )


def test_day_allocations_are_summed_and_added_to_the_weekly_average():
    """Tages-Zuweisungen sind punktuell und werden darum direkt addiert."""
    allocs = [
        _week("p1", date(2026, 7, 6), 600),
        Allocation("p1", date(2026, 7, 13), 120, "day"),
        Allocation("p1", date(2026, 7, 20), 60, "day"),
    ]

    result = planned_minutes(allocs, JULY_START, JULY_END)

    assert result["p1"] == int(round(600 * WEEKS_PER_MONTH)) + 180


def test_allocations_outside_the_period_are_ignored():
    """Der Juli-Wert darf sich nicht aendern, wenn August-Wochen dazukommen.

    Genau diese Trennung entscheidet, ob der Agent eine Juli- oder eine
    August-Zahl nennt.
    """
    allocs = [
        _week("p1", date(2026, 7, 6), 600),
        _week("p1", date(2026, 8, 3), 1200),
    ]

    july = planned_minutes(allocs, JULY_START, JULY_END)
    august = planned_minutes(allocs, AUG_START, AUG_END)

    assert july["p1"] == int(round(600 * WEEKS_PER_MONTH))
    assert august["p1"] == int(round(1200 * WEEKS_PER_MONTH))


def test_empty_allocations_yield_an_empty_result():
    assert planned_minutes([], JULY_START, JULY_END) == {}


def test_partial_period_is_normalized_to_its_own_length():
    """Zwei Wochen dürfen nicht denselben Wert liefern wie ein ganzer Monat.

    Ohne eigenen Faktor galt fuer jeden Zeitraum die Monatsnormierung -- eine
    Anfrage «wie viel in der ersten Julihaelfte» haette die volle Monatszahl
    ergeben. Solche Zahlen landen in Kundenmails.
    """
    allocs = [_week("p1", date(2026, 7, d), 600) for d in (6, 13)]

    half = planned_minutes(allocs, JULY_START, date(2026, 7, 15), weeks=weeks_in_period(JULY_START, date(2026, 7, 15)))
    whole = planned_minutes(allocs, JULY_START, JULY_END)

    assert half["p1"] < whole["p1"]
    assert half["p1"] == int(round(600 * 15 / 7))


def test_month_semantics_stay_unchanged_without_an_explicit_factor():
    """Das Cockpit rechnet mit 52/12. Der Standardwert darf sich nicht verschieben,
    sonst driften Cockpit und Agent auseinander -- genau das soll die geteilte
    Bibliothek verhindern."""
    allocs = [_week("p1", date(2026, 7, 6), 600)]

    assert planned_minutes(allocs, JULY_START, JULY_END) == planned_minutes(
        allocs, JULY_START, JULY_END, weeks=WEEKS_PER_MONTH
    )


def test_weeks_in_period_counts_both_ends():
    assert weeks_in_period(date(2026, 8, 3), date(2026, 8, 9)) == 1.0
    assert weeks_in_period(date(2026, 8, 3), date(2026, 8, 16)) == 2.0


# ── Toggl-Ist ────────────────────────────────────────────────────────────────


def test_billable_seconds_are_summed_across_rates():
    summary = [{"id": 7, "sub_groups": [{"rates": [{"billable_seconds": 3600}, {"billable_seconds": 1800}]}]}]

    assert actual_minutes_by_toggl_project(summary) == {7: 90}


def test_plain_seconds_count_when_no_rates_are_present():
    """Ohne Rate-Angabe ist ``seconds`` die einzige Quelle -- nicht-verrechenbare Zeit."""
    summary = [{"id": 7, "sub_groups": [{"seconds": 7200}]}]

    assert actual_minutes_by_toggl_project(summary) == {7: 120}


def test_the_larger_of_rate_and_total_wins():
    """Steht beides da, gilt der groessere Wert.

    Sonst verschwindet der nicht-verrechenbare Anteil einer Untergruppe -- die
    ausgewiesene Auslastung waere zu niedrig.
    """
    summary = [{"id": 7, "sub_groups": [{"rates": [{"billable_seconds": 3600}], "seconds": 7200}]}]

    assert actual_minutes_by_toggl_project(summary) == {7: 120}


def test_projects_outside_the_allowlist_are_dropped():
    summary = [{"id": 7, "sub_groups": [{"seconds": 3600}]}, {"id": 9, "sub_groups": [{"seconds": 3600}]}]

    assert actual_minutes_by_toggl_project(summary, {7}) == {7: 60}


def test_groups_without_time_are_omitted():
    summary = [{"id": 7, "sub_groups": [{"seconds": 0}]}]

    assert actual_minutes_by_toggl_project(summary) == {}


# ── Kunden-Aggregation ───────────────────────────────────────────────────────


def _agg(pid: str, client_id: int, flt: str | None = None) -> CapacityProjectRef:
    return CapacityProjectRef(id=pid, name=pid, toggl_client_id=client_id, toggl_billable_filter=flt)


def test_aggregated_projects_collect_all_toggl_projects_of_their_client():
    toggl = [
        {"id": 1, "client_id": 50, "billable": True},
        {"id": 2, "client_id": 50, "billable": False},
        {"id": 3, "client_id": 99, "billable": True},
    ]

    mapping = resolve_aggregated_toggl_ids([_agg("a", 50)], toggl)

    assert sorted(mapping["a"]) == [1, 2]


def test_billable_filter_narrows_the_aggregation():
    toggl = [
        {"id": 1, "client_id": 50, "billable": True},
        {"id": 2, "client_id": 50, "billable": False},
    ]

    assert resolve_aggregated_toggl_ids([_agg("a", 50, "billable")], toggl)["a"] == [1]
    assert resolve_aggregated_toggl_ids([_agg("a", 50, "non_billable")], toggl)["a"] == [2]


def test_directly_linked_projects_are_not_aggregated():
    """Ein Projekt mit eigener Toggl-ID darf nicht zusaetzlich ueber den Kunden zaehlen."""
    proj = CapacityProjectRef(id="a", name="A", toggl_project_id=1, toggl_client_id=50)

    assert resolve_aggregated_toggl_ids([proj], [{"id": 2, "client_id": 50}]) == {}


# ── Fertiger Bericht ─────────────────────────────────────────────────────────


def test_report_delivers_finished_hours_and_the_difference():
    """Fertig gerechnete Stunden: ein 35B-Modell soll Zahlen nennen, nicht addieren."""
    proj = CapacityProjectRef(id="p1", name="Cheetah", client_name="MBA", toggl_project_id=42)
    allocs = [_week("p1", date(2026, 8, d), 600) for d in (3, 10, 17, 24)]

    report = build_report([proj], allocs, {42: 1200}, AUG_START, AUG_END)

    assert len(report) == 1
    entry = report[0]
    assert entry["project"] == "Cheetah"
    assert entry["planned_hours"] == round(600 * WEEKS_PER_MONTH / 60, 1)
    assert entry["actual_hours"] == 20.0
    assert entry["delta_hours"] == round(entry["planned_hours"] - 20.0, 1)


def test_negative_delta_signals_more_recorded_than_planned():
    proj = CapacityProjectRef(id="p1", name="Cheetah", toggl_project_id=42)
    allocs = [Allocation("p1", date(2026, 8, 3), 600, "day")]

    entry = build_report([proj], allocs, {42: 1800}, AUG_START, AUG_END)[0]

    assert entry["planned_hours"] == 10.0
    assert entry["delta_hours"] == -20.0


def test_report_filters_by_client_or_project_name():
    projects = [
        CapacityProjectRef(id="p1", name="Cheetah", client_name="MBA", toggl_project_id=1),
        CapacityProjectRef(id="p2", name="Sympholio", client_name="Andere AG", toggl_project_id=2),
    ]
    allocs = [Allocation("p1", AUG_START, 600, "day"), Allocation("p2", AUG_START, 600, "day")]

    names = [e["project"] for e in build_report(projects, allocs, {}, AUG_START, AUG_END, client="mba")]

    assert names == ["Cheetah"]


def test_projects_without_plan_and_without_actual_are_omitted():
    """Leerzeilen kosten Kontext im Prompt und sagen nichts aus."""
    projects = [
        CapacityProjectRef(id="p1", name="Aktiv", toggl_project_id=1),
        CapacityProjectRef(id="p2", name="Ruht", toggl_project_id=2),
    ]

    report = build_report(projects, [Allocation("p1", AUG_START, 600, "day")], {}, AUG_START, AUG_END)

    assert [e["project"] for e in report] == ["Aktiv"]


def test_aggregated_project_sums_the_actuals_of_its_toggl_projects():
    proj = _agg("a", 50)
    allocs = [Allocation("a", AUG_START, 600, "day")]

    entry = build_report(
        [proj], allocs, {1: 600, 2: 300}, AUG_START, AUG_END, aggregated_ids={"a": [1, 2]}
    )[0]

    assert entry["actual_hours"] == 15.0


@pytest.mark.parametrize("client", ["", None, "   "])
def test_blank_client_filter_keeps_every_project(client):
    proj = CapacityProjectRef(id="p1", name="Cheetah", toggl_project_id=1)
    allocs = [Allocation("p1", AUG_START, 600, "day")]

    assert len(build_report([proj], allocs, {}, AUG_START, AUG_END, client=client)) == 1
