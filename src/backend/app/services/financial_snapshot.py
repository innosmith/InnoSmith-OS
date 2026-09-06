"""Financial-Snapshot-Builder für LLM-Finanzanalysen.

Aggregiert die vorhandenen Finanz-Endpunkte (Bexio, Toggl, InvoiceInsight) zu
einem sektionierten Snapshot. Liefert sowohl ein strukturiertes Dict (für das
Frontend / Debugging) als auch eine kompakte Markdown-Repräsentation, die als
Kontext an das LLM übergeben wird.

Token-bewusst: pro Analyse-Typ werden nur die relevanten Sektionen gebaut.
"""

import logging
from datetime import date
from typing import Any, Awaitable, Callable

from app.models import User

logger = logging.getLogger("taskpilot.financial_snapshot")


# ── Sektions-Katalog ─────────────────────────────────────
# title: menschenlesbar; builder: async fn(user) -> dict (Rohdaten)
SECTION_TITLES: dict[str, str] = {
    "overview": "KPI-Übersicht",
    "balance_sheet": "Saldenbilanz & Bilanzkennzahlen",
    "cashflow_forecast": "Liquiditätsvorschau & geplante Abflüsse",
    "expenses_by_category": "Aufwand nach Kategorie (12M)",
    "expenses_by_account": "Aufwand nach Einzelkonto (12M)",
    "yoy": "Jahresvergleich (YoY)",
    "debtors": "Debitoren / Kunden",
    "creditors": "Kreditoren (InvoiceInsight)",
}


def _chf(value: Any) -> str:
    """Formatiert eine Zahl als CHF mit Tausender-Trennzeichen."""
    try:
        n = float(value)
    except (ValueError, TypeError):
        return str(value)
    return f"CHF {n:,.0f}".replace(",", "'")


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}%"
    except (ValueError, TypeError):
        return str(value)


# ── Sektions-Builder ─────────────────────────────────────

async def _build_overview(user: User) -> dict:
    from app.routers import finance

    ov = await finance.get_overview(user=user)
    return ov.model_dump()


async def _build_balance_sheet(user: User) -> dict:
    from app.routers import finance

    bs = await finance.get_balance_sheet(user=user)
    return bs.model_dump()


async def _build_expenses_by_category(user: User) -> dict:
    from app.routers import finance

    ec = await finance.get_expense_categories(user=user)
    return ec.model_dump()


async def _build_expenses_by_account(user: User) -> dict:
    from app.routers import finance

    ea = await finance.get_expenses_by_account(months=12, user=user)
    return ea.model_dump()


async def _build_yoy(user: User) -> dict:
    from app.routers import finance

    yoy = await finance.get_year_over_year(user=user)
    return yoy.model_dump()


async def _build_cashflow(user: User) -> dict:
    from app.routers import finance

    # months_back=12, damit der Renderer die Zusammensetzung der echten
    # Finanzierungsabflüsse der letzten 12 Monate aggregieren kann.
    cf = await finance.get_cashflow(months_back=12, months_forward=12, user=user)
    return cf.model_dump()


async def _build_debtors(user: User) -> dict:
    from app.routers import debtors

    deb = await debtors.get_debtors(user=user)
    data = deb.model_dump()
    # Toggl-Tagesreihe ist für die Analyse zu granular -> entfernen
    data.get("toggl_month", {}).pop("daily_hours", None)
    return data


async def _build_creditors(user: User) -> dict:
    """InvoiceInsight: KPIs, Kostenverteilung, wiederkehrend vs. einmalig, Anomalien."""
    from app.routers import creditors

    client = creditors._get_client(user)

    async def _safe(coro, fallback):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            logger.warning("InvoiceInsight-Sektion fehlgeschlagen: %s", e)
            return fallback

    year = date.today().year
    kpis = await _safe(client.get_kpis(year_from=year - 1, year_to=year), {})
    cost_dist = await _safe(client.get_cost_distribution(year_from=year - 1, year_to=year), {})
    recurring = await _safe(client.get_recurring_vs_onetime(year_from=year - 1, year_to=year), {})
    vendors = await _safe(client.get_vendor_overview(), {})
    return {
        "kpis": kpis,
        "cost_distribution": cost_dist,
        "recurring_vs_onetime": recurring,
        "vendor_overview": vendors,
    }


