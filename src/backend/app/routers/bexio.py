"""FastAPI Router für Bexio Buchhaltung (Frontend-Zugriff)."""

import logging
import sys
from pathlib import Path

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.deps import get_current_user, require_role
from app.models import User

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "bexio"))
from bexio_client import BexioClient, BexioConfig, decode_token_expiry  # noqa: E402

logger = logging.getLogger("taskpilot.bexio.router")

router = APIRouter(prefix="/api/bexio", tags=["bexio"])

# ── Cache-Layer (analog Pipedrive) ───────────────────────
_invoice_cache: TTLCache = TTLCache(maxsize=200, ttl=900)
_bank_cache: TTLCache = TTLCache(maxsize=20, ttl=300)
_payment_cache: TTLCache = TTLCache(maxsize=500, ttl=900)
_account_cache: TTLCache = TTLCache(maxsize=500, ttl=3600)
_static_cache: TTLCache = TTLCache(maxsize=50, ttl=3600)


def _get_bexio_client(user: User) -> BexioClient:
    """Bexio-Client aus User-Settings oder Env-Variablen erstellen."""
    from app.services.fachsysteme import bexio_zugang

    return bexio_zugang(user)


# ── Verbindungstest ──────────────────────────────────────

def _augment_with_token_expiry(client: BexioClient, result: dict) -> dict:
    """Ergaenzt das Ergebnis um die Ablauf-Info des Tokens (PAT laeuft nach 6 Monaten ab)."""
    expiry = client.token_expiry()
    if expiry:
        result["token_expires_at"] = expiry["expires_at"]
        result["token_days_remaining"] = expiry["days_remaining"]
        result["token_expired"] = expiry["is_expired"]
    return result


@router.get("/test-connection")
async def test_connection(user: User = Depends(require_role("owner"))):
    try:
        client = _get_bexio_client(user)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Bexio Client-Erstellung fehlgeschlagen")
        raise HTTPException(status_code=400, detail="Bexio-Client konnte nicht erstellt werden")
    try:
        result = await client.test_connection()
        return _augment_with_token_expiry(client, result)
    except Exception as e:
        logger.exception("Bexio Verbindungstest fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Bexio-Verbindungstest fehlgeschlagen")


# ── Kontakte ─────────────────────────────────────────────

class ContactSummary(BaseModel):
    id: int
    name_1: str
    name_2: str | None = None
    mail: str | None = None
    contact_type_id: int | None = None


