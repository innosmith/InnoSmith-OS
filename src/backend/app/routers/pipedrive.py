"""FastAPI Router fuer Pipedrive CRM-Daten (Frontend-Zugriff).

Stellt die Pipedrive-Daten dem Frontend ueber REST-Endpunkte bereit,
ohne dass das Frontend direkt mit der Pipedrive-API kommunizieren muss.
Zweistufiges In-Memory-Caching reduziert API-Token-Verbrauch massiv.
"""

import base64
import io
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query
from PIL import Image
from pydantic import BaseModel

from app.auth.deps import get_current_user, require_role
from app.models import User

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "pipedrive"))
from pipedrive_client import PipedriveClient, PipedriveConfig  # noqa: E402

logger = logging.getLogger("taskpilot.pipedrive")

router = APIRouter(prefix="/api/pipedrive", tags=["pipedrive"])

# ── Cache-Layer ──────────────────────────────────────
# Personen-Stammdaten (Name, Bild, Firma) aendern extrem selten → 24h TTL
_person_cache: TTLCache = TTLCache(maxsize=2000, ttl=86400)
# Listen (Deals, Leads, Activities) aendern sich selten → 1h TTL
_list_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
# Pipelines/Stages aendern fast nie → 1h TTL
_static_cache: TTLCache = TTLCache(maxsize=50, ttl=3600)


def _get_pipedrive_client(user: User) -> PipedriveClient:
    """Pipedrive-Client aus User-Settings oder Env-Variablen erstellen."""
    settings = user.settings or {}
    token = settings.get("pipedrive_api_token") or ""
    domain = settings.get("pipedrive_domain") or "innosmith"

    if not token:
        from app.config import get_settings
        app_cfg = get_settings()
        token = app_cfg.pipedrive_api_token
        domain = app_cfg.pipedrive_domain or domain

    if not token:
        raise HTTPException(status_code=400, detail="Pipedrive API-Token nicht konfiguriert")

    return PipedriveClient(PipedriveConfig(api_token=token, company_domain=domain))


# ── Verbindungstest ──────────────────────────────────────

@router.get("/test-connection")
async def test_connection(user: User = Depends(require_role("owner"))):
    import logging
    logger = logging.getLogger("taskpilot.pipedrive")
    try:
        client = _get_pipedrive_client(user)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Pipedrive Client-Erstellung fehlgeschlagen")
        raise HTTPException(status_code=400, detail="Pipedrive-Client konnte nicht erstellt werden")
    try:
        result = await client.test_connection()
        return result
    except Exception as e:
        logger.exception("Pipedrive Verbindungstest fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Pipedrive-Verbindungstest fehlgeschlagen")


# ── Deals ────────────────────────────────────────────────

class DealSummary(BaseModel):
    id: int
    title: str
    status: str | None = None
    value: float | None = None
    currency: str | None = None
    stage_id: int | None = None
    person_name: str | None = None
    org_name: str | None = None


