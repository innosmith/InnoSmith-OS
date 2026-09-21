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
from datetime import date, datetime, timedelta

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_role
from app.database import get_db
from app.models import User
from app.services import datenraum_lesen as dl
from app.services import debitoren_laufbuch as lb
from app.services.audit import log_audit
from app.services.datenraum_lesen import DatenraumUnbrauchbar
from app.services.debitoren_lauf import periodengrenzen
from app.services.trockenlauf import BexioSperre, GraphSperre, TogglSperre

logger = logging.getLogger("taskpilot.debtors")

router = APIRouter(prefix="/api/debtors", tags=["debtors"])

# Was ein Schreibaufruf mindestens mitbringen muss. Ausgelagert, weil derselbe
# Satz an fünf Endpunkten steht und eine Beschreibung, die an fünf Stellen
# steht, ist an vier davon veraltet.
_ECHT = """Ob wirklich geschrieben wird. Vorgabe ist **nein**: der Aufruf läuft
vollständig durch — mit den echten Daten aus Bexio und Toggl —, und nur die
Aufrufe, die etwas verändern, werden angehalten und protokolliert.

Die Sicherung liegt in diesem Vorgabewert, nicht in einer Einstellung: ein
vergessenes Feld schreibt nichts. Die Einstellung ``debitoren_trockenlauf``
bestimmt allein, ob die Oberfläche zwei Klicks verlangt oder einen."""


async def _abschluss(db: AsyncSession, *, trocken: bool) -> None:
    """Festschreiben oder zurückrollen.

    Der Trockenlauf durchläuft auch die eigenen Schreibwege — Laufbuch-Vermerk,
    Audit-Eintrag — und rollt am Ende zurück. Damit gibt es keine zweite
    Fassung der Schreiblogik, die von der echten abweichen könnte, und ein
    Trockenlauf hinterlässt trotzdem keine Spur.
    """
    if trocken:
        await db.rollback()
    else:
        await db.commit()

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


# ── Prüfung des Rechnungslaufs ───────────────────────────
#
# Der einzige Endpunkt dieses Routers, der **nicht** aus dem Datenraum liest.
# Die Begründung steht im Kopf von ``services/debitoren_lauf.py``: Positionen
# eines Entwurfs sind kein analytisches Datum, und wer während des Laufs eine
# Zahl korrigiert, darf nicht bis zum nächsten Abgleich auf das Urteil warten.
# Kein Zwischenspeicher, aus demselben Grund.


class BefundOut(BaseModel):
    regel: str
    titel: str
    feld: str
    zustand: str
    gewicht: str
    begruendung: str
    erwartet: str | None = None
    ist: str | None = None


class VermerkOut(BaseModel):
    """Was am Lauf zu einer Rechnung vermerkt ist.

    Getrennt vom Prüfergebnis, weil es aus einer anderen Quelle stammt: das
    Ergebnis kommt live aus Bexio und Toggl, der Vermerk aus dem Laufbuch.
    Beides in ein Feld zu mischen hiesse, eine Entscheidung wie eine Messung
    aussehen zu lassen.
    """

    zurueckgestellt: bool = False
    grund: str | None = None
    dokumente_erzeugt_am: datetime | None = None
    mailentwurf_id: str | None = None
    versendet_am: datetime | None = None
    abgelegt_am: datetime | None = None


class RechnungspruefungOut(BaseModel):
    rechnung: str
    rechnung_id: int | None = None
    """Die Bexio-Kennung — die Identität, über die Vermerke laufen."""
    projekt: str
    kunde: str
    vertragsart: str
    gesamt: str | None = None
    versandbereit: bool
    befunde: list[BefundOut]
    vermerk: VermerkOut = VermerkOut()


class AenderungOut(BaseModel):
    positionsart: str
    position_id: int
    handlung: str
    feld: str
    alt: str
    neu: str
    begruendung: str


class VorschlagOut(BaseModel):
    """Was an einer Rechnung zu tun wäre — berechnet, nicht angewendet."""

    rechnung: str
    projekt: str
    aenderungen: list[AenderungOut] = []
    hindernisse: list[str] = []
    hinweise: list[str] = []


class PruefungOut(BaseModel):
    periode: str
    regeln: list[str]
    versandbereit: int
    blockiert: int
    rechnungen: list[RechnungspruefungOut]
    ohne_vertrag: list[dict] = []
    ohne_entwurf: list[dict] = []
    ausserhalb: list[dict] = []
    intern: list[str] = []
    auftragsluecken: list[dict] = []
    """Was die Bexio-Aufträge sagen: Dauerauftrag ohne Rechnung, abgelaufener
    Auftrag mit Aktivität, laufender Auftrag ohne Vertrag."""
    auffaelligkeiten: dict[str, list[str]] = {}
    vorschlaege: list[VorschlagOut] = []
    uebertrag: dict[str, float] = {}
    """Der Stand je Vertrag, gelesen aus der jüngsten Rechnung davor."""
    uebertrag_herkunft: dict[str, str] = {}
    """Aus welchem Monat der Stand stammt. Bei einer belegten Pause ist das
    nicht der Vormonat, und das gehört sichtbar."""
    uebertrag_hinweise: list[str] = []
    """Wo kein Stand belegt ist und warum. Kein technischer Fehler, sondern ein
    Befund: Stunden in Toggl ohne Rechnung sind eine Lücke in der Reihe."""
    stammdaten: list[str] = []
    fehler: list[str] = []


def _periode(month: str | None) -> tuple[int, int]:
    """Den Leistungsmonat bestimmen. Vorgabe ist der **Vormonat**, weil der Lauf
    am Anfang des Folgemonats stattfindet."""
    heute = date.today()
    vormonat = date(heute.year, heute.month, 1) - timedelta(days=1)
    if not month:
        return vormonat.year, vormonat.month
    try:
        teile = month.split("-")
        jahr, monat = int(teile[0]), int(teile[1])
        if not (1 <= monat <= 12) or not (2000 <= jahr <= heute.year + 1):
            raise ValueError
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Ungültiges Monatsformat, erwartet YYYY-MM")
    return jahr, monat


