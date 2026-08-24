"""Router für LLM-gestützte Finanzanalysen.

Pipeline: Finanz-Snapshot bauen -> (bei Cloud-Modell) anonymisieren ->
Treuhänder-Prompt zusammensetzen -> Prompt-Review -> LLM streamen (SSE) ->
de-anonymisieren -> persistieren.

Die Modellauswahl/-fähigkeiten kommen aus dem bestehenden LLM-Adapter
(/api/models); hier wird nur die Capability-Anforderung pro Analyse-Typ geführt.
"""

import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import litellm
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.auth.deps import require_role
from app.config import get_settings
from app.database import async_session, get_db
from app.models import FinanceAnalysis, FinanceDocument, User
from app.routers.uploads import _scan_with_clamav
from ai9 import content_converter as cc
from app.services import analysis_prompts as ap
from app.services import financial_snapshot as fs

litellm.drop_params = True

logger = logging.getLogger("taskpilot.analysis")
router = APIRouter(prefix="/api/analysis", tags=["analysis"])

# Temp-Verzeichnis fuer hochgeladene Finanzdokumente (Extraktion, dann geloescht)
_DOC_TMP_DIR = Path(tempfile.gettempdir()) / "taskpilot-finance-docs"
_DOC_TMP_DIR.mkdir(parents=True, exist_ok=True)

# Token-bewusste Obergrenze fuer extrahierten Dokumenttext
MAX_DOC_TEXT_CHARS = 60000

# Entitäten und Schwelle stehen in app/services/anon_politik.py -- siehe dort,
# warum diese Liste nicht mehr je Router ausgeschrieben wird.


def _setup_api_keys() -> None:
    """API-Keys als Env-Vars setzen, damit litellm sie findet."""
    s = get_settings()
    if s.openai_api_key:
        os.environ["OPENAI_API_KEY"] = s.openai_api_key
    if s.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = s.anthropic_api_key
    if s.gemini_api_key:
        os.environ["GEMINI_API_KEY"] = s.gemini_api_key
    if s.perplexity_api_key:
        os.environ["PERPLEXITYAI_API_KEY"] = s.perplexity_api_key


def _should_enable_thinking(model_id: str) -> bool:
    try:
        return litellm.supports_reasoning(model=model_id)
    except Exception:
        return False


def _is_gemini_deep_research(m: str) -> bool:
    return m.startswith("gemini/deep-research") or m in (
        "deep-research-preview-04-2026",
        "deep-research-max-preview-04-2026",
    )


_RUECKSTAND_HINWEIS = (
    "\n\n---\n\n"
    "> **Achtung -- erfundene Namen im Bericht.**\n"
    "> Dieser Bericht entstand mit einem Cloud-Modell und wurde dafuer maskiert. "
    "Beim Zurueckbilden blieben folgende Ersatzwerte stehen: {namen}.\n"
    "> Sie sehen echt aus und sind es nicht. Bitte pruefen, bevor der Bericht "
    "weiterverwendet wird.\n"
)


async def _deanonymize(text: str, session_id: str) -> str:
    """Bildet den Bericht zurueck und macht Rueckstaende im Text selbst sichtbar.

    Bis zum 24.08.2026 gab diese Funktion bei jedem Fehlschlag stillschweigend
    ihre Eingabe zurueck -- also den **maskierten** Text. Der wanderte dann als
    fertiger Bericht in die Datenbank, und weil die Maskierung Ersatznamen statt
    Platzhalter verwendet, las er sich vollkommen unauffaellig. Ein Bericht ueber
    «Hess & Partner», wo «InnoSmith GmbH» stehen muesste, gibt keinen Anlass zu
    zweifeln.

    Der Hinweis steht bewusst **im Berichtstext** und nicht nur im Log: Der
    Bericht wird gelesen, das Log nicht.
    """
    from app.services import anon_politik

    klar, rueckstaende = await anon_politik.bilde_zurueck(text, session_id)
    if rueckstaende:
        logger.warning("Analyse: Ersatzwerte nach der Rueckbildung: %s", rueckstaende[:5])
        return klar + _RUECKSTAND_HINWEIS.format(namen=", ".join(rueckstaende[:10]))
    return klar


