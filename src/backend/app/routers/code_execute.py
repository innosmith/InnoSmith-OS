"""Router für Code-Execution via Chat-Interface (Schicht 2).

Generiert Python-Code via LLM und führt ihn in der Docker-Sandbox aus.
Unterstützt zwei Flows:
1. generate_and_execute: LLM generiert Code aus natürlicher Sprache → Preview → Execute
2. execute_direct: Berater gibt Code direkt ein → Execute
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid

import mimetypes

import httpx
import litellm
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.auth.deps import get_current_user, require_role
from app.config import get_settings
from app.database import get_db, async_session
from app.models import User
from app.models.models import LlmConversation, LlmMessage

litellm.drop_params = True

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/code", tags=["code-execute"])

CODE_GENERATION_SYSTEM_PROMPT = """Du bist ein vielseitiger Python-Code-Generator. Du erzeugst sauberen,
eigenständig lauffähigen Python-Code für BELIEBIGE Zwecke: Datenanalyse, Automation,
Berechnungen, Text-/Datei-Verarbeitung, Generierung von Dokumenten, Grafiken, Spielen
oder interaktiven Web-Artefakten.

AUSGABEFORMAT:
- Antworte NUR mit dem Python-Code (kein Markdown, keine Erklärungen drumherum).
- Der Code muss ohne weitere Eingriffe lauffähig sein.

LAUFZEITUMGEBUNG (wichtig — daran halten):
- HEADLESS: Es gibt KEIN Terminal, KEIN Fenster, KEINE GUI, KEINE Tastatur/Maus in Echtzeit.
- Nutze KEIN input() und lies NICHT interaktiv von der Tastatur, AUSSER es wurden explizit
  Standard-Eingabedaten (stdin) bereitgestellt (siehe Hinweis in der Aufgabe). Dann darfst du
  sys.stdin/input() verwenden, um diese vorbereiteten Daten zu lesen.
- KEIN Netzwerkzugriff (keine API-Calls, keine Downloads, kein pip install zur Laufzeit).
- Verwende KEINE GUI-Toolkits (tkinter, pygame-Fenster, cv2.imshow o. Ä.) — sie haben kein Display.

INTERAKTIVE / GRAFISCHE AUFGABEN (Spiele, Visualisierungen, UIs):
- Erzeuge ein EIGENSTÄNDIGES HTML-Artefakt: Schreibe eine vollständige, in sich geschlossene
  Datei nach /workspace/ (z. B. /workspace/index.html oder /workspace/game.html) mit
  eingebettetem CSS + JavaScript (Canvas/DOM). Diese Datei wird dem Nutzer im Browser als
  spielbare/interaktive Vorschau angezeigt — dort funktionieren Tastatur, Maus und Animation.
- Der Python-Code baut also den HTML/JS-Inhalt und schreibt ihn in die Datei; er startet
  selbst KEINE Echtzeitschleife mit Tastatureingabe.
- Gib am Ende per print() kurz aus, welche Artefakt-Datei erzeugt wurde.

DATEIEN & AUSGABE:
- Eingabedateien (falls vorhanden) liegen read-only in /input/.
- Schreibe alle erzeugten Dateien (Bilder, HTML, CSV, XLSX, JSON, ...) nach /workspace/.
- Das aktuelle Arbeitsverzeichnis ist /workspace/ (persistent über Iterationen derselben
  Konversation — du kannst auf zuvor erzeugte Dateien aufbauen).
- Fasse Ergebnisse/Kennzahlen zusätzlich per print() zusammen.

VERFÜGBARE PACKAGES:
- Standardbibliothek + pandas, numpy, matplotlib (Agg-Backend, nur Dateiausgabe),
  seaborn, openpyxl, scipy, pyyaml, jinja2, tabulate, xlsxwriter.

