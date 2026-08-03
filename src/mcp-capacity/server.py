"""MCP-Server für die Kapazitätsplanung -- Plan gegen Ist und Abwesenheiten.

Warum es diesen Server gibt: Die Kapazitätsplanung war bislang nur über die REST-API
erreichbar, also nur für den Menschen. Der Agent musste sich Zahlen zu Stunden und
Verfügbarkeit aus dem E-Mail-Archiv zusammenreimen -- und tat das prompt falsch. Am
03.08.2026 übernahm ein Entwurf den Satz «für Juli haben wir noch 14h Budget» aus
einer Mail vom 02.07.2026 als heutigen Stand. Für Fakten, die sich fortlaufend
ändern, braucht der Agent Zugriff auf das Fachsystem statt auf dessen Nachhall im
Archiv.

Die Rechenlogik liegt in ``src/capacity/capacity_report.py`` und wird mit dem
Backend geteilt, damit die Zahlen im Entwurf und die im Cockpit dieselben sind.
Dieser Server liefert nur Abfrage und Formatierung.
"""

import asyncio
import json
import logging
import os
import sys
from calendar import monthrange
from datetime import date, timedelta

import asyncpg
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "capacity"))
sys.path.insert(0, os.path.join(_HERE, "..", "toggl"))

from capacity_report import (  # noqa: E402
    Allocation,
    CapacityProjectRef,
    actual_minutes_by_toggl_project,
    build_report,
    resolve_aggregated_toggl_ids,
    weeks_in_period,
)
from toggl_client import TogglClient, TogglConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_capacity")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


_pool: asyncpg.Pool | None = None
_toggl: TogglClient | None = None