async def _load_documents_md(document_ids: list[str] | None, user: User) -> str:
    """Laedt extrahierten Text der gewaehlten Finanzdokumente als Markdown-Abschnitt."""
    if not document_ids:
        return ""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(FinanceDocument).where(
                    FinanceDocument.id.in_(document_ids),
                    FinanceDocument.user_id == user.id,
                )
            )
        ).scalars().all()
    if not rows:
        return ""
    parts = [
        "## Hochgeladene Jahresrechnung / Finanzbeleg",
        "_Vom Berater bereitgestellter Originalbeleg (wird vor dem Versand an ein "
        "Cloud-Modell mit-anonymisiert). Nutze ihn zur Validierung und Anreicherung "
        "der abgeleiteten Kennzahlen (z. B. Saldovortrag, Eigenkapital, Bilanz)._",
        "",
    ]
    for r in rows:
        parts.append(f"### {r.label}")
        parts.append((r.extracted_text or "").strip())
        parts.append("")
    return "\n".join(parts)


async def _prepare(
    user: User,
    analysis_type: str,
    model: str,
    anonymize: bool | None,
    document_ids: list[str] | None = None,
) -> dict:
    """Baut Snapshot + Prompt; anonymisiert bei Bedarf. Kern für /prepare und /run."""
    from app.services.hermes_worker import _is_local_model

    type_def = ap.get_analysis_type(analysis_type)
    if not type_def:
        raise HTTPException(status_code=400, detail=f"Unbekannter Analyse-Typ: {analysis_type}")

    snapshot = await fs.build_snapshot(user, type_def["sections"])
    snapshot_md = snapshot["markdown"]

    # Optional: hochgeladene Jahresrechnung anhaengen (vor der Anonymisierung,
    # damit der Belegtext im Review sichtbar und mit-anonymisiert ist).
    doc_md = await _load_documents_md(document_ids, user)
    if doc_md:
        snapshot_md = f"{snapshot_md}\n\n{doc_md}"

    # Anonymisierung: Default = bei Cloud-Modellen an, lokal aus. Override möglich.
    is_local = _is_local_model(model)
    do_anon = (not is_local) if anonymize is None else bool(anonymize)

    session_id = ""
    diff: list[dict] = []
    restbestaende: list[str] = []
    md_for_prompt = snapshot_md
    if do_anon:
        from app.services import anon_politik

        try:
            md_for_prompt, session_id, diff, restbestaende = await anon_politik.maskiere(
                snapshot_md
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Anonymisierung fehlgeschlagen, breche ab: %s", e)
            raise HTTPException(
                status_code=503,
                detail="Anonymisierung nicht möglich (Content-Service nicht erreichbar). "
                "Bitte lokales Modell wählen oder Anonymisierung deaktivieren.",
            )
        if restbestaende:
            logger.warning("Analyse-Prompt: Restbestaende gemeldet: %s", restbestaende[:5])

    user_prompt = ap.build_user_prompt(analysis_type, md_for_prompt)
    return {
        "analysis_type": analysis_type,
        "title": type_def["title"],
        "model": model,
        "system_prompt": ap.SYSTEM_PROMPT,
        "prompt": user_prompt,
        "anonymized": do_anon,
        "session_id": session_id,
        "diff": diff,
        # Bruchstuecke echter Werte, die im maskierten Prompt stehen geblieben sind.
        # Hier wird gemeldet und nicht abgebrochen -- anders als beim Agent-Job, weil
        # dieser Weg beaufsichtigt ist: Der Prompt steht in der Vorschau, und erst
        # eine bewusste Freigabe schickt ihn hinaus. Die Entscheidung gehoert dem
        # Menschen, der den Text vor sich hat; ihm die Stelle zu zeigen ist mehr wert,
        # als ihm den Weg zu versperren.
        "restbestaende": restbestaende,
        "snapshot_meta": snapshot["meta"],
        "is_local": is_local,
        "document_ids": document_ids or [],
    }


# ── Endpoints ────────────────────────────────────────────

@router.get("/types")
async def list_types(user: User = Depends(require_role("owner"))):
    """Verfügbare Analyse-Typen für die Galerie."""
    return {"types": ap.ANALYSIS_TYPES}


# ── Hochgeladene Finanzdokumente (Jahresrechnung) ─────────

def _doc_meta(r: FinanceDocument) -> dict:
    return {
        "id": str(r.id),
        "label": r.label,
        "filename": r.filename,
        "mime": r.mime,
        "file_size": r.file_size,
        "text_chars": len(r.extracted_text or ""),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/documents")
async def list_documents(
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Listet die hochgeladenen Finanzdokumente des Owners (ohne Volltext)."""
    rows = (
        await db.execute(
            select(FinanceDocument)
            .where(FinanceDocument.user_id == user.id)
            .order_by(FinanceDocument.created_at.desc())
        )
    ).scalars().all()
    return {"documents": [_doc_meta(r) for r in rows]}


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    label: str | None = Form(default=None),
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Lädt ein Finanzdokument (PDF/DOCX) hoch, scannt es, extrahiert den Text.

    Es wird nur der extrahierte Text gespeichert (kein Binary) -- die
    Anonymisierung erfolgt erst pro Analyse-Lauf vor dem Versand an ein Cloud-LLM.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei")
    if not await _scan_with_clamav(data):
        raise HTTPException(status_code=400, detail="Datei hat den Virenscan nicht bestanden")

    suffix = Path(file.filename or "upload").suffix
    tmp_path = _DOC_TMP_DIR / f"{uuid.uuid4().hex}{suffix}"
    try:
        tmp_path.write_bytes(data)
        extracted = await cc.call_tool("extract_content", input_file=str(tmp_path))
    except RuntimeError:
        logger.exception("Text-Extraktion (Finanzdokument) fehlgeschlagen")
        raise HTTPException(status_code=503, detail="Content-Service nicht erreichbar")
    finally:
        tmp_path.unlink(missing_ok=True)

    text = (extracted if isinstance(extracted, str) else str(extracted)).strip()
    if not text:
        raise HTTPException(status_code=422, detail="Kein Text aus dem Dokument extrahierbar")
    if len(text) > MAX_DOC_TEXT_CHARS:
        text = text[:MAX_DOC_TEXT_CHARS] + "\n\n[... Text gekürzt ...]"

    row = FinanceDocument(
        label=(label or file.filename or "Dokument").strip(),
        filename=file.filename,
        mime=file.content_type,
        file_size=len(data),
        extracted_text=text,
        user_id=user.id,
    )
    db.add(row)
    await db.flush()
    return _doc_meta(row)


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Löscht ein hochgeladenes Finanzdokument."""
    row = (
        await db.execute(
            select(FinanceDocument).where(
                FinanceDocument.id == doc_id,
                FinanceDocument.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Dokument nicht gefunden")
    await db.delete(row)
    await db.flush()
    return {"ok": True}


@router.post("/prepare")
async def prepare_analysis(body: dict, user: User = Depends(require_role("owner"))):
    """Baut den (ggf. anonymisierten) Prompt für den Human-Review -- ohne LLM-Call.

    Showcase: zeigt den fertigen Prompt + die Anonymisierungs-Ersetzungen, bevor
    irgendetwas an ein Cloud-Modell gesendet wird.
    """
    analysis_type = body.get("analysis_type", "")
    model = body.get("model", "")
    anonymize = body.get("anonymize")
    document_ids = body.get("document_ids") or []
    if not model:
        raise HTTPException(status_code=400, detail="model fehlt")

    prep = await _prepare(user, analysis_type, model, anonymize, document_ids)
    return {
        "analysis_type": prep["analysis_type"],
        "title": prep["title"],
        "model": prep["model"],
        "system_prompt": prep["system_prompt"],
        "prompt": prep["prompt"],
        "anonymized": prep["anonymized"],
        "session_id": prep["session_id"],
        "diff": prep["diff"],
        "restbestaende": prep["restbestaende"],
        "snapshot_meta": prep["snapshot_meta"],
    }


@router.post("/run")
async def run_analysis(body: dict, user: User = Depends(require_role("owner"))):
    """Führt die Analyse aus und streamt den Report via SSE.

    Nimmt bevorzugt den reviewten Prompt aus /prepare entgegen (prompt + session_id),
    baut ihn sonst selbst.
    """
    analysis_type = body.get("analysis_type", "")
    model = body.get("model", "")
    if not model:
        raise HTTPException(status_code=400, detail="model fehlt")

    type_def = ap.get_analysis_type(analysis_type)
    if not type_def:
        raise HTTPException(status_code=400, detail=f"Unbekannter Analyse-Typ: {analysis_type}")

    user_prompt = body.get("prompt")
    system_prompt = body.get("system_prompt") or ap.SYSTEM_PROMPT
    session_id = body.get("session_id") or ""
    anonymized = bool(body.get("anonymized", False))
    snapshot_meta = body.get("snapshot_meta") or {}

    # Kein vorbereiteter Prompt -> jetzt bauen (z. B. direkter Aufruf ohne Review).
    if not user_prompt:
        prep = await _prepare(user, analysis_type, model, body.get("anonymize"), body.get("document_ids") or [])
        user_prompt = prep["prompt"]
        system_prompt = prep["system_prompt"]
        session_id = prep["session_id"]
        anonymized = prep["anonymized"]
        snapshot_meta = prep["snapshot_meta"]

    title = type_def["title"]
    user_id = user.id
    is_deep = _is_gemini_deep_research(model)

    async def _persist(status: str, report: str, thinking: str, tokens: int, cost: float | None, error: str | None) -> str:
        async with async_session() as db:
            row = FinanceAnalysis(
                analysis_type=analysis_type,
                title=title,
                model=model,
                anonymized=anonymized,
                status=status,
                prompt=user_prompt,
                report=report or None,
                thinking=thinking or None,
                snapshot_meta=snapshot_meta,
                tokens=tokens or None,
                cost_usd=cost,
                error_message=error,
                user_id=user_id,
            )
            db.add(row)
            await db.commit()
            return str(row.id)

    async def generate_deep_research():
        """Gemini Deep Research via Interactions API."""
        from app.services.gemini_research import stream_research

        full_response = ""
        full_thinking = ""
        gemini_model = model.replace("gemini/", "") if model.startswith("gemini/") else None
        try:
            async for event in stream_research(user_prompt, model=gemini_model):
                et = event.get("type")
                if et == "thought":
                    full_thinking += event["content"] + "\n"
                    yield {"event": "thinking", "data": json.dumps({"content": event["content"]})}
                elif et == "text":
                    full_response += event["content"]
                    yield {"event": "chunk", "data": json.dumps({"content": event["content"]})}
                elif et == "status":
                    yield {"event": "status", "data": json.dumps({"content": event["content"]})}
                elif et == "error":
                    aid = await _persist("failed", full_response, full_thinking, 0, None, event.get("content"))
                    yield {"event": "error", "data": json.dumps({"error": event.get("content"), "analysis_id": aid})}
                    return
                elif et == "done" and event.get("content") and not full_response:
                    full_response = event["content"]
        except Exception:
            logger.exception("Deep-Research-Analyse fehlgeschlagen")
            aid = await _persist("failed", full_response, full_thinking, 0, None, "Deep Research fehlgeschlagen")
            yield {"event": "error", "data": json.dumps({"error": "Deep Research fehlgeschlagen", "analysis_id": aid})}
            return

        if anonymized and session_id:
            full_response = await _deanonymize(full_response, session_id)
        analysis_id = await _persist("completed", full_response, full_thinking.strip(), 0, None, None)
        yield {"event": "done", "data": json.dumps({
            "analysis_id": analysis_id,
            "tokens": 0,
            "cost_usd": None,
            "content": full_response,
            "thinking": full_thinking.strip() or None,
            "model": model,
        })}

    async def generate():
        _setup_api_keys()
        full_response = ""
        full_thinking = ""
        total_tokens = 0
        cost_usd = 0.0

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        extra_params: dict = {}
        if not model.startswith("ollama/") and _should_enable_thinking(model):
            if model.startswith("anthropic/"):
                extra_params["thinking"] = {"type": "enabled", "budget_tokens": 8192}
            else:
                extra_params["thinking"] = {"type": "enabled"}

        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                temperature=1.0 if extra_params.get("thinking") else 0.4,
                stream=True,
                api_base=get_settings().ollama_base_url if model.startswith("ollama/") else None,
                **extra_params,
            )
            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta:
                    rc = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
                    if not rc and hasattr(delta, "thinking_blocks"):
                        blocks = delta.thinking_blocks or []
                        if blocks and isinstance(blocks, list):
                            b = blocks[0]
                            rc = b.get("thinking", "") if isinstance(b, dict) else getattr(b, "thinking", "")
                    if rc:
                        full_thinking += rc
                        yield {"event": "thinking", "data": json.dumps({"content": rc})}
                    if delta.content:
                        full_response += delta.content
                        yield {"event": "chunk", "data": json.dumps({"content": delta.content})}
                if hasattr(chunk, "usage") and chunk.usage:
                    total_tokens = getattr(chunk.usage, "total_tokens", 0) or 0
        except Exception:
            logger.exception("Analyse-Streaming fehlgeschlagen mit Modell %s", model)
            aid = await _persist("failed", full_response, full_thinking, total_tokens, None, "LLM-Streaming fehlgeschlagen")
            yield {"event": "error", "data": json.dumps({"error": "LLM-Streaming fehlgeschlagen", "analysis_id": aid})}
            return

        try:
            cost_usd = litellm.cost_calculator.completion_cost(
                model=model, prompt=str(messages), completion=full_response,
            )
        except Exception:
            cost_usd = 0.0

        # <think>-Tags abspalten (z. B. Perplexity)
        clean = full_response
        m = re.search(r"<think>(.*?)</think>", full_response, re.DOTALL)
        if m:
            if not full_thinking:
                full_thinking = m.group(1).strip()
            clean = re.sub(r"<think>.*?</think>\s*", "", full_response, flags=re.DOTALL).strip()

        if anonymized and session_id:
            clean = await _deanonymize(clean, session_id)

        analysis_id = await _persist(
            "completed", clean, full_thinking, total_tokens,
            round(cost_usd, 6) if cost_usd and cost_usd > 0 else None, None,
        )
        yield {"event": "done", "data": json.dumps({
            "analysis_id": analysis_id,
            "tokens": total_tokens,
            "cost_usd": round(cost_usd, 6) if cost_usd and cost_usd > 0 else None,
            "content": clean,
            "thinking": full_thinking or None,
            "model": model,
        })}

    if is_deep:
        return EventSourceResponse(generate_deep_research())
    return EventSourceResponse(generate())


@router.get("/history")
async def list_history(
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Liste vergangener Analysen (ohne Volltext-Report)."""
    res = await db.execute(
        select(FinanceAnalysis).order_by(FinanceAnalysis.created_at.desc()).limit(limit)
    )
    rows = res.scalars().all()
    return {
        "items": [
            {
                "id": str(r.id),
                "analysis_type": r.analysis_type,
                "title": r.title,
                "model": r.model,
                "anonymized": r.anonymized,
                "status": r.status,
                "tokens": r.tokens,
                "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
                "created_at": r.created_at.isoformat(),
                "preview": (r.report[:160] + "...") if r.report and len(r.report) > 160 else (r.report or ""),
            }
            for r in rows
        ]
    }


@router.get("/{analysis_id}")
async def get_analysis(
    analysis_id: uuid.UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(FinanceAnalysis).where(FinanceAnalysis.id == analysis_id))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Analyse nicht gefunden")
    return {
        "id": str(r.id),
        "analysis_type": r.analysis_type,
        "title": r.title,
        "model": r.model,
        "anonymized": r.anonymized,
        "status": r.status,
        "prompt": r.prompt,
        "report": r.report,
        "thinking": r.thinking,
        "snapshot_meta": r.snapshot_meta,
        "tokens": r.tokens,
        "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
        "error_message": r.error_message,
        "created_at": r.created_at.isoformat(),
    }


@router.delete("/{analysis_id}")
async def delete_analysis(
    analysis_id: uuid.UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(FinanceAnalysis).where(FinanceAnalysis.id == analysis_id))
    r = res.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="Analyse nicht gefunden")
    await db.delete(r)
    return {"ok": True}


@router.post("/{analysis_id}/export")
async def export_analysis(
    analysis_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Exportiert den Report als DOCX/PDF (Markdown via bestehende Export-Infra)."""
    res = await db.execute(select(FinanceAnalysis).where(FinanceAnalysis.id == analysis_id))
    r = res.scalar_one_or_none()
    if not r or not r.report:
        raise HTTPException(status_code=404, detail="Analyse/Report nicht gefunden")

    from app.services.document_export import ConvertOptions, convert_markdown

    fmt = body.get("format", "pdf")
    if fmt not in ("docx", "pdf", "pptx"):
        raise HTTPException(status_code=400, detail="Ungültiges Format")
    base = f"finanzanalyse-{r.analysis_type}-{r.created_at.date().isoformat()}"
    opts = ConvertOptions(format=fmt, title=r.title, author="InnoSmith", title_page=True, toc=True)
    return await convert_markdown(r.report, base, opts)