_BUILDERS: dict[str, Callable[[User], Awaitable[dict]]] = {
    "overview": _build_overview,
    "balance_sheet": _build_balance_sheet,
    "cashflow_forecast": _build_cashflow,
    "expenses_by_category": _build_expenses_by_category,
    "expenses_by_account": _build_expenses_by_account,
    "yoy": _build_yoy,
    "debtors": _build_debtors,
    "creditors": _build_creditors,
}


# ── Markdown-Renderer pro Sektion ────────────────────────

def _vat_method_label(d: dict) -> str:
    method = (d.get("vat_method") or "saldo").lower()
    if method == "saldo":
        rate = d.get("vat_saldo_rate")
        rate_str = f" {float(rate) * 100:.1f}%" if rate is not None else ""
        return f"Saldosteuersatz{rate_str}"
    if method == "effektiv":
        rate = d.get("vat_rate")
        rate_str = f" {float(rate) * 100:.1f}%" if rate is not None else ""
        return f"effektive Methode{rate_str}"
    return "ohne MWST-Abzug"


def _md_overview(d: dict) -> str:
    vat_label = _vat_method_label(d)
    as_of = d.get("as_of_date") or ""
    as_of_str = f" per {as_of}" if as_of else ""
    closed_label = d.get("closed_until_label") or ""
    closed_str = f" per Ende {closed_label}" if closed_label else " (abgeschlossene Monate)"
    lines = [
        f"- Banksaldo: {_chf(d.get('bank_balance'))} ({d.get('bank_account_name') or 'Hauptkonto'})",
        f"- Offene Debitoren: {_chf(d.get('open_invoices_total'))} ({d.get('open_invoices_count', 0)} Rechnungen)",
        f"- Umsatz YTD abgeschlossen{closed_str} (brutto / netto, Methode: {vat_label}; "
        f"faire Vergleichsbasis): {_chf(d.get('revenue_ytd_closed'))} / "
        f"{_chf(d.get('revenue_ytd_net_closed'))}",
        f"- Umsatz YTD live{as_of_str} (inkl. geschätztem laufendem Monat, brutto / netto): "
        f"{_chf(d.get('revenue_ytd_live'))} / {_chf(d.get('revenue_ytd_net'))}",
        f"- Aufwand YTD{closed_str}: {_chf(d.get('expenses_ytd_closed'))}",
        f"- Gewinnmarge YTD{closed_str} (auf Nettoumsatz): {_pct(d.get('profit_margin_ytd'))} "
        f"(Vorjahr stichtagsgleich: {_pct(d.get('profit_margin_ytd_prior'))})",
        f"- Gewinn YTD{closed_str} (netto, vor Steuern): {_chf(d.get('profit_ytd'))}",
        f"- EBITDA YTD{closed_str} (Basis Nettoumsatz): {_chf(d.get('ebitda_ytd'))}",
        f"- Personalaufwand YTD{closed_str}: {_chf(d.get('personnel_cost_ytd'))} "
        f"(Jahres-Hochrechnung: {_chf(d.get('personnel_cost_annualized'))}) "
        f"— bei Einzelinhaber faktisch der Inhaberlohn inkl. Sozialabgaben",
        f"- Personalquote YTD: {_pct(d.get('personalquote_ytd'))}",
        f"- DSO (Tage bis Zahlung): {d.get('dso_days') if d.get('dso_days') is not None else 'n/a'}",
        f"- Burn Rate (Monat): {_chf(d.get('burn_rate'))}",
        f"- Runway: {d.get('runway_months') if d.get('runway_months') is not None else 'n/a'} Monate "
        f"(inkl. Debitoren: {d.get('runway_months_incl_debtors') if d.get('runway_months_incl_debtors') is not None else 'n/a'})",
        f"- Prognose Jahresumsatz (gesichert): {_chf(d.get('forecast_year_revenue'))}",
        f"- Prognose Jahresumsatz (inkl. Run-Rate-Auffüllung): {_chf(d.get('forecast_year_revenue_runrate'))}",
        f"- Prognose Liquidität Jahresende (nur gesicherte Pipeline): {_chf(d.get('forecast_year_end_cashflow'))}",
        f"- Prognose Liquidität Jahresende (Run-Rate gehalten, realistisches Szenario): {_chf(d.get('forecast_year_end_runrate'))}",
        f"- Jahresumsatzziel: {_chf(d.get('annual_revenue_goal'))} (Lücke: {_chf(d.get('revenue_gap_to_goal'))})",
        f"- Liquiditätsgrad 2: {_pct(d.get('liquiditaet_2'))} | EK-Quote: {_pct(d.get('ek_quote'))}",
        "",
        f"_Lesehilfe:_ Es gibt zwei YTD-Sichten. (1) „abgeschlossen{closed_str}“ = nur "
        "vollständig abgeschlossene Monate -- dies ist die FAIRE Basis für alle Margen, "
        "Quoten und Vorjahresvergleiche (Vorjahr ist stichtagsgleich auf dieselben Monate "
        "gerechnet). (2) „live“ = abgeschlossene Monate plus geschätzter laufender Monat "
        "(früh aus Kapazitätsplanung, im Verlauf aus Toggl-Ist) -- aktuellste Sicht, aber "
        "der laufende Monat ist eine Schätzung. Da Rechnungen oft erst gegen Monatsende "
        "gestellt werden, ist der gebuchte laufende Monat sonst untervertreten; schliesse "
        "daher NIE aus dem rohen laufenden Monat auf einen Umsatzrückgang. Für die "
        "Umsatzentwicklung gilt die Sektion „Jahresvergleich (YoY)“ bzw. die abgeschlossene "
        "Basis. "
        f"Umsatz brutto = fakturiert inkl. MWST; netto = nach "
        f"MWST-Methode ({vat_label}). Bei der Saldosteuersatz-Methode wird der "
        "Normalsatz fakturiert, an die ESTV aber nur der Saldosteuersatz "
        "abgeliefert -- der Netto-Umsatz ist daher höher als bei effektiver "
        "Abrechnung. "
        "„Offene Debitoren\" stammen aus der Debitorenbuchhaltung (offene "
        "Kundenrechnungen); die Bilanzposition „Forderungen\" kann abweichen, "
        "da sie zusätzlich Vorsteuer-/Verrechnungssteuer-Guthaben enthält "
        "(keine Inkonsistenz).",
    ]
    return "\n".join(lines)


