"""Lokale Embeddings fuer das episodische Agent-Gedaechtnis.

Nutzt bewusst das lokale Ollama-Modell (Default ``Qwen3-Embedding-0.6B``, 1024-dim) statt eines
Cloud-Providers, damit auch vertrauliche Inhalte indexiert werden duerfen, ohne
die Datenklasse zu verletzen. Alle Funktionen sind **best-effort**: faellt Ollama
aus oder ist das Modell nicht vorhanden, geben sie ``None`` zurueck und der
aufrufende Pfad (Job-Verarbeitung) laeuft ungestoert weiter.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("taskpilot.embeddings")

# Qwen3-Embedding ist instruction-aware: ein Instruct-Prefix auf der QUERY-Seite
# (nicht auf der Dokument-Seite) verbessert Retrieval um 1-5 %. Instruktionen
# werden laut Qwen-Empfehlung auf Englisch formuliert, auch bei deutschen Inhalten.
QUERY_INSTRUCT = (
    "Instruct: Given a new email triage situation, retrieve similar past cases "
    "and how they were ultimately handled.\nQuery: "
)

# Query-Instruktion fuer die user-facing Dokument-/E-Mail-Suche (nicht Triage).
SEARCH_QUERY_INSTRUCT = (
    "Instruct: Given a search query, retrieve relevant documents and emails "
    "that answer or relate to it.\nQuery: "
)


async def embed_text(
    text: str,
    *,
    is_query: bool = False,
    model: str | None = None,
    dim: int | None = None,
    instruct: str | None = None,
) -> list[float] | None:
    """Erzeugt ein Embedding fuer ``text`` via lokalem Ollama.

    ``is_query=True`` stellt der Eingabe den instruction-aware Query-Prefix voran
    (nur fuer Recall-Anfragen; Dokumente/Episoden werden ohne Prefix eingebettet).
    ``model``/``dim`` erlauben einen zweiten Index (z. B. der staerkere
    Such-Index mit Qwen3-Embedding-4B/2560) neben dem Agent-Default (0.6B/1024).
    ``instruct`` ueberschreibt den Query-Prefix (Default: Triage-Instruktion).

    Gibt ``None`` zurueck, wenn Ollama nicht erreichbar ist, das Modell fehlt
    oder die Antwort unerwartet ist. Niemals Exceptions nach aussen werfen.
    """
    cfg = get_settings()
    use_model = model or cfg.embed_model
    use_dim = dim or cfg.embed_dim
    clean = (text or "").strip()
    if not clean:
        return None

    prefix = instruct if instruct is not None else QUERY_INSTRUCT
    prompt = (prefix + clean) if is_query else clean
    base = cfg.ollama_base_url.rstrip("/")
    payload = {"model": use_model, "prompt": prompt[:8000]}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base}/api/embeddings", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - best-effort, darf Job nicht stoppen
        logger.warning("Embedding fehlgeschlagen (Modell=%s): %s", use_model, exc)
        return None

    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        logger.warning("Embedding-Antwort ohne 'embedding'-Feld")
        return None

    if len(vec) != use_dim:
        logger.warning(
            "Embedding-Dimension %d != erwartete %d (Modell=%s) -- verworfen",
            len(vec), use_dim, use_model,
        )
        return None
    return [float(x) for x in vec]


def to_pgvector(vec: list[float]) -> str:
    """Formatiert einen Float-Vektor als pgvector-Literal ('[0.1,0.2,...]')."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
