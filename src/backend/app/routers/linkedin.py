"""Router für LLM-basierte LinkedIn-Profil-Extraktion.

Nimmt den Plaintext einer LinkedIn-Profilseite entgegen (via Content
Script innerText) und extrahiert strukturierte Profildaten via LLM.
Primärmodell ist Gemini Flash-Lite; bei Ausfall einmaliger Fallback
auf die vorherige Flash-Lite-Generation.

LinkedIn-Profildaten sind öffentlich → Cloud-LLMs sind erlaubt und
massiv schneller als lokale Modelle.

Hinweis: Der Aufruf geht bewusst in-process via litellm.acompletion
direkt an Google (nicht über den LiteLLM-Proxy). So bleibt die
Extraktion unabhängig vom Proxy-Image-Stand und dessen Modellkatalog.
"""

import json
import logging
import os
import re

import litellm
from fastapi import APIRouter, Depends, HTTPException
from litellm.exceptions import APIConnectionError, RateLimitError, Timeout
from pydantic import BaseModel, Field

from app.auth.deps import get_current_user, require_role
from app.config import get_settings
from app.models import User

logger = logging.getLogger("taskpilot.linkedin")
litellm.drop_params = True

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])

# Primär: günstigstes aktuelles Extraktions-Modell. Fallback: stabile Vorgänger-Generation.
LINKEDIN_EXTRACT_MODEL = "gemini/gemini-3.1-flash-lite"
LINKEDIN_EXTRACT_FALLBACK_MODEL = "gemini/gemini-2.5-flash-lite"

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "linkedin_profile",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Vollständiger Name (Vorname + Nachname)"},
                "headline": {"type": "string", "description": "Berufsbezeichnung / Headline"},
                "location": {"type": "string", "description": "Standort / Ort"},
                "job_title": {"type": "string", "description": "Aktuelle Berufsbezeichnung (ohne Firma)"},
                "companies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Liste der aktuellen Firmen / Organisationen",
                },
            },
            "required": ["name", "headline", "location", "job_title", "companies"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = (
    "Du extrahierst strukturierte Profildaten aus dem Plaintext einer LinkedIn-Profilseite. "
    "Antworte NUR mit validem JSON, kein anderer Text. Felder: "
    "name (Vorname + Nachname), headline (Berufsbezeichnung/Tagline wie auf dem Profil), "
    "location (Standort/Ort, z.B. 'Bern, Schweiz'), "
    "job_title (aktuelle Berufsbezeichnung aus dem Berufserfahrung/Experience-Bereich, NICHT die Headline), "
    "companies (Liste der aktuellen Firmen/Organisationen aus dem Berufserfahrung/Experience-Bereich). "
    "WICHTIG: job_title soll die konkrete Rolle aus der Berufserfahrung sein "
    "(z.B. 'Leiterin Internal Services'), nicht die Headline. "
    "Falls ein Feld nicht vorhanden ist, gib einen leeren String "
    "bzw. ein leeres Array zurück. Erfinde keine Daten."
)


def _setup_api_keys():
    """API-Keys aus Settings als Env-Vars setzen (wie chat.py)."""
    s = get_settings()
    if s.openai_api_key:
        os.environ["OPENAI_API_KEY"] = s.openai_api_key
    if s.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = s.gemini_api_key


def _clean_input(text: str) -> str:
    """Bereinigt Plaintext-Input: konsolidiert Whitespace, kürzt auf sinnvolle Länge."""
    return re.sub(r"\s{3,}", "\n", text).strip()


def _extract_json(text: str) -> dict:
    """Extrahiert JSON aus LLM-Output (auch aus Markdown-Code-Blöcken)."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1).strip())
    raise json.JSONDecodeError("Kein JSON gefunden", text, 0)


def _http_detail_for_llm_error(exc: Exception) -> str:
    """Übersetzt LLM-Fehler in eine sichere, verständliche Detail-Meldung."""
    if isinstance(exc, RateLimitError):
        return "LLM-Kontingent aufgebraucht oder Rate-Limit erreicht"
    if isinstance(exc, Timeout):
        return "LLM-Timeout bei der Profil-Extraktion"
    if isinstance(exc, APIConnectionError):
        return "LLM-Anbieter nicht erreichbar"
    if isinstance(exc, ValueError):
        return str(exc) or "LLM hat keinen Inhalt zurückgegeben"
    return "LinkedIn-Profil-Extraktion fehlgeschlagen"


async def _call_extract_model(model: str, clean_text: str):
    """Einzelner Extraktions-Call gegen ein Cloud-Modell (in-process, ohne Proxy)."""
    return await litellm.acompletion(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": clean_text},
        ],
        response_format=RESPONSE_FORMAT,
        temperature=0,
        timeout=30,
        # Thinking-Tokens zählen bei Gemini als Output — für Extraktion ausschalten.
        reasoning_effort="none",
    )


class ExtractProfileRequest(BaseModel):
    html: str = Field(..., min_length=50, max_length=500_000, description="Plaintext der LinkedIn-Profilseite (via innerText)")


class ExtractedProfile(BaseModel):
    name: str = ""
    headline: str = ""
    location: str = ""
    job_title: str = ""
    companies: list[str] = []
    extraction_method: str = "llm"


@router.post("/extract-profile", response_model=ExtractedProfile)
async def extract_profile_from_html(
    body: ExtractProfileRequest,
    user: User = Depends(require_role("owner")),
):
    """Extrahiert LinkedIn-Profildaten aus Plaintext via Cloud-LLM."""
    _setup_api_keys()

    clean_text = _clean_input(body.html)
    if len(clean_text) > 100_000:
        clean_text = clean_text[:100_000]

    models = [LINKEDIN_EXTRACT_MODEL, LINKEDIN_EXTRACT_FALLBACK_MODEL]
    last_exc: Exception | None = None

    for idx, model in enumerate(models):
        try:
            response = await _call_extract_model(model, clean_text)

            content = response.choices[0].message.content
            if not content:
                raise ValueError("LLM hat keinen Inhalt zurückgegeben")

            data = _extract_json(content)

            if idx > 0:
                logger.info("LinkedIn-Extraktion via Fallback-Modell %s", model)

            return ExtractedProfile(
                name=data.get("name", ""),
                headline=data.get("headline", ""),
                location=data.get("location", ""),
                job_title=data.get("job_title", ""),
                companies=data.get("companies", []),
                extraction_method="llm",
            )

        except json.JSONDecodeError as exc:
            # JSON-Parse-Fehler: kein Modell-Fallback — Antwort war da, aber unbrauchbar.
            logger.warning("LLM-Antwort war kein valides JSON (%s): %s", model, exc)
            raise HTTPException(
                status_code=503,
                detail="LLM-Antwort konnte nicht als JSON geparst werden",
            )
        except Exception as exc:
            last_exc = exc
            is_last = idx == len(models) - 1
            if is_last:
                logger.exception("LinkedIn-Profil-Extraktion fehlgeschlagen (Modell %s)", model)
                raise HTTPException(
                    status_code=503,
                    detail=_http_detail_for_llm_error(exc),
                )
            logger.warning(
                "LinkedIn-Extraktion mit %s fehlgeschlagen (%s), versuche Fallback %s",
                model,
                type(exc).__name__,
                models[idx + 1],
            )

    # Defensiv — die Schleife wirft immer; für den Typ-Checker.
    raise HTTPException(
        status_code=503,
        detail=_http_detail_for_llm_error(last_exc or RuntimeError("unbekannt")),
    )