def _md_balance_sheet(d: dict) -> str:
    lines = [
        "**Aktiven**",
        f"- Flüssige Mittel: {_chf(d.get('fluessige_mittel'))}",
        f"- Forderungen (Konten 1100–1199, inkl. Vorsteuer-Guthaben): {_chf(d.get('forderungen'))}",
        f"- Vorräte / angef. Leistungen: {_chf(d.get('vorraete'))}",
        f"- Aktive Abgrenzung: {_chf(d.get('aktive_abgrenzung'))}",
        f"- Umlaufvermögen: {_chf(d.get('umlaufvermoegen'))}",
        f"- Anlagevermögen: {_chf(d.get('anlagevermoegen'))}",
        f"- **Total Aktiven: {_chf(d.get('aktiven_total'))}**",
        "",
        "**Passiven**",
        f"- Kurzfristiges Fremdkapital: {_chf(d.get('kurzfristiges_fk'))}",
        f"- Langfristiges Fremdkapital: {_chf(d.get('langfristiges_fk'))}",
        f"- Eigenkapital (gebucht, Stammkapital + Reserven): {_chf(d.get('eigenkapital_gebucht'))}",
        f"- Saldovortrag (rekonstruierte Ausgleichsgrösse, Schätzung): {_chf(d.get('gewinnvortrag_kumuliert'))}",
        f"- Jahresergebnis (laufend): {_chf(d.get('jahresergebnis_laufend'))}",
        f"- Eigenkapital total: {_chf(d.get('eigenkapital_total'))}",
        f"- **Total Passiven: {_chf(d.get('passiven_total'))}**",
        "",
        "**Kennzahlen**",
        f"- EK-Quote: {_pct(d.get('ek_quote'))} | FK-Quote: {_pct(d.get('fk_quote'))}",
        f"- Liquiditätsgrad 1/2/3: {_pct(d.get('liquiditaet_1'))} / {_pct(d.get('liquiditaet_2'))} / {_pct(d.get('liquiditaet_3'))}",
        f"- Working Capital: {_chf(d.get('working_capital'))}",
        f"- Anlagedeckungsgrad 2: {_pct(d.get('anlagedeckungsgrad_2'))}",
    ]
    lines.append(
        "- Methodik: Die Bilanz wird aus dem Journal des laufenden "
        "Geschäftsjahres abgeleitet. Der Saldovortrag früherer Jahre ist in "
        "Bexio noch nicht ins Eigenkapital umgebucht und wird hier als "
        "Ausgleichsgrösse rekonstruiert, damit Aktiven = Passiven gilt. EK- "
        "und FK-Quote nutzen einheitlich die Bilanzsumme (Total Aktiven) als "
        "Nenner. Für den definitiven Abschluss sollte die Eröffnungsbilanz in "
        "Bexio nachgetragen werden."
    )
    diff = d.get("bilanz_differenz") or 0
    if abs(diff) > 1:
        lines.append(
            f"- Hinweis: Verbleibende Bilanz-Differenz = {_chf(diff)} "
            "(sollte ~0 sein; sonst Datenproblem in den Bestandeskonten)."
        )
    return "\n".join(lines)


