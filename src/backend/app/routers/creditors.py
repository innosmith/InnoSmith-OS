"""Kreditoren-Modul -- InvoiceInsight-Integration via MCP.

Stellt REST-Endpoints bereit, die den InvoiceInsight MCP-Server abfragen
und die Daten fuer das Frontend-Dashboard aufbereiten.
"""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.auth.deps import get_current_user, require_role
from app.config import get_settings
from app.models import User
from app.services.invoiceinsight_client import InvoiceInsightClient
from app.services.invoiceinsight_rest import (
    SchnittstellenFehler,
    beleg_holen,
    beleg_korrigieren,
)

logger = logging.getLogger("taskpilot.creditors")

router = APIRouter(prefix="/api/creditors", tags=["creditors"])


def _parse_table_string(text: str, years: list[int] | None = None) -> list[dict]:
    """Parst pandas DataFrame-Textdarstellungen in strukturierte Dicts."""
    lines = [l for l in text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return []
    try:
        import re
        header_parts = re.split(r"\s{2,}", lines[0].strip())
        results = []
        for line in lines[1:]:
            parts = re.split(r"\s{2,}", line.strip())
            if len(parts) < 2:
                continue
            idx_and_name = parts[0]
            name = re.sub(r"^\d+\s+", "", idx_and_name).strip()
            values = parts[1:]
            row: dict[str, Any] = {"name": name}
            for i, val in enumerate(values):
                key = header_parts[i + 1] if i + 1 < len(header_parts) else f"col_{i}"
                try:
                    row[key] = float(val)
                except (ValueError, TypeError):
                    row[key] = val
            results.append(row)
        return results
    except Exception as e:
        logger.warning("Tabelle konnte nicht geparst werden: %s", e)
        return []


def _get_client(user: User) -> InvoiceInsightClient:
    settings = user.settings or {}
    api_key = settings.get("invoiceinsight_api_key") or ""
    url = settings.get("invoiceinsight_url") or ""
    if not api_key:
        cfg = get_settings()
        api_key = cfg.invoiceinsight_api_key
        url = url or cfg.invoiceinsight_url
    if not api_key:
        raise HTTPException(status_code=400, detail="InvoiceInsight API-Key nicht konfiguriert")
    if not url:
        raise HTTPException(status_code=503, detail="InvoiceInsight-URL nicht konfiguriert")
    return InvoiceInsightClient(url=url, api_key=api_key)


async def _safe_call(coro, fallback: Any = None) -> Any:
    try:
        return await coro
    except Exception as e:
        logger.warning("InvoiceInsight-Aufruf fehlgeschlagen: %s", e)
        return fallback


@router.get("/dashboard")
async def get_dashboard(
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    categories: str | None = Query(default=None, description="Kommaseparierte Kategorien"),
    user: User = Depends(require_role("owner")),
):
    """Aggregierte Dashboard-Sicht: KPIs + Kostenverteilung + Metadata."""
    client = _get_client(user)
    cat_list = [c.strip() for c in categories.split(",")] if categories else None
    kpis = await _safe_call(client.get_kpis(year_from=year_from, year_to=year_to), {})
    cost_dist = await _safe_call(
        client.get_cost_distribution(year_from=year_from, year_to=year_to, categories=cat_list), {},
    )
    metadata = await _safe_call(client.get_metadata(), {})
    return {"kpis": kpis, "cost_distribution": cost_dist, "metadata": metadata}


@router.get("/invoices")
async def search_invoices(
    query: str = Query(default="", description="Suchbegriff"),
    year: int | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    args: dict[str, Any] = {"query": query, "limit": limit}
    if year:
        args["year"] = year
    if category:
        args["category"] = category
    return await client.call_tool("search_invoices", args)


@router.get("/invoices/filtered")
async def get_filtered_invoices(
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    categories: str | None = Query(default=None, description="Kommaseparierte Kategorien"),
    vendors: str | None = Query(default=None, description="Kommaseparierte Kreditoren"),
    min_amount: float | None = Query(default=None),
    max_amount: float | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    args: dict[str, Any] = {"limit": limit}
    if year_from:
        args["year_from"] = year_from
    if year_to:
        args["year_to"] = year_to
    if categories:
        args["categories"] = [c.strip() for c in categories.split(",")]
    if vendors:
        args["vendors"] = [v.strip() for v in vendors.split(",")]
    if min_amount is not None:
        args["min_amount"] = min_amount
    if max_amount is not None:
        args["max_amount"] = max_amount
    return await client.call_tool("get_filtered_invoices", args)


@router.get("/invoice/{invoice_id}")
async def get_invoice_detail(
    invoice_id: int,
    user: User = Depends(require_role("owner")),
):
    """Ein Beleg zum Ansehen -- ``invoice_id`` ist hier die **Zeilennummer**.

    Nicht die Kennung des Belegs: die Gegenseite liest mit ``df.iloc[]``. Fürs
    Ansehen genügt das, fürs Schreiben nicht -- dort führt die Verwechslung zum
    falschen Beleg, ohne dass ein Fehler entsteht. Der Korrekturweg benutzt
    deshalb ``/beleg/{beleg_id}`` mit der Datenbankkennung aus ``beleg_id``.
    """
    client = _get_client(user)
    return await client.call_tool("get_invoice_details", {"invoice_id": invoice_id})


def _rest_zugang(user: User) -> tuple[str, str]:
    """Adresse und Token der HTTP-Schnittstelle, Owner-Einstellung vor Umgebung."""
    settings = user.settings or {}
    cfg = get_settings()
    url = settings.get("invoiceinsight_rest_url") or cfg.invoiceinsight_rest_url
    token = settings.get("invoiceinsight_rest_token") or cfg.invoiceinsight_rest_token
    if not url or not token:
        raise HTTPException(
            status_code=503,
            detail="InvoiceInsight-Schnittstelle nicht konfiguriert",
        )
    return url, token


@router.get("/beleg/{beleg_id}")
async def get_beleg(
    beleg_id: int,
    user: User = Depends(require_role("owner")),
):
    """Ein Beleg anhand seiner Datenbankkennung, mit allen gelesenen Feldern."""
    url, token = _rest_zugang(user)
    try:
        return await beleg_holen(url, token, beleg_id)
    except SchnittstellenFehler as fehler:
        raise HTTPException(status_code=fehler.status, detail=fehler.meldung) from fehler
    except RuntimeError as fehler:
        raise HTTPException(status_code=503, detail=str(fehler)) from fehler


@router.patch("/beleg/{beleg_id}")
async def patch_beleg(
    beleg_id: int,
    felder: dict[str, Any],
    user: User = Depends(require_role("owner")),
):
    """Einzelne Werte eines Belegs richtigstellen.

    Der Urheber kommt aus der Anmeldung, nie aus dem Rumpf -- sonst liesse sich
    eine Korrektur einer anderen Person zuschreiben, und die Spur wäre wertlos.
    Ein mitgeschicktes ``geprueft_von`` wird deshalb verworfen.
    """
    url, token = _rest_zugang(user)
    bereinigt = {k: v for k, v in felder.items() if k != "geprueft_von"}
    if not bereinigt:
        raise HTTPException(status_code=422, detail="Keine Änderung übermittelt")
    try:
        return await beleg_korrigieren(url, token, beleg_id, bereinigt, user.email)
    except SchnittstellenFehler as fehler:
        # Unverändert weiterreichen: bei 422 steckt in ``detail`` je Eintrag
        # ein ``loc``, und nur damit landet die Meldung in der Maske neben dem
        # Feld, das sie meint.
        raise HTTPException(status_code=fehler.status, detail=fehler.rohdetail) from fehler
    except RuntimeError as fehler:
        raise HTTPException(status_code=503, detail=str(fehler)) from fehler


@router.get("/vendors")
async def get_top_vendors(
    top_n: int = Query(default=15, ge=1, le=50),
    year: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    args: dict[str, Any] = {"top_n": top_n}
    if year:
        args["year"] = year
    return await client.call_tool("get_top_vendors", args)


@router.get("/vendor/{vendor_name}")
async def get_vendor_detail(
    vendor_name: str,
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    return await client.call_tool("get_vendor_details", {"vendor_name": vendor_name})


@router.get("/trends")
async def get_monthly_trend(
    year: int | None = Query(default=None),
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    category: str | None = Query(default=None),
    categories: str | None = Query(default=None, description="Kommaseparierte Kategorien (Alias)"),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    args: dict[str, Any] = {}
    if year:
        args["year"] = year
    if year_from:
        args["year_from"] = year_from
    if year_to:
        args["year_to"] = year_to
    effective_cat = category or (categories.split(",")[0].strip() if categories else None)
    if effective_cat:
        args["category"] = effective_cat
    return await client.call_tool("get_monthly_trend", args)


@router.get("/category-trend")
async def get_category_trend(
    category: str | None = Query(default=None, description="Kategoriename"),
    years: int = Query(default=3, ge=1, le=10),
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    args: dict[str, Any] = {"years": years}
    if category:
        args["category"] = category
    if year_from:
        args["year_from"] = year_from
    if year_to:
        args["year_to"] = year_to
    return await client.call_tool("get_category_trend", args)


@router.get("/renewal-calendar")
async def get_renewal_calendar(
    vendors: str | None = Query(default=None, description="Kommaseparierte Kreditoren"),
    months_ahead: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    """Erneuerungskalender, gruppiert nach Dringlichkeit."""
    client = _get_client(user)
    vendor_list = [v.strip() for v in vendors.split(",")] if vendors else None
    raw = await _safe_call(
        client.get_renewal_calendar(vendors=vendor_list, months_ahead=months_ahead), [],
    )
    if isinstance(raw, dict) and any(k in raw for k in ("critical", "warning", "info")):
        return raw
    entries = raw if isinstance(raw, list) else []
    critical, warning, info = [], [], []
    for e in entries:
        days = e.get("Tage_bis_Renewal") if isinstance(e, dict) else None
        if days is not None and days < 0:
            continue
        if days is not None and days <= 30:
            critical.append(e)
        elif days is not None and days <= 60:
            warning.append(e)
        else:
            info.append(e)
    return {"critical": critical, "warning": warning, "info": info}


@router.get("/upcoming")
async def get_upcoming_payments(
    n: int = Query(default=10, ge=1, le=50),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    return await client.call_tool("get_upcoming_payments", {"n": n})


@router.get("/cashflow-forecast")
async def get_cashflow_forecast(user: User = Depends(require_role("owner"))):
    client = _get_client(user)
    return await client.get_cashflow_forecast()


@router.get("/recurring")
async def get_recurring_vs_onetime(
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    """Aufschlüsselung in wiederkehrende vs. einmalige Kosten."""
    client = _get_client(user)
    raw = await _safe_call(
        client.get_recurring_vs_onetime(year_from=year_from, year_to=year_to), {},
    )
    if not isinstance(raw, dict):
        return {"recurring": [], "onetime": [], "recurring_total": 0, "onetime_total": 0}

    def parse_section(data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, str):
            return _parse_table_string(data)
        return []

    rec = parse_section(raw.get("recurring", []))
    one = parse_section(raw.get("onetime", []))
    rec_total = sum(float(r.get("Total_CHF", r.get("total_chf", 0)) or 0) for r in rec)
    one_total = sum(float(r.get("Total_CHF", r.get("total_chf", 0)) or 0) for r in one)
    return {"recurring": rec, "onetime": one, "recurring_total": rec_total, "onetime_total": one_total}


@router.get("/anomalies")
async def get_anomalies(
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    """Anomalien, gruppiert nach Schweregrad."""
    client = _get_client(user)
    raw = await _safe_call(
        client.get_anomalies(year_from=year_from, year_to=year_to), [],
    )
    if isinstance(raw, dict) and any(k in raw for k in ("critical", "warning", "info")):
        return raw
    entries = raw if isinstance(raw, list) else []
    critical, warning, info = [], [], []
    severity_map = {"HIGH": critical, "CRITICAL": critical, "MEDIUM": warning, "WARNING": warning}
    for e in entries:
        sev = (e.get("severity") or "INFO").upper() if isinstance(e, dict) else "INFO"
        severity_map.get(sev, info).append(e)
    return {"critical": critical, "warning": warning, "info": info}


@router.get("/yoy")
async def get_yoy_comparison(
    year_from: int | None = Query(default=None),
    year_to: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    """Jahresvergleich der Kosten nach Kategorien."""
    client = _get_client(user)
    raw = await _safe_call(
        client.get_yoy_comparison(year_from=year_from, year_to=year_to), {},
    )
    if not isinstance(raw, dict):
        return {"data": [], "years": []}
    data = raw.get("data")
    years = raw.get("years", [])
    categories = []
    if isinstance(data, list):
        categories = data
    elif isinstance(data, str):
        categories = _parse_table_string(data, years)
    proj = raw.get("projection_data")
    proj_cats = []
    if isinstance(proj, list):
        proj_cats = proj
    elif isinstance(proj, str):
        proj_cats = _parse_table_string(proj, years)
    return {
        "categories": categories,
        "projection": proj_cats,
        "years": years,
        "current_year_incomplete": raw.get("current_year_incomplete", False),
        "current_year_months": raw.get("current_year_months"),
    }


@router.get("/vat")
async def get_vat_summary(
    year: int | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    client = _get_client(user)
    args: dict[str, Any] = {}
    if year:
        args["year"] = year
    return await client.call_tool("get_vat_summary", args)


@router.get("/data-quality")
async def get_data_quality(user: User = Depends(require_role("owner"))):
    client = _get_client(user)
    return await client.get_data_quality()


@router.get("/vendor-overview")
async def get_vendor_overview(user: User = Depends(require_role("owner"))):
    client = _get_client(user)
    return await client.get_vendor_overview()


@router.post("/deep-research")
async def generate_research_prompt(user: User = Depends(require_role("owner"))):
    client = _get_client(user)
    return await client.call_tool("generate_research_prompt")


@router.get("/invoice/{invoice_id}/pdf")
async def get_invoice_pdf(
    invoice_id: int,
    user: User = Depends(require_role("owner")),
):
    """Proxy fuer PDF-Dateipfade aus InvoiceInsight."""
    client = _get_client(user)
    try:
        detail = await client.call_tool("get_invoice_details", {"invoice_id": invoice_id})
        pdf_path = None
        if isinstance(detail, dict):
            pdf_path = detail.get("pdf_path") or detail.get("Dateipfad")
        if not pdf_path:
            raise HTTPException(status_code=404, detail="Kein PDF verfuegbar")
        return {"pdf_path": pdf_path, "note": "PDF-Dateipfad vom MCP-Server"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Kreditoren-PDF-Pfad konnte nicht geladen werden für Rechnung %s", invoice_id)
        raise HTTPException(status_code=500, detail="Kreditoren-Rechnungs-PDF konnte nicht geladen werden")


@router.get("/invoice/{invoice_id}/pdf/view")
async def view_invoice_pdf(
    invoice_id: int,
    user: User = Depends(require_role("owner")),
):
    """Liefert das PDF einer Kreditoren-Rechnung als Binary-Response."""
    client = _get_client(user)
    try:
        detail = await client.call_tool("get_invoice_details", {"invoice_id": invoice_id})
        pdf_path_str = None
        if isinstance(detail, dict):
            pdf_path_str = detail.get("pdf_path") or detail.get("Dateipfad")
        if not pdf_path_str:
            raise HTTPException(status_code=404, detail="Kein PDF verfuegbar")
        pdf_path = Path(pdf_path_str).resolve()
        if not pdf_path.is_file():
            raise HTTPException(status_code=404, detail="PDF-Datei nicht gefunden")
        return FileResponse(
            str(pdf_path),
            media_type="application/pdf",
            filename=pdf_path.name,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Kreditoren-PDF konnte nicht angezeigt werden für Rechnung %s", invoice_id)
        raise HTTPException(status_code=500, detail="Kreditoren-Rechnungs-PDF konnte nicht angezeigt werden")


@router.post("/cache/clear")
async def clear_cache(user: User = Depends(require_role("owner"))):
    client = _get_client(user)
    client.invalidate_cache()
    try:
        await client.call_tool("refresh_cache")
    except Exception as e:
        logger.warning("Remote cache refresh fehlgeschlagen: %s", e)
    return {"status": "ok"}
