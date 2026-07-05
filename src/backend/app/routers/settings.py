import secrets
from datetime import datetime, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_role
# Alias, weil dieses Modul einen Route-Handler ``get_settings`` definiert, der den
# Config-Getter sonst im Namespace ueberschattet (Coroutine statt Settings).
from app.config import get_settings as get_app_settings
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _merge_settings(db: AsyncSession, user: User, patch: dict) -> dict:
    """Mergt die übergebenen Keys atomar ins ``settings``-JSONB des Users.

    Statt das gesamte ``settings``-Objekt zu lesen, in Python zu verändern und
    komplett zurückzuschreiben (Read-Modify-Write), führt diese Funktion den Merge
    direkt in der Datenbank aus (``settings || patch``). Damit serialisiert
    PostgreSQL gleichzeitige Writes auf Zeilenebene und es gehen keine Felder mehr
    verloren, wenn mehrere Settings-PATCH-Requests kurz hintereinander/überlappend
    eintreffen (z.B. Zivilstand per ``onChange`` und Kanton per ``onBlur``).

    Keys mit Wert ``None`` werden aus dem JSONB entfernt, alle anderen gesetzt.
    """
    to_set = {k: v for k, v in patch.items() if v is not None}
    to_remove = [k for k, v in patch.items() if v is None]

    expr = func.coalesce(User.settings, cast({}, JSONB))
    if to_set:
        expr = expr.op("||")(cast(to_set, JSONB))
    for key in to_remove:
        expr = expr.op("-")(key)

    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(settings=expr)
        .execution_options(synchronize_session=False)
    )
    # ORM-Instanz mit dem frisch gemergten Wert synchronisieren (innerhalb der Transaktion).
    await db.refresh(user, attribute_names=["settings"])
    return dict(user.settings or {})


class UserSettings(BaseModel):
    agenda_background_url: str | None = None
    agenda_background_type: str | None = None
    task_detail_mode: str | None = None
    sidebar_collapsed: bool | None = None
    app_logo_url: str | None = None
    sidebar_color: str | None = None
    show_column_count: bool | None = None
    cockpit_background_url: str | None = None
    cockpit_background_type: str | None = None
    cockpit_calendar_exclude_categories: str | None = None
    cockpit_calendar_hide_private: bool | None = None
    creditors_overview_exclude_vendors: str | None = None
    inbox_background_url: str | None = None
    agents_background_url: str | None = None
    signale_background_url: str | None = None
    finance_background_url: str | None = None
    debtors_background_url: str | None = None
    creditors_background_url: str | None = None
    chat_background_url: str | None = None
    projects_background_url: str | None = None
    capacity_background_url: str | None = None
    project_sidebar_order: list[str] | None = None
    debtor_budgets: dict | None = None
    # Finanz-/Cashflow-Prognose
    default_hourly_rate: float | None = None
    forecast_pipeline_weight: float | None = None
    forecast_fill_horizon_months: int | None = None
    forecast_vat_rate: float | None = None
    annual_revenue_goal: float | None = None
    min_liquidity: float | None = None
    # Finanzanalysen: MWST-Methode + Steuer-Kontext
    vat_method: str | None = None        # "saldo" | "effektiv" | "none"
    vat_saldo_rate: float | None = None  # Saldosteuersatz (Dezimal, z.B. 0.062)
    tax_canton: str | None = None        # Sitz-/Wohnkanton (z.B. "Bern")
    civil_status: str | None = None      # Zivilstand (z.B. "verheiratet")
    # Briefings (Daily/Weekly/Monthly)
    briefing_daily_enabled: bool | None = None
    briefing_daily_time: str | None = None      # "HH:MM" lokal (nur Werktage)
    briefing_weekly_enabled: bool | None = None
    briefing_weekly_day: int | None = None      # 0=Montag … 6=Sonntag
    briefing_weekly_time: str | None = None
    briefing_monthly_enabled: bool | None = None
    briefing_monthly_time: str | None = None    # am letzten Kalendertag
    # Follow-up-Erkennung (unbeantwortete gesendete E-Mails)
    followup_enabled: bool | None = None
    followup_wait_days: int | None = None       # Arbeitstage bis Vorschlag