async def get_pool() -> asyncpg.Pool:
    """Pool über die Prozesslebensdauer halten (Muster wie mcp-taskpilot)."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=_env("TP_DB_HOST", "localhost"),
            port=int(_env("TP_DB_PORT", "5435")),
            user=_env("TP_DB_USER", "taskpilot"),
            password=_env("TP_DB_PASSWORD", "taskpilot_dev_2026"),
            database=_env("TP_DB_NAME", "taskpilot_dev"),
            min_size=1,
            max_size=2,
        )
    return _pool


def get_toggl() -> TogglClient | None:
    """Toggl-Client wiederverwenden; ``None``, wenn kein Token gesetzt ist."""
    global _toggl
    if _toggl is None:
        cfg = TogglConfig.from_env()
        if not cfg.is_configured:
            return None
        _toggl = TogglClient(cfg)
    return _toggl


TOOLS = [
    Tool(
        name="get_capacity_overview",
        description=(
            "Geplante und erfasste Stunden pro Projekt für einen Zeitraum -- die "
            "verbindliche Antwort auf Fragen nach Kapazität, Auslastung, "
            "verfügbaren oder verbrauchten Stunden. Zahlen sind fertig gerechnet "
            "(Plan aus der Kapazitätsplanung, Ist aus Toggl). Ohne Angabe gilt der "
            "aktuelle Monat. WICHTIG: Das ist die Planung, KEIN Vertragskontingent "
            "-- daraus folgt keine Aussage darüber, welches Budget ein Kunde noch "
            "abrufen darf."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM, Standard: aktueller Monat"},
                "from": {"type": "string", "description": "Startdatum YYYY-MM-DD (statt month)"},
                "to": {"type": "string", "description": "Enddatum YYYY-MM-DD (statt month)"},
                "client": {
                    "type": "string",
                    "description": "Auf Kunden oder Projektname eingrenzen (Teilstring, z. B. 'MBA')",
                },
            },
        },
    ),
    Tool(
        name="get_absences",
        description=(
            "Abwesenheiten für einen Zeitraum: Ferien, Feiertage, Krankheit. Die "
            "verbindliche Antwort auf «bin ich da / ab wann wieder erreichbar» -- "
            "nicht Angaben aus älteren E-Mails verwenden."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Startdatum YYYY-MM-DD, Standard: heute"},
                "to": {"type": "string", "description": "Enddatum YYYY-MM-DD, Standard: +90 Tage"},
            },
            "required": [],
        },
    ),
]

server = Server("taskpilot-capacity")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


class BadArgument(Exception):
    """Unbrauchbares Argument -- die Meldung geht an das Modell und muss es leiten."""


def _parse_day(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise BadArgument(f"«{field}» muss ein Datum als YYYY-MM-DD sein, erhalten: «{raw}»") from None


def _resolve_period(args: dict) -> tuple[date, date, str, float | None]:
    """Bestimmt den Auswertungszeitraum aus ``month`` oder ``from``/``to``.

    Der vierte Rückgabewert ist der Normierungsfaktor in Wochen: ``None`` für einen
    ganzen Monat (dann gilt 52/12 wie im Cockpit), sonst die echte Zeitraumlänge.
    Ohne diese Unterscheidung lieferte eine Anfrage über zwei Wochen denselben Wert
    wie über den ganzen Monat.
    """
    raw_from = (args.get("from") or "").strip()
    raw_to = (args.get("to") or "").strip()
    if raw_from and raw_to:
        start = _parse_day(raw_from, "from")
        end = _parse_day(raw_to, "to")
        if end < start:
            raise BadArgument(f"«to» ({raw_to}) liegt vor «from» ({raw_from})")
        return start, end, f"{raw_from}..{raw_to}", weeks_in_period(start, end)

    month = (args.get("month") or "").strip()
    if month:
        try:
            year, mon = int(month[:4]), int(month[5:7])
            start = date(year, mon, 1)
        except ValueError:
            raise BadArgument(
                f"«month» muss das Format YYYY-MM haben, erhalten: «{month}»"
            ) from None
    else:
        today = date.today()
        year, mon = today.year, today.month
        start = date(year, mon, 1)
    end = date(year, mon, monthrange(year, mon)[1])
    return start, end, f"{year:04d}-{mon:02d}", None


def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def _load_projects(pool: asyncpg.Pool) -> list[CapacityProjectRef]:
    rows = await pool.fetch(
        "SELECT id, name, client_name, status, toggl_project_id, toggl_client_id, "
        "toggl_billable_filter FROM capacity_projects "
        "WHERE toggl_project_id IS NOT NULL OR toggl_client_id IS NOT NULL"
    )
    return [
        CapacityProjectRef(
            id=str(r["id"]),
            name=r["name"],
            client_name=r["client_name"],
            status=r["status"],
            toggl_project_id=r["toggl_project_id"],
            toggl_client_id=r["toggl_client_id"],
            toggl_billable_filter=r["toggl_billable_filter"],
        )
        for r in rows
    ]


async def _load_allocations(
    pool: asyncpg.Pool, period_start: date, period_end: date
) -> list[Allocation]:
    # Ab Montag der Startwoche, weil Zuweisungen auf Wochenanfänge datiert sind.
    rows = await pool.fetch(
        "SELECT capacity_project_id, week_start, minutes, allocation_type "
        "FROM capacity_allocations WHERE week_start >= $1 AND week_start <= $2",
        _monday_of(period_start),
        period_end,
    )
    return [
        Allocation(
            project_id=str(r["capacity_project_id"]),
            week_start=r["week_start"],
            minutes=r["minutes"],
            allocation_type=r["allocation_type"],
        )
        for r in rows
    ]


async def _toggl_actuals(
    projects: list[CapacityProjectRef], period_start: date, period_end: date
) -> tuple[dict[int, int], dict[str, list[int]]]:
    """Ist-Minuten pro Toggl-Projekt plus Kunden-Aggregation. Best-effort.

    Fehlt der Toggl-Token oder ist die API nicht erreichbar, gibt es keine
    Ist-Zahlen -- der Plan allein ist immer noch nützlicher als nichts, und der
    Aufrufer erkennt es am Feld ``actual_available``.
    """
    client = get_toggl()
    if client is None:
        return {}, {}

    aggregated: dict[str, list[int]] = {}
    try:
        if any(p.toggl_client_id and not p.toggl_project_id for p in projects):
            toggl_projects = await client.list_projects(active=None)
            aggregated = resolve_aggregated_toggl_ids(
                projects,
                [
                    {
                        "id": p.get("id"),
                        "client_id": p.get("client_id") or p.get("cid"),
                        "billable": p.get("billable", False),
                    }
                    for p in toggl_projects
                ],
            )

        allowed = {p.toggl_project_id for p in projects if p.toggl_project_id}
        for ids in aggregated.values():
            allowed.update(ids)

        summary = await client.get_summary_by_project(
            start_date=period_start.isoformat(),
            end_date=period_end.isoformat(),
            billable=None,
        )
        return actual_minutes_by_toggl_project(summary, allowed), aggregated
    except Exception as exc:  # noqa: BLE001 - best-effort, Plan bleibt nutzbar
        logger.warning("Toggl-Ist nicht abrufbar: %s", exc)
        return {}, aggregated


async def _capacity_overview(pool: asyncpg.Pool, args: dict) -> dict:
    period_start, period_end, label, weeks = _resolve_period(args)
    projects = await _load_projects(pool)
    if not projects:
        return {"period": label, "projects": [], "note": "keine Kapazitätsprojekte gepflegt"}

    allocations = await _load_allocations(pool, period_start, period_end)
    actuals, aggregated = await _toggl_actuals(projects, period_start, period_end)
    report = build_report(
        projects,
        allocations,
        actuals,
        period_start,
        period_end,
        aggregated_ids=aggregated,
        client=args.get("client"),
        weeks=weeks,
    )
    out = {
        "period": label,
        "from": period_start.isoformat(),
        "to": period_end.isoformat(),
        "actual_available": bool(actuals),
        "count": len(report),
        "projects": report,
        "hinweis": (
            "planned_hours = Planung, actual_hours = in Toggl erfasst, "
            "delta_hours = Plan minus Ist. Kein Vertragskontingent."
        ),
    }
    if not report:
        # Ein leeres ``projects`` schweigt -- und genau dieses Schweigen hat das
        # Modell im Fall vom 03.08.2026 mit einem Archivfund gefuellt. Die leere
        # Antwort muss darum ausdruecklich sagen, dass sie eine Aussage IST.
        scope = f" für «{args['client']}»" if args.get("client") else ""
        out["antwort"] = (
            f"Für {label} ist{scope} keine Kapazität geplant. Das ist ein belegter "
            "Befund, keine Datenlücke -- eine ältere Zahl aus E-Mails gilt hier nicht."
        )
    return out


async def _absences(pool: asyncpg.Pool, args: dict) -> dict:
    raw_from = (args.get("from") or "").strip()
    raw_to = (args.get("to") or "").strip()
    start = _parse_day(raw_from, "from") if raw_from else date.today()
    end = _parse_day(raw_to, "to") if raw_to else start + timedelta(days=90)
    if end < start:
        raise BadArgument(f"«to» ({end}) liegt vor «from» ({start})")

    rows = await pool.fetch(
        "SELECT date, type, label, hours FROM capacity_time_off "
        "WHERE date >= $1 AND date <= $2 ORDER BY date",
        start,
        end,
    )
    days = [
        {
            "date": r["date"].isoformat(),
            "type": r["type"],
            "label": r["label"],
            "hours": r["hours"],
        }
        for r in rows
    ]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "count": len(days),
        "days": days,
    }


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        pool = await get_pool()
        if name == "get_capacity_overview":
            payload = await _capacity_overview(pool, arguments or {})
        elif name == "get_absences":
            payload = await _absences(pool, arguments or {})
        else:
            payload = {"error": f"Unbekanntes Tool: {name}"}
    except BadArgument as exc:
        # Kein Stacktrace: das Modell soll die Korrektur lesen, nicht den Traceback.
        payload = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - Fehler als Tool-Ergebnis zurückgeben
        logger.exception("Tool %s fehlgeschlagen", name)
        payload = {"error": f"Fehler in {name}: {exc}"}
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