async def _pruefstand(user: User, jahr: int, monat: int):
    """Entwürfe, Positionen und Stunden holen und auswerten.

    Gemeinsame Grundlage von Ansicht und Anwenden. Dass beide **denselben** Weg
    nehmen, ist die tragende Eigenschaft: der Browser schickt beim Anwenden nur
    die Auswahl, nie eine Zahl. Sonst wäre die angezeigte Herleitung eine
    Erzählung neben dem, was tatsächlich geschrieben wird.
    """
    from app.services import debitoren_lauf as dlauf
    from app.services.debitorenvertraege import laden
    from app.services.fachsysteme import bexio_zugang, toggl_zugang

    bestand, stammbefund = laden()
    bexio = bexio_zugang(user)
    toggl, workspace = toggl_zugang(user)

    roh = await dlauf.abrufen(bexio, toggl, jahr=jahr, monat=monat, workspace=workspace)
    # Der Übertragsstand kommt aus den Rechnungen davor, nicht aus einer
    # gepflegten Datei: was dort steht, hat der Kunde gesehen. Toggl entscheidet,
    # ob ein Monat ohne Rechnung eine Pause war oder eine Lücke.
    stand = await dlauf.uebertrag_zu_monatsbeginn(
        bexio, bestand, jahr=jahr, monat=monat, toggl=toggl, workspace=workspace
    )
    auswertung = dlauf.auswerten(
        roh, bestand, jahr=jahr, monat=monat, uebertrag_vormonat=stand.staende
    )
    return auswertung, stand, stammbefund, bexio, roh