def _md_cashflow_forecast(d: dict) -> str:
    """Liquiditäts-Brücke: erklärt geplante Abflüsse bis Jahresende.

    Beantwortet die Frage, warum die Liquidität trotz gesichertem Umsatz
    bis Jahresende sinken kann (Steuern, MWST, Investitionen, Finanzierung).
    """
    months = d.get("months", []) or []
    forecast = [m for m in months if m.get("is_forecast")]
    history = [m for m in months if not m.get("is_forecast")]
    lines = [
        f"- Startsaldo (heute): {_chf(d.get('start_balance'))}",
        f"- Mindestliquidität (Schwelle): {_chf(d.get('min_liquidity'))}",
        f"- Run-Rate Einnahmen/Aufwand (Ø Monat): {_chf(d.get('forecast_revenue_monthly'))}"
        f" / {_chf(d.get('forecast_expenses_monthly'))}",
    ]

    # Zusammensetzung der echten Finanzierungsabflüsse der letzten 12 Monate.
    fin_total = 0.0
    fin_by_label: dict[str, float] = {}
    for m in history:
        fin_total += float(m.get("fin_outflow") or 0)
        for si in m.get("special_items", []) or []:
            label = si.get("label") or "Übrige Finanzierung"
            fin_by_label[label] = fin_by_label.get(label, 0.0) + float(si.get("amount") or 0)
    if fin_total > 100 or fin_by_label:
        lines.append("")
        lines.append(
            f"**Finanzierungsabflüsse letzte 12 Monate (effektiv): {_chf(fin_total)}** "
            "(echte Finanzierung: Ausschüttungen, Kontokorrent Gesellschafter, "
            "Darlehen — NICHT Kreditoren/MWST, diese sind operativ):"
        )
        for label, amt in sorted(fin_by_label.items(), key=lambda x: -x[1]):
            if abs(amt) < 100:
                continue
            lines.append(f"    - {label}: {_chf(amt)}")

    lines.append("")
    lines.append(
        "**Prognose pro Monat** (Einnahmen, Aufwand, Sonderabflüsse, erwartete Liquidität am Monatsende):"
    )
    for m in forecast:
        comp_parts = []
        if m.get("forecast_committed"):
            comp_parts.append(f"gesichert {_chf(m.get('forecast_committed'))}")
        if m.get("forecast_pipeline"):
            comp_parts.append(f"Pipeline {_chf(m.get('forecast_pipeline'))}")
        if m.get("forecast_fill"):
            comp_parts.append(f"Run-Rate-Auffüllung {_chf(m.get('forecast_fill'))}")
        comp = f" [Einnahmen: {', '.join(comp_parts)}]" if comp_parts else ""
        lines.append(
            f"- {m.get('month')}: Einnahmen {_chf(m.get('revenue'))}, "
            f"Aufwand {_chf(m.get('expenses'))}, "
            f"Δ {_chf(m.get('delta'))} → Liquidität {_chf(m.get('cumulative'))}{comp}"
        )
        for si in m.get("special_items", []) or []:
            lines.append(
                f"    - Sonderabfluss: {si.get('label')}: {_chf(si.get('amount'))}"
            )
        if m.get("fin_outflow"):
            lines.append(
                f"    - Finanzierungs-Abfluss (12M-amortisiert): {_chf(m.get('fin_outflow'))}"
            )
        if m.get("invest_outflow"):
            lines.append(
                f"    - Investitions-Abfluss (12M-amortisiert): {_chf(m.get('invest_outflow'))}"
            )
    lines.append("")
    lines.append(
        "_Methodik-Hinweis: In den Prognosemonaten sind Finanzierungs- und "
        "Investitionsabflüsse als 12M-Durchschnitt der Vergangenheit geglättet "
        "(rechnerische Annahme, KEINE vertraglich fixen Zahlungen). Operative "
        "Abflüsse umfassen Lieferanten-/Kreditoren- und MWST-Zahlungen. "
        "Run-Rate-Auffüllung = rechnerische Baseline, keine gesicherte Zusage. "
        "Die gesicherte Pipeline sinkt gegen Jahresende bewusst — kein "
        "automatisches Insolvenzsignal._"
    )
    return "\n".join(lines)


