"""Router für verfügbare LLM-Modelle — dynamisch von Ollama + Cloud-APIs."""

import logging
import re
import time

import httpx
import litellm
from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user, require_role
from app.config import get_settings
from app.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])

_cache: dict = {"data": None, "expires_at": 0.0}
_litellm_caps_cache: dict = {"data": None, "expires_at": 0.0}
CACHE_TTL_SECONDS = 300


KNOWN_PERPLEXITY_MODELS = [
    ("sonar-pro", "Sonar Pro (Web Search)"),
    ("sonar", "Sonar (Web Search)"),
    ("sonar-deep-research", "Sonar Deep Research"),
    ("sonar-reasoning-pro", "Sonar Reasoning Pro"),
    ("sonar-reasoning", "Sonar Reasoning"),
]

KNOWN_GEMINI_RESEARCH_MODELS = [
    ("deep-research-preview-04-2026", "Gemini Deep Research"),
    ("deep-research-max-preview-04-2026", "Gemini Deep Research Max"),
]


async def _fetch_litellm_capabilities() -> dict[str, dict]:
    """LiteLLM Proxy /model/info abfragen fuer dynamische Capabilities (gecacht)."""
    now = time.time()
    if _litellm_caps_cache["data"] and now < _litellm_caps_cache["expires_at"]:
        return _litellm_caps_cache["data"]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{get_settings().litellm_base_url}/v1/model/info")
            if resp.status_code == 200:
                data = resp.json()
                caps = {}
                for m in data.get("data", []):
                    name = m["model_name"]
                    info = m.get("model_info", {})
                    params = info.get("supported_openai_params") or []
                    caps[name] = {
                        "supports_reasoning": info.get("supports_reasoning") is True,
                        "has_reasoning_effort": "reasoning_effort" in params,
                        "has_thinking_param": "thinking" in params,
                    }
                _litellm_caps_cache["data"] = caps
                _litellm_caps_cache["expires_at"] = now + CACHE_TTL_SECONDS
                return caps
    except Exception as e:
        logger.warning("LiteLLM Proxy nicht erreichbar fuer Capabilities: %s", e)

    return _litellm_caps_cache.get("data") or {}


def _get_capabilities(model_id: str, litellm_caps: dict | None = None) -> list[str]:
    """Welche Modi ein Modell unterstützt — dynamisch via LiteLLM-Proxy."""
    caps = ["chat"]
    provider = model_id.split("/")[0] if "/" in model_id else ""
    model = model_id.split("/")[-1] if "/" in model_id else model_id

    if provider == "gemini":
        caps.append("web_search")
        if "deep-research" in model or "thinking" in model:
            caps.append("deep_research")
    if provider == "perplexity":
        caps.append("web_search")
        if "deep-research" in model:
            caps.append("deep_research")

    if litellm_caps and model_id in litellm_caps:
        info = litellm_caps[model_id]
        if info.get("supports_reasoning") or info.get("has_reasoning_effort"):
            caps.append("thinking")
    else:
        try:
            if litellm.supports_reasoning(model=model_id):
                caps.append("thinking")
        except Exception:
            pass

    # Heuristik fuer brandneue Modelle, die LiteLLM noch nicht kennt.
    # Claude: Extended Thinking ab 3.7 sowie alle Opus/Sonnet/Haiku ab Generation 4
    # (opus-4, opus-5, sonnet-4.5, haiku-4.5, …) — generationsunabhaengig.
    if provider == "anthropic" and "thinking" not in caps:
        if "3-7-" in model or "3.7" in model or re.search(
            r"(?:opus|sonnet|haiku)-(?:[4-9]|\d{2,})", model
        ):
            caps.append("thinking")
    # Moderne OpenAI-Reasoning-Modelle (o-Serie, gpt-5 und neuere Generationen).
    if provider == "openai" and "thinking" not in caps:
        if model.startswith(("o1", "o3", "o4")) or re.match(r"gpt-([5-9]|\d{2,})", model):
            caps.append("thinking")

    # Duplikate entfernen, Reihenfolge erhalten
    return list(dict.fromkeys(caps))


def _make_entry(model_id: str, friendly_name: str, model_type: str, provider: str, litellm_caps: dict | None = None) -> dict:
    return {
        "id": model_id,
        "name": friendly_name,
        "type": model_type,
        "provider": provider,
        "capabilities": _get_capabilities(model_id, litellm_caps),
    }