SETTINGS_FIELDS = [
    "agenda_background_url", "agenda_background_type", "task_detail_mode",
    "sidebar_collapsed", "app_logo_url", "sidebar_color",
    "show_column_count", "cockpit_background_url", "cockpit_background_type",
    "cockpit_calendar_exclude_categories", "cockpit_calendar_hide_private",
    "creditors_overview_exclude_vendors",
    "inbox_background_url", "agents_background_url", "signale_background_url",
    "finance_background_url", "debtors_background_url", "creditors_background_url",
    "chat_background_url", "projects_background_url", "capacity_background_url",
    "project_sidebar_order", "debtor_budgets",
    "default_hourly_rate", "forecast_pipeline_weight",
    "forecast_fill_horizon_months", "forecast_vat_rate",
    "annual_revenue_goal", "min_liquidity",
    "vat_method", "vat_saldo_rate", "tax_canton", "civil_status",
    "briefing_daily_enabled", "briefing_daily_time",
    "briefing_weekly_enabled", "briefing_weekly_day", "briefing_weekly_time",
    "briefing_monthly_enabled", "briefing_monthly_time",
    "followup_enabled", "followup_wait_days",
]


class BrandingSettings(BaseModel):
    app_logo_url: str | None = None
    sidebar_color: str | None = None