def _md_expenses_by_category(d: dict) -> str:
    cats = d.get("categories", []) or []
    lines = [f"Zeitraum: {d.get('period_from')} bis {d.get('period_to')} ({d.get('months_covered', 0)} Monate)"]
    for c in cats:
        if abs(c.get("total_12m", 0)) < 1:
            continue
        lines.append(
            f"- {c.get('label')}: {_chf(c.get('total_12m'))} total "
            f"(Ø {_chf(c.get('monthly_average'))}/Monat, {c.get('recurrence')})"
        )
    return "\n".join(lines)


def _md_expenses_by_account(d: dict) -> str:
    accts = d.get("accounts", []) or []
    lines = [
        f"Zeitraum: {d.get('period_from')} bis {d.get('period_to')}. "
        f"Total: {_chf(d.get('total'))} (YTD: {_chf(d.get('total_ytd'))})",
        "Top-Konten nach Aufwand:",
    ]
    for a in accts[:25]:
        lines.append(
            f"- {a.get('account_no')} {a.get('name')} [{a.get('category_label')}]: "
            f"{_chf(a.get('total'))} (YTD {_chf(a.get('total_ytd'))}, Ø {_chf(a.get('monthly_avg_12m'))}/M)"
        )
    return "\n".join(lines)


def _md_yoy(d: dict) -> str:
    cy = d.get("current_year")
    py = d.get("prior_year")
    as_of = d.get("as_of_date") or ""
    until = d.get("compare_until_label") or ""
    horizon = (
        f" (stichtagsgleicher Vergleich bis und mit {until} {cy}; "
        f"der laufende, unvollständige Monat ist ausgeschlossen)"
        if until else ""
    )
    lines = [
        f"Aktuelles Jahr {cy} vs. {py}, Stand {as_of or 'heute'}{horizon}:",
        f"- Umsatz YTD (faire Basis): {_chf(d.get('revenue_current_ytd'))} vs. "
        f"{_chf(d.get('revenue_prior_ytd'))} (Wachstum: {_pct(d.get('growth_pct'))})",
    ]
    # Monatsdetails (nur Monate mit Umsatz), damit das LLM die Saisonalität sieht.
    months = d.get("months", []) or []
    rows = [
        m for m in months
        if (m.get("revenue_current") or 0) > 0 or (m.get("revenue_prior") or 0) > 0
    ]
    if rows:
        lines.append("Monatsumsätze (aktuell vs. Vorjahr):")
        for m in rows:
            lines.append(
                f"- {m.get('month_label')}: {_chf(m.get('revenue_current'))} vs. "
                f"{_chf(m.get('revenue_prior'))}"
            )
    lines.append(
        "_Hinweis: Dieser Vergleich ist die massgebliche Quelle für die "
        "Umsatzentwicklung -- nicht die Differenz der Brutto-YTD-Werte aus der "
        "KPI-Übersicht (die den laufenden, oft noch leeren Monat enthält)._"
    )
    return "\n".join(lines)