SICHERHEIT & QUALITÄT:
- KEIN subprocess, os.system, exec(), eval(), requests, urllib, socket.
- Robustes Error-Handling mit try/except an den relevanten Stellen.
- Schweizer Deutsch in Kommentaren (ss statt ß)."""


# Verlaufsfenster für iteratives Coding. Erweitert (vorher 8×4000): moderne
# Coder-Modelle (32B, 32k+ Kontext) verkraften mehr Verlauf problemlos — das
# Modell sieht damit auch länger zurückliegende Iterationen derselben Session.
_HISTORY_LIMIT = 16
_HISTORY_MAX_CHARS = 8000

# Budget für angepinnte Konversations-Dokumente im Code-Prompt (zusätzlich
# zum Verlauf; strikt additiv, damit der Code-Modus keinen Rückschritt macht).
_PINNED_CONTEXT_BUDGET = 24_000


async def _load_history_messages(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    limit: int = _HISTORY_LIMIT,
) -> list[dict]:
    """Lädt die letzten Konversationsnachrichten als LLM-Message-Historie.

    Ermöglicht iteratives Coding: das Modell sieht die vorige Aufgabe, den zuletzt
    erzeugten Code und die letzte Ausführung (stdout/stderr) und kann darauf aufbauen.
    """
    result = await session.execute(
        select(LlmMessage)
        .where(LlmMessage.conversation_id == conversation_id)
        .order_by(LlmMessage.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()  # chronologisch
    history: list[dict] = []
    for m in rows:
        if m.role not in ("user", "assistant"):
            continue
        content = (m.content or "").strip()
        if not content:
            continue
        if len(content) > _HISTORY_MAX_CHARS:
            content = content[:_HISTORY_MAX_CHARS] + "\n… [gekürzt]"
        history.append({"role": m.role, "content": content})
    return history


async def _load_pinned_context_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> dict | None:
    """Angepinnte Konversations-Dokumente als zusätzliche Kontext-Message.

    Teilt die Kontext-Pipeline mit Chat/Agent: hat der Berater in dieser
    Konversation Dokumente angehängt (z. B. eine CSV-Beschreibung oder ein
    Anforderungsdokument), sieht der Code-Generator sie ebenfalls. Best-effort;
    bei Fehlern oder ohne Dokumente wird nichts injiziert (kein Rückschritt).
    """
    try:
        from app.services.conversation_context import (
            build_pinned_context_block,
            load_pinned_items,
        )

        items = await load_pinned_items(session, conversation_id)
        block = build_pinned_context_block(items, budget_chars=_PINNED_CONTEXT_BUDGET)
        if not block:
            return None
        return {
            "role": "user",
            "content": (
                "Kontext: Folgende Dokumente sind in dieser Konversation angehängt. "
                "Nutze sie als fachliche Referenz für den Code (sie liegen NICHT "
                "als Dateien in /input/, ausser sie sind dort explizit aufgeführt):\n\n"
                + block
            ),
        }
    except Exception:  # noqa: BLE001 - Kontext ist optional, Code-Flow hat Vorrang
        logger.warning("Angepinnter Kontext für Code-Modus konnte nicht geladen werden")
        return None


def _build_user_prompt(
    task_description: str,
    input_file_names: list[str] | None = None,
    has_stdin: bool = False,
) -> str:
    user_prompt = f"Aufgabe: {task_description}"
    if input_file_names:
        user_prompt += f"\n\nVerfügbare Input-Dateien in /input/: {', '.join(input_file_names)}"
    if has_stdin:
        user_prompt += (
            "\n\nEs wurden Standard-Eingabedaten (stdin) bereitgestellt. Lies sie bei Bedarf "
            "mit sys.stdin bzw. input()."
        )
    return user_prompt


async def _generate_code(
    task_description: str,
    model: str,
    input_file_names: list[str] | None = None,
    history: list[dict] | None = None,
    has_stdin: bool = False,
) -> str:
    """LLM aufrufen um Python-Code zu generieren (nicht-streamend, für /generate)."""
    settings = get_settings()

    if settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
    if settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    user_prompt = _build_user_prompt(task_description, input_file_names, has_stdin)

    messages = [{"role": "system", "content": CODE_GENERATION_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        temperature=0.2,
        api_base=get_settings().ollama_base_url if model.startswith("ollama/") else None,
    )

    code = response.choices[0].message.content.strip()
    return _extract_code(code)


def _artifacts_marker(scope: str | None, files: list[dict] | None) -> str:
    """Maschinenlesbarer Marker, damit erzeugte Artefakte auch nach Reload
    im Frontend gerendert werden können. Wird als HTML-Kommentar eingebettet
    (von der Markdown-Anzeige ignoriert, vom ChatPage vor dem Rendern geparst).
    Format: <!--tp-artifacts:SCOPE:name1|name2-->
    """
    if not scope or not files:
        return ""
    names = "|".join(f["name"] for f in files if f.get("name"))
    if not names:
        return ""
    return f"\n\n<!--tp-artifacts:{scope}:{names}-->"


def _extract_code(raw: str) -> str:
    """Extrahiert reinen Python-Code aus LLM-Antwort (entfernt Markdown-Fences)."""
    code = raw.strip()
    if code.startswith("```python"):
        code = code[len("```python"):].strip()
    if code.startswith("```"):
        code = code[3:].strip()
    if code.endswith("```"):
        code = code[:-3].strip()
    return code


async def _generate_code_streaming(
    task_description: str,
    model: str,
    input_file_names: list[str] | None = None,
    history: list[dict] | None = None,
    has_stdin: bool = False,
):
    """LLM-Streaming-Generator: yielded (event_type, content) Tupel.

    event_type: 'thinking' | 'token' | 'done'
    """
    settings = get_settings()

    if settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
    if settings.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    user_prompt = _build_user_prompt(task_description, input_file_names, has_stdin)

    messages = [{"role": "system", "content": CODE_GENERATION_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    response = await asyncio.wait_for(
        litellm.acompletion(
            model=model,
            messages=messages,
            temperature=0.2,
            stream=True,
            api_base=get_settings().ollama_base_url if model.startswith("ollama/") else None,
        ),
        timeout=120,
    )

    full_content = ""
    thinking_buffer = ""
    in_thinking = False

    async for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            continue

        # Reasoning/Thinking content (separate Felder bei manchen Providern)
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning:
            thinking_buffer += reasoning
            yield ("thinking", reasoning)
            continue

        text = delta.content or ""
        if not text:
            continue

        # Erkennung von <think>...</think> Blöcken (Ollama/Qwen)
        if "<think>" in text and not in_thinking:
            parts = text.split("<think>", 1)
            if parts[0]:
                full_content += parts[0]
                yield ("token", parts[0])
            in_thinking = True
            text = parts[1]

        if in_thinking:
            if "</think>" in text:
                parts = text.split("</think>", 1)
                thinking_buffer += parts[0]
                yield ("thinking", parts[0])
                in_thinking = False
                remainder = parts[1]
                if remainder:
                    full_content += remainder
                    yield ("token", remainder)
            else:
                thinking_buffer += text
                yield ("thinking", text)
        else:
            full_content += text
            yield ("token", text)

    yield ("done", full_content)


async def _execute_in_sandbox(
    code: str,
    input_files: dict | None = None,
    timeout: int = 300,
    stdin_data: str | None = None,
    workspace_key: str | None = None,
) -> dict:
    """Code in der Docker-Sandbox ausführen — via Sandbox-Executor-Sidecar.

    Das Backend hat bewusst KEINEN Docker-Zugriff. Die Ausführung läuft über den
    token-geschützten Sandbox-Executor (``TP_SANDBOX_EXECUTOR_URL``), der als
    einziger Dienst den Docker-Socket sieht.

    - ``stdin_data``: optionale Standard-Eingabe für das Programm.
    - ``workspace_key``: stabiler Schlüssel (z. B. Konversations-ID) für einen
      persistenten Workspace über mehrere Iterationen.
    """
    settings = get_settings()
    base_url = (settings.sandbox_executor_url or "").rstrip("/")
    token = settings.sandbox_executor_token

    if not base_url:
        return {
            "success": False, "stdout": "",
            "stderr": "Sandbox-Executor nicht konfiguriert (TP_SANDBOX_EXECUTOR_URL fehlt).",
            "generated_files": [], "run_id": None, "scope": None,
        }
    if not token:
        return {
            "success": False, "stdout": "",
            "stderr": "Sandbox-Executor nicht konfiguriert (TP_SANDBOX_EXECUTOR_TOKEN fehlt).",
            "generated_files": [], "run_id": None, "scope": None,
        }

    payload = {
        "code": code,
        "input_files": input_files,
        "timeout_seconds": timeout,
        "stdin_data": stdin_data,
        "workspace_key": workspace_key,
    }
    # HTTP-Timeout etwas über dem Sandbox-Timeout, damit der Executor selbst
    # sauber (via docker kill) abbrechen kann, bevor der Client aufgibt.
    http_timeout = httpx.Timeout(timeout + 30, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(
                f"{base_url}/execute",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        logger.exception("Sandbox-Executor nicht erreichbar (%s)", base_url)
        return {
            "success": False, "stdout": "",
            "stderr": f"Sandbox-Executor nicht erreichbar unter {base_url}: {e}",
            "generated_files": [], "run_id": None, "scope": None,
        }

    if resp.status_code != 200:
        detail = resp.text[:500]
        return {
            "success": False, "stdout": "",
            "stderr": f"Sandbox-Executor Fehler (HTTP {resp.status_code}): {detail}",
            "generated_files": [], "run_id": None, "scope": None,
        }

    return resp.json()


@router.post("/conversations/{conversation_id}/generate")
async def generate_code(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Code aus natürlicher Sprache generieren (ohne Ausführung).

    Gibt den generierten Code zurück. Berater kann ihn reviewen und dann
    via /execute bestätigen.
    """
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    from app.services.llm_defaults import get_default_local_model_from_settings

    task_description = body.get("content", "")
    user_settings = user.settings or {}
    code_model_fallback = user_settings.get("llm_default_local_model") or "ollama/qwen2.5-coder:32b"
    model = body.get("model") or code_model_fallback
    input_file_names = body.get("input_file_names", [])

    if not task_description:
        raise HTTPException(status_code=400, detail="content (Aufgabenbeschreibung) fehlt")

    # Historie VOR dem Anhängen der neuen Nutzernachricht laden (iteratives Coding).
    history = await _load_history_messages(db, conv.id)
    pinned_msg = await _load_pinned_context_message(db, conv.id)
    if pinned_msg:
        history = [pinned_msg, *history]
    has_stdin = bool((body.get("stdin_data") or "").strip())

    user_msg = LlmMessage(
        conversation_id=conv.id,
        role="user",
        content=task_description,
    )
    db.add(user_msg)
    await db.flush()

    try:
        code = await _generate_code(task_description, model, input_file_names, history, has_stdin)
    except Exception as e:
        logger.exception("Code-Generierung fehlgeschlagen")
        raise HTTPException(status_code=500, detail="Code-Generierung fehlgeschlagen")

    assistant_msg = LlmMessage(
        conversation_id=conv.id,
        role="assistant",
        content=f"```python\n{code}\n```",
        model=model,
    )
    db.add(assistant_msg)
    await db.commit()

    return {
        "code": code,
        "model": model,
        "message_id": str(assistant_msg.id),
        "conversation_id": str(conv.id),
        "status": "generated",
    }