async def _fetch_ollama_models(litellm_caps: dict | None = None) -> list[dict]:
    """Alle lokal verfuegbaren Ollama-Modelle abfragen."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{get_settings().ollama_base_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    name = m["name"]
                    model_id = f"ollama/{name}"
                    size_gb = m.get("size", 0) / (1024 ** 3)
                    size_label = f" ({size_gb:.0f}GB)" if size_gb > 1 else ""
                    friendly = name.replace(":", " ").replace("-", " ").title() + size_label
                    models.append(_make_entry(model_id, friendly, "local", "ollama", litellm_caps))
                return models
    except Exception as e:
        logger.warning("Ollama nicht erreichbar: %s", e)
    return []


async def _fetch_openai_models(litellm_caps: dict | None = None) -> list[dict]:
    """OpenAI-Modelle via /v1/models abfragen (kostenloser API-Call).

    Negativliste statt Prefix-Whitelist — neue Generationen (gpt-5, gpt-5.5, …)
    erscheinen automatisch, ohne die Config bei jedem Release nachzuziehen.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        return []

    # Chat-/Reasoning-Modelle behalten; Spezial-APIs und Embeddings ausblenden.
    EXCLUDE = (
        "instruct", "audio", "realtime", "search", "tts", "dall-e", "whisper",
        "embed", "moderation", "transcribe", "diarize", "image", "codex",
        "babbage", "davinci", "curie", "ada",
    )
    # Nur Chat-taugliche IDs: gpt-*, o1/o3/o4, chatgpt-*
    INCLUDE_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                models = []
                for m in data.get("data", []):
                    mid = m["id"]
                    if not any(mid.startswith(p) for p in INCLUDE_PREFIXES):
                        continue
                    if any(x in mid for x in EXCLUDE):
                        continue
                    model_id = f"openai/{mid}"
                    models.append(_make_entry(model_id, mid, "cloud", "openai", litellm_caps))
                models.sort(key=lambda x: x["id"])
                return models
    except Exception as e:
        logger.warning("OpenAI Models API nicht erreichbar: %s", e)
    return []