def _md_debtors(d: dict) -> str:
    tm = d.get("toggl_month", {}) or {}
    lines = [
        f"- Total offene Debitoren: {_chf(d.get('total_open'))}",
        f"- Umsatz YTD (Debitoren): {_chf(d.get('total_revenue_ytd'))}",
        f"- DSO: {d.get('dso_days') if d.get('dso_days') is not None else 'n/a'} Tage",
        f"- Toggl laufender Monat: {tm.get('billable_hours', 0)}h fakturierbar von {tm.get('total_hours', 0)}h "
        f"({_pct(tm.get('billable_ratio'))}), Umsatz {_chf(tm.get('total_amount'))}, "
        f"Prognose Monat {_chf(tm.get('forecast_month_amount'))}",
    ]
    debtors = d.get("debtors", []) or []
    if debtors:
        lines.append("Top-Kunden (Umsatz YTD):")
        for x in debtors[:8]:
            lines.append(
                f"- {x.get('contact_name')}: {_chf(x.get('revenue_ytd'))} "
                f"(offen: {_chf(x.get('open_invoices_total'))}, Vorjahr: {_chf(x.get('revenue_prior_year'))})"
            )
    return "\n".join(lines)


def _md_creditors(d: dict) -> str:
    lines: list[str] = []
    kpis = d.get("kpis") or {}
    if isinstance(kpis, dict) and kpis:
        lines.append("**KPIs (InvoiceInsight)**")
        for k, v in kpis.items():
            lines.append(f"- {k}: {v}")
    # Diese Resource antwortet je nach InvoiceInsight-Version mit Datensätzen oder
    # mit einem fertigen Textblock. Beides muss durchgehen: am 06.09.2026 lieferte sie
    # Text, und der Zugriff darauf wie auf einen Datensatz liess die **ganze**
    # Kreditoren-Sektion ausfallen. Der Snapshot zeigte dann «(Daten nicht verfügbar)»,
    # obwohl KPIs und Lieferanten vorlagen -- und die Totalzeile hätte davor
    # «CHF None» behauptet, was schlimmer ist als eine fehlende Zeile.
    rec = d.get("recurring_vs_onetime") or {}
    if isinstance(rec, dict) and rec:
        summen = [
            f"wiederkehrend {_chf(rec['recurring_total'])}" if "recurring_total" in rec else "",
            f"einmalig {_chf(rec['onetime_total'])}" if "onetime_total" in rec else "",
        ]
        if any(summen):
            lines.append("")
            lines.append("**Wiederkehrend vs. einmalig**: " + ", ".join(s for s in summen if s))

        for art, beschriftung in (("recurring", "wiederkehrend"), ("onetime", "einmalig")):
            inhalt = rec.get(art)
            if isinstance(inhalt, list):
                for r in inhalt[:15]:
                    if not isinstance(r, dict):
                        lines.append(f"- ({beschriftung}) {r}")
                        continue
                    name = r.get("name") or r.get("Kreditor") or r.get("Kategorie") or "?"
                    total = r.get("Total_CHF") or r.get("total_chf") or r.get("Total") or 0
                    lines.append(f"- ({beschriftung}) {name}: {_chf(total)}")
            elif isinstance(inhalt, str) and inhalt.strip():
                lines.append("")
                lines.append(f"**{beschriftung.capitalize()} (roh)**: {inhalt.strip()[:1500]}")
    cd = d.get("cost_distribution")
    if cd:
        lines.append("")
        lines.append(f"**Kostenverteilung (roh)**: {str(cd)[:1500]}")

    # Lieferanten-Übersicht (zentral fürs Tool-/Kosten-Audit) -- defensiv rendern,
    # da die Resource je nach Version Liste oder Dict zurückgibt.
    vendors = d.get("vendor_overview")
    vlist = None
    if isinstance(vendors, list):
        vlist = vendors
    elif isinstance(vendors, dict):
        vlist = vendors.get("vendors") or vendors.get("items") or vendors.get("data")
    if isinstance(vlist, list) and vlist:
        lines.append("")
        lines.append("**Top-Lieferanten (InvoiceInsight)**")
        for v in vlist[:20]:
            if not isinstance(v, dict):
                lines.append(f"- {v}")
                continue
            name = v.get("name") or v.get("Kreditor") or v.get("vendor") or "?"
            total = (v.get("Total_CHF") or v.get("total_chf") or v.get("Total")
                     or v.get("total") or v.get("amount") or 0)
            count = v.get("count") or v.get("Anzahl") or v.get("invoices")
            cat = v.get("category") or v.get("Kategorie")
            extra = f", {count} Rechnungen" if count else ""
            extra += f", {cat}" if cat else ""
            lines.append(f"- {name}: {_chf(total)}{extra}")
    elif isinstance(vendors, (dict, str)) and vendors:
        lines.append("")
        lines.append(f"**Lieferanten-Übersicht (roh)**: {str(vendors)[:1500]}")

    return "\n".join(lines) if lines else "(Keine InvoiceInsight-Daten verfügbar)"


