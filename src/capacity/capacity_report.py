"""Kapazitäts-Auswertung: Plan gegen Ist -- geteilt zwischen Backend und MCP-Server.

Warum eine geteilte Bibliothek und keine zweite Implementierung: Die Zahlen, die
der Agent in einem E-Mail-Entwurf nennt, müssen dieselben sein, die im Cockpit
stehen. Zwei Implementierungen driften -- und eine Abweichung fällt erst auf, wenn
sie beim Kunden in der Mail steht.

Geteilt wird bewusst nur die **Rechenlogik**, nicht der Datenbankzugriff: das
Backend arbeitet mit SQLAlchemy, der MCP-Server mit asyncpg. Beide Treiber in eine
Bibliothek zu zwingen wäre teurer als die paar Zeilen Abfrage doppelt zu haben.
Heikel und darum hier zuhause sind die beiden Stellen, an denen man sich
verrechnen kann: die Monatsnormierung der Wochen-Zuweisungen und die
Billable-Sekunden-Auswertung des Toggl-Summary-Reports.

Ohne schwere Importe (nur Standardbibliothek), damit der MCP-Server sie per
PYTHONPATH laden kann -- dasselbe Muster wie ``src/toggl/`` und ``src/email-graph/``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

# Ein Monat entspricht 52/12 Wochen. Die Kapazitätsplanung führt Zuweisungen pro
# KW; eine Monatszahl entsteht daher aus dem Wochenmittel mal diesem Faktor, nicht
# aus der Summe der Wochen, die in den Monat fallen -- sonst wäre ein Monat mit
# fünf erfassten Wochen automatisch 25 % "teurer" als einer mit vier.
WEEKS_PER_MONTH = 52 / 12


@dataclass(frozen=True)
class Allocation:
    """Eine Zuweisung: Projekt, Kalenderwoche, Minuten, Art (``week``/``day``)."""

    project_id: str
    week_start: date
    minutes: int
    allocation_type: str = "week"


@dataclass(frozen=True)
class CapacityProjectRef:
    """Kapazitätsprojekt mit seiner Toggl-Verknüpfung.

    Ein Projekt hängt entweder an einem Toggl-Projekt (``toggl_project_id``) oder
    aggregiert alle Projekte eines Toggl-Kunden (``toggl_client_id``), optional
    gefiltert auf ``billable``/``non_billable``.
    """

    id: str
    name: str
    client_name: str | None = None
    status: str = "bestätigt"
    toggl_project_id: int | None = None
    toggl_client_id: int | None = None
    toggl_billable_filter: str | None = None


def weeks_in_period(period_start: date, period_end: date) -> float:
    """Länge eines Zeitraums in Wochen (beide Enden inklusive)."""
    return ((period_end - period_start).days + 1) / 7


def planned_minutes(
    allocations: list[Allocation],
    period_start: date,
    period_end: date,
    weeks: float | None = None,
) -> dict[str, int]:
    """Geplante Minuten pro Projekt für einen Zeitraum.

    Wochen-Zuweisungen werden über das Wochenmittel auf die Zeitraumlänge normiert,
    Tages-Zuweisungen direkt summiert. Rein und damit ohne DB testbar.

    ``weeks`` ist die Länge, auf die normiert wird; ohne Angabe gilt Monatslänge
    (52/12). Das ist wichtig für Teilzeiträume: ohne eigenen Faktor würde eine
    Anfrage über zwei Wochen denselben Wert liefern wie über den ganzen Monat --
    eine Zahl, die so in einer Kundenmail landen könnte.
    """
    week_minutes: dict[str, list[int]] = defaultdict(list)
    day_minutes: dict[str, int] = defaultdict(int)

    for alloc in allocations:
        if not (period_start <= alloc.week_start <= period_end):
            continue
        if alloc.allocation_type == "day":
            day_minutes[alloc.project_id] += alloc.minutes
        else:
            week_minutes[alloc.project_id].append(alloc.minutes)

    factor = WEEKS_PER_MONTH if weeks is None else weeks
    out: dict[str, int] = defaultdict(int)
    for pid, mins in week_minutes.items():
        out[pid] = int(round(sum(mins) / len(mins) * factor))
    for pid, mins in day_minutes.items():
        out[pid] += mins
    return dict(out)


def actual_minutes_by_toggl_project(
    summary: list[dict], allowed_ids: set[int] | None = None
) -> dict[int, int]:
    """Ist-Minuten pro Toggl-Projekt aus dem Summary-Report.

    Die Struktur ist unangenehm: pro Gruppe liegen Untergruppen, deren Zeit
    entweder in ``rates[].billable_seconds`` oder in ``seconds``/``time`` steht.
    Steht beides da, gilt der grössere Wert -- nicht-verrechenbare Anteile gehen
    sonst verloren. Diese Regel stammt aus der Debitorenansicht und ist der Grund,
    weshalb die Funktion hier zentral liegt.
    """
    out: dict[int, int] = {}
    for group in summary:
        pid = group.get("id") or 0
        if allowed_ids is not None and pid not in allowed_ids:
            continue
        sub_groups = group.get("sub_groups") or group.get("items") or []
        seconds = 0.0
        for item in sub_groups:
            rates = item.get("rates") or []
            for rate_info in rates:
                seconds += rate_info.get("billable_seconds", 0) or 0
            total = item.get("seconds", 0) or item.get("time", 0) or 0
            if not rates:
                seconds += total
            elif total > seconds:
                seconds = total
        if seconds > 0:
            out[pid] = int(seconds / 60)
    return out


def resolve_aggregated_toggl_ids(
    projects: list[CapacityProjectRef], toggl_projects: list[dict]
) -> dict[str, list[int]]:
    """Ordnet Kunden-aggregierten Projekten ihre Toggl-Projekt-IDs zu.

    ``toggl_projects`` sind Einträge mit ``id``, ``client_id`` und ``billable``.
    """
    mapping: dict[str, list[int]] = {}
    for proj in projects:
        if proj.toggl_project_id or not proj.toggl_client_id:
            continue
        flt = proj.toggl_billable_filter
        mapping[proj.id] = [
            tp["id"]
            for tp in toggl_projects
            if tp.get("id")
            and tp.get("client_id") == proj.toggl_client_id
            and (
                not flt
                or (flt == "non_billable" and not tp.get("billable"))
                or (flt == "billable" and tp.get("billable"))
            )
        ]
    return mapping


def build_report(
    projects: list[CapacityProjectRef],
    allocations: list[Allocation],
    actual_by_toggl_id: dict[int, int],
    period_start: date,
    period_end: date,
    aggregated_ids: dict[str, list[int]] | None = None,
    client: str | None = None,
    weeks: float | None = None,
) -> list[dict]:
    """Fügt Plan und Ist pro Projekt zusammen -- fertige Zahlen, keine Rohdaten.

    Bewusst fertig gerechnet: ein 35B-Modell soll Zahlen nennen, nicht addieren.
    Die Differenz ist Plan minus Ist; ein negativer Wert heisst also, es wurde mehr
    erfasst als geplant.
    """
    plan = planned_minutes(allocations, period_start, period_end, weeks)
    agg = aggregated_ids or {}
    needle = (client or "").strip().lower()

    out: list[dict] = []
    for proj in projects:
        if needle and needle not in (proj.client_name or "").lower() and needle not in proj.name.lower():
            continue
        if proj.toggl_project_id:
            actual = actual_by_toggl_id.get(proj.toggl_project_id, 0)
        else:
            actual = sum(actual_by_toggl_id.get(t, 0) for t in agg.get(proj.id, []))
        planned = plan.get(proj.id, 0)
        if not planned and not actual:
            continue
        out.append({
            "project": proj.name,
            "client": proj.client_name,
            "status": proj.status,
            "planned_hours": round(planned / 60, 1),
            "actual_hours": round(actual / 60, 1),
            "delta_hours": round((planned - actual) / 60, 1),
            "toggl_project_id": proj.toggl_project_id,
        })
    out.sort(key=lambda e: e["planned_hours"], reverse=True)
    return out