@router.get("/contacts", response_model=list[ContactSummary])
async def list_contacts(
    limit: int = Query(default=50, le=200),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"contacts:{limit}"
    cached = _static_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    contacts = await client.list_contacts(limit=limit)
    result = [
        ContactSummary(
            id=c.get("id", 0), name_1=c.get("name_1", ""),
            name_2=c.get("name_2"), mail=c.get("mail"),
            contact_type_id=c.get("contact_type_id"),
        )
        for c in contacts
    ]
    _static_cache[cache_key] = result
    return result


@router.get("/contacts/{contact_id}")
async def get_contact(contact_id: int, user: User = Depends(require_role("owner"))):
    client = _get_bexio_client(user)
    return await client.get_contact(contact_id)


@router.get("/contacts/{contact_id}/orders")
async def list_orders_for_contact(
    contact_id: int,
    limit: int = Query(default=50, le=200),
    user: User = Depends(require_role("owner")),
):
    client = _get_bexio_client(user)
    return await client.list_orders(contact_id=contact_id, limit=limit)


# ── Rechnungen ───────────────────────────────────────────

class InvoiceSummary(BaseModel):
    id: int
    document_nr: str | None = None
    title: str | None = None
    contact_id: int | None = None
    total_gross: str | None = None
    total_net: str | None = None
    total: str | None = None
    currency_id: int | None = None
    kb_item_status_id: int | None = None
    is_valid_from: str | None = None
    is_valid_to: str | None = None


@router.get("/invoices", response_model=list[InvoiceSummary])
async def list_invoices(
    limit: int = Query(default=100, le=500),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"invoices:{limit}"
    cached = _invoice_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    invoices = await client.list_invoices(limit=limit)
    result = [
        InvoiceSummary(
            id=inv.get("id", 0),
            document_nr=inv.get("document_nr"),
            title=inv.get("title"),
            contact_id=inv.get("contact_id"),
            total_gross=inv.get("total_gross"),
            total_net=inv.get("total_net"),
            total=inv.get("total"),
            currency_id=inv.get("currency_id"),
            kb_item_status_id=inv.get("kb_item_status_id"),
            is_valid_from=inv.get("is_valid_from"),
            is_valid_to=inv.get("is_valid_to"),
        )
        for inv in invoices
    ]
    _invoice_cache[cache_key] = result
    return result


@router.get("/invoices/search", response_model=list[InvoiceSummary])
async def search_invoices(
    status: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"inv_search:{status}:{from_date}:{to_date}"
    cached = _invoice_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    invoices = await client.search_invoices(status=status, from_date=from_date, to_date=to_date)
    result = [
        InvoiceSummary(
            id=inv.get("id", 0),
            document_nr=inv.get("document_nr"),
            title=inv.get("title"),
            contact_id=inv.get("contact_id"),
            total_gross=inv.get("total_gross"),
            total_net=inv.get("total_net"),
            total=inv.get("total"),
            currency_id=inv.get("currency_id"),
            kb_item_status_id=inv.get("kb_item_status_id"),
            is_valid_from=inv.get("is_valid_from"),
            is_valid_to=inv.get("is_valid_to"),
        )
        for inv in invoices
    ]
    _invoice_cache[cache_key] = result
    return result


# ── Bankkonten ───────────────────────────────────────────

@router.get("/bank-accounts")
async def list_bank_accounts(user: User = Depends(require_role("owner"))):
    cached = _bank_cache.get("bank_accounts")
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    result = await client.list_bank_accounts()
    _bank_cache["bank_accounts"] = result
    return result


@router.get("/bank-accounts/{account_id}")
async def get_bank_account(account_id: int, user: User = Depends(require_role("owner"))):
    cache_key = f"bank:{account_id}"
    cached = _bank_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    result = await client.get_bank_account(account_id)
    _bank_cache[cache_key] = result
    return result


# ── Zahlungen ────────────────────────────────────────────

@router.get("/payments")
async def list_payments(
    limit: int = Query(default=200, le=500),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"payments:{limit}"
    cached = _payment_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    result = await client.list_payments(limit=limit)
    _payment_cache[cache_key] = result
    return result


# ── Kontenplan ───────────────────────────────────────────

@router.get("/accounts")
async def list_accounts(user: User = Depends(require_role("owner"))):
    cached = _account_cache.get("accounts_all")
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    result = await client.list_accounts()
    _account_cache["accounts_all"] = result
    return result


# ── Projekte ─────────────────────────────────────────────

class ProjectSummary(BaseModel):
    id: int
    name: str
    contact_id: int | None = None
    status_id: int | None = None


@router.get("/projects", response_model=list[ProjectSummary])
async def list_projects(
    limit: int = Query(default=50, le=200),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"projects:{limit}"
    cached = _static_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_bexio_client(user)
    projects = await client.list_projects(limit=limit)
    result = [
        ProjectSummary(
            id=p.get("id", 0), name=p.get("name", ""),
            contact_id=p.get("contact_id"),
            status_id=p.get("pr_state_id"),
        )
        for p in projects
    ]
    _static_cache[cache_key] = result
    return result


# ── Suche ────────────────────────────────────────────────

@router.get("/search")
async def search_bexio(
    q: str = Query(min_length=1),
    user: User = Depends(require_role("owner")),
):
    client = _get_bexio_client(user)
    contacts = await client.search_contact_by_name(q)
    return {
        "contacts": [
            {"id": c.get("id"), "name_1": c.get("name_1"), "name_2": c.get("name_2"), "mail": c.get("mail")}
            for c in contacts
        ],
    }


# ── Cache-Verwaltung ─────────────────────────────────────

@router.post("/cache/clear")
async def clear_cache(user: User = Depends(require_role("owner"))):
    _invoice_cache.clear()
    _bank_cache.clear()
    _payment_cache.clear()
    _account_cache.clear()
    _static_cache.clear()
    logger.info("Bexio-Caches manuell geleert")
    return {"status": "ok", "message": "Alle Bexio-Caches geleert"}


@router.get("/cache/stats")
async def cache_stats(user: User = Depends(require_role("owner"))):
    return {
        "invoice_cache": {"size": len(_invoice_cache), "maxsize": _invoice_cache.maxsize, "ttl": _invoice_cache.ttl},
        "bank_cache": {"size": len(_bank_cache), "maxsize": _bank_cache.maxsize, "ttl": _bank_cache.ttl},
        "payment_cache": {"size": len(_payment_cache), "maxsize": _payment_cache.maxsize, "ttl": _payment_cache.ttl},
        "account_cache": {"size": len(_account_cache), "maxsize": _account_cache.maxsize, "ttl": _account_cache.ttl},
        "static_cache": {"size": len(_static_cache), "maxsize": _static_cache.maxsize, "ttl": _static_cache.ttl},
    }