# ── Ausgaben-Lesart ──────────────────────────────────────
#
# Drei Bestände berühren Ausgaben, und nur einer beantwortet «wie viel». Stehen die
# Kreditoren- und die Journal-Sektion zusammen im Snapshot, sieht das Modell zwei
# Ausgabensummen ohne Hinweis darauf, dass sie verschiedene Fragen beantworten -- und
# die naheliegende Handlung, sie zu addieren, ergibt eine Zahl, die es nicht gibt.
#
# Die Zahlen sind gemessen (03.09.2026, Geschäftsjahr 2025) und stehen bewusst dabei:
# eine Warnung ohne Zahl wirkt nicht. Dieselbe Lesart steht als ``KREDITOREN_LESART``
# im MCP-Server des Datenraums -- sie gilt für Mensch und Maschine gleich.
AUSGABEN_LESART = """**Lesart der Ausgabenzahlen -- vor jeder Aussage beachten**

Die beiden Ausgaben-Sektionen dieses Snapshots stammen aus verschiedenen Beständen
und beantworten verschiedene Fragen. **Nie über beide summieren** -- das zählt doppelt.

- *Aufwand nach Kategorie / nach Konto* kommt aus dem Buchungsjournal und beantwortet
  «was hat uns X gekostet». Das ist die vollständige Sicht. Die Zahlen sind **netto** --
  Gegenbuchungen im Haben eines Aufwandskontos sind abgezogen. Wer das Journal selbst
  abfragt und nur die Sollseite summiert, erhält für 2025 401'459 statt 335'982 CHF:
  43'335 davon sind Umbuchungen zwischen zwei Aufwandskonten, also derselbe Betrag
  zweimal, und 22'142 CHF Rückerstattungen fehlen. Weicht eine selbst gerechnete Zahl
  um diese Grössenordnung ab, ist das der Grund.
- *Kreditoren (InvoiceInsight)* beantwortet «wofür genau, welcher Lieferant, welches
  Abo». Als Ausgabensumme ist es **ungeeignet**: 2025 liefen nur 88'177 von 335'982
  CHF Aufwand über den Kreditorenweg, also 26 Prozent. Kartenbezahlte Abos erreichen
  die Buchhaltung anders (Soll Aufwandskonto / Haben Kontokorrent Gesellschafter) und
  fehlen dort vollständig -- Cursor stand 2026 mit 12'924 CHF im Journal und mit null
  in den Kreditoren.

Für «was ist offen und fällig» ist umgekehrt der Kreditorenbestand richtig und das
Journal ungeeignet."""


