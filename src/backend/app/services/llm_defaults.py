"""Zentrale Logik für Default-LLM-Modellwahl."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.principal import get_owner_settings

FALLBACK_LOCAL_MODEL = "ollama/qwen3.6:latest"


async def get_default_local_model(db: AsyncSession) -> str:
    """Liest llm_default_local_model aus den Owner-Settings.

    Wird systemweit genutzt für Triage, Agent-Jobs und Code-Execution,
    überall wo ein lokales Modell als Default benötigt wird.
    """
    settings = await get_owner_settings(db)
    return settings.get("llm_default_local_model") or FALLBACK_LOCAL_MODEL


def get_default_local_model_from_settings(settings: dict) -> str:
    """Synchrone Variante — wenn User-Settings bereits geladen sind."""
    return settings.get("llm_default_local_model") or FALLBACK_LOCAL_MODEL
