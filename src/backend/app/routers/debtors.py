"""Debitorensicht -- Kundenzentrisches Finanz-Cockpit.

Liest wie die Finanzansicht ausschliesslich aus dem **Datenraum**
(``services/datenraum_lesen.py``). Vorher standen hier eigene Kopien von
``_parse_invoice_total`` und ``_invoice_is_open`` -- mit denselben zwei stillen
Fehlern wie in ``finance.py``: eine Entwurfsrechnung galt als offene Forderung, und
``total_gross`` wurde als Bruttobetrag gelesen, obwohl es die Positionssumme vor
Rabatt ist.

Dass es Kopien waren, ist der eigentliche Befund. Zwei Ansichten desselben Hauses
zeigten dieselbe Kennzahl aus demselben Bestand, konnten aber unterschiedlich
antworten -- und niemand hätte gewusst, welche recht hat. Deshalb sind die Helfer
nicht korrigiert, sondern entfernt.
"""

import logging
from collections import defaultdict
from datetime import date, timedelta

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.deps import get_current_user, require_role
from app.models import User
from app.services import datenraum_lesen as dl
from app.services.datenraum_lesen import DatenraumUnbrauchbar

logger = logging.getLogger("taskpilot.debtors")

router = APIRouter(prefix="/api/debtors", tags=["debtors"])

# Die TTL ist das Sicherheitsnetz; ausschlaggebend ist der Datenraum-Stand im
# Schlüssel. Der frühere Unterschied zwischen «laufender Monat kurz, abgeschlossene
# Monate lange» ist damit hinfällig: alles hängt am Abgleich, nicht an einer Uhr.
_cache: TTLCache = TTLCache(maxsize=10, ttl=300)
_toggl_month_cache: TTLCache = TTLCache(maxsize=36, ttl=900)


def _schluessel(basis: str) -> str:
    return f"{basis}@{dl.stand_kennung()}"


def _datenraum_fehler(exc: DatenraumUnbrauchbar) -> HTTPException:
    logger.warning("Debitorensicht: Datenraum unbrauchbar -- %s", exc)
    return HTTPException(status_code=503, detail=f"Debitorendaten nicht auswertbar: {exc}")


# ── Response-Modelle ─────────────────────────────────────

class TogglProjectRow(BaseModel):
    project_id: int = 0
    project_name: str
    client_id: int | None = None
    client_name: str = ""
    hours: float = 0
    billable_hours: float = 0
    is_billable: bool = True
    pct_of_total: float = 0
    rate_per_hour: float = 0
    amount: float = 0
    budget_hours: float | None = None
    budget_pct: float | None = None


class DailyHours(BaseModel):
    date: str
    billable: float = 0
    non_billable: float = 0


class TogglMonthSummary(BaseModel):
    total_hours: float = 0
    billable_hours: float = 0
    non_billable_hours: float = 0
    billable_ratio: float = 0
    total_amount: float = 0
    avg_daily_hours: float = 0
    forecast_month_amount: float = 0
    working_days_total: int = 0
    working_days_elapsed: int = 0
    projects: list[TogglProjectRow] = []
    daily_hours: list[DailyHours] = []


class DebtorSummary(BaseModel):
    contact_id: int
    contact_name: str
    revenue_ytd: float = 0
    revenue_prior_year: float = 0
    revenue_delta_pct: float | None = None
    open_invoices_count: int = 0
    open_invoices_total: float = 0
    avg_payment_days: float | None = None
    aging_0_30: float = 0
    aging_31_60: float = 0
    aging_61_90: float = 0
    aging_over_90: float = 0
    project_count: int = 0


class RevenueByMonth(BaseModel):
    contact_id: int
    contact_name: str
    months: dict[str, float] = {}


class Datenstand(BaseModel):
    stand: str | None = None
    alter_stunden: float | None = None
    veraltet: bool = False


class DebtorsResponse(BaseModel):
    toggl_month: TogglMonthSummary
    debtors: list[DebtorSummary]
    revenue_trend: list[RevenueByMonth] = []
    total_open: float = 0
    total_revenue_ytd: float = 0
    dso_days: float | None = None
    currency: str = "CHF"
    datenstand: Datenstand = Datenstand()