@router.get("/pruefung", response_model=PruefungOut)
async def pruefung(
    month: str | None = Query(None, description="Leistungsmonat YYYY-MM, Vorgabe: Vormonat"),
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die Entwürfe eines Leistungsmonats gegen die Verträge halten.

    Ersetzt den Excel-Bericht. Rein lesend: nichts wird in Bexio, Toggl oder
    Outlook verändert — auch das Laufbuch wird nur gelesen und nicht angelegt.
    """
    jahr, monat = _periode(month)
    auswertung, stand, stammbefund, _bexio, _roh = await _pruefstand(user, jahr, monat)

    # Das Laufbuch liegt über dem Bestand, es ersetzt ihn nicht: welche
    # Rechnungen es gibt, sagt Bexio; was mit ihnen geschehen ist, das Laufbuch.
    kennungen = {(e.get("nummer") or ""): e.get("rechnung_id") for e in _roh.entwuerfe}
    karte = lb.als_karte(await lb.lauf_holen(db, jahr=jahr, monat=monat))

    return PruefungOut(
        periode=f"{jahr:04d}-{monat:02d}",
        regeln=list(pr_regeln()),
        versandbereit=auswertung.versandbereit,
        blockiert=auswertung.blockiert,
        rechnungen=[
            RechnungspruefungOut(
                rechnung=e.rechnung,
                rechnung_id=kennungen.get(e.rechnung),
                projekt=e.projekt,
                kunde=e.kunde,
                vertragsart=e.vertragsart.value,
                gesamt=e.gesamt.value if e.gesamt else None,
                versandbereit=e.versandbereit,
                vermerk=VermerkOut(**karte.get(kennungen.get(e.rechnung), {})),
                befunde=[
                    BefundOut(
                        regel=b.regel, titel=b.titel, feld=b.feld,
                        zustand=b.zustand.value, gewicht=b.gewicht.value,
                        begruendung=b.begruendung, erwartet=b.erwartet, ist=b.ist,
                    )
                    for b in e.befunde
                ],
            )
            for e in auswertung.ergebnisse
        ],
        ohne_vertrag=auswertung.ohne_vertrag,
        ohne_entwurf=auswertung.ohne_entwurf,
        ausserhalb=auswertung.ausserhalb,
        intern=auswertung.intern,
        auftragsluecken=auswertung.auftragsluecken,
        auffaelligkeiten=auswertung.auffaelligkeiten,
        vorschlaege=[
            VorschlagOut(
                rechnung=v.rechnung, projekt=v.projekt,
                aenderungen=[
                    AenderungOut(
                        positionsart=a.positionsart, position_id=a.position_id,
                        handlung=a.handlung, feld=a.feld, alt=a.alt, neu=a.neu,
                        begruendung=a.begruendung,
                    )
                    for a in v.aenderungen
                ],
                hindernisse=v.hindernisse, hinweise=v.hinweise,
            )
            for v in auswertung.vorschlaege
        ],
        uebertrag=stand.staende,
        uebertrag_herkunft=stand.herkunft,
        uebertrag_hinweise=stand.hinweise,
        # Mängel der Stammdatei gehören in dieselbe Ansicht: ein Vertrag ohne
        # Empfänger fällt sonst erst auf, wenn die Mail nicht abgeht.
        stammdaten=_stammdatenmaengel(stammbefund),
        fehler=auswertung.fehler,
    )


class ErzeugenIn(BaseModel):
    month: str | None = None
    vertraege: list[str] | None = None
    """Für welche Verträge Entwürfe entstehen sollen. ``None`` heisst: für alle
    mit dem Befund «Auftrag ohne Rechnung». Welche Aufträge das sind, wird hier
    neu ermittelt — der Browser schickt keine Auftragskennungen."""
    echt: bool = False
    """Ob wirklich geschrieben wird — siehe ``_ECHT``."""


class ErzeugtOut(BaseModel):
    auftrag: str
    titel: str
    rechnung_id: int | None = None
    nummer: str = ""
    datum: str | None = None
    faellig: str | None = None
    hindernis: str = ""


class ErzeugenOut(BaseModel):
    periode: str
    stichtag: str
    erzeugt: int
    gescheitert: int
    protokoll: list[ErzeugtOut] = []
    uebergangen: list[str] = []
    trocken: bool = True
    vermerke: list[str] = []
    """Die angehaltenen Schreibaufrufe, wortwörtlich wie sie ergangen wären."""


@router.post("/pruefung/erzeugen", response_model=ErzeugenOut)
async def entwuerfe_erzeugen(
    eingabe: ErzeugenIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Aus den fälligen Bexio-Aufträgen Entwürfe erzeugen, auf den Monatsletzten
    datiert.

    Ersetzt «Aufträge → Rechnungen generieren» in der Bexio-Oberfläche und den
    Handgriff danach, das Datum zu korrigieren.

    **Welche Aufträge fällig sind, entscheidet nicht der Browser.** Er nennt
    Verträge; die Aufträge dazu kommen aus ``auftragslage``, also aus Takt,
    Vertragszustand und dem, was im Monat schon fakturiert ist. Eine
    Schnittstelle, die Auftragskennungen entgegennähme, erzeugte auf Zuruf
    Rechnungen aus beliebigen Aufträgen.

    **Das Datum ist der Grund für diesen Endpunkt.** Bexio datiert auf heute,
    der Lauf findet am ersten oder zweiten des Folgemonats statt, und die
    Umsatzabgrenzung verlangt den Monatsletzten. Die Verschiebung ist deshalb
    Teil derselben Handlung und kein zweiter Schritt, den man vergessen kann.
    """
    from app.services.debitoren_erzeugen import erzeugen

    jahr, monat = _periode(eingabe.month)
    auswertung, _stand, _befund, bexio, _roh = await _pruefstand(user, jahr, monat)
    _von, bis = periodengrenzen(jahr, monat)

    trocken = not eingabe.echt
    if trocken:
        bexio = BexioSperre(bexio)

    gewuenscht = eingabe.vertraege
    auftraege: list[tuple[int, str]] = []
    behandelt: set[str] = set()
    for luecke in auswertung.auftragsluecken:
        if luecke.get("art") != "auftrag_ohne_rechnung":
            continue
        schluessel = luecke.get("vertrag") or ""
        if gewuenscht is not None and schluessel not in gewuenscht:
            continue
        behandelt.add(schluessel)
        for kennung in luecke.get("auftrag_ids") or []:
            auftraege.append((int(kennung), luecke.get("titel") or schluessel))

    protokoll = await erzeugen(bexio, auftraege, stichtag=bis)

    # Sobald der Lauf etwas anlegt, gibt es ihn. Vorher nicht: ein Lauf, der
    # allein vom Ansehen der Prüfung entstünde, stünde für jeden zufällig
    # aufgerufenen Monat in der Datenbank.
    if protokoll.erzeugt:
        await lb.lauf_oeffnen(
            db, jahr=jahr, monat=monat, stichtag=bis, user_id=user.id
        )

    await log_audit(
        db, user, action="bexio_entwuerfe_erzeugt", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={
            "erzeugt": protokoll.erzeugt,
            "gescheitert": protokoll.gescheitert,
            "stichtag": bis.isoformat(),
            "rechnungen": [e.nummer for e in protokoll.eintraege if e.nummer],
        },
    )
    await _abschluss(db, trocken=trocken)

    return ErzeugenOut(
        periode=f"{jahr:04d}-{monat:02d}",
        stichtag=bis.isoformat(),
        erzeugt=protokoll.erzeugt,
        gescheitert=protokoll.gescheitert,
        trocken=trocken,
        vermerke=bexio.protokoll() if trocken else [],
        protokoll=[
            ErzeugtOut(
                auftrag=e.auftrag, titel=e.titel, rechnung_id=e.rechnung_id,
                nummer=e.nummer, datum=e.datum, faellig=e.faellig,
                hindernis=e.hindernis,
            )
            for e in protokoll.eintraege
        ],
        uebergangen=sorted(set(gewuenscht or []) - behandelt),
    )


class AnwendenIn(BaseModel):
    month: str | None = None
    rechnungen: list[str] | None = None
    """Auf welche Rechnungen angewendet wird. ``None`` heisst: alle mit
    offenen Anpassungen. Mehr kommt vom Browser nicht — die Werte werden hier
    neu gerechnet."""
    echt: bool = False
    """Ob wirklich geschrieben wird — siehe ``_ECHT``."""


class AngewendetOut(BaseModel):
    rechnung: str
    position_id: int
    handlung: str
    alt: str
    neu: str
    erfolg: bool
    meldung: str = ""


class ZurueckstellenIn(BaseModel):
    month: str | None = None
    rechnung_id: int
    grund: str | None = None
    """Pflicht beim Zurückstellen. Beim Zurückholen bleibt der alte Grund
    stehen — er gehört zur Geschichte der Rechnung."""


@router.post("/pruefung/zuruecklegen", response_model=VermerkOut)
async def zuruecklegen(
    eingabe: ZurueckstellenIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Eine Rechnung aus dem Lauf nehmen — die einzige vorgesehene Ausnahme.

    Ein leerer oder fehlender Grund wird abgewiesen. Das ist keine Formstrenge:
    in drei Wochen sieht eine bewusste Ausnahme ohne Begründung genauso aus wie
    ein Versehen, und dann ist der Lauf nicht mehr abschliessbar, weil niemand
    weiss, ob noch etwas offen ist.
    """
    jahr, monat = _periode(eingabe.month)
    _von, bis = periodengrenzen(jahr, monat)

    if not (eingabe.grund or "").strip():
        raise HTTPException(400, "Zurückstellen verlangt eine Begründung")

    lauf = await lb.lauf_oeffnen(db, jahr=jahr, monat=monat, stichtag=bis, user_id=user.id)
    zeile = await lb.zuruecklegen(
        db, lauf, rechnung_id=eingabe.rechnung_id, grund=eingabe.grund or ""
    )
    await log_audit(
        db, user, action="debitorenlauf_zurueckgestellt", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={"rechnung_id": eingabe.rechnung_id, "grund": zeile.grund},
    )
    await db.commit()
    return VermerkOut(
        zurueckgestellt=zeile.zurueckgestellt, grund=zeile.grund,
        dokumente_erzeugt_am=zeile.dokumente_erzeugt_am,
        mailentwurf_id=zeile.mailentwurf_id,
        versendet_am=zeile.versendet_am, abgelegt_am=zeile.abgelegt_am,
    )


@router.post("/pruefung/zurueckholen", response_model=VermerkOut)
async def zurueckholen(
    eingabe: ZurueckstellenIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Eine zurückgestellte Rechnung wieder in den Lauf nehmen."""
    jahr, monat = _periode(eingabe.month)
    lauf = await lb.lauf_holen(db, jahr=jahr, monat=monat)
    zeile = (
        await lb.zurueckholen(db, lauf, rechnung_id=eingabe.rechnung_id)
        if lauf is not None else None
    )
    if zeile is None:
        raise HTTPException(404, "Zu dieser Rechnung ist im Lauf nichts vermerkt")

    await log_audit(
        db, user, action="debitorenlauf_zurueckgeholt", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={"rechnung_id": eingabe.rechnung_id},
    )
    await db.commit()
    return VermerkOut(
        zurueckgestellt=zeile.zurueckgestellt, grund=zeile.grund,
        dokumente_erzeugt_am=zeile.dokumente_erzeugt_am,
        mailentwurf_id=zeile.mailentwurf_id,
        versendet_am=zeile.versendet_am, abgelegt_am=zeile.abgelegt_am,
    )


class AnwendenOut(BaseModel):
    periode: str
    geschrieben: int
    fehlgeschlagen: int
    protokoll: list[AngewendetOut] = []
    uebergangen: list[str] = []
    """Angeforderte Rechnungen, für die es nichts anzuwenden gab."""
    zurueckgestellt: list[str] = []
    """Bewusst übersprungen — sie ruhen im Lauf. Getrennt ausgewiesen, damit
    «nichts geschrieben» nicht wie ein Fehlschlag aussieht."""
    trocken: bool = True
    vermerke: list[str] = []


@router.post("/pruefung/anwenden", response_model=AnwendenOut)
async def anwenden(
    eingabe: AnwendenIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die berechneten Anpassungen in die Bexio-Entwürfe schreiben.

    Der einzige schreibende Weg dieses Prozesses. Drei Eigenschaften tragen ihn:

    **Neu gerechnet, nicht übernommen.** Der Browser schickt nur, *welche*
    Rechnungen gemeint sind. Jede Menge und jeder Text entsteht hier aus den
    Stunden in Toggl und den Positionen in Bexio — wer Werte entgegennähme,
    hätte eine Schnittstelle, über die sich jede Zahl auf jede Rechnung
    schreiben lässt.

    **Nur Entwürfe.** ``abrufen`` liest ausschliesslich Entwürfe; eine bereits
    ausgestellte Rechnung ist damit gar nicht im Bestand, über den hier
    geschrieben wird.

    **Jede Zeile einzeln protokolliert.** Scheitert eine, laufen die übrigen
    weiter und die gescheiterte steht im Protokoll. Ein Abbruch in der Mitte
    hinterliesse eine halb angepasste Rechnung, ohne dass jemand wüsste, welche
    Hälfte.
    """
    jahr, monat = _periode(eingabe.month)
    auswertung, _stand, _befund, bexio, _roh = await _pruefstand(user, jahr, monat)

    trocken = not eingabe.echt
    if trocken:
        bexio = BexioSperre(bexio)

    # Die Rechnungsnummer allein genügt nicht — geschrieben wird über die
    # Bexio-Kennung. Sie steht in den Rohdaten, nicht im Vorschlag.
    kennungen = {
        (e.get("nummer") or ""): e.get("rechnung_id")
        for e in _roh.entwuerfe
    }

    # Eine zurückgestellte Rechnung bleibt unberührt, auch wenn der Browser sie
    # nennt. Das ist die Bedeutung von «zurückgestellt»: nicht «später dran»,
    # sondern «vorerst nicht anfassen». Ohne diese Sperre nähme der
    # Sammelknopf sie beim nächsten Druck wieder mit.
    ruht = {
        r_id for r_id, v in lb.als_karte(
            await lb.lauf_holen(db, jahr=jahr, monat=monat)
        ).items() if v["zurueckgestellt"]
    }

    gewuenscht = eingabe.rechnungen
    protokoll: list[AngewendetOut] = []
    behandelt: set[str] = set()
    uebersprungen: list[str] = []

    for vorschlag in auswertung.vorschlaege:
        if gewuenscht is not None and vorschlag.rechnung not in gewuenscht:
            continue
        if not vorschlag.aenderungen:
            continue
        kennung = kennungen.get(vorschlag.rechnung)
        if kennung is None:
            continue
        if kennung in ruht:
            uebersprungen.append(vorschlag.rechnung)
            continue
        behandelt.add(vorschlag.rechnung)

        for aenderung in vorschlag.aenderungen:
            erfolg, meldung = True, ""
            try:
                if aenderung.handlung == "entfernen":
                    await bexio.delete_invoice_position(
                        int(kennung), aenderung.position_id, aenderung.positionsart
                    )
                else:
                    await bexio.update_invoice_position(
                        int(kennung), aenderung.position_id,
                        aenderung.positionsart, aenderung.nutzdaten,
                    )
            except Exception as exc:  # noqa: BLE001 -- eine Zeile darf den Lauf nicht reissen
                erfolg = False
                meldung = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Anwenden fehlgeschlagen: %s Position %s: %s",
                    vorschlag.rechnung, aenderung.position_id, exc,
                )
            protokoll.append(AngewendetOut(
                rechnung=vorschlag.rechnung, position_id=aenderung.position_id,
                handlung=aenderung.handlung, alt=aenderung.alt, neu=aenderung.neu,
                erfolg=erfolg, meldung=meldung,
            ))

    geschrieben = sum(1 for p in protokoll if p.erfolg)
    await log_audit(
        db, user, action="bexio_positionen_angepasst", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={
            "geschrieben": geschrieben,
            "fehlgeschlagen": len(protokoll) - geschrieben,
            "rechnungen": sorted(behandelt),
            "zurueckgestellt": sorted(uebersprungen),
        },
    )
    await _abschluss(db, trocken=trocken)

    return AnwendenOut(
        periode=f"{jahr:04d}-{monat:02d}",
        geschrieben=geschrieben,
        fehlgeschlagen=len(protokoll) - geschrieben,
        protokoll=protokoll,
        trocken=trocken,
        vermerke=bexio.protokoll() if trocken else [],
        # Die ruhenden hier abziehen: «übergangen» heisst «es gab nichts zu
        # tun». Bei einer zurückgestellten Rechnung gab es etwas, und es wurde
        # bewusst nicht getan. In beiden Listen zu stehen macht aus einer
        # Entscheidung einen Zweifelsfall.
        uebergangen=sorted(set(gewuenscht or []) - behandelt - set(uebersprungen)),
        zurueckgestellt=sorted(uebersprungen),
    )


class MailIn(BaseModel):
    month: str | None = None
    rechnungen: list[str] | None = None
    """Auf welche Rechnungsnummern Entwürfe entstehen. ``None`` heisst: alle
    nicht zurückgestellten Entwürfe ohne bestehenden Mailentwurf."""
    echt: bool = False
    """Ob wirklich geschrieben wird — siehe ``_ECHT``."""


class MailZeileOut(BaseModel):
    rechnung: str
    rechnung_id: int
    bezeichnung: str
    mailentwurf_id: str = ""
    seiten: int = 0
    mit_rapport: bool = False
    hindernisse: list[str] = []
    hindernis: str = ""


class MailOut(BaseModel):
    periode: str
    angelegt: int
    uebergangen: int
    fehlgeschlagen: int
    protokoll: list[MailZeileOut] = []
    zurueckgestellt: list[str] = []
    trocken: bool = True
    vermerke: list[str] = []


@router.get("/pruefung/dokument/{rechnung_id}")
async def dokument_holen(
    rechnung_id: int,
    month: str | None = Query(None),
    user: User = Depends(require_role("owner")),
):
    """Das versandfertige PDF einer Rechnung — neu gebaut, nicht aus einem
    Zwischenspeicher.

    Vorschau, kein Vermerk. Wer hier etwas sieht, sieht denselben Stand, den
    der Mailentwurf eine Minute später anhängt: Bexio und Toggl, jetzt.
    """
    from app.services.debitoren_versand import dokumente_bauen
    from app.services.debitorenvertraege import laden

    jahr, monat = _periode(month)
    _auswertung, _stand, _befund, bexio, roh = await _pruefstand(user, jahr, monat)
    bestand, _ = laden()
    ergebnis = await dokumente_bauen(
        bexio, entwuerfe=roh.entwuerfe, buchungen=roh.buchungen,
        bestand=bestand, jahr=jahr, monat=monat, nur={rechnung_id},
    )
    if ergebnis.fehler:
        raise HTTPException(502, ergebnis.fehler[0])
    if ergebnis.uebergangen:
        raise HTTPException(404, ergebnis.uebergangen[0].get("grund") or "kein Dokument")
    if not ergebnis.beilagen:
        raise HTTPException(404, "Zu dieser Rechnung gibt es keinen Entwurf")
    beilage = ergebnis.beilagen[0]
    return Response(
        content=beilage.dokument,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'inline; filename="{beilage.nummer} {beilage.bezeichnung}.pdf"'
            ),
        },
    )


@router.post("/pruefung/mailentwuerfe", response_model=MailOut)
async def mailentwuerfe_anlegen(
    eingabe: MailIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Dokumente bauen und als Mailentwurf ins Postfach legen.

    **Nur Entwürfe, nie Versand.** Die Mail bleibt im Ordner Entwürfe; senden
    tut ein Mensch. Ein zweiter Aufruf legt keinen zweiten Entwurf an — das
    Laufbuch hält die Kennung, und ohne sie entstünde bei jedem Druck eine
    weitere Mail im Postfach.

    Zurückgestellte Rechnungen bleiben unberührt, auch wenn der Browser sie
    nennt.
    """
    from app.services.debitoren_versand import dokumente_bauen, mails_anlegen
    from app.services.debitorenvertraege import laden
    from app.services.graph import get_graph_client

    jahr, monat = _periode(eingabe.month)
    _auswertung, _stand, _befund, bexio, roh = await _pruefstand(user, jahr, monat)
    bestand, _ = laden()
    _von, bis = periodengrenzen(jahr, monat)

    graph = get_graph_client()
    if graph is None:
        raise HTTPException(400, "Graph ist nicht konfiguriert — ohne Postfach kein Entwurf")

    trocken = not eingabe.echt
    if trocken:
        graph = GraphSperre(graph)

    lauf = await lb.lauf_oeffnen(db, jahr=jahr, monat=monat, stichtag=bis, user_id=user.id)
    karte = lb.als_karte(lauf)

    gewuenscht = eingabe.rechnungen
    ruht: list[str] = []
    schon_da: list[str] = []
    auswahl: set[int] = set()

    for entwurf in roh.entwuerfe:
        nummer = entwurf.get("nummer") or ""
        kennung = entwurf.get("rechnung_id")
        if kennung is None:
            continue
        if gewuenscht is not None and nummer not in gewuenscht:
            continue
        vermerk = karte.get(int(kennung), {})
        if vermerk.get("zurueckgestellt"):
            ruht.append(nummer)
            continue
        if vermerk.get("mailentwurf_id"):
            schon_da.append(nummer)
            continue
        auswahl.add(int(kennung))

    # ``nur`` ist immer die Auswahl, auch wenn sie leer ist. ``None`` hiesse
    # «alle Entwürfe» und würde genau die Rechnungen bauen, die oben bewusst
    # ausgenommen wurden — zurückgestellte und solche mit bestehendem Entwurf.
    gebaut = await dokumente_bauen(
        bexio, entwuerfe=roh.entwuerfe, buchungen=roh.buchungen,
        bestand=bestand, jahr=jahr, monat=monat, nur=auswahl,
    )

    mails = await mails_anlegen(
        graph, gebaut.beilagen, bestand, jahr=jahr, monat=monat
    )

    protokoll: list[MailZeileOut] = []
    beilage_nach_id = {b.rechnung_id: b for b in gebaut.beilagen}

    for vermerk in mails.angelegt:
        beilage = beilage_nach_id.get(vermerk.rechnung_id)
        await lb.schritt_vermerken(
            db, lauf, rechnung_id=vermerk.rechnung_id,
            schritt="dokumente_erzeugt_am", nummer=vermerk.nummer,
        )
        await lb.schritt_vermerken(
            db, lauf, rechnung_id=vermerk.rechnung_id,
            schritt="mailentwurf_id", wert=vermerk.mailentwurf_id,
            nummer=vermerk.nummer,
        )
        protokoll.append(MailZeileOut(
            rechnung=vermerk.nummer, rechnung_id=vermerk.rechnung_id,
            bezeichnung=vermerk.bezeichnung, mailentwurf_id=vermerk.mailentwurf_id,
            seiten=beilage.seiten if beilage else 0,
            mit_rapport=beilage.mit_rapport if beilage else False,
            hindernisse=beilage.hindernisse if beilage else [],
        ))

    for vermerk in mails.uebergangen + mails.fehler:
        protokoll.append(MailZeileOut(
            rechnung=vermerk.nummer, rechnung_id=vermerk.rechnung_id,
            bezeichnung=vermerk.bezeichnung, hindernis=vermerk.hindernis,
        ))
    for zeile in gebaut.fehler:
        protokoll.append(MailZeileOut(
            rechnung=zeile.split(":", 1)[0], rechnung_id=0,
            bezeichnung="", hindernis=zeile,
        ))
    for extra in gebaut.uebergangen:
        protokoll.append(MailZeileOut(
            rechnung=extra.get("nummer") or "",
            rechnung_id=int(extra.get("rechnung_id") or 0),
            bezeichnung=extra.get("titel") or "",
            hindernis=extra.get("grund") or "",
        ))

    await log_audit(
        db, user, action="debitorenlauf_mailentwuerfe", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={
            "angelegt": [m.nummer for m in mails.angelegt],
            "schon_vorhanden": schon_da,
            "zurueckgestellt": ruht,
        },
    )
    await _abschluss(db, trocken=trocken)

    return MailOut(
        periode=f"{jahr:04d}-{monat:02d}",
        angelegt=len(mails.angelegt),
        uebergangen=len(schon_da) + len(mails.uebergangen),
        fehlgeschlagen=len(mails.fehler) + len(gebaut.fehler),
        protokoll=protokoll,
        zurueckgestellt=ruht,
        trocken=trocken,
        vermerke=graph.protokoll() if trocken else [],
    )


class VersendetIn(BaseModel):
    month: str | None = None
    rechnung_id: int


class AusstellenIn(BaseModel):
    month: str | None = None
    rechnungen: list[str] | None = None
    """Welche Rechnungsnummern ausgestellt werden. ``None`` heisst: alle, deren
    Versand bestätigt ist und die noch Entwurf sind."""
    echt: bool = False
    """Ob wirklich geschrieben wird — siehe ``_ECHT``."""


class AusgestelltOut(BaseModel):
    rechnung: str
    rechnung_id: int
    ausgestellt: bool
    hindernis: str = ""


class AusstellenOut(BaseModel):
    periode: str
    ausgestellt: int
    uebergangen: int
    fehlgeschlagen: int
    protokoll: list[AusgestelltOut] = []
    trocken: bool = True
    vermerke: list[str] = []


@router.post("/pruefung/versendet", response_model=VermerkOut)
async def versendet_bestaetigen(
    eingabe: VersendetIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Bestätigen, dass die Mail rausgegangen ist — nicht senden.

    Der Versand geschieht im Postfach. Hier wird nur festgehalten, dass er
    geschehen ist. Ohne diesen Vermerk darf die Rechnung in Bexio nicht
    ausgestellt werden: sonst stünde eine Forderung in den Büchern, die die
    Kundschaft nie gesehen hat.
    """
    jahr, monat = _periode(eingabe.month)
    lauf = await lb.lauf_holen(db, jahr=jahr, monat=monat)
    if lauf is None:
        raise HTTPException(404, "Für diesen Monat ist kein Lauf offen")

    karte = lb.als_karte(lauf)
    vermerk = karte.get(eingabe.rechnung_id)
    if vermerk is None or not vermerk.get("mailentwurf_id"):
        raise HTTPException(
            409,
            "Versand bestätigen setzt einen Mailentwurf voraus — "
            "sonst gäbe es nichts, das rausgegangen sein könnte",
        )
    if vermerk.get("zurueckgestellt"):
        raise HTTPException(409, "Eine zurückgestellte Rechnung gilt nicht als versendet")

    zeile, _ = await lb.schritt_vermerken(
        db, lauf, rechnung_id=eingabe.rechnung_id, schritt="versendet_am"
    )
    await log_audit(
        db, user, action="debitorenlauf_versendet", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={"rechnung_id": eingabe.rechnung_id},
    )
    await db.commit()
    return VermerkOut(
        zurueckgestellt=zeile.zurueckgestellt, grund=zeile.grund,
        dokumente_erzeugt_am=zeile.dokumente_erzeugt_am,
        mailentwurf_id=zeile.mailentwurf_id,
        versendet_am=zeile.versendet_am, abgelegt_am=zeile.abgelegt_am,
    )


@router.post("/pruefung/ausstellen", response_model=AusstellenOut)
async def ausstellen(
    eingabe: AusstellenIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Bestätigt versendete Entwürfe in Bexio auf «offen» setzen.

    **Nur nach Versandbestätigung.** Ohne sie wäre die Forderung gebucht, bevor
    die Kundschaft die Rechnung gesehen hat. Bereits Ausgestelltes ist nichts
    zu tun, kein Fehler.

    Bexio ``send`` wird hier **nicht** aufgerufen — das verschickte selbst eine
    E-Mail. Der Versand liegt im Postfach.
    """
    jahr, monat = _periode(eingabe.month)
    _auswertung, _stand, _befund, bexio, roh = await _pruefstand(user, jahr, monat)
    lauf = await lb.lauf_holen(db, jahr=jahr, monat=monat)
    karte = lb.als_karte(lauf)

    trocken = not eingabe.echt
    if trocken:
        bexio = BexioSperre(bexio)

    gewuenscht = eingabe.rechnungen
    protokoll: list[AusgestelltOut] = []

    for entwurf in roh.entwuerfe:
        nummer = entwurf.get("nummer") or ""
        kennung = entwurf.get("rechnung_id")
        if kennung is None:
            continue
        if gewuenscht is not None and nummer not in gewuenscht:
            continue
        vermerk = karte.get(int(kennung), {})
        if vermerk.get("zurueckgestellt"):
            protokoll.append(AusgestelltOut(
                rechnung=nummer, rechnung_id=int(kennung),
                ausgestellt=False, hindernis="zurückgestellt",
            ))
            continue
        if not vermerk.get("versendet_am"):
            protokoll.append(AusgestelltOut(
                rechnung=nummer, rechnung_id=int(kennung),
                ausgestellt=False,
                hindernis="Versand ist nicht bestätigt — ohne das keine Forderung",
            ))
            continue
        try:
            geaendert = await bexio.issue_invoice(int(kennung))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ausstellen von %s gescheitert: %s", nummer, exc)
            protokoll.append(AusgestelltOut(
                rechnung=nummer, rechnung_id=int(kennung),
                ausgestellt=False, hindernis=f"{type(exc).__name__}: {exc}",
            ))
            continue
        protokoll.append(AusgestelltOut(
            rechnung=nummer, rechnung_id=int(kennung),
            ausgestellt=geaendert,
            hindernis="" if geaendert else "stand bereits auf offen",
        ))

    await log_audit(
        db, user, action="debitorenlauf_ausgestellt", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={
            "ausgestellt": [p.rechnung for p in protokoll if p.ausgestellt],
            "uebergangen": [p.rechnung for p in protokoll if not p.ausgestellt],
        },
    )
    await _abschluss(db, trocken=trocken)

    return AusstellenOut(
        periode=f"{jahr:04d}-{monat:02d}",
        trocken=trocken,
        vermerke=bexio.protokoll() if trocken else [],
        ausgestellt=sum(1 for p in protokoll if p.ausgestellt),
        uebergangen=sum(1 for p in protokoll if not p.ausgestellt and "bereits" in p.hindernis),
        fehlgeschlagen=sum(
            1 for p in protokoll
            if not p.ausgestellt and "bereits" not in p.hindernis
        ),
        protokoll=protokoll,
    )


class AblegenIn(BaseModel):
    month: str | None = None
    rechnungen: list[str] | None = None
    """Welche Rechnungsnummern abgelegt werden. ``None`` heisst: alle, deren
    Versand bestätigt ist und die noch nicht abgelegt sind."""
    echt: bool = False
    """Ob wirklich geschrieben wird — siehe ``_ECHT``."""


class AblageDateiOut(BaseModel):
    pfad: str
    bytes: int = 0
    lag_schon: bool = False
    hindernis: str = ""


class AblageZeileOut(BaseModel):
    rechnung: str
    rechnung_id: int
    ordner: str = ""
    dateien: list[AblageDateiOut] = []
    hindernis: str = ""


class AblegenOut(BaseModel):
    periode: str
    abgelegt: int
    uebergangen: int
    fehlgeschlagen: int
    protokoll: list[AblageZeileOut] = []
    trocken: bool = True
    vermerke: list[str] = []


@router.post("/pruefung/ablegen", response_model=AblegenOut)
async def ablegen(
    eingabe: AblegenIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die versendete Rechnung ins Kundenarchiv in OneDrive legen.

    **Nur nach bestätigtem Versand.** Das Archiv ist die Ablage dessen, was die
    Kundschaft hat; etwas dort abzulegen, das noch im Entwurfsordner liegt,
    macht aus dem Archiv eine Absichtserklärung.

    **Nie überschreiben.** Eine schon vorhandene Datei ist kein Fehler, sondern
    der Vermerk «lag schon da». Nach dem Schreiben wird gelesen und die Grösse
    verglichen — ein Upload, der mit 200 antwortet und nichts abgelegt hat,
    fiele sonst erst im nächsten Steuerjahr auf.

    Das Dokument wird **neu gebaut**, nicht zwischengespeichert. Dass es
    dasselbe ist wie das Versendete, garantiert nicht ein Puffer, sondern die
    Quelle: Bexio und Toggl für eine abgeschlossene Periode ändern sich nicht.
    """
    from app.services import debitoren_ablage as ablage
    from app.services.debitoren_versand import dokumente_bauen
    from app.services.debitorenvertraege import laden
    from app.services.graph import get_graph_client

    jahr, monat = _periode(eingabe.month)
    _auswertung, _stand, _befund, bexio, roh = await _pruefstand(user, jahr, monat)
    bestand, _ = laden()

    graph = get_graph_client()
    if graph is None:
        raise HTTPException(400, "Graph ist nicht konfiguriert — ohne OneDrive keine Ablage")

    trocken = not eingabe.echt
    if trocken:
        graph = GraphSperre(graph)

    lauf = await lb.lauf_holen(db, jahr=jahr, monat=monat)
    if lauf is None:
        raise HTTPException(404, "Für diesen Monat ist kein Lauf offen")
    karte = lb.als_karte(lauf)

    gewuenscht = eingabe.rechnungen
    protokoll: list[AblageZeileOut] = []
    auswahl: set[int] = set()

    for entwurf in roh.entwuerfe:
        nummer = entwurf.get("nummer") or ""
        kennung = entwurf.get("rechnung_id")
        if kennung is None:
            continue
        if gewuenscht is not None and nummer not in gewuenscht:
            continue
        vermerk = karte.get(int(kennung), {})
        if vermerk.get("zurueckgestellt"):
            protokoll.append(AblageZeileOut(
                rechnung=nummer, rechnung_id=int(kennung),
                hindernis="zurückgestellt",
            ))
            continue
        if not vermerk.get("versendet_am"):
            protokoll.append(AblageZeileOut(
                rechnung=nummer, rechnung_id=int(kennung),
                hindernis=(
                    "Versand ist nicht bestätigt — das Archiv hält fest, was "
                    "die Kundschaft hat"
                ),
            ))
            continue
        if vermerk.get("abgelegt_am"):
            protokoll.append(AblageZeileOut(
                rechnung=nummer, rechnung_id=int(kennung),
                hindernis="war schon abgelegt",
            ))
            continue
        auswahl.add(int(kennung))

    # Wie beim Mailentwurf: ``nur`` ist immer die Auswahl, auch leer. ``None``
    # hiesse «alle» und baute genau die, die oben ausgenommen wurden.
    gebaut = await dokumente_bauen(
        bexio, entwuerfe=roh.entwuerfe, buchungen=roh.buchungen,
        bestand=bestand, jahr=jahr, monat=monat, nur=auswahl,
    )

    ergebnisse = await ablage.ablegen(
        graph, gebaut.beilagen, bestand, jahr=jahr, monat=monat
    )

    for e in ergebnisse:
        if e.gelungen:
            await lb.schritt_vermerken(
                db, lauf, rechnung_id=e.rechnung_id,
                schritt="abgelegt_am", nummer=e.nummer,
            )
        protokoll.append(AblageZeileOut(
            rechnung=e.nummer, rechnung_id=e.rechnung_id, ordner=e.ordner,
            dateien=[
                AblageDateiOut(
                    pfad=d.pfad, bytes=d.bytes_abgelegt,
                    lag_schon=d.lag_schon, hindernis=d.hindernis,
                )
                for d in e.dateien
            ],
            hindernis=e.hindernis,
        ))

    for zeile in gebaut.fehler:
        protokoll.append(AblageZeileOut(
            rechnung=zeile.split(":", 1)[0], rechnung_id=0, hindernis=zeile,
        ))

    await log_audit(
        db, user, action="debitorenlauf_abgelegt", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={
            "abgelegt": [
                {"rechnung": e.nummer, "ordner": e.ordner}
                for e in ergebnisse if e.gelungen
            ],
            "gescheitert": [
                {"rechnung": e.nummer, "hindernis": e.hindernis or "Datei"}
                for e in ergebnisse if not e.gelungen
            ],
        },
    )
    await _abschluss(db, trocken=trocken)

    return AblegenOut(
        periode=f"{jahr:04d}-{monat:02d}",
        trocken=trocken,
        vermerke=graph.protokoll() if trocken else [],
        abgelegt=sum(1 for e in ergebnisse if e.gelungen),
        uebergangen=sum(1 for p in protokoll if "schon" in p.hindernis)
        + sum(1 for p in protokoll if p.hindernis == "zurückgestellt"),
        fehlgeschlagen=sum(1 for e in ergebnisse if not e.gelungen) + len(gebaut.fehler),
        protokoll=protokoll,
    )


class VerrechnungsartIn(BaseModel):
    month: str | None = None
    rechnungen: list[str] | None = None
    """Rechnungsnummern; ohne Angabe alle bestätigt versendeten des Monats."""
    intern: bool = False
    """Zusätzlich die eigenen Projekte auf «keine Verrechnung» setzen.

    Erst zulässig, wenn im Monat keine Rechnung mehr offen steht.
    """
    echt: bool = False


class VerrechnungszeileOut(BaseModel):
    eintrag_id: int
    datum: str = ""
    projekt: str = ""
    stunden: float = 0.0
    art: str = ""
    grund: str = ""
    vorhanden: list[str] = []
    gesetzt: bool = False
    hindernis: str = ""


class VerrechnungsartOut(BaseModel):
    periode: str
    gesetzt: int = 0
    schon_richtig: int = 0
    fehlgeschlagen: int = 0
    stunden_gesetzt: float = 0.0
    protokoll: list[VerrechnungszeileOut] = []
    fremd: list[VerrechnungszeileOut] = []
    ohne_zuordnung: list[VerrechnungszeileOut] = []
    uebergangen: list[str] = []
    hindernis: str = ""
    trocken: bool = True
    vermerke: list[str] = []


def _verrechnungszeile(vorschlag, *, gesetzt: bool = False, hindernis: str = ""):
    return VerrechnungszeileOut(
        eintrag_id=vorschlag.eintrag_id,
        datum=vorschlag.datum,
        projekt=vorschlag.projekt,
        stunden=vorschlag.stunden,
        art=vorschlag.soll,
        grund=vorschlag.grund,
        vorhanden=list(vorschlag.vorhanden),
        gesetzt=gesetzt,
        hindernis=hindernis,
    )


@router.post("/pruefung/verrechnungsart", response_model=VerrechnungsartOut)
async def verrechnungsart_setzen(
    eingabe: VerrechnungsartIn,
    request: Request,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die Verrechnungsart in Toggl nachtragen — der letzte Schritt.

    **Nur nach bestätigtem Versand**, und das ist hier keine Ordnungsfrage:
    Toggl ist im Lauf die Beweisgrundlage. Geht etwas schief, ist es die
    Instanz, die sagt, was geleistet wurde. Wer vorher taggt, hat sie
    angefasst, bevor er sie gebraucht hat.

    Die eigenen Projekte (``intern``) hängen an keiner Rechnung und kommen
    darum zuletzt — erst wenn im Monat keine Rechnung mehr offen steht, ist
    «keine Verrechnung» eine Tatsache und nicht eine Vermutung.

    **Ein vorhandener, anderer Tag wird nie überschrieben.** Er ist eine
    Menschenentscheidung und wird unter ``fremd`` gemeldet, mit beiden Werten.
    """
    from app.services import debitoren_verrechnungsart as va
    from app.services.debitorenvertraege import laden
    from app.services.fachsysteme import toggl_zugang

    jahr, monat = _periode(eingabe.month)
    bestand, _ = laden()
    _auswertung, _stand, _befund, _bexio, roh = await _pruefstand(user, jahr, monat)

    toggl, workspace = toggl_zugang(user)
    tags = await toggl.list_tags(workspace)

    trocken = not eingabe.echt
    if trocken:
        toggl = TogglSperre(toggl, tags=tags)

    lauf = await lb.lauf_holen(db, jahr=jahr, monat=monat)
    if lauf is None:
        raise HTTPException(404, "Für diesen Monat ist kein Lauf offen")
    karte = lb.als_karte(lauf)

    # ── Welche Verträge sind fällig ──
    gewuenscht = eingabe.rechnungen
    vertraege: set[str] = set()
    uebergangen: list[str] = []

    for entwurf in roh.entwuerfe:
        nummer = entwurf.get("nummer") or ""
        kennung = entwurf.get("rechnung_id")
        if kennung is None:
            continue
        if gewuenscht is not None and nummer not in gewuenscht:
            continue
        vertrag = bestand.finden(entwurf.get("titel") or "")
        if vertrag is None:
            uebergangen.append(f"{nummer}: kein Vertrag zuzuordnen")
            continue
        vermerk = karte.get(int(kennung), {})
        if vermerk.get("zurueckgestellt"):
            uebergangen.append(f"{nummer}: zurückgestellt")
            continue
        if not vermerk.get("versendet_am"):
            uebergangen.append(
                f"{nummer}: Versand ist nicht bestätigt — Toggl bleibt bis "
                "dahin unangetastet"
            )
            continue
        vertraege.add(vertrag.schluessel)

    # ── Die eigenen Projekte nur, wenn der Monat durch ist ──
    hindernis = ""
    intern = eingabe.intern
    if intern:
        offen = [
            r.nummer or str(r.rechnung_id)
            for r in lauf.rechnungen
            if not r.zurueckgestellt and r.versendet_am is None
        ]
        if offen:
            intern = False
            hindernis = (
                "Die eigenen Projekte bleiben ungetaggt, solange der Monat "
                f"offene Rechnungen hat: {', '.join(sorted(offen))}"
            )

    plan = va.planen(roh.buchungen, bestand, vertraege=vertraege, intern=intern)
    ergebnisse = await va.setzen(toggl, workspace, plan, tags=tags)

    # Ein Eintrag ohne Ergebnis ist nicht gescheitert, sondern **nicht
    # versucht**: ``setzen`` hält an, sobald der Schreibweg mehr verändert als
    # das Tag. Beides gleich zu benennen verwischte genau diesen Abbruch.
    nach_kennung = {e.eintrag_id: e for e in ergebnisse}
    protokoll: list[VerrechnungszeileOut] = []
    for v in plan.zu_setzen:
        ergebnis = nach_kennung.get(v.eintrag_id)
        protokoll.append(_verrechnungszeile(
            v,
            gesetzt=bool(ergebnis and ergebnis.gelungen),
            hindernis=ergebnis.hindernis if ergebnis else "nicht versucht",
        ))

    gelungen = [e for e in ergebnisse if e.gelungen]
    await log_audit(
        db, user, action="debitorenlauf_verrechnungsart", resource="debitorenlauf",
        resource_id=f"{jahr:04d}-{monat:02d}", request=request,
        details={
            "vertraege": sorted(vertraege),
            "intern": intern,
            "gesetzt": len(gelungen),
            "arten": sorted({e.art for e in gelungen}),
            "gescheitert": [
                {"eintrag": e.eintrag_id, "hindernis": e.hindernis}
                for e in ergebnisse if not e.gelungen
            ],
        },
    )
    await _abschluss(db, trocken=trocken)

    return VerrechnungsartOut(
        periode=f"{jahr:04d}-{monat:02d}",
        trocken=trocken,
        vermerke=toggl.protokoll() if trocken else [],
        gesetzt=len(gelungen),
        schon_richtig=len(plan.schon_richtig),
        fehlgeschlagen=sum(1 for e in ergebnisse if not e.gelungen),
        stunden_gesetzt=round(
            sum(
                v.stunden for v in plan.zu_setzen
                if v.eintrag_id in nach_kennung and nach_kennung[v.eintrag_id].gelungen
            ),
            2,
        ),
        protokoll=protokoll,
        fremd=[_verrechnungszeile(v) for v in plan.fremd],
        ohne_zuordnung=[_verrechnungszeile(v) for v in plan.ohne_zuordnung],
        uebergangen=uebergangen,
        hindernis=hindernis,
    )


def pr_regeln() -> tuple[str, ...]:
    from app.services.debitoren_pruefung import REGELN

    return REGELN


def _stammdatenmaengel(befund: dict) -> list[str]:
    """Was an der Vertragsdatei fehlt, in einer Sprache für die Ansicht.

    Die offenen Fragen stehen **im Wortlaut** dabei und nicht als Zähler: eine
    Frage, die niemand liest, ist so gut wie keine.
    """
    zeilen: list[str] = []
    zeilen += list(befund.get("verworfen") or [])
    zeilen += list(befund.get("bemaengelt") or [])
    ohne = befund.get("ohne_uebertragsangabe") or []
    if ohne:
        zeilen.append(
            f"{len(ohne)} Vertrag/Verträge nennen Fixstunden ohne Angabe zur "
            f"Übertragbarkeit und gelten deshalb als variabel: {', '.join(ohne[:8])}"
        )
    for frage in befund.get("offene_fragen") or []:
        zeilen.append(f"Offen — {frage.get('gegenstand')}: {frage.get('frage')}")
    return zeilen


@router.post("/cache/clear")
async def clear_cache(user: User = Depends(require_role("owner"))):
    _cache.clear()
    _toggl_month_cache.clear()
    logger.info("Debtors-Cache manuell geleert")
    return {"status": "ok", "message": "Debtors-Cache geleert"}