_RENDERERS: dict[str, Callable[[dict], str]] = {
    "overview": _md_overview,
    "balance_sheet": _md_balance_sheet,
    "cashflow_forecast": _md_cashflow_forecast,
    "expenses_by_category": _md_expenses_by_category,
    "expenses_by_account": _md_expenses_by_account,
    "yoy": _md_yoy,
    "debtors": _md_debtors,
    "creditors": _md_creditors,
}


# ── Public API ───────────────────────────────────────────

async def build_snapshot(user: User, sections: list[str]) -> dict:
    """Baut die angeforderten Snapshot-Sektionen.

    Returns ein Dict mit:
      - ``sections``: {name: {"title", "data", "markdown", "error"?}}
      - ``markdown``: zusammengesetzter Markdown-Block (LLM-Kontext)
      - ``meta``: {generated_at, sections, currency}
    """
    result_sections: dict[str, dict] = {}
    md_parts: list[str] = []

    for name in sections:
        builder = _BUILDERS.get(name)
        if builder is None:
            continue
        title = SECTION_TITLES.get(name, name)
        try:
            data = await builder(user)
            renderer = _RENDERERS.get(name)
            md = renderer(data) if renderer else ""
            result_sections[name] = {"title": title, "data": data, "markdown": md}
            md_parts.append(f"## {title}\n\n{md}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Snapshot-Sektion '%s' fehlgeschlagen: %s", name, e)
            result_sections[name] = {"title": title, "data": {}, "markdown": "", "error": str(e)}
            md_parts.append(f"## {title}\n\n(Daten nicht verfügbar)")

    today = date.today()

    # Steuer-Kontext aus den Owner-Settings (für Lohn/Dividende, Steuerthemen).
    from app.services.finance_settings import get_forecast_settings_from_settings

    fset = get_forecast_settings_from_settings(getattr(user, "settings", None))
    ctx_parts = []
    if fset.tax_canton:
        ctx_parts.append(f"Sitz-/Wohnkanton: {fset.tax_canton}")
    if fset.civil_status:
        ctx_parts.append(f"Zivilstand: {fset.civil_status}")
    ctx_line = (" | " + " | ".join(ctx_parts)) if ctx_parts else ""

    # Die Lesart nur dann, wenn beide Ausgabenbestände nebeneinander stehen. Ein
    # Hinweis auf eine Verwechslung, die im vorliegenden Snapshot nicht möglich ist,
    # wäre Rauschen -- und Rauschen entwertet auch die Hinweise, die tragen.
    journal_sektionen = {"expenses_by_category", "expenses_by_account", "overview",
                         "cashflow_forecast"}
    lesart = ""
    if "creditors" in result_sections and journal_sektionen & set(result_sections):
        lesart = f"{AUSGABEN_LESART}\n\n"

    # Datenstand statt Abrufdatum: die Zahlen sind so alt wie der letzte Abgleich,
    # nicht so alt wie diese Anfrage. Der Unterschied entscheidet darüber, ob eine
    # Aussage über «den laufenden Monat» belastbar ist.
    from app.services import datenraum_lesen as dl

    try:
        datenstand = dl.stand()
    except Exception as e:  # noqa: BLE001 -- ein fehlender Stand darf keine Analyse verhindern
        logger.warning("Datenstand nicht lesbar: %s", e)
        datenstand = {}
    stand_text = (
        f"Datenstand: {datenstand['stand'][:16].replace('T', ' ')} UTC "
        f"({datenstand['alter_stunden']} h alt)"
        if datenstand.get("stand") else f"Stand: {today.isoformat()}"
    )

    markdown = (
        f"# Finanz-Snapshot\n\n"
        f"{stand_text} | Währung: CHF | Rechtsform: GmbH{ctx_line}\n\n"
        + lesart
        + "\n\n".join(md_parts)
    )

    return {
        "sections": result_sections,
        "markdown": markdown,
        "meta": {
            "generated_at": today.isoformat(),
            "sections": list(result_sections.keys()),
            "currency": "CHF",
            "datenstand": datenstand,
        },
    }