# ── Arbeitstage-Berechnung ───────────────────────────────

def _working_days_in_month(year: int, month: int) -> int:
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    count = 0
    d = first
    while d <= last:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def _working_days_elapsed(year: int, month: int) -> int:
    first = date(year, month, 1)
    today = date.today()
    end = min(today, date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year + 1, 1, 1) - timedelta(days=1))
    count = 0
    d = first
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


# ── Toggl-Monats-Cockpit (pro Monat, gecacht) ────────────

def _month_bounds(year: int, month: int) -> tuple[str, str, bool]:
    """Liefert (month_start, month_end, is_current) fuer einen Monat.

    Beim laufenden Monat endet der Zeitraum bei ``today`` ("bis heute"),
    bei abgeschlossenen Monaten beim letzten Kalendertag.
    """
    today = date.today()
    is_current = year == today.year and month == today.month
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    month_end = today if is_current else last
    return first.isoformat(), month_end.isoformat(), is_current


def _compute_toggl_month(user: User, year: int, month: int) -> TogglMonthSummary:
    """Das Monats-Cockpit der Zeiterfassung aus dem Datenraum.

    Fuer abgeschlossene Monate liefern die Arbeitstage-Helfer automatisch
    ``elapsed == total`` (Fortschritt 100%), womit die Prognose dem Ist
    entspricht. Zukunftsmonate ergeben eine leere Zusammenfassung.

    Der Stundensatz je Projekt ist der **Effektivsatz** (Betrag durch verrechenbare
    Stunden). Die frühere Fassung nahm den ersten Satz, den die Toggl-Antwort
    nannte -- bei zwei Sätzen im selben Monat passte er nicht zum Betrag daneben.
    """
    today = date.today()
    if date(year, month, 1) > today:
        return TogglMonthSummary()

    month_start, month_end, _is_current = _month_bounds(year, month)
    eintraege = dl.zeiteintraege(month_start, month_end, nur_verrechenbar=False)

    budgets: dict[str, dict] = (user.settings or {}).get("debtor_budgets") or {}

    je_projekt: dict[int, dict] = defaultdict(lambda: {
        "hours": 0.0, "billable_hours": 0.0, "amount": 0.0,
        "name": "", "client_name": "", "client_id": None,
    })
    daily_map: dict[str, dict[str, float]] = {}

    for e in eintraege:
        pid = int(e["projekt_id"]) if e["projekt_id"] is not None else 0
        eintrag = je_projekt[pid]
        eintrag["hours"] += e["stunden"]
        eintrag["name"] = eintrag["name"] or e["projekt"]
        eintrag["client_name"] = eintrag["client_name"] or e["kunde"]
        if e["verrechenbar"]:
            eintrag["billable_hours"] += e["stunden"]
            eintrag["amount"] += e["betrag"]

        tag = daily_map.setdefault(e["datum"], {"billable": 0.0, "non_billable": 0.0})
        tag["billable" if e["verrechenbar"] else "non_billable"] += e["stunden"]

    rows: list[TogglProjectRow] = []
    total_hours = 0.0
    total_billable = 0.0
    total_amount = 0.0

    for pid, data in je_projekt.items():
        if data["hours"] <= 0:
            continue
        total_hours += data["hours"]
        total_billable += data["billable_hours"]
        total_amount += data["amount"]

        budget_cfg = budgets.get(str(pid)) or {}
        budget_hours = budget_cfg.get("monthly_hours") if budget_cfg else None
        budget_pct = (
            round(data["hours"] / budget_hours * 100, 1)
            if budget_hours and budget_hours > 0 else None
        )
        satz = (
            data["amount"] / data["billable_hours"]
            if data["billable_hours"] > 0 else 0.0
        )

        rows.append(TogglProjectRow(
            project_id=pid,
            project_name=data["name"] or f"Projekt {pid}",
            client_id=data["client_id"],
            client_name=data["client_name"],
            hours=round(data["hours"], 2),
            billable_hours=round(data["billable_hours"], 2),
            is_billable=data["billable_hours"] > 0,
            rate_per_hour=round(satz, 2),
            amount=round(data["amount"], 2),
            budget_hours=budget_hours,
            budget_pct=budget_pct,
        ))

    for row in rows:
        if total_hours > 0:
            row.pct_of_total = round(row.hours / total_hours * 100, 1)
    rows.sort(key=lambda x: x.hours, reverse=True)

    wd_total = _working_days_in_month(year, month)
    wd_elapsed = _working_days_elapsed(year, month)

    avg_daily = total_hours / wd_elapsed if wd_elapsed > 0 else 0
    billable_daily = total_billable / wd_elapsed if wd_elapsed > 0 else 0
    rate_avg = total_amount / total_billable if total_billable > 0 else 0
    forecast_amount = billable_daily * wd_total * rate_avg if wd_elapsed > 0 else 0

    return TogglMonthSummary(
        total_hours=round(total_hours, 2),
        billable_hours=round(total_billable, 2),
        non_billable_hours=round(total_hours - total_billable, 2),
        billable_ratio=round(total_billable / total_hours * 100, 1) if total_hours > 0 else 0,
        total_amount=round(total_amount, 2),
        avg_daily_hours=round(avg_daily, 2),
        forecast_month_amount=round(forecast_amount, 2),
        working_days_total=wd_total,
        working_days_elapsed=wd_elapsed,
        projects=rows,
        daily_hours=[
            DailyHours(
                date=d,
                billable=round(v["billable"], 2),
                non_billable=round(v["non_billable"], 2),
            )
            for d, v in sorted(daily_map.items())
        ],
    )