@router.post("/conversations/{conversation_id}/execute")
async def execute_code(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Generierten oder manuellen Code in der Sandbox ausführen.

    Gibt das Ergebnis (stdout, stderr, Dateien) zurück.
    Dies ist das HITL-Gate: Der Berater hat den Code gesehen und bestätigt die Ausführung.
    """
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    code = body.get("code", "")
    input_files = body.get("input_files")
    stdin_data = body.get("stdin_data")
    timeout = min(body.get("timeout_seconds", 300), 900)

    if not code.strip():
        raise HTTPException(status_code=400, detail="Kein Code übergeben")

    logger.info("[code-execute] Sandbox-Ausführung gestartet (conv=%s, timeout=%ds)", conversation_id, timeout)

    exec_result = await _execute_in_sandbox(
        code, input_files, timeout, stdin_data=stdin_data, workspace_key=str(conv.id)
    )

    output_parts = []
    if exec_result["success"]:
        output_parts.append("**Ausführung erfolgreich**")
        if exec_result.get("stdout"):
            output_parts.append(f"```\n{exec_result['stdout']}\n```")
        if exec_result.get("generated_files"):
            files_list = ", ".join(f["name"] for f in exec_result["generated_files"])
            output_parts.append(f"Erzeugte Dateien: {files_list}")
        if exec_result.get("warning"):
            output_parts.append(f"Hinweis: {exec_result['warning']}")
    else:
        output_parts.append("**Ausführung fehlgeschlagen**")
        if exec_result.get("error"):
            output_parts.append(f"Fehler: {exec_result['error']}")
        if exec_result.get("stderr"):
            output_parts.append(f"```\n{exec_result['stderr']}\n```")

    result_content = "\n\n".join(output_parts) + _artifacts_marker(
        exec_result.get("scope"), exec_result.get("generated_files")
    )

    result_msg = LlmMessage(
        conversation_id=conv.id,
        role="assistant",
        content=result_content,
    )
    db.add(result_msg)
    await db.commit()

    return {
        "success": exec_result["success"],
        "stdout": exec_result.get("stdout", ""),
        "stderr": exec_result.get("stderr", ""),
        "generated_files": exec_result.get("generated_files", []),
        "scope": exec_result.get("scope"),
        "warning": exec_result.get("warning"),
        "duration_seconds": exec_result.get("duration_seconds", 0),
        "run_id": exec_result.get("run_id"),
        "message_id": str(result_msg.id),
    }


@router.post("/conversations/{conversation_id}/generate-and-execute")
async def generate_and_execute(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Code generieren UND ausführen in einem Schritt (für Chat-Flow mit impliziter Genehmigung).

    SSE-Stream: code_generated → executing → result
    """
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    task_description = body.get("content", "")
    user_settings = user.settings or {}
    code_model_fallback = user_settings.get("llm_default_local_model") or "ollama/qwen2.5-coder:32b"
    model = body.get("model") or code_model_fallback
    input_files = body.get("input_files")
    stdin_data = body.get("stdin_data")
    has_stdin = bool((stdin_data or "").strip())
    timeout = min(body.get("timeout_seconds", 300), 900)
    conv_id_str = str(conv.id)

    if not task_description:
        raise HTTPException(status_code=400, detail="content (Aufgabenbeschreibung) fehlt")

    async def generate():
        t_start = time.time()

        yield {"event": "status", "data": json.dumps({"phase": "connecting", "model": model})}

        async with async_session() as save_db:
            history = await _load_history_messages(save_db, uuid.UUID(conv_id_str))
            pinned_msg = await _load_pinned_context_message(save_db, uuid.UUID(conv_id_str))
            if pinned_msg:
                history = [pinned_msg, *history]
            user_msg = LlmMessage(
                conversation_id=uuid.UUID(conv_id_str),
                role="user",
                content=task_description,
            )
            save_db.add(user_msg)
            await save_db.commit()

        yield {"event": "status", "data": json.dumps({"phase": "generating", "model": model})}

        try:
            input_file_names = list(input_files.keys()) if input_files else []
            full_code = ""
            async for event_type, content in _generate_code_streaming(
                task_description, model, input_file_names, history, has_stdin
            ):
                if event_type == "thinking":
                    yield {"event": "thinking", "data": json.dumps({"content": content})}
                elif event_type == "token":
                    yield {"event": "token", "data": json.dumps({"content": content})}
                elif event_type == "done":
                    full_code = content

            code = _extract_code(full_code)
        except asyncio.TimeoutError:
            yield {"event": "error", "data": json.dumps({"error": "Timeout: LLM hat nicht innerhalb von 120s geantwortet. Ist Ollama aktiv?"})}
            return
        except Exception as e:
            logger.exception("Code-Generierung via SSE fehlgeschlagen")
            yield {"event": "error", "data": json.dumps({"error": "Code-Generierung fehlgeschlagen"})}
            return

        yield {"event": "code_generated", "data": json.dumps({"code": code, "model": model})}
        yield {"event": "status", "data": json.dumps({"phase": "executing"})}

        exec_task = asyncio.create_task(
            _execute_in_sandbox(
                code, input_files, timeout, stdin_data=stdin_data, workspace_key=conv_id_str
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {exec_task},
                    timeout=20.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if exec_task in done:
                    exec_result = exec_task.result()
                    break
                # SSE-Keepalive: ohne Bytes schliessen manche Proxies (nginx ~60s, Tunnel ~100s).
                yield {"event": "keepalive", "data": "{}"}
        except Exception as e:
            if not exec_task.done():
                exec_task.cancel()
            logger.exception("Sandbox-Ausführung fehlgeschlagen")
            yield {"event": "error", "data": json.dumps({"error": "Sandbox-Ausführung fehlgeschlagen"})}
            return

        async with async_session() as save_db:
            code_msg = LlmMessage(
                conversation_id=uuid.UUID(conv_id_str),
                role="assistant",
                content=f"```python\n{code}\n```",
                model=model,
            )
            save_db.add(code_msg)

            output_parts = []
            if exec_result["success"]:
                output_parts.append("**Ausführung erfolgreich**")
                if exec_result.get("stdout"):
                    output_parts.append(f"```\n{exec_result['stdout']}\n```")
                if exec_result.get("generated_files"):
                    files_list = ", ".join(f["name"] for f in exec_result["generated_files"])
                    output_parts.append(f"Erzeugte Dateien: {files_list}")
                if exec_result.get("warning"):
                    output_parts.append(f"Hinweis: {exec_result['warning']}")
            else:
                output_parts.append("**Ausführung fehlgeschlagen**")
                if exec_result.get("error"):
                    output_parts.append(f"Fehler: {exec_result['error']}")
                if exec_result.get("stderr"):
                    output_parts.append(f"```\n{exec_result['stderr']}\n```")

            result_content = "\n\n".join(output_parts) + _artifacts_marker(
                exec_result.get("scope"), exec_result.get("generated_files")
            )
            result_msg = LlmMessage(
                conversation_id=uuid.UUID(conv_id_str),
                role="assistant",
                content=result_content,
            )
            save_db.add(result_msg)
            await save_db.commit()

        yield {"event": "result", "data": json.dumps({
            "success": exec_result["success"],
            "stdout": exec_result.get("stdout", ""),
            "stderr": exec_result.get("stderr", ""),
            "generated_files": exec_result.get("generated_files", []),
            "scope": exec_result.get("scope"),
            "warning": exec_result.get("warning"),
            "duration_seconds": exec_result.get("duration_seconds", 0),
        })}

        yield {"event": "done", "data": json.dumps({
            "elapsed_s": round(time.time() - t_start, 1),
        })}

    return EventSourceResponse(generate())


# Für Vorschau (inline) erlaubte Content-Types. HTML/SVG werden im Frontend
# grundsätzlich in einem sandboxed <iframe> mit Null-Origin dargestellt.
_INLINE_TYPES = {
    "text/html", "image/svg+xml", "image/png", "image/jpeg", "image/gif",
    "image/webp", "text/plain", "application/json", "text/csv",
}

# Erlaubte Scope-Form (vom Sandbox-Executor vergeben): conv-<key> (persistent),
# run-<id> / script-<id> (ephemer). Strikte Whitelist, damit keine fremden
# Pfade adressierbar sind (Path-Traversal-Schutz zusaetzlich im Executor).
_ARTIFACT_SCOPE_RE = re.compile(r"^(?:conv|run|script)-[A-Za-z0-9_-]{1,72}$")


async def _proxy_executor_artifact(scope: str, name: str) -> Response:
    """Proxyt eine Artefakt-Datei token-authentifiziert vom Sandbox-Executor.

    Gemeinsame Logik fuer den konv-gebundenen und den scope-basierten Endpoint:
    validiert den Dateinamen, holt die Datei vom Executor und liefert sie mit
    gehaerteten Headern (CSP, nosniff) aus.
    """
    safe_name = os.path.basename(name)
    if not safe_name or safe_name != name:
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")

    settings = get_settings()
    base_url = (settings.sandbox_executor_url or "").rstrip("/")
    token = settings.sandbox_executor_token
    if not base_url or not token:
        raise HTTPException(status_code=503, detail="Sandbox-Executor nicht konfiguriert")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.get(
                f"{base_url}/artifacts/{scope}/{safe_name}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        logger.exception("Artefakt-Abruf fehlgeschlagen (%s)", base_url)
        raise HTTPException(status_code=502, detail=f"Executor nicht erreichbar: {e}")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Artefakt nicht gefunden")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Executor-Fehler (HTTP {resp.status_code})")

    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    disposition = "inline" if media_type in _INLINE_TYPES else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        # Defense-in-Depth: erzeugte Artefakte dürfen keine externen Ressourcen laden.
        "Content-Security-Policy": (
            "default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline' 'unsafe-eval'; font-src data:; media-src 'self' data: blob:; "
            "frame-ancestors 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=resp.content, media_type=media_type, headers=headers)


@router.get("/artifacts/{scope}/{name}")
async def get_artifact_by_scope(
    scope: str,
    name: str,
    user: User = Depends(require_role("owner")),
):
    """Liefert eine im Sandbox-Workspace erzeugte Datei anhand ihres Scopes aus.

    Owner-only. Deckt sowohl persistente Konversations-Workspaces (``conv-<id>``)
    als auch ephemere Ausfuehrungen (``run-<id>``) ab — genutzt vom Agent-Modus,
    dessen Sandbox-Tool den Scope maschinenlesbar zurueckmeldet. Der Scope wird
    strikt validiert; der Executor erzwingt zusaetzlich Path-Traversal-Schutz.
    """
    if not _ARTIFACT_SCOPE_RE.match(scope):
        raise HTTPException(status_code=400, detail="Ungültiger Scope")
    return await _proxy_executor_artifact(scope, name)


@router.get("/conversations/{conversation_id}/artifacts/{name}")
async def get_code_artifact(
    conversation_id: uuid.UUID,
    name: str,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Liefert eine im Sandbox-Workspace der Konversation erzeugte Datei aus.

    Owner-only. Der Scope wird serverseitig aus der Konversations-ID abgeleitet,
    sodass keine fremden Workspaces adressierbar sind. Das Backend proxyt die
    Datei token-authentifiziert vom Sandbox-Executor.
    """
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    safe_name = os.path.basename(name)
    if not safe_name or safe_name != name:
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")

    settings = get_settings()
    base_url = (settings.sandbox_executor_url or "").rstrip("/")
    token = settings.sandbox_executor_token
    if not base_url or not token:
        raise HTTPException(status_code=503, detail="Sandbox-Executor nicht konfiguriert")

    scope = f"conv-{conversation_id}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = await client.get(
                f"{base_url}/artifacts/{scope}/{safe_name}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        logger.exception("Artefakt-Abruf fehlgeschlagen (%s)", base_url)
        raise HTTPException(status_code=502, detail=f"Executor nicht erreichbar: {e}")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Artefakt nicht gefunden")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Executor-Fehler (HTTP {resp.status_code})")

    media_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    disposition = "inline" if media_type in _INLINE_TYPES else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        # Defense-in-Depth: erzeugte Artefakte dürfen keine externen Ressourcen laden.
        "Content-Security-Policy": (
            "default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline' 'unsafe-eval'; font-src data:; media-src 'self' data: blob:; "
            "frame-ancestors 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=resp.content, media_type=media_type, headers=headers)