@router.get("/branding", response_model=BrandingSettings)
async def get_branding(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BrandingSettings:
    """Öffentliche Branding-Settings (Logo, Sidebar-Farbe) für alle Rollen."""
    owner = (await db.execute(select(User).where(User.role == "owner").limit(1))).scalar_one_or_none()
    if not owner or not owner.settings:
        return BrandingSettings()
    s = owner.settings
    return BrandingSettings(app_logo_url=s.get("app_logo_url"), sidebar_color=s.get("sidebar_color"))


@router.get("", response_model=UserSettings)
async def get_settings(
    user: User = Depends(require_role("owner")),
) -> UserSettings:
    s = user.settings or {}
    return UserSettings(**{f: s.get(f) for f in SETTINGS_FIELDS})


@router.patch("", response_model=UserSettings)
async def update_settings(
    body: UserSettings,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> UserSettings:
    merged = await _merge_settings(db, user, body.model_dump(exclude_unset=True))
    return UserSettings(**{f: merged.get(f) for f in SETTINGS_FIELDS})


# --- Triage-Einstellungen ---

class TriageSettings(BaseModel):
    triage_prompt: str | None = None
    triage_interval_seconds: int | None = None
    triage_enabled: bool | None = None
    inbox_hidden_folders: list[str] | None = None


class TriageSettingsResponse(TriageSettings):
    integrations_active_env: bool = True
    app_env: str = "prod"


TRIAGE_FIELDS = ["triage_prompt", "triage_interval_seconds", "triage_enabled", "inbox_hidden_folders"]


@router.get("/triage", response_model=TriageSettingsResponse)
async def get_triage_settings(
    user: User = Depends(require_role("owner")),
) -> TriageSettingsResponse:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")
    s = user.settings or {}
    cfg = get_app_settings()
    return TriageSettingsResponse(
        **{f: s.get(f) for f in TRIAGE_FIELDS},
        integrations_active_env=cfg.integrations_active,
        app_env=cfg.app_env,
    )


@router.put("/triage", response_model=TriageSettings)
async def update_triage_settings(
    body: TriageSettings,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> TriageSettings:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")
    merged = await _merge_settings(db, user, body.model_dump(exclude_unset=True))
    return TriageSettings(**{f: merged.get(f) for f in TRIAGE_FIELDS})


# --- Suchindex-Einstellungen (Ausschluss-Pfade) ---

class SearchIndexSettings(BaseModel):
    # OneDrive-Pfade (relativ zu Drive-Root), die NICHT indexiert werden sollen.
    excluded_paths: list[str] | None = None


class SearchIndexUpdateResponse(SearchIndexSettings):
    purged: int = 0  # Anzahl beim Speichern bereinigter Chunk-Zeilen


@router.get("/search-index", response_model=SearchIndexSettings)
async def get_search_index_settings(
    user: User = Depends(require_role("owner")),
) -> SearchIndexSettings:
    """Aktuelle Ausschlussliste (gespeichert oder Default)."""
    from app.services.semantic_index import _DEFAULT_EXCLUDED_PATHS

    s = user.settings or {}
    raw = s.get("search_excluded_paths")
    if "search_excluded_paths" in s and isinstance(raw, list):
        paths = [str(p) for p in raw]
    else:
        paths = list(_DEFAULT_EXCLUDED_PATHS)
    return SearchIndexSettings(excluded_paths=paths)


@router.put("/search-index", response_model=SearchIndexUpdateResponse)
async def update_search_index_settings(
    body: SearchIndexSettings,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> SearchIndexUpdateResponse:
    """Speichert die Ausschlussliste und bereinigt sofort bestehende Zeilen (keine Leichen)."""
    from app.services.semantic_index import purge_excluded_documents

    paths = [p.strip() for p in (body.excluded_paths or []) if p and p.strip()]
    await _merge_settings(db, user, {"search_excluded_paths": paths})
    purged = 0
    try:
        purged = await purge_excluded_documents(db, paths)
        await db.commit()
    except Exception:  # noqa: BLE001 - Speichern der Liste darf trotzdem gelingen
        await db.rollback()
    return SearchIndexUpdateResponse(excluded_paths=paths, purged=purged)


# --- Integrations-Einstellungen (Pipedrive etc.) ---

class IntegrationSettings(BaseModel):
    pipedrive_api_token: str | None = None
    pipedrive_domain: str | None = None
    toggl_api_token: str | None = None
    toggl_workspace_id: int | None = None
    bexio_api_token: str | None = None


class IntegrationSettingsResponse(IntegrationSettings):
    integrations_active_env: bool = True
    triage_enabled: bool = True
    app_env: str = "prod"


INTEGRATION_FIELDS = ["pipedrive_api_token", "pipedrive_domain", "toggl_api_token", "toggl_workspace_id", "bexio_api_token"]


def _mask_token(token: str) -> str:
    if not token:
        return ""
    return f"{'*' * (len(token) - 4)}{token[-4:]}" if len(token) > 4 else "****"


@router.get("/integrations", response_model=IntegrationSettingsResponse)
async def get_integration_settings(
    user: User = Depends(require_role("owner")),
) -> IntegrationSettingsResponse:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")
    s = user.settings or {}
    cfg = get_app_settings()
    return IntegrationSettingsResponse(
        pipedrive_api_token=_mask_token(s.get("pipedrive_api_token") or ""),
        pipedrive_domain=s.get("pipedrive_domain") or "innosmith",
        toggl_api_token=_mask_token(s.get("toggl_api_token") or ""),
        toggl_workspace_id=s.get("toggl_workspace_id"),
        bexio_api_token=_mask_token(s.get("bexio_api_token") or ""),
        integrations_active_env=cfg.integrations_active,
        triage_enabled=s.get("triage_enabled", True),
        app_env=cfg.app_env,
    )


@router.put("/integrations", response_model=IntegrationSettings)
async def update_integration_settings(
    body: IntegrationSettings,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> IntegrationSettings:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")
    patch: dict = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        # Maskierte Tokens (unverändert aus dem GET zurückgesendet) nicht überschreiben.
        if isinstance(value, str) and value.startswith("****"):
            continue
        patch[field] = value
    merged = await _merge_settings(db, user, patch)
    return IntegrationSettings(
        pipedrive_api_token=_mask_token(merged.get("pipedrive_api_token") or ""),
        pipedrive_domain=merged.get("pipedrive_domain") or "innosmith",
        toggl_api_token=_mask_token(merged.get("toggl_api_token") or ""),
        toggl_workspace_id=merged.get("toggl_workspace_id"),
        bexio_api_token=_mask_token(merged.get("bexio_api_token") or ""),
    )


class TriageTogglePayload(BaseModel):
    triage_enabled: bool


@router.patch("/integrations/triage-toggle")
async def toggle_triage(
    body: TriageTogglePayload,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Schaltet triage_enabled im Owner-Settings-JSONB um (Runtime-Toggle)."""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")
    await _merge_settings(db, user, {"triage_enabled": body.triage_enabled})
    return {"triage_enabled": body.triage_enabled}


# --- LLM-Einstellungen ---

class LlmProviderConfig(BaseModel):
    enabled: bool = False
    models: list[str] = []


class LlmSettingsPayload(BaseModel):
    llm_providers: dict[str, LlmProviderConfig] | None = None
    llm_default_model: str | None = None
    llm_default_local_model: str | None = None
    llm_default_temperature: float | None = None


LLM_FIELDS = ["llm_providers", "llm_default_model", "llm_default_local_model", "llm_default_temperature"]


@router.get("/llm", response_model=LlmSettingsPayload)
async def get_llm_settings(
    user: User = Depends(require_role("owner")),
) -> LlmSettingsPayload:
    s = user.settings or {}
    raw_providers = s.get("llm_providers")
    providers = None
    if raw_providers and isinstance(raw_providers, dict):
        providers = {
            k: LlmProviderConfig(**v) if isinstance(v, dict) else v
            for k, v in raw_providers.items()
        }
    return LlmSettingsPayload(
        llm_providers=providers,
        llm_default_model=s.get("llm_default_model"),
        llm_default_local_model=s.get("llm_default_local_model"),
        llm_default_temperature=s.get("llm_default_temperature"),
    )


@router.put("/llm", response_model=LlmSettingsPayload)
async def update_llm_settings(
    body: LlmSettingsPayload,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> LlmSettingsPayload:
    patch: dict = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "llm_providers" and isinstance(value, dict):
            serialized = {}
            for k, v in value.items():
                if isinstance(v, dict):
                    serialized[k] = v
                else:
                    serialized[k] = v.model_dump() if hasattr(v, "model_dump") else v
            patch[field] = serialized
        else:
            patch[field] = value
    current = await _merge_settings(db, user, patch)

    raw_providers = current.get("llm_providers")
    providers = None
    if raw_providers and isinstance(raw_providers, dict):
        providers = {
            k: LlmProviderConfig(**v) if isinstance(v, dict) else v
            for k, v in raw_providers.items()
        }
    return LlmSettingsPayload(
        llm_providers=providers,
        llm_default_model=current.get("llm_default_model"),
        llm_default_local_model=current.get("llm_default_local_model"),
        llm_default_temperature=current.get("llm_default_temperature"),
    )


# --- Extension API-Key ---

API_KEY_PREFIX = "tpk_"


class ApiKeyResponse(BaseModel):
    api_key: str
    created_at: str


class ApiKeyStatus(BaseModel):
    has_key: bool
    created_at: str | None = None


@router.get("/extension-api-key", response_model=ApiKeyStatus)
async def get_api_key_status(
    user: User = Depends(require_role("owner")),
) -> ApiKeyStatus:
    """Pruefen ob ein Extension-API-Key existiert."""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")
    s = user.settings or {}
    has_key = bool(s.get("extension_api_key_hash"))
    return ApiKeyStatus(
        has_key=has_key,
        created_at=s.get("extension_api_key_created_at"),
    )


@router.post("/extension-api-key", response_model=ApiKeyResponse)
async def generate_api_key(
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    """Neuen Extension-API-Key generieren (invalidiert vorherigen)."""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")

    raw_key = secrets.token_hex(32)
    api_key = f"{API_KEY_PREFIX}{raw_key}"

    key_hash = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).isoformat()

    await _merge_settings(db, user, {
        "extension_api_key_hash": key_hash,
        "extension_api_key_created_at": now,
    })

    return ApiKeyResponse(api_key=api_key, created_at=now)


@router.delete("/extension-api-key")
async def revoke_api_key(
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Extension-API-Key widerrufen."""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner")

    await _merge_settings(db, user, {
        "extension_api_key_hash": None,
        "extension_api_key_created_at": None,
    })

    return {"ok": True, "message": "API-Key widerrufen"}