def _get_toggl_month_cached(user: User, year: int, month: int) -> TogglMonthSummary:
    """Monatsdaten gemerkt laden -- gebunden an den Stand des Datenraums."""
    schluessel = _schluessel(f"{year}-{month:02d}")
    cached = _toggl_month_cache.get(schluessel)
    if cached is not None:
        return cached
    result = _compute_toggl_month(user, year, month)
    _toggl_month_cache[schluessel] = result
    return result


# ── Haupt-Endpoint ───────────────────────────────────────

@router.get("", response_model=DebtorsResponse)
async def get_debtors(
    user: User = Depends(require_role("owner")),
):
    """Debitorenübersicht: Monats-Cockpit der Zeiterfassung + Debitoren."""
    schluessel = _schluessel("debtors")
    cached = _cache.get(schluessel)
    if cached is not None:
        return cached

    try:
        result = _debtors_berechnen(user)
    except DatenraumUnbrauchbar as exc:
        raise _datenraum_fehler(exc) from exc

    _cache[schluessel] = result
    return result


def _debtors_berechnen(user: User) -> DebtorsResponse:
    today = date.today()
    current_year = today.year
    prior_year = current_year - 1

    toggl_month = _get_toggl_month_cached(user, today.year, today.month)

    # Pro Kontakt aggregieren. ``ist_umsatz`` schliesst Entwürfe aus -- sie sind
    # weder Umsatz noch offene Forderung.
    by_contact: dict[int, dict] = defaultdict(lambda: {
        "name": "",
        "revenue_ytd": 0.0, "revenue_prior": 0.0,
        "open_count": 0, "open_total": 0.0,
        "aging_0_30": 0.0, "aging_31_60": 0.0,
        "aging_61_90": 0.0, "aging_over_90": 0.0,
        "by_month": defaultdict(float),
    })

    for inv in dl.rechnungen():
        cid = inv.get("kunden_id")
        if not cid or not inv["ist_umsatz"]:
            continue
        cid = int(cid)
        eintrag = by_contact[cid]
        eintrag["name"] = eintrag["name"] or inv["kunde"]
        eintrag["by_month"][inv["monat"]] += inv["brutto"]

        if inv["monat"].startswith(str(current_year)):
            eintrag["revenue_ytd"] += inv["brutto"]
        elif inv["monat"].startswith(str(prior_year)):
            eintrag["revenue_prior"] += inv["brutto"]

        if inv["offen"] > 0.01:
            eintrag["open_count"] += 1
            eintrag["open_total"] += inv["offen"]
            # Gestaffelt nach dem Alter der Rechnung, nicht nach Verzug -- so war es
            # schon vorher, und der Wechsel wäre eine fachliche Entscheidung.
            alter = (today - date.fromisoformat(inv["datum"])).days
            if alter <= 30:
                eintrag["aging_0_30"] += inv["offen"]
            elif alter <= 60:
                eintrag["aging_31_60"] += inv["offen"]
            elif alter <= 90:
                eintrag["aging_61_90"] += inv["offen"]
            else:
                eintrag["aging_over_90"] += inv["offen"]

    debtors: list[DebtorSummary] = []
    total_open = 0.0
    total_revenue_ytd = 0.0

    for cid, data in by_contact.items():
        if data["revenue_ytd"] <= 0 and data["open_total"] <= 0 and data["revenue_prior"] <= 0:
            continue

        rev_ytd = data["revenue_ytd"]
        rev_prior = data["revenue_prior"]
        total_open += data["open_total"]
        total_revenue_ytd += rev_ytd

        debtors.append(DebtorSummary(
            contact_id=cid,
            contact_name=data["name"] or f"Kontakt {cid}",
            revenue_ytd=round(rev_ytd, 2),
            revenue_prior_year=round(rev_prior, 2),
            revenue_delta_pct=(
                round((rev_ytd - rev_prior) / rev_prior * 100, 1) if rev_prior > 0 else None
            ),
            open_invoices_count=data["open_count"],
            open_invoices_total=round(data["open_total"], 2),
            aging_0_30=round(data["aging_0_30"], 2),
            aging_31_60=round(data["aging_31_60"], 2),
            aging_61_90=round(data["aging_61_90"], 2),
            aging_over_90=round(data["aging_over_90"], 2),
        ))

    debtors.sort(key=lambda x: x.revenue_ytd, reverse=True)

    dso_days = None
    if total_revenue_ytd > 0 and today.month > 0:
        daily_rev = total_revenue_ytd / (today.month * 30)
        if daily_rev > 0:
            dso_days = round(total_open / daily_rev, 0)

    # Umsatztrend: Top-5-Kunden, letzte 12 Monate
    revenue_trend = [
        RevenueByMonth(
            contact_id=cid,
            contact_name=data["name"] or f"Kontakt {cid}",
            months=dict(sorted(data["by_month"].items())[-12:]),
        )
        for cid, data in sorted(
            by_contact.items(), key=lambda x: x[1]["revenue_ytd"], reverse=True
        )[:5]
        if data["by_month"]
    ]

    return DebtorsResponse(
        toggl_month=toggl_month,
        debtors=debtors,
        revenue_trend=revenue_trend,
        total_open=round(total_open, 2),
        total_revenue_ytd=round(total_revenue_ytd, 2),
        dso_days=dso_days,
        datenstand=Datenstand(**dl.stand()),
    )


@router.get("/toggl-month", response_model=TogglMonthSummary)
async def get_toggl_month(
    month: str | None = Query(None, description="Monat im Format YYYY-MM (Default: aktueller Monat)"),
    user: User = Depends(require_role("owner")),
):
    """Toggl-Monats-Cockpit fuer einen bestimmten Monat (fuer die Monatsnavigation)."""
    today = date.today()
    year, mon = today.year, today.month
    if month:
        try:
            parts = month.split("-")
            year, mon = int(parts[0]), int(parts[1])
            if not (1 <= mon <= 12) or year < 2000 or year > today.year + 1:
                raise ValueError
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Ungueltiges Monatsformat, erwartet YYYY-MM")
    try:
        return _get_toggl_month_cached(user, year, mon)
    except DatenraumUnbrauchbar as exc:
        raise _datenraum_fehler(exc) from exc


@router.post("/cache/clear")
async def clear_cache(user: User = Depends(require_role("owner"))):
    _cache.clear()
    _toggl_month_cache.clear()
    logger.info("Debtors-Cache manuell geleert")
    return {"status": "ok", "message": "Debtors-Cache geleert"}
