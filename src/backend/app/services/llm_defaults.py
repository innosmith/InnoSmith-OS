"""Zentrale Logik für Default-LLM-Modellwahl."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.principal import get_owner_settings

FALLBACK_LOCAL_MODEL = "ollama/qwen3.8:27b"


async def get_default_local_model(db: AsyncSession) -> str:
    """Liest llm_default_local_model aus den Owner-Settings.

    Zuständig für Chat-Fallback und Code-Ausführung -- also dort, wo die Einstellung
    tatsächlich steuert, welches Modell rechnet. Fehlt sie, gilt
    ``FALLBACK_LOCAL_MODEL`` (Install-Default).

    **Nicht** für die Beschriftung von Triage-Jobs. Der Worker folgt
    ``triage_model``, und bis zum 06.09.2026 schrieb ``triage.py`` trotzdem diesen
    Wert nach ``agent_jobs.llm_model``. Die Beschriftung folgte damit einer
    Einstellung, die den Lauf nicht beeinflusste: in drei Wochen vier verschiedene
    Modellnamen auf Jobs, die alle dasselbe Modell benutzten. Wer ein Feld
    "verwendetes Modell" nennt, muss das verwendete Modell hineinschreiben --
    sonst ist jede Auswertung darüber falsch, ohne es anzuzeigen.
    """
    settings = await get_owner_settings(db)
    return settings.get("llm_default_local_model") or FALLBACK_LOCAL_MODEL


def get_default_local_model_from_settings(settings: dict) -> str:
    """Synchrone Variante — wenn User-Settings bereits geladen sind."""
    return settings.get("llm_default_local_model") or FALLBACK_LOCAL_MODEL


async def get_draft_model(db: AsyncSession) -> str:
    """Schreib-Modell für Pass 2b. Leerer String = lokales Standardmodell.

    Der Berater entscheidet pro Betrieb, ob Antwort-Entwürfe von einem öffentlichen
    Modell geschrieben werden — nicht ein Deployment-Flag. Solange
    ``draft_cloud_enabled`` nicht gesetzt ist, bleibt der Schreib-Pass lokal, auch
    wenn ein Modellname hinterlegt ist. So kann ein Modell in Ruhe konfiguriert und
    geprüft werden, bevor es tatsächlich E-Mails formuliert.

    Reihenfolge: Owner-Settings vor ``TP_DRAFT_MODEL`` — die Umgebungsvariable ist
    nur noch der Startwert für eine frische Installation.
    """
    from app.config import get_settings

    settings = await get_owner_settings(db)
    if not settings.get("draft_cloud_enabled"):
        return ""
    model = (settings.get("draft_model") or "").strip()
    return model or (get_settings().draft_model or "").strip()