@router.get("/deals", response_model=list[DealSummary])
async def list_deals(
    pipeline_id: int | None = None,
    stage_id: int | None = None,
    status: str = "open",
    limit: int = Query(default=20, le=100),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"deals:{pipeline_id}:{stage_id}:{status}:{limit}"
    cached = _list_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_pipedrive_client(user)
    deals = await client.list_deals(
        pipeline_id=pipeline_id, stage_id=stage_id, status=status, limit=limit
    )
    result = [DealSummary(**{k: d.get(k) for k in DealSummary.model_fields}) for d in deals]
    _list_cache[cache_key] = result
    return result


@router.get("/deals/{deal_id}")
async def get_deal(deal_id: int, user: User = Depends(require_role("owner"))):
    client = _get_pipedrive_client(user)
    return await client.get_deal(deal_id)


# ── Leads ────────────────────────────────────────────────

class LeadSummary(BaseModel):
    id: str
    title: str
    person_id: int | None = None
    person_name: str | None = None
    organization_id: int | None = None
    org_name: str | None = None
    expected_close_date: str | None = None
    value: float | None = None
    currency: str | None = None


@router.get("/leads", response_model=list[LeadSummary])
async def list_leads(
    limit: int = Query(default=20, le=100),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"leads:{limit}"
    cached = _list_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_pipedrive_client(user)
    leads = await client.list_leads(limit=limit)
    result = []
    for lead in leads:
        val = lead.get("value") or {}
        result.append(LeadSummary(
            id=lead.get("id", ""),
            title=lead.get("title", ""),
            person_id=lead.get("person_id"),
            person_name=lead.get("person_name"),
            organization_id=lead.get("organization_id"),
            org_name=lead.get("organization_name"),
            expected_close_date=lead.get("expected_close_date"),
            value=val.get("amount") if isinstance(val, dict) else None,
            currency=val.get("currency") if isinstance(val, dict) else None,
        ))
    _list_cache[cache_key] = result
    return result


# ── Persons ──────────────────────────────────────────────

class PersonSummary(BaseModel):
    id: int
    name: str
    email: str | None = None
    org_name: str | None = None


@router.get("/persons", response_model=list[PersonSummary])
async def list_persons(
    limit: int = Query(default=20, le=100),
    user: User = Depends(require_role("owner")),
):
    client = _get_pipedrive_client(user)
    persons = await client.list_persons(limit=limit)
    result = []
    for p in persons:
        emails = p.get("email", [])
        email_str = emails[0].get("value", "") if isinstance(emails, list) and emails else ""
        result.append(PersonSummary(
            id=p.get("id"), name=p.get("name", ""), email=email_str,
            org_name=p.get("org_name"),
        ))
    return result


@router.get("/persons/{person_id}")
async def get_person(person_id: int, user: User = Depends(require_role("owner"))):
    client = _get_pipedrive_client(user)
    return await client.get_person(person_id)


# ── Activities ───────────────────────────────────────────

class ActivitySummary(BaseModel):
    id: int
    subject: str
    type: str | None = None
    done: bool | None = None
    due_date: str | None = None
    deal_id: int | None = None
    person_name: str | None = None


@router.get("/activities", response_model=list[ActivitySummary])
async def list_activities(
    done: bool | None = None,
    deal_id: int | None = None,
    person_id: int | None = None,
    limit: int = Query(default=20, le=100),
    user: User = Depends(require_role("owner")),
):
    cache_key = f"activities:{done}:{deal_id}:{person_id}:{limit}"
    cached = _list_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_pipedrive_client(user)
    acts = await client.list_activities(done=done, deal_id=deal_id, person_id=person_id, limit=limit)
    result = [ActivitySummary(**{k: a.get(k) for k in ActivitySummary.model_fields}) for a in acts]
    _list_cache[cache_key] = result
    return result


# ── Pipelines & Stages ──────────────────────────────────

class PipelineSummary(BaseModel):
    id: int
    name: str
    active: bool | None = None


class StageSummary(BaseModel):
    id: int
    name: str
    pipeline_id: int | None = None
    order_nr: int | None = None


@router.get("/pipelines", response_model=list[PipelineSummary])
async def list_pipelines(user: User = Depends(require_role("owner"))):
    cached = _static_cache.get("pipelines")
    if cached is not None:
        return cached
    client = _get_pipedrive_client(user)
    pls = await client.list_pipelines()
    result = [PipelineSummary(**{k: p.get(k) for k in PipelineSummary.model_fields}) for p in pls]
    _static_cache["pipelines"] = result
    return result


@router.get("/pipelines/{pipeline_id}/stages", response_model=list[StageSummary])
async def list_stages(pipeline_id: int, user: User = Depends(require_role("owner"))):
    cache_key = f"stages:{pipeline_id}"
    cached = _static_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _get_pipedrive_client(user)
    stages = await client.list_stages(pipeline_id=pipeline_id)
    result = [StageSummary(**{k: s.get(k) for k in StageSummary.model_fields}) for s in stages]
    _static_cache[cache_key] = result
    return result


# ── Pipeline-Summary (aggregiert) ────────────────────────

@router.get("/pipeline-summary")
async def get_pipeline_summary(
    pipeline_id: int | None = None,
    user: User = Depends(require_role("owner")),
):
    client = _get_pipedrive_client(user)
    return await client.get_pipeline_summary(pipeline_id=pipeline_id)


# ── Suche ────────────────────────────────────────────────

@router.get("/search")
async def search_crm(
    term: str = Query(min_length=1),
    item_types: str = "deal,person,organization",
    limit: int = Query(default=10, le=50),
    user: User = Depends(require_role("owner")),
):
    client = _get_pipedrive_client(user)
    return await client.search_items(term, item_types, limit)


# ── E-Mail-Lookup ────────────────────────────────────────

class PersonLookupResult(BaseModel):
    id: int
    name: str
    email: str | None = None
    org_name: str | None = None
    org_id: int | None = None
    phone: str | None = None
    pic_url: str | None = None
    open_deals_count: int = 0
    open_deals: list[DealSummary] = []


def _extract_pic_url(person: dict) -> str | None:
    """Profilbild-URL robust aus Pipedrive Person-Daten extrahieren."""
    pic = person.get("picture_id") or person.get("picture") or {}
    if isinstance(pic, dict):
        pics = pic.get("pictures", {})
        if isinstance(pics, dict):
            return pics.get("128") or pics.get("512") or pics.get("small") or None
        url = pic.get("url") or pic.get("value") or None
        if url:
            return url
    if isinstance(pic, str) and pic.startswith("http"):
        return pic
    pic_url_direct = person.get("pic_url") or person.get("picture_url") or None
    if pic_url_direct:
        return pic_url_direct
    return None


def _extract_email(person: dict) -> str:
    """Primaere E-Mail-Adresse robust aus Person extrahieren."""
    emails_raw = person.get("email", [])
    if isinstance(emails_raw, list) and emails_raw:
        first = emails_raw[0]
        return first.get("value", "") if isinstance(first, dict) else str(first)
    if isinstance(emails_raw, str):
        return emails_raw
    return person.get("primary_email", "") or ""


def _extract_phone(person: dict) -> str:
    """Primaere Telefonnummer aus Person extrahieren."""
    phones = person.get("phone", [])
    if isinstance(phones, list) and phones:
        first = phones[0]
        return first.get("value", "") if isinstance(first, dict) else str(first)
    return ""


@router.get("/lookup-email", response_model=PersonLookupResult | None)
async def lookup_email(
    email: str = Query(min_length=3),
    include_deals: bool = Query(default=False),
    user: User = Depends(require_role("owner")),
):
    """Person in Pipedrive anhand der E-Mail-Adresse suchen (24h-Cache)."""
    normalized = email.strip().lower()

    cached = _person_cache.get(normalized)
    if cached is not None:
        if include_deals and cached.open_deals_count == 0 and not cached.open_deals:
            pass  # Re-fetch deals if explicitly requested but not cached
        else:
            return cached

    client = _get_pipedrive_client(user)

    person_id: int | None = None

    try:
        results = await client.search_persons_by_email(normalized, 5)
        for item in results:
            item_data = item.get("item", item)
            pid = item_data.get("id")
            if pid:
                person_id = pid
                break
    except Exception as exc:
        logger.debug("search_persons_by_email fehlgeschlagen: %s", exc)

    if not person_id:
        try:
            results = await client.search_items(normalized, "person", 5)
            for item in results:
                item_data = item.get("item", item)
                pid = item_data.get("id")
                if pid:
                    person_id = pid
                    break
        except Exception as exc:
            logger.debug("search_items fallback fehlgeschlagen: %s", exc)

    if not person_id:
        _person_cache[normalized] = None
        return None

    try:
        person = await client.get_person_v1(person_id)
    except Exception:
        return None

    person_email = _extract_email(person)
    phone = _extract_phone(person)
    pic_url = _extract_pic_url(person)

    org = person.get("org_id") or {}
    org_name = org.get("name") if isinstance(org, dict) else person.get("org_name", "")
    org_id = org.get("value") if isinstance(org, dict) else None

    person_deals: list[DealSummary] = []
    if include_deals:
        try:
            deals = await client.list_deals(limit=20)
            person_deals = [
                DealSummary(**{k: d.get(k) for k in DealSummary.model_fields})
                for d in deals
                if d.get("person_id") == person_id or d.get("person_name") == person.get("name")
            ]
        except Exception:
            pass

    result = PersonLookupResult(
        id=person_id,
        name=person.get("name", ""),
        email=person_email,
        org_name=org_name,
        org_id=org_id,
        phone=phone,
        pic_url=pic_url,
        open_deals_count=len(person_deals),
        open_deals=person_deals[:3],
    )
    _person_cache[normalized] = result

    all_emails = _get_all_emails(person)
    for alt_email in all_emails:
        alt_norm = alt_email.strip().lower()
        if alt_norm and alt_norm != normalized:
            _person_cache[alt_norm] = result

    return result


def _get_all_emails(person: dict) -> list[str]:
    """Alle E-Mail-Adressen einer Person extrahieren fuer Cache-Aliase."""
    emails_raw = person.get("email", [])
    if isinstance(emails_raw, list):
        return [
            e.get("value", "") if isinstance(e, dict) else str(e)
            for e in emails_raw
            if (e.get("value", "") if isinstance(e, dict) else str(e))
        ]
    if isinstance(emails_raw, str) and emails_raw:
        return [emails_raw]
    return []


# ── Quick-Contact ────────────────────────────────────────

class QuickContactRequest(BaseModel):
    name: str
    email: str
    org_name: str | None = None
    phone: str | None = None


class QuickContactResponse(BaseModel):
    id: int
    name: str


@router.post("/quick-contact", response_model=QuickContactResponse)
async def create_quick_contact(
    body: QuickContactRequest,
    user: User = Depends(require_role("owner")),
):
    """Neuen Kontakt in Pipedrive anlegen."""
    client = _get_pipedrive_client(user)

    kwargs: dict = {"email": [{"value": body.email, "primary": True, "label": "work"}]}
    if body.phone:
        kwargs["phone"] = [{"value": body.phone, "primary": True, "label": "work"}]

    if body.org_name:
        orgs = await client.search_items(body.org_name, "organization", 1)
        if orgs:
            org_data = orgs[0].get("item", orgs[0])
            kwargs["org_id"] = org_data.get("id")
        else:
            pass

    person = await client.create_person(body.name, **kwargs)

    normalized = body.email.strip().lower()
    _person_cache.pop(normalized, None)

    return QuickContactResponse(id=person.get("id", 0), name=person.get("name", body.name))


# ── Batch Lookup ──────────────────────────────────────

class BatchLookupRequest(BaseModel):
    emails: list[str]


class BatchLookupItem(BaseModel):
    email: str
    person: PersonLookupResult | None = None


@router.post("/lookup-emails", response_model=list[BatchLookupItem])
async def batch_lookup_emails(
    body: BatchLookupRequest,
    user: User = Depends(require_role("owner")),
):
    """Mehrere E-Mail-Adressen in einem Request nachschlagen (Cache-aware)."""
    results: list[BatchLookupItem] = []
    uncached_emails: list[str] = []

    for raw_email in body.emails[:50]:
        normalized = raw_email.strip().lower()
        cached = _person_cache.get(normalized)
        if cached is not None:
            results.append(BatchLookupItem(email=raw_email, person=cached if cached else None))
        else:
            uncached_emails.append(raw_email)
            results.append(BatchLookupItem(email=raw_email, person=None))

    if uncached_emails:
        for email_addr in uncached_emails:
            try:
                person = await lookup_email(email=email_addr, include_deals=False, user=user)
                for item in results:
                    if item.email == email_addr:
                        item.person = person
                        break
            except Exception:
                pass

    return results


# ── LinkedIn Sync ─────────────────────────────────────

# Field-Key-Cache (LinkedIn-Feld-Key aendert sich nie)
_field_key_cache: TTLCache = TTLCache(maxsize=20, ttl=86400)


def _normalize_linkedin_url(url: str) -> str:
    """LinkedIn-URL normalisieren fuer zuverlaessigen Vergleich."""
    url = url.split("?")[0].rstrip("/")
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.lower()


async def _resolve_linkedin_field_key(client) -> str | None:
    """LinkedIn-Feld-Key dynamisch aus Pipedrive personFields ermitteln."""
    cached = _field_key_cache.get("linkedin_key")
    if cached:
        return cached
    try:
        fields = await client.list_person_fields()
        for f in fields:
            name = (f.get("field_name") or f.get("name") or "").lower().strip()
            if name in ("linkedin", "linkedin url", "linkedin-url", "linkedin profil"):
                key = f.get("field_code") or f.get("key")
                if key:
                    _field_key_cache["linkedin_key"] = key
                    return key
    except Exception as exc:
        logger.warning("Konnte LinkedIn-Field-Key nicht ermitteln: %s", exc)
    return None


async def _resolve_role_field_key(client) -> str | None:
    """Rolle-Feld-Key dynamisch aus Pipedrive personFields ermitteln."""
    cached = _field_key_cache.get("role_key")
    if cached:
        return cached
    try:
        fields = await client.list_person_fields()
        for f in fields:
            name = (f.get("field_name") or f.get("name") or "").lower().strip()
            if name in ("rolle", "role", "job title", "titel"):
                key = f.get("field_code") or f.get("key")
                if key:
                    _field_key_cache["role_key"] = key
                    return key
    except Exception as exc:
        logger.warning("Konnte Rolle-Field-Key nicht ermitteln: %s", exc)
    return None


class FieldDiff(BaseModel):
    field: str
    field_label: str
    old_value: str | None = None
    new_value: str | None = None


class MatchedPerson(BaseModel):
    person_id: int
    name: str
    org_name: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    pic_url: str | None = None
    pipedrive_url: str
    changes: list[FieldDiff] = []


class LinkedInLookupRequest(BaseModel):
    name: str
    linkedin_url: str | None = None
    org_name: str | None = None
    job_title: str | None = None
    profile_image_url: str | None = None


class LinkedInLookupResponse(BaseModel):
    match_type: str  # "linkedin_url" | "name" | "none"
    matches: list[MatchedPerson] = []


@router.post("/linkedin-lookup", response_model=LinkedInLookupResponse)
async def linkedin_lookup(
    body: LinkedInLookupRequest,
    user: User = Depends(require_role("owner")),
):
    """LinkedIn-Profil in Pipedrive suchen: zuerst per LinkedIn-URL, dann per Name."""
    client = _get_pipedrive_client(user)
    domain = (user.settings or {}).get("pipedrive_domain", "innosmith")
    linkedin_key = await _resolve_linkedin_field_key(client)
    role_key = await _resolve_role_field_key(client)

    def _build_pipedrive_url(pid: int) -> str:
        return f"https://{domain}.pipedrive.com/person/{pid}"

    def _get_field_value(person: dict, key: str | None) -> str | None:
        if not key:
            return None
        val = person.get(key)
        return str(val).strip() if val else None

    def _compute_diff(person: dict) -> list[FieldDiff]:
        diffs: list[FieldDiff] = []
        old_li = _get_field_value(person, linkedin_key) or ""
        new_li = body.linkedin_url or ""
        if new_li and _normalize_linkedin_url(old_li) != _normalize_linkedin_url(new_li):
            diffs.append(FieldDiff(field="linkedin_url", field_label="LinkedIn",
                                   old_value=old_li or None, new_value=new_li))

        old_role = _get_field_value(person, role_key) or ""
        new_role = body.job_title or ""
        if new_role and old_role.lower().strip() != new_role.lower().strip():
            diffs.append(FieldDiff(field="job_title", field_label="Rolle",
                                   old_value=old_role or None, new_value=new_role))

        old_org = ""
        org_data = person.get("org_id") or person.get("org_name")
        if isinstance(org_data, dict):
            old_org = org_data.get("name", "")
        elif isinstance(org_data, str):
            old_org = org_data
        new_org = body.org_name or ""
        if new_org and old_org.lower().strip() != new_org.lower().strip():
            diffs.append(FieldDiff(field="org_name", field_label="Organisation",
                                   old_value=old_org or None, new_value=new_org))

        old_pic = _extract_pic_url(person)
        if body.profile_image_url:
            label = "Profilbild aktualisieren" if old_pic else "Profilbild hinzufügen"
            if old_pic:
                diffs.append(FieldDiff(field="picture", field_label=label,
                                       old_value="vorhanden", new_value="neues Bild"))
            else:
                diffs.append(FieldDiff(field="picture", field_label=label,
                                       old_value=None, new_value="vorhanden"))
        return diffs

    def _person_to_match(person: dict) -> MatchedPerson:
        org = person.get("org_id") or {}
        org_name = org.get("name") if isinstance(org, dict) else person.get("org_name", "")
        return MatchedPerson(
            person_id=person.get("id", 0),
            name=person.get("name", ""),
            org_name=org_name,
            job_title=_get_field_value(person, role_key),
            linkedin_url=_get_field_value(person, linkedin_key),
            pic_url=_extract_pic_url(person),
            pipedrive_url=_build_pipedrive_url(person.get("id", 0)),
            changes=_compute_diff(person),
        )

    # Phase 1: Suche per LinkedIn-URL (eindeutigster Match)
    if body.linkedin_url and linkedin_key:
        slug = urlparse(body.linkedin_url).path.rstrip("/").split("/")[-1]
        if slug:
            try:
                results = await client.search_items(slug, "person", 10)
                for item in results:
                    item_data = item.get("item", item)
                    pid = item_data.get("id")
                    if pid:
                        person = await client.get_person_v1(pid)
                        stored_url = _get_field_value(person, linkedin_key) or ""
                        if _normalize_linkedin_url(stored_url) == _normalize_linkedin_url(body.linkedin_url):
                            return LinkedInLookupResponse(
                                match_type="linkedin_url",
                                matches=[_person_to_match(person)],
                            )
            except Exception as exc:
                logger.debug("LinkedIn-URL-Suche fehlgeschlagen: %s", exc)

    # Phase 2: Suche per Name
    try:
        results = await client.search_items(body.name, "person", 10)
        matches = []
        for item in results:
            item_data = item.get("item", item)
            pid = item_data.get("id")
            if pid:
                person = await client.get_person_v1(pid)
                matches.append(_person_to_match(person))
        if matches:
            return LinkedInLookupResponse(match_type="name", matches=matches)
    except Exception as exc:
        logger.debug("Name-Suche fehlgeschlagen: %s", exc)

    return LinkedInLookupResponse(match_type="none", matches=[])


def _decode_base64_image(raw: str) -> bytes | None:
    """Base64-String (optional mit data:…;base64, Prefix) in Bytes umwandeln."""
    if not raw:
        return None
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        data = base64.b64decode(raw)
        return data if len(data) > 500 else None
    except Exception as exc:
        logger.warning("Base64-Decode fehlgeschlagen: %s", exc)
        return None


def _ensure_min_size(image_data: bytes, min_px: int = 128, max_px: int = 512) -> bytes:
    """Bild auf min_px hochskalieren bzw. auf max_px herunterskalieren. Gibt JPEG zurück."""
    img = Image.open(io.BytesIO(image_data))
    img = img.convert("RGB")
    w, h = img.size
    if w < min_px or h < min_px:
        scale = max(min_px / w, min_px / h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        logger.info("Profilbild hochskaliert: %dx%d → %dx%d", w, h, img.width, img.height)
    elif w > max_px or h > max_px:
        scale = min(max_px / w, max_px / h)
        img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        logger.info("Profilbild herunterskaliert: %dx%d → %dx%d", w, h, img.width, img.height)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


class LinkedInSyncRequest(BaseModel):
    action: str  # "create" | "update"
    person_id: int | None = None
    name: str
    org_name: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    profile_image_url: str | None = None
    profile_image_base64: str | None = None
    update_fields: list[str] | None = None


class LinkedInSyncResponse(BaseModel):
    person_id: int
    name: str
    action: str  # "created" | "updated"
    pipedrive_url: str
    fields_updated: list[str] = []


@router.post("/linkedin-sync", response_model=LinkedInSyncResponse)
async def linkedin_sync(
    body: LinkedInSyncRequest,
    user: User = Depends(require_role("owner")),
):
    """LinkedIn-Kontakt in Pipedrive erstellen oder aktualisieren."""
    client = _get_pipedrive_client(user)
    domain = (user.settings or {}).get("pipedrive_domain", "innosmith")
    linkedin_key = await _resolve_linkedin_field_key(client)
    role_key = await _resolve_role_field_key(client)

    import httpx as _httpx

    async def _download_image(url: str) -> bytes | None:
        try:
            async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as http:
                resp = await http.get(url)
                if resp.status_code == 200 and len(resp.content) > 500:
                    return resp.content
        except Exception as exc:
            logger.warning("Bild-Download fehlgeschlagen (%s): %s", url[:80], exc)
        return None

    async def _resolve_org_id(org_name: str) -> int | None:
        """Organisation in Pipedrive finden — und bei Bedarf neu anlegen.

        Früher wurde ein unbekannter Firmenname stillschweigend verworfen:
        Die Person entstand ohne Organisation, die Antwort meldete das Feld
        aber trotzdem als gesetzt.
        """
        wanted = (org_name or "").strip()
        if not wanted:
            return None

        try:
            orgs = await client.search_items(wanted, "organization", 10)
        except Exception as exc:
            logger.warning("Organisationssuche fehlgeschlagen (%r): %s", wanted, exc)
            orgs = []

        # itemSearch ist unscharf — nur einen echten Namenstreffer übernehmen,
        # sonst landet die Person bei einer ähnlich benannten fremden Firma.
        for item in orgs:
            data = item.get("item", item)
            if (data.get("name") or "").strip().lower() == wanted.lower():
                return data.get("id")

        try:
            created = await client.create_organization(wanted)
            org_id = created.get("id")
            if org_id:
                logger.info("Pipedrive-Organisation neu angelegt: %r (id=%s)", wanted, org_id)
                return org_id
            logger.warning("Organisation %r angelegt, aber keine ID erhalten", wanted)
        except Exception as exc:
            logger.warning("Organisation %r konnte nicht angelegt werden: %s", wanted, exc)
        return None

    if body.action == "create":
        kwargs: dict = {}
        org_resolved = False

        if body.org_name:
            org_id = await _resolve_org_id(body.org_name)
            if org_id:
                kwargs["org_id"] = org_id
                org_resolved = True
            else:
                logger.warning(
                    "Organisation %r konnte weder gefunden noch angelegt werden", body.org_name
                )

        if body.linkedin_url:
            if linkedin_key:
                kwargs[linkedin_key] = body.linkedin_url
            else:
                logger.warning("linkedin_key ist None — LinkedIn-URL kann nicht geschrieben werden")
        if body.job_title:
            if role_key:
                kwargs[role_key] = body.job_title
            else:
                logger.warning("role_key ist None — Rolle kann nicht geschrieben werden")

        person = await client.create_person(body.name, **kwargs)
        person_id = person.get("id", 0)

        # Nur melden, was wirklich geschrieben wurde — sonst zeigt die Extension
        # Erfolg für Felder an, die leer geblieben sind.
        fields_created = ["name"]
        if org_resolved:
            fields_created.append("org")
        if linkedin_key and body.linkedin_url:
            fields_created.append("linkedin_url")
        if role_key and body.job_title:
            fields_created.append("job_title")

        if (body.profile_image_base64 or body.profile_image_url) and person_id:
            image_data = _decode_base64_image(body.profile_image_base64)
            if not image_data and body.profile_image_url:
                image_data = await _download_image(body.profile_image_url)
            if image_data:
                try:
                    image_data = _ensure_min_size(image_data)
                    await client.upload_person_picture(person_id, image_data)
                    fields_created.append("picture")
                except Exception as exc:
                    logger.warning("Profilbild-Upload fehlgeschlagen: %s", exc)

        _person_cache.clear()
        return LinkedInSyncResponse(
            person_id=person_id,
            name=person.get("name", body.name),
            action="created",
            pipedrive_url=f"https://{domain}.pipedrive.com/person/{person_id}",
            fields_updated=fields_created,
        )

    elif body.action == "update" and body.person_id:
        update_fields = set(body.update_fields or [])
        kwargs = {}
        updated: list[str] = []

        logger.info(
            "LinkedIn-Sync Update: person_id=%s, update_fields=%s, linkedin_key=%s, role_key=%s",
            body.person_id, update_fields, linkedin_key, role_key,
        )

        if "org_name" in update_fields and body.org_name:
            org_id = await _resolve_org_id(body.org_name)
            if org_id:
                kwargs["org_id"] = org_id
                updated.append("org_name")
            else:
                logger.warning(
                    "Organisation %r konnte weder gefunden noch angelegt werden", body.org_name
                )

        if "linkedin_url" in update_fields and body.linkedin_url:
            if linkedin_key:
                kwargs[linkedin_key] = body.linkedin_url
                updated.append("linkedin_url")
            else:
                logger.warning("linkedin_key ist None — LinkedIn-URL kann nicht geschrieben werden")

        if "job_title" in update_fields and body.job_title:
            if role_key:
                kwargs[role_key] = body.job_title
                updated.append("job_title")
            else:
                logger.warning("role_key ist None — Rolle kann nicht geschrieben werden")

        if kwargs:
            logger.info("Pipedrive update_person(%s): %s", body.person_id, list(kwargs.keys()))
            await client.update_person(body.person_id, **kwargs)

        if "picture" in update_fields and (body.profile_image_base64 or body.profile_image_url):
            image_data = _decode_base64_image(body.profile_image_base64)
            if image_data:
                logger.info("Profilbild aus Base64: %d bytes", len(image_data))
            if not image_data and body.profile_image_url:
                logger.info("Profilbild-Download von: %s", body.profile_image_url[:80])
                image_data = await _download_image(body.profile_image_url)

            if image_data:
                try:
                    image_data = _ensure_min_size(image_data)
                    await client.upload_person_picture(body.person_id, image_data)
                    updated.append("picture")
                    logger.info("Profilbild erfolgreich hochgeladen (%d bytes)", len(image_data))
                except Exception as exc:
                    logger.warning("Profilbild-Upload fehlgeschlagen: %s", exc)
            else:
                logger.warning("Kein gültiges Profilbild vorhanden")

        _person_cache.clear()
        return LinkedInSyncResponse(
            person_id=body.person_id,
            name=body.name,
            action="updated",
            pipedrive_url=f"https://{domain}.pipedrive.com/person/{body.person_id}",
            fields_updated=updated,
        )

    raise HTTPException(status_code=400, detail="Ungueltige Aktion oder fehlende person_id")


# ── Cache-Verwaltung ──────────────────────────────────

@router.post("/cache/clear")
async def clear_cache(user: User = Depends(require_role("owner"))):
    """Alle Pipedrive-Caches leeren."""
    _person_cache.clear()
    _list_cache.clear()
    _static_cache.clear()
    logger.info("Pipedrive-Caches manuell geleert")
    return {"status": "ok", "message": "Alle Caches geleert"}


@router.get("/cache/stats")
async def cache_stats(user: User = Depends(require_role("owner"))):
    """Cache-Statistiken anzeigen."""
    return {
        "person_cache": {"size": len(_person_cache), "maxsize": _person_cache.maxsize, "ttl": _person_cache.ttl},
        "list_cache": {"size": len(_list_cache), "maxsize": _list_cache.maxsize, "ttl": _list_cache.ttl},
        "static_cache": {"size": len(_static_cache), "maxsize": _static_cache.maxsize, "ttl": _static_cache.ttl},
    }