async def _fetch_gemini_models(litellm_caps: dict | None = None) -> list[dict]:
    """Google Gemini-Modelle via API abfragen (kostenloser Call)."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": settings.gemini_api_key},
            )
            if resp.status_code == 200:
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    if not name.startswith("models/gemini-"):
                        continue
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" not in methods:
                        continue
                    if any(x in name for x in ("tts", "robotics", "image", "customtools")):
                        continue
                    short = name.replace("models/", "")
                    model_id = f"gemini/{short}"
                    display = m.get("displayName", short)
                    models.append(_make_entry(model_id, display, "cloud", "gemini", litellm_caps))
                return models
    except Exception as e:
        logger.warning("Gemini Models API nicht erreichbar: %s", e)
    return []


def _anthropic_models_from_litellm(litellm_caps: dict | None = None) -> list[dict]:
    """Fallback: Anthropic-Modelle aus der (statischen) LiteLLM-Liste."""
    raw_models = litellm.models_by_provider.get("anthropic", [])
    models = []
    for mid in sorted(raw_models):
        model_id = f"anthropic/{mid}"
        friendly = mid.replace("-", " ").replace("20", " 20").strip()
        friendly = " ".join(w.capitalize() if not w[0].isdigit() else w for w in friendly.split())
        models.append(_make_entry(model_id, friendly, "cloud", "anthropic", litellm_caps))
    return models


async def _fetch_anthropic_models(litellm_caps: dict | None = None) -> list[dict]:
    """Anthropic-Modelle live via Models API (damit neue Modelle sofort erscheinen).

    Die LiteLLM-Liste ist statisch und hinkt neuen Releases (z. B. Opus 4.8)
    hinterher. Die Anthropic Models API liefert immer den aktuellen Stand.
    Bei Fehlern Fallback auf die LiteLLM-Liste.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        return []

    EXCLUDE = ("deprecated",)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                params={"limit": 100},
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                models = []
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if not mid or any(x in mid for x in EXCLUDE):
                        continue
                    model_id = f"anthropic/{mid}"
                    display = m.get("display_name") or mid
                    models.append(_make_entry(model_id, display, "cloud", "anthropic", litellm_caps))
                if models:
                    # Neueste zuerst (Anthropic liefert created_at-sortiert, neueste oben)
                    return models
            else:
                logger.warning("Anthropic Models API HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("Anthropic Models API nicht erreichbar: %s", e)

    return _anthropic_models_from_litellm(litellm_caps)


def _get_perplexity_models(litellm_caps: dict | None = None) -> list[dict]:
    """Bekannte Perplexity-Modelle (kein kostenloser List-Endpoint)."""
    settings = get_settings()
    if not settings.perplexity_api_key:
        return []
    return [
        _make_entry(f"perplexity/{mid}", name, "cloud", "perplexity", litellm_caps)
        for mid, name in KNOWN_PERPLEXITY_MODELS
    ]


def _get_gemini_research_models(litellm_caps: dict | None = None) -> list[dict]:
    """Gemini Deep Research Modelle (Interactions API)."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return []
    return [
        _make_entry(f"gemini/{mid}", name, "cloud", "gemini", litellm_caps)
        for mid, name in KNOWN_GEMINI_RESEARCH_MODELS
    ]


async def _fetch_all_models() -> dict:
    """Alle verfuegbaren Modelle von allen Providern sammeln."""
    import asyncio

    litellm_caps = await _fetch_litellm_capabilities()

    ollama_task = asyncio.create_task(_fetch_ollama_models(litellm_caps))
    openai_task = asyncio.create_task(_fetch_openai_models(litellm_caps))
    gemini_task = asyncio.create_task(_fetch_gemini_models(litellm_caps))
    anthropic_task = asyncio.create_task(_fetch_anthropic_models(litellm_caps))

    ollama_models = await ollama_task
    openai_models = await openai_task
    gemini_models = await gemini_task
    anthropic_models = await anthropic_task
    perplexity_models = _get_perplexity_models(litellm_caps)
    gemini_research_models = _get_gemini_research_models(litellm_caps)

    local_models = ollama_models
    # Reihenfolge: Anthropic -> Gemini -> OpenAI -> Perplexity -> Deep-Research.
    # Konsumenten, die ohne eigene Gruppierung rendern, erhalten so die richtige Sortierung.
    cloud_models = anthropic_models + gemini_models + openai_models + perplexity_models + gemini_research_models

    return {"local": local_models, "cloud": cloud_models}


@router.get("/available")
async def list_available_models(user: User = Depends(require_role("owner"))) -> dict:
    """Alle verfügbaren LLM-Modelle für die Einstellungsseite."""
    now = time.time()
    if _cache["data"] and now < _cache["expires_at"]:
        return _cache["data"]
    result = await _fetch_all_models()
    _cache["data"] = result
    _cache["expires_at"] = now + CACHE_TTL_SECONDS
    return result


@router.get("")
async def list_models(user: User = Depends(require_role("owner"))) -> dict:
    """Vom User aktivierte LLM-Modelle."""
    now = time.time()
    if not (_cache["data"] and now < _cache["expires_at"]):
        result = await _fetch_all_models()
        _cache["data"] = result
        _cache["expires_at"] = now + CACHE_TTL_SECONDS

    all_models = _cache["data"]
    settings = user.settings or {}
    llm_providers = settings.get("llm_providers", {})

    if not llm_providers:
        return all_models

    enabled_model_ids = set()
    active_providers = set()
    for provider_name, provider_config in llm_providers.items():
        if provider_config.get("enabled", False):
            active_providers.add(provider_name)
            enabled_model_ids.update(provider_config.get("models", []))

    if not enabled_model_ids:
        return all_models

    for m in all_models.get("local", []) + all_models.get("cloud", []):
        if "deep_research" in m.get("capabilities", []):
            if m["provider"] in active_providers:
                enabled_model_ids.add(m["id"])

    return {
        "local": all_models.get("local", []),
        "cloud": [m for m in all_models.get("cloud", []) if m["id"] in enabled_model_ids],
    }


@router.post("/cache/clear")
async def clear_cache(user: User = Depends(require_role("owner"))) -> dict:
    """Modell-Cache leeren, damit beim naechsten Abruf neu geladen wird."""
    _cache["data"] = None
    _cache["expires_at"] = 0.0
    return {"ok": True}
