"""Hermes Agent-Runtime Worker: verarbeitet queued AgentJobs via Hermes AIAgent.

Ersetzt den fruheren Nanobot-Worker. Laeuft als Hintergrund-Task im FastAPI-
Backend und pollt alle 10s die ``agent_jobs``-Queue. Hermes ist synchron
(``AIAgent.run_conversation`` blockiert), deshalb wird jeder Job ueber
``asyncio.to_thread`` ausgefuehrt, damit der Event-Loop frei bleibt.

Architektur (Spike-validiert, siehe docs/hermes-vs-nanobot-entscheidung.md):
- Persistenter ``AIAgent`` pro Worker (Provider ``custom`` -> Ollama ``/v1``).
- MCP-Tools werden einmalig via ``discover_mcp_tools()`` registriert (eigener
  Hintergrund-Event-Loop in Hermes) und vom Agent automatisch genutzt.
- Nach der LLM-Klassifikation laeuft dieselbe deterministische Post-Processing-
  Logik wie zuvor (JSON parsen, Task erstellen, Draft zuordnen).

Transparenz: ``reasoning_callback`` (echtes Thinking) und die Tool-Callbacks
werden in einen Job-Trace geschrieben (``metadata_json['trace']``), damit man
in der Agent-Queue nachvollziehen kann, was der Agent gedacht und getan hat.

Thinking-Politik: Standardmaessig AN (Transparenz + Demo). Der Disable-Hebel
fuer qwen3.5/3.6 ist ``extra_body.chat_template_kwargs.enable_thinking=False``
(``/no_think`` funktioniert in dieser Modellgeneration NICHT). Er ist als
opt-in-Policy vorbereitet (``_thinking_disabled``), aber bewusst nicht im
Default-Pfad scharfgeschaltet, da das Verhalten ueber Ollama ``/v1``
versionsabhaengig ist und vor Aktivierung live verifiziert werden muss.
"""

import ast
import asyncio
import json
import logging
import os
import re
import time
import uuid

import httpx
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import async_session
from app.models import (
    AgentFeedback,
    AgentJob,
    BoardColumn,
    CapacityTimeOff,
    ChatTriage,
    ChecklistItem,
    EmailTriage,
    MeetingTranscript,
    Project,
    Task,
    User,
)
from app.services.hermes_config import (
    get_hermes_home,
    populate_hermes_env,
    write_hermes_config,
)
from app.services.draft_prompt import (
    render_dossier_block,
    render_draft_task,
    render_gather_task,
)
from app.services.learning import has_content_between_greeting_and_closing, record_episode
from app.services.notification import (
    notify_agent_awaiting_approval,
    notify_agent_completed,
    notify_chat_triage_task,
    notify_task_suggested,
)
from app.services.triage_labels import (
    FALLBACK_LABEL,
    NO_CATEGORY,
    TRIAGE_LABELS,
    move_target,
    normalize_label,
)
from app.core.principal import get_owner_settings, system_principal_id

logger = logging.getLogger("taskpilot.hermes_worker")

POLL_INTERVAL = 10
REAP_INTERVAL = 60
DRAFT_CLEANUP_INTERVAL = 300  # 5 Minuten
RESWEEP_INTERVAL = 3600  # 60 Minuten: still durchgefallene Triages erneut einreihen
STALE_TIMEOUT_MINUTES = 30
# Maximale Anzahl automatischer Re-Triagen pro E-Mail (verhindert Endlosschleifen).
MAX_RESWEEP = 2
# Nur frische Mails resweepen. Aeltere Mails sind im Postfach oft verschoben/geloescht
# (-> get_email 404) und erzeugten nur Churn + "neue" Vorschlaege aus alten Items.
RESWEEP_MAX_AGE_DAYS = 7

HERMES_HOME = get_hermes_home()
# Hermes-native Skills (Progressive Disclosure via skill_view). Der Worker laedt sie
# nicht mehr als String, sondern weist den Agenten an, sie selbst zu laden.
EMAIL_TRIAGE_SKILL = HERMES_HOME / "skills" / "email-triage" / "SKILL.md"
EMAIL_TRIAGE_REFERENCES = HERMES_HOME / "skills" / "email-triage" / "references"
EMAIL_STYLE_SKILL = HERMES_HOME / "skills" / "email-style" / "SKILL.md"
# Legacy-Fallbacks (Flat-Dateien aus der Nanobot-Aera) -- nur falls die nativen
# Skills (noch) nicht ausgerollt sind. Werden nach erfolgreicher Migration entfernt.
LEGACY_TRIAGE_SKILL = HERMES_HOME / "skills" / "mail-triage.md"
LEGACY_STYLE_PROFILE = HERMES_HOME / "schreibstil-anthony.md"
# Rueckwaerts-Kompatibilitaet: einzelne Module/Tests referenzieren diese Namen noch.
TRIAGE_SKILL = EMAIL_TRIAGE_SKILL
STYLE_PROFILE = EMAIL_STYLE_SKILL

# Mapping alter (Nanobot-)Skill-Namen auf die neuen Hermes-Skill-Verzeichnisse.
# Generische AgentJobs koennen noch die alten Namen in ``metadata.skill`` tragen.
_SKILL_NAME_ALIASES: dict[str, str] = {
    "mail-triage": "email-triage",
    "crm-assistant": "crm-pipedrive",
    "signa-recherche": "signa-research",
}

PIPELINE_COLUMNS = {
    "focus": "a0000000-0000-0000-0000-000000000001",
    "this_week": "a0000000-0000-0000-0000-000000000002",
    "next_week": "a0000000-0000-0000-0000-000000000003",
    "this_month": "a0000000-0000-0000-0000-000000000005",
}

WORKER_SYSTEM_PROMPT = (
    "Du bist der TaskPilot-Agent von Anthony Smith (InnoSmith GmbH, Schweiz). "
    "Du nutzt deine MCP-Tools aktiv und behauptest nie, keinen Zugriff zu haben. "
    "Befolge die Instruktionen in der Nachricht exakt und Schritt fuer Schritt. "
    "Wenn du eine dauerhaft gueltige Tatsache ueber Anthony, einen Absender oder "
    "eine Arbeitsweise lernst (z. B. eine stabile Praeferenz oder Triage-Regel), "
    "halte sie knapp mit dem memory-Tool fest, damit sie kuenftig verfuegbar ist. "
    "Sprache: Schweizer Hochdeutsch. Verbindlich: immer 'ss' statt 'ß' und korrekte "
    "Umlaute 'ä'/'ö'/'ü' -- NIEMALS die Umschreibungen 'ae'/'oe'/'ue'. "
    "Schreibe natuerliches, fehlerfreies Deutsch ohne englische Brocken oder erfundene Woerter. "
    "Ton: freundlich, klar und direkt, aber nie forsch oder schroff gegenueber Kunden. "
    "Zeitzone: Europe/Zurich."
)

# ── Runtime-State ────────────────────────────────────────
_worker_task: asyncio.Task | None = None
_agent = None  # persistenter Worker-AIAgent (volle Allowlist)
_triage_agent = None  # persistenter Triage-AIAgent (reduzierte Allowlist, Paket C)
_gather_agent = None  # persistenter Recherche-AIAgent (Pass 2a, Fachsystem-Zugang)
_runtime_ready = False
_runtime_lock: asyncio.Lock | None = None
_trajectory_shim_installed = False

# Trace-Sink fuer den aktuellen Job (Worker verarbeitet sequentiell).
_job_trace: list[dict] = []
_MAX_TRACE_EVENTS = 200
# Vom Budget reserviert: Tool-Events sind fuer die Nachvollziehbarkeit wichtiger
# als Denk-Text und duerfen von ihm nicht verdraengt werden.
_RESERVED_TOOL_EVENTS = 60
# Obergrenze pro gebuendeltem Denk-Event (siehe ``_on_reasoning``).
_MAX_THINKING_CHARS = 4000

# Echte Outlook-Draft-ID des aktuellen Jobs, deterministisch aus dem
# create_draft-Tool-Ergebnis erfasst. NIEMALS die vom LLM in den JSON-Block
# abgetippte ID verwenden -- lange Graph-IDs (~152 Zeichen) werden vom Modell
# verstuemmelt, was den spaeteren get_email-Abruf (Snapshot/Cleanup/Preview)
# scheitern laesst und die Freigabe-Karte aus dem Cockpit verschwinden laesst.
_job_created_draft_id: str | None = None

# Neue Message-ID, falls die E-Mail im aktuellen Job per move_email_to_folder
# verschoben wurde. Ein Move aendert die Graph-Message-ID (Graph liefert die neue
# ID als ``new_id``). Wird fuer die deterministische Finalisierung benoetigt, damit
# Kategorie/ungelesen auf der FINALEN ID landen und nicht auf einer veralteten.
_job_moved_message_id: str | None = None

# Vollstaendige Menge der im aktuellen Job aufgerufenen Tool-Namen. Bewusst
# UNABHAENGIG vom 200-Event-Trace-Limit gefuehrt: spaete Tools (create_draft,
# search_my_replies, set_categories, update_sender_profile) laufen erst nach
# Schritt 6-7 und fielen sonst aus dem gekappten Trace -- was self_grade und das
# Kontext-Gate systematisch verfaelschte. Quelle der Wahrheit fuer tools_used.
_job_tool_names: set[str] = set()

# ``create_draft`` liegt je nach Betriebsart in einer anderen Graph-Registrierung:
# im Einpass-Betrieb in ``graph`` (safe), im Zwei-Pass-Betrieb in ``graphAdmin``
# (siehe ``_DRAFT_TOOLS`` in mcp-graph/server.py). Die Erfassung der Draft-ID
# akzeptiert deshalb beide Namen -- unabhaengig davon, welche Config gerade laeuft.
_CREATE_DRAFT_TOOLS: frozenset[str] = frozenset({
    "mcp_graph_create_draft",
    "mcp_graphAdmin_create_draft",
})

# Recherche-Tools, deren Treffer als Quellen-Nachweis erfasst werden. Grundlage der
# Provenance: bei der Freigabe soll sichtbar sein, WORAUF sich ein Entwurf stuetzt --
# insbesondere, ob Material eines anderen Kunden eingeflossen ist. Die
# Kundeneingrenzung passiert bewusst nicht als harter Suchfilter (das kostet Recall),
# sondern durch Sichtbarkeit im HITL-Review.
_CONTEXT_SEARCH_TOOLS: frozenset[str] = frozenset({
    "mcp_taskpilot_semantic_search_documents",
    "mcp_graph_search_files",
    "mcp_graph_search_emails",
})

# Im aktuellen Job recherchierte Quellen (Titel/Typ/Absender/Datum), dedupliziert.
_job_context_sources: list[dict] = []

# Volltext dessen, was der Job tatsaechlich gesehen hat: Schreib-Prompt (inkl.
# Mailtext, Thread-Block und Dossier) plus die Roh-Ergebnisse der Lese-Tools.
# Grundlage der Faktenbindungs-Pruefung: was hier nicht vorkommt, hat das Modell
# erfunden. Pro Eintrag und in der Summe gedeckelt, damit ein tool-intensiver Job
# den Speicher nicht flutet.
_job_evidence: list[str] = []
_MAX_EVIDENCE_ENTRY_CHARS = 60_000
_MAX_EVIDENCE_TOTAL_CHARS = 600_000


def _record_evidence(text_body: str | None) -> None:
    """Nimmt einen gesehenen Text ins Beweismaterial auf (best-effort, gedeckelt)."""
    if not text_body:
        return
    if sum(len(e) for e in _job_evidence) >= _MAX_EVIDENCE_TOTAL_CHARS:
        return
    _job_evidence.append(str(text_body)[:_MAX_EVIDENCE_ENTRY_CHARS])


def _draft_tool_name() -> str:
    """Tool-Name fuer ``create_draft`` im aktuell konfigurierten Betrieb.

    Wird in den Schreib-Prompt gesetzt, damit das Modell das Tool unter dem
    Namen anspricht, unter dem es tatsaechlich registriert ist.
    """
    if get_settings().two_pass_draft:
        return "mcp_graphAdmin_create_draft"
    return "mcp_graph_create_draft"


def _get_runtime_lock() -> asyncio.Lock:
    global _runtime_lock
    if _runtime_lock is None:
        _runtime_lock = asyncio.Lock()
    return _runtime_lock


def _install_trajectory_path_shim() -> None:
    """Buendelt Hermes-Trajektorien in ``~/.hermes/trajectories/`` statt im Backend-CWD.

    Hermes' ``save_trajectory`` schreibt relativ ins aktuelle Arbeitsverzeichnis
    (``trajectory_samples.jsonl`` / ``failed_trajectories.jsonl``) und bietet keinen
    Pfad-/Env-Hook. Damit die gesammelten Trajektorien (Grundlage fuer Inspektion +
    spaeteres Fine-Tuning) an einem definierten Ort liegen, ersetzen wir
    ``run_agent._save_trajectory_to_file`` durch einen Wrapper mit absolutem Pfad.
    Idempotent, best-effort -- darf den Worker-Start nie verhindern.
    """
    global _trajectory_shim_installed
    if _trajectory_shim_installed:
        return
    try:
        import run_agent
        from agent.trajectory import save_trajectory as _orig_save_trajectory

        traj_dir = HERMES_HOME / "trajectories"
        traj_dir.mkdir(parents=True, exist_ok=True)

        def _save_to_hermes_home(trajectory, model, completed, filename=None):
            if filename is None:
                base = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"
                filename = str(traj_dir / base)
            return _orig_save_trajectory(trajectory, model, completed, filename=filename)

        run_agent._save_trajectory_to_file = _save_to_hermes_home
        _trajectory_shim_installed = True
        logger.info("Trajektorien-Pfad-Shim aktiv -> %s", traj_dir)
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Trajektorien-Pfad-Shim konnte nicht installiert werden (ignoriert)")


# ── Thinking-Policy ──────────────────────────────────────

# Jobtypen/Skills, bei denen Thinking deaktiviert werden DARF (rein mechanisch).
# Leer im Default: Thinking bleibt ueberall an (Transparenz). Erst nach
# Live-Verifikation gegen die Ollama-Version befuellen.
_THINKING_DISABLED_JOB_TYPES: set[str] = set()

# Triage-Jobtypen: fuer diese greift zusaetzlich der Config-Schalter
# ``triage_disable_thinking`` (Eval-gesteuert, Default aus).
_TRIAGE_JOB_TYPES: set[str] = {"email_triage", "chat_triage"}


def _thinking_disabled(job_type: str | None, skill: str | None) -> bool:
    """True, wenn Thinking fuer diesen Job deaktiviert werden soll (Default: nie).

    Zwei Quellen: die statische Liste ``_THINKING_DISABLED_JOB_TYPES`` (mechanisch)
    und der Config-Schalter ``triage_disable_thinking`` fuer Triage-Jobs. Letzterer
    ist Eval-gesteuert (siehe scripts/eval/ --no-think) und standardmaessig aus,
    weil unsere Triage agentisch mit Tool-Use laeuft.
    """
    if job_type and job_type in _THINKING_DISABLED_JOB_TYPES:
        return True
    if job_type in _TRIAGE_JOB_TYPES and get_settings().triage_disable_thinking:
        return True
    return False


# ── Trace-Callbacks (Transparenz) ────────────────────────

def _trace_append(event: dict) -> None:
    """Haengt ein Trace-Event an, mit reserviertem Budget fuer Tool-Events.

    Denk-Events duerfen nur den vorderen Teil des Budgets belegen. Sonst
    verdraengen sie die Tool-Events, die fuer die Nachvollziehbarkeit eigentlich
    entscheidend sind -- welches Tool mit welchem Ergebnis lief.
    """
    if event.get("type") == "thinking":
        if len(_job_trace) >= _MAX_TRACE_EVENTS - _RESERVED_TOOL_EVENTS:
            return
    if len(_job_trace) < _MAX_TRACE_EVENTS:
        _job_trace.append(event)


def _tag_trace_pass(pass_name: str) -> None:
    """Markiert alle noch unmarkierten Trace-Events mit dem laufenden Pass.

    Ein Triage-Job besteht aus mehreren Agenten-Laeufen (Klassifikation, Sammeln,
    Schreiben). Ohne Markierung ist im Cockpit nicht erkennbar, welcher Lauf welches
    Tool aufgerufen hat -- und ob der Entwurf ueberhaupt vom Schreib-Pass stammt.
    Der Aufruf erfolgt jeweils direkt nach einem Lauf, solange dessen Events die
    einzigen unmarkierten sind.
    """
    for event in _job_trace:
        if "pass" not in event:
            event["pass"] = pass_name


def _on_reasoning(text: str) -> None:
    """Denk-Text in den Trace schreiben, aufeinanderfolgende Stuecke gebuendelt.

    Hermes liefert Reasoning als Stream von Deltas -- teils Token fuer Token. Ein
    Event pro Delta fuellte das Trace-Limit nach rund 200 Denk-Tokens komplett auf
    (beobachtet: 'The', ' user', ' wants', ...), sodass spaetere Tool-Events und
    der gesamte Schreib-Pass nie im Trace landeten. Zusammenhaengender Denk-Text
    wird deshalb an das letzte Denk-Event angehaengt statt neu angelegt.
    """
    if not text:
        return
    chunk = str(text)
    if _job_trace and _job_trace[-1].get("type") == "thinking":
        last = _job_trace[-1]
        merged = f"{last.get('text', '')}{chunk}"
        if len(merged) <= _MAX_THINKING_CHARS:
            last["text"] = merged
            return
    _trace_append({"type": "thinking", "text": chunk[:_MAX_THINKING_CHARS]})


def _on_tool_start(tc_id, name, args) -> None:
    # Tool-Namen ungekappt mitschreiben (Quelle der Wahrheit fuer tools_used),
    # bevor das 200-Event-Trace-Limit greift.
    if name:
        _job_tool_names.add(str(name))
    event = {"type": "tool_start", "name": str(name)}
    # Bei Skill-Aufrufen den geladenen Skill-Namen miterfassen (Grundlage fuer
    # die Skill-Nutzungs-Analytics im Intelligenz-Tab). Best-effort -- args kann
    # ein Dict oder ein JSON-String sein.
    if str(name) in ("skill_view", "skill_manage"):
        skill = None
        try:
            if isinstance(args, dict):
                skill = args.get("name") or args.get("skill")
            elif isinstance(args, str) and args.strip().startswith("{"):
                skill = (json.loads(args) or {}).get("name")
        except Exception:  # noqa: BLE001 - Trace darf nie scheitern
            skill = None
        if skill:
            event["skill"] = str(skill)
    _trace_append(event)


def _extract_draft_id_from_tool_result(result) -> str | None:
    """Liest die echte Draft-ID aus dem (vollstaendigen) create_draft-Tool-Ergebnis.

    Das Tool-Ergebnis ist mehrfach verschachtelt: Hermes wrappt das MCP-Ergebnis als
    ``{"result": "<innerer JSON-String>"}``, und der innere String enthaelt erst das
    eigentliche ``{"id": "<echte Graph-ID>", ...}`` des MCP-Graph-Servers. Wir suchen
    deshalb rekursiv durch Dicts/Listen und JSON-Strings nach dem ersten ``id``-Feld.
    Das Callback erhaelt das ungekuerzte Ergebnis -- so bleibt die lange ID
    (~152 Zeichen) vollstaendig erhalten. Regex auf (auch escaptem) Text als Fallback.
    Gibt ``None`` zurueck, wenn keine ID gefunden wird.
    """

    def _search(obj, depth: int = 0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            if obj.get("id"):
                return str(obj["id"])
            # Wrapper-Schluessel zuerst (Hermes: "result", MCP-TextContent: "text").
            for key in ("result", "text", "data", "content"):
                if key in obj:
                    found = _search(obj[key], depth + 1)
                    if found:
                        return found
            for value in obj.values():
                found = _search(value, depth + 1)
                if found:
                    return found
            return None
        if isinstance(obj, list):
            for item in obj:
                found = _search(item, depth + 1)
                if found:
                    return found
            return None
        if isinstance(obj, str):
            s = obj.strip()
            if s[:1] in ("{", "["):
                try:
                    return _search(json.loads(s), depth + 1)
                except (json.JSONDecodeError, ValueError):
                    return None
        return None

    if not isinstance(result, str):
        found = _search(result)
        if found:
            return found
        text = str(result)
    else:
        found = _search(result)
        if found:
            return found
        text = result

    # Fallback: toleriere Backslash-escaptes "id":"..." aus doppelt kodiertem JSON.
    m = re.search(r'\\?"id\\?"\s*:\s*\\?"([^"\\]+)', text)
    return m.group(1) if m else None


def _extract_new_id_from_move_result(result) -> str | None:
    """Liest die neue Message-ID aus dem move_email_to_folder-Tool-Ergebnis.

    Der MCP-Graph-Server liefert ``{"status": "moved", ..., "new_id": "<neue ID>"}``,
    von Hermes als ``{"result": "<innerer JSON-String>"}`` gewrappt. Wir suchen
    rekursiv durch Dicts/Listen/JSON-Strings nach dem Feld ``new_id``. Regex als
    Fallback fuer doppelt kodiertes JSON. ``None``, wenn nichts gefunden wird.
    """

    def _search(obj, depth: int = 0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            if obj.get("new_id"):
                return str(obj["new_id"])
            for key in ("result", "text", "data", "content"):
                if key in obj:
                    found = _search(obj[key], depth + 1)
                    if found:
                        return found
            for value in obj.values():
                found = _search(value, depth + 1)
                if found:
                    return found
            return None
        if isinstance(obj, list):
            for item in obj:
                found = _search(item, depth + 1)
                if found:
                    return found
            return None
        if isinstance(obj, str):
            s = obj.strip()
            if s[:1] in ("{", "["):
                try:
                    return _search(json.loads(s), depth + 1)
                except (json.JSONDecodeError, ValueError):
                    return None
        return None

    found = _search(result)
    if found:
        return found
    text = result if isinstance(result, str) else str(result)
    m = re.search(r'\\?"new_id\\?"\s*:\s*\\?"([^"\\]+)', text)
    return m.group(1) if m else None


def _collect_context_sources(result, limit: int) -> None:
    """Erfasst die Treffer eines Recherche-Tools als Quellen-Nachweis.

    Das Ergebnis ist mehrfach verschachtelt (Hermes wrappt das MCP-Ergebnis, dessen
    Text wiederum JSON enthaelt), darum dieselbe rekursive Suche wie bei der
    Draft-ID. Erfasst wird bewusst nur Metadatum, kein Volltext: Titel, Quelle,
    Absender und Datum genuegen, damit bei der Freigabe erkennbar ist, worauf sich
    der Entwurf stuetzt. Best-effort -- ein Parse-Fehler darf den Job nie stoppen.
    """
    items = _find_result_items(result)
    if not items:
        return
    seen = {(s.get("source_type"), s.get("title")) for s in _job_context_sources}
    for item in items:
        if len(_job_context_sources) >= limit:
            return
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("subject") or item.get("name") or "").strip()
        if not title:
            continue
        entry = {
            "title": title[:160],
            "source_type": str(item.get("source_type") or item.get("type") or "unbekannt"),
        }
        for key, target in (("from", "from"), ("sender", "from"), ("date", "date"),
                            ("received", "date"), ("url", "url")):
            value = item.get(key)
            if value and target not in entry:
                entry[target] = str(value)[:200]
        key = (entry["source_type"], entry["title"])
        if key in seen:
            continue
        seen.add(key)
        _job_context_sources.append(entry)


def _find_result_items(obj, depth: int = 0) -> list:
    """Sucht rekursiv die Trefferliste (``results``/``items``) in einem Tool-Ergebnis."""
    if depth > 6:
        return []
    if isinstance(obj, dict):
        for key in ("results", "items", "documents", "hits"):
            value = obj.get(key)
            if isinstance(value, list):
                return value
        for key in ("result", "text", "data", "content"):
            if key in obj:
                found = _find_result_items(obj[key], depth + 1)
                if found:
                    return found
        return []
    if isinstance(obj, list):
        for item in obj:
            found = _find_result_items(item, depth + 1)
            if found:
                return found
        return []
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in ("{", "["):
            try:
                return _find_result_items(json.loads(s), depth + 1)
            except (json.JSONDecodeError, ValueError):
                return []
    return []


def _on_tool_complete(tc_id, name, args, result) -> None:
    global _job_created_draft_id, _job_moved_message_id
    # Tool-Namen vollstaendig (ungekappt) erfassen -- dient als verlaessliche
    # Quelle fuer tools_used/self_grade, unabhaengig vom 200-Event-Trace-Limit.
    if name:
        _job_tool_names.add(str(name))
    if str(name) in _CONTEXT_SEARCH_TOOLS:
        try:
            _collect_context_sources(result, get_settings().draft_context_max_sources)
        except Exception:  # noqa: BLE001 - Provenance darf den Job nie stoppen
            logger.warning("Quellen-Erfassung fehlgeschlagen (tool=%s)", str(name)[:60])
    # Jedes Tool-Ergebnis ist potenzieller Beleg fuer eine Angabe im Entwurf --
    # ungekappt (anders als im Trace), sonst schlaegt die Faktenbindung bei langen
    # Treffern falschen Alarm.
    _record_evidence(result if isinstance(result, str) else str(result))
    # Echte Draft-ID deterministisch aus dem Tool-Ergebnis erfassen (statt aus dem
    # vom LLM abgetippten JSON). Unabhaengig vom 200-Event-Trace-Limit -- so geht
    # die ID auch bei langlaufenden, tool-intensiven Jobs nicht verloren.
    if str(name) in _CREATE_DRAFT_TOOLS:
        real_id = _extract_draft_id_from_tool_result(result)
        if real_id:
            _job_created_draft_id = real_id  # last-wins: der zuletzt erzeugte Entwurf zaehlt
            logger.info("Echte Draft-ID aus create_draft erfasst (len=%d)", len(real_id))
        else:
            logger.warning(
                "create_draft lief, aber keine ID aus Tool-Ergebnis extrahierbar: %s",
                str(result)[:300],
            )
    # Neue Message-ID nach einem Move deterministisch erfassen -- ein Move aendert
    # die Graph-ID, sodass die spaetere Finalisierung (Kategorie/ungelesen) sonst
    # auf einer veralteten ID landen wuerde. last-wins.
    if str(name) == "mcp_graph_move_email_to_folder":
        new_mid = _extract_new_id_from_move_result(result)
        if new_mid:
            _job_moved_message_id = new_mid
            logger.info("Neue Message-ID nach Move erfasst (len=%d)", len(new_mid))
        else:
            logger.warning(
                "move_email_to_folder lief, aber keine new_id extrahierbar: %s",
                str(result)[:300],
            )
    _trace_append({"type": "tool_complete", "name": str(name), "result": str(result)[:500]})


# ── Prompt-Bausteine (framework-agnostisch) ──────────────

def _triage_skill_available() -> bool:
    """True, wenn der Hermes-native ``email-triage``-Skill ausgerollt ist."""
    return EMAIL_TRIAGE_SKILL.exists()


def _style_skill_available() -> bool:
    """True, wenn der Hermes-native ``email-style``-Skill ausgerollt ist."""
    return EMAIL_STYLE_SKILL.exists()


def _load_triage_skill() -> str:
    """Datei-Fallback fuer den Triage-Skill (nur falls skill_view scheitert).

    Bevorzugt den nativen Skill (SKILL.md + references), sonst die Legacy-Flat-Datei.
    """
    if EMAIL_TRIAGE_SKILL.exists():
        parts = [EMAIL_TRIAGE_SKILL.read_text(encoding="utf-8")]
        if EMAIL_TRIAGE_REFERENCES.is_dir():
            for ref in sorted(EMAIL_TRIAGE_REFERENCES.glob("*.md")):
                parts.append(f"\n\n---\n\n# {ref.name}\n\n{ref.read_text(encoding='utf-8')}")
        return "".join(parts)
    if LEGACY_TRIAGE_SKILL.exists():
        return LEGACY_TRIAGE_SKILL.read_text(encoding="utf-8")
    logger.warning("Triage-Skill nicht gefunden: %s / %s", EMAIL_TRIAGE_SKILL, LEGACY_TRIAGE_SKILL)
    return ""


def _load_style_profile() -> str:
    """Datei-Fallback fuer den Schreibstil-Kanon (nur falls skill_view scheitert)."""
    if EMAIL_STYLE_SKILL.exists():
        return EMAIL_STYLE_SKILL.read_text(encoding="utf-8")
    if LEGACY_STYLE_PROFILE.exists():
        return LEGACY_STYLE_PROFILE.read_text(encoding="utf-8")
    logger.warning("Schreibstil-Kanon nicht gefunden: %s / %s", EMAIL_STYLE_SKILL, LEGACY_STYLE_PROFILE)
    return ""


async def _load_projects_context() -> str:
    """Laedt alle aktiven Projekte aus der DB und formatiert sie als Prompt-Kontext."""
    async with async_session() as db:
        result = await db.execute(
            select(Project).where(Project.status != "archived").order_by(Project.name)
        )
        projects = list(result.scalars().all())

    if not projects:
        return "## VERFÜGBARE PROJEKTE\nKeine aktiven Projekte vorhanden."

    lines = ["## VERFÜGBARE PROJEKTE", ""]
    for p in projects:
        lines.append(f'- "{p.name}" (id: {p.id})')
    lines.append("")
    lines.append("Wähle bei triage_class='task' das passendste Projekt aus dieser Liste für das Feld suggested_project.")
    lines.append("Falls kein Projekt passt, setze suggested_project auf null.")
    return "\n".join(lines)


async def _build_recall_block(
    meta: dict,
    *,
    job_type: str | None = "email_triage",
    query: str | None = None,
) -> str:
    """Few-Shot-Recall: gelernte Lektionen aus aehnlichen frueheren Korrekturen.

    Hoechstes Lernsignal -- zeigt dem Agenten, wie der Berater in vergleichbaren
    Faellen frueher korrigiert hat, damit derselbe Fehler nicht wiederholt wird.
    ``job_type`` filtert die Episoden (None = alle Job-Typen); ``query`` erlaubt
    eine eigene Suchanfrage (Default: E-Mail-Metadaten aus ``meta``).
    Best-effort: ohne Embedding-Modell/Episoden faellt der Block weg.
    """
    cfg = get_settings()
    if not cfg.agent_recall_enabled:
        return ""
    try:
        from app.services.learning import recall_similar_episodes

        if not query:
            subject = meta.get("subject", "")
            from_addr = meta.get("from_address", "")
            from_name = meta.get("from_name", "")
            preview = meta.get("body_preview", "")
            query = f"E-Mail von {from_name} <{from_addr}>: '{subject}'. {preview[:300]}"

        async with async_session() as db:
            episodes = await recall_similar_episodes(
                db, query=query, job_type=job_type, k=3, corrected_only=True,
            )
        lessons = [e for e in episodes if (e.get("lesson") or "").strip()]
        if not lessons:
            return ""

        lines = []
        for e in lessons:
            sim = e.get("similarity")
            sim_pct = f" ({round(float(sim) * 100)}% aehnlich)" if isinstance(sim, (int, float)) else ""
            sender = e.get("sender_email") or "?"
            lines.append(f"- Frueherer Fall ({sender}){sim_pct}: {e['lesson'].strip()}")

        return (
            "\n---\n\n## GELERNTE LEKTIONEN AUS FRÜHEREN KORREKTUREN (BEACHTEN!)\n"
            "Der Berater hat in ähnlichen Fällen früher korrigiert. Wiederhole diese "
            "Fehler NICHT:\n" + "\n".join(lines) + "\n"
        )
    except Exception:  # noqa: BLE001 - best-effort, darf Prompt-Bau nie stoppen
        logger.warning("Recall-Block konnte nicht erzeugt werden")
        return ""


def _compute_self_grade(
    meta: dict, result_meta: dict, tools_used: list[str]
) -> dict:
    """Deterministisches Self-Grading eines Triage-Jobs (Saeule 3).

    Prueft anhand der tatsaechlich aufgerufenen Tools, ob der Agent die im Prompt
    geforderten Pflicht-Kontexte geladen hat (Thread/Absender-History/-Profil) und
    -- bei einem Entwurf -- den Stil-Anker (`search_my_replies`) genutzt hat. Rein
    und damit unabhaengig testbar. Tool-Namen werden per Substring gematcht, um
    MCP-Praefixe abzufangen.

    Tool-Nutzung allein sagt nichts ueber Wahrheit: am 04.08.2026 meldete diese
    Funktion 1.0 fuer einen Entwurf mit erfundener IP-Adresse. Darum zaehlt bei
    einem Entwurf zusaetzlich das Ergebnis der Faktenbindung (``draft_quality``).
    """

    def used(key: str) -> bool:
        return any(key in (t or "") for t in tools_used)

    has_conversation = bool(meta.get("conversation_id"))
    has_draft = bool(result_meta.get("draft_id"))

    checks: dict[str, bool] = {
        "sender_history_loaded": used("search_sender_history"),
        "sender_profile_loaded": used("get_sender_profile"),
    }
    if has_conversation:
        checks["thread_loaded"] = used("get_thread")
    if has_draft:
        checks["style_anchor_used"] = used("search_my_replies")
        checks["values_grounded"] = result_meta.get("draft_quality") != "ungrounded"

    passed = sum(1 for v in checks.values() if v)
    total = len(checks) or 1
    missing = [k for k, v in checks.items() if not v]
    return {
        "score": round(passed / total, 2),
        "checks": checks,
        "missing": missing,
    }


def _apply_grounding_check(meta: dict, draft_html: str, job_id) -> None:
    """Markiert einen Entwurf, dessen Angaben in keiner gesehenen Quelle stehen.

    Der Entwurf wird NICHT verworfen -- der Mensch entscheidet, wie beim
    Platzhalter-Test. Aber er kommt mit gedeckelter Confidence und benannter
    Fundstelle in die Freigabe, damit eine erfundene Adresse nicht als Detailwissen
    durchgeht. Belege sind der Schreib-Prompt und alle Tool-Ergebnisse des Jobs
    (``_job_evidence``); fehlt beides, wird nicht geprueft, statt alles zu
    beanstanden.
    """
    from app.services.text_style import placeholder_markers, ungrounded_values

    if not _job_evidence:
        return
    try:
        missing = ungrounded_values(draft_html, _job_evidence)
        placeholders = placeholder_markers(draft_html)
    except Exception:  # noqa: BLE001 - Pruefung darf den Job nie stoppen
        logger.warning("Job %s: Faktenbindungs-Pruefung fehlgeschlagen", job_id)
        return
    if not missing and not placeholders:
        return

    warnings: list[str] = []
    if missing:
        meta["ungrounded_values"] = missing[:12]
        warnings.append(
            "Diese Angaben stehen in keiner recherchierten Quelle: "
            + ", ".join(missing[:6])
        )
        logger.warning(
            "Job %s: unbelegte Angaben im Entwurf: %s", job_id, missing[:12]
        )
    if placeholders:
        meta["draft_placeholders"] = placeholders[:12]
        warnings.append("Unausgefüllte Platzhalter: " + ", ".join(placeholders[:6]))
        logger.warning(
            "Job %s: Platzhalter im Entwurf: %s", job_id, placeholders[:12]
        )

    meta["draft_quality"] = "ungrounded"
    existing_warning = meta.get("context_warning")
    warning_text = " ".join(warnings) + " Bitte vor dem Senden prüfen."
    meta["context_warning"] = (
        f"{existing_warning} {warning_text}".strip() if existing_warning else warning_text
    )
    current = meta.get("confidence")
    if current is None or current > 0.4:
        meta["confidence"] = 0.4


# Skill/Kontext, in dem eine Leitregel wirkt. 'general' wirkt immer mit.
_DEFAULT_RULE_CONTEXT = "triage"
# Alte scope-Werte auf die aktiven Kontexte abbilden (Rueckwaertskompatibilitaet).
_SCOPE_CONTEXT_ALIASES: dict[str, str] = {
    "email-triage": "triage",
    "email-style": "draft",
}


async def _build_rules_block(*contexts: str) -> str:
    """Freigegebene LLM-Leitregeln der passenden Kontexte in den Prompt einspeisen.

    Nur ``status='active'`` und ``rule_type='llm'`` wirken; Vorschlaege (``proposed``)
    bleiben bis zur HITL-Freigabe folgenlos, deterministische Regeln laufen separat
    in ``triage.py``. Es greifen Regeln, deren ``scope`` zu einem der ``contexts``
    passt, plus ``general`` (kontextuebergreifend). So wirken Regeln genau dort, wo
    der Kontext aktiv ist -- Triage, Entwurf oder Chat. Best-effort.
    """
    wanted = {_SCOPE_CONTEXT_ALIASES.get(c, c) for c in contexts} or {_DEFAULT_RULE_CONTEXT}
    wanted.add("general")
    try:
        from app.models import LearnedRule

        async with async_session() as db:
            result = await db.execute(
                select(LearnedRule)
                .where(
                    LearnedRule.status == "active",
                    LearnedRule.rule_type == "llm",
                    LearnedRule.scope.in_(tuple(wanted)),
                    LearnedRule.user_id == await system_principal_id(db),
                )
                .order_by(LearnedRule.approved_at.desc())
                .limit(20)
            )
            rules = result.scalars().all()
        if not rules:
            return ""
        lines = [f"- [{r.scope}] {r.rule_text}" for r in rules]
        return (
            "\n---\n\n## AKTIVE GELERNTE REGELN (vom Berater freigegeben -- VERBINDLICH)\n"
            "Diese Regeln wurden aus deinen frueheren Korrekturen abgeleitet und "
            "freigegeben. Befolge sie:\n" + "\n".join(lines) + "\n"
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Aktive-Regeln-Block (%s) konnte nicht erzeugt werden", ",".join(sorted(wanted)))
        return ""


async def _build_sender_style_block(from_addr: str) -> str:
    """Per-Absender-Stilprofil in den Prompt einspeisen (Ton-Treffsicherheit).

    Nutzt die ueber Korrekturen gelernten Felder aus ``sender_profiles``
    (Beziehung, Tonalitaet, Sprache, ``learned_tone``, ``style_notes``,
    ``correction_count``). Best-effort -- ohne Profil faellt der Block weg.
    """
    if not from_addr:
        return ""
    try:
        from app.models import SenderProfile

        async with async_session() as db:
            row = await db.execute(
                select(SenderProfile).where(
                    SenderProfile.email == from_addr.lower(),
                    SenderProfile.user_id == await system_principal_id(db),
                )
            )
            p = row.scalar_one_or_none()
        if p is None:
            return ""

        facts: list[str] = []
        if p.display_name:
            facts.append(f"Name: {p.display_name}")
        if p.relationship:
            facts.append(f"Beziehung: {p.relationship}")
        if p.tone:
            facts.append(f"Tonalitaet: {p.tone}")
        if p.language:
            facts.append(f"Sprache: {p.language}")

        lines: list[str] = []
        if facts:
            lines.append("- " + "; ".join(facts))
        learned = p.learned_tone if isinstance(p.learned_tone, dict) else {}
        if learned:
            lt = ", ".join(f"{k}={v}" for k, v in learned.items())
            lines.append(f"- Gelernte Tonmerkmale: {lt}")
        notes = (p.style_notes or "").strip()
        if notes:
            # Begrenzen: style_notes ist die einzige unbegrenzte, ueber Korrekturen
            # wachsende Textquelle im Prompt -- Cap haelt den Klassifikations-Prompt
            # schlank (lokale Modelle reagieren empfindlich auf Prompt-Laenge).
            if len(notes) > 600:
                notes = notes[:600].rstrip() + " […]"
            lines.append("- Stil-Notizen aus frueheren Korrekturen:\n" + notes)
        if p.correction_count:
            lines.append(
                f"- Achtung: {p.correction_count} manuelle Stil-Korrektur(en) an diesen "
                "Kontakt erfasst -- richte Anrede, Ton und Laenge besonders genau danach."
            )

        if not lines:
            return ""
        return (
            "\n---\n\n## ABSENDER-STILPROFIL (fuer diesen Kontakt -- VERBINDLICH beim Draft)\n"
            "Beachte das gelernte Profil dieses Absenders genau (Anrede/Du-Sie, Ton, Laenge):\n"
            + "\n".join(lines) + "\n"
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Sender-Style-Block konnte nicht erzeugt werden")
        return ""


async def _build_style_anchor_block(meta: dict) -> str:
    """Few-Shot-Stil-Anker aus dem lokalen Style-Store (semantisch aehnliche eigene Antworten).

    Ergaenzt ``search_my_replies`` (nur derselbe Kontakt) um die stilistisch/
    thematisch passendsten eigenen Antworten ueber ALLE Kontakte -- entscheidend fuer
    neue Absender ohne History. Best-effort: ohne Store/Embedding faellt der Block weg.
    """
    if not get_settings().style_store_enabled:
        return ""
    try:
        from app.services.style_store import find_style_anchors

        subject = meta.get("subject", "")
        preview = (meta.get("body_preview") or "")[:400]
        from_addr = meta.get("from_address", "")
        query = f"Betreff: {subject}\n{preview}"
        async with async_session() as db:
            anchors = await find_style_anchors(
                db, query_text=query, recipient=from_addr, k=3,
                user_id=await system_principal_id(db),
            )
        blocks: list[str] = []
        for a in anchors:
            body = (a.get("body_text") or "").strip()
            if not body:
                continue
            if len(body) > 700:
                body = body[:700].rstrip() + " […]"
            subj = a.get("subject") or ""
            blocks.append(f'### Beispiel (Betreff: "{subj}")\n{body}')
        if not blocks:
            return ""
        return (
            "\n---\n\n## SO SCHREIBT ANTHONY (echte frühere Antworten -- Ton kalibrieren, NICHT kopieren)\n"
            "Diese von Anthony gesendeten Antworten treffen Ton, Rhythmus und Länge. "
            "Nimm sie als Stil-Vorbild, übernimm aber KEINE Formulierungen wörtlich -- "
            "schreibe passend zum aktuellen Inhalt neu:\n\n" + "\n\n".join(blocks) + "\n"
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Stil-Anker-Block konnte nicht erzeugt werden")
        return ""


# Signalwoerter fuer eine Terminanfrage -- loesen im Draft-Pass den Kalender-Check aus.
_CALENDAR_INTENT_PATTERNS = [
    r"termin", r"meeting", r"besprechung", r"kalender", r"verf[üu]gbar",
    r"wann\s+(?:passt|h[äa]tt|hast|k[öo]nn|kannst|w[äa]r)", r"zeitfenster",
    r"\bslot", r"\bcall\b", r"telefonat", r"appointment", r"available",
    r"schedule", r"treffen", r"sitzung", r"zoom", r"teams-call",
]


def _looks_like_scheduling(subject: str, preview: str) -> bool:
    """Heuristik: Geht es in der Mail um einen Termin/Verfügbarkeit? Rein/testbar."""
    text = f"{subject}\n{preview}".lower()
    return any(re.search(p, text) for p in _CALENDAR_INTENT_PATTERNS)


def _build_calendar_draft_step(
    subject: str, preview: str, available_from: date | None = None
) -> str:
    """Konditionale Kalender-Anweisung: bei Terminwunsch echte freie Slots vorschlagen.

    Nur lesend (``find_free_slots``); es wird KEIN Termin erstellt. Faellt weg, wenn
    die Mail nicht nach einem Termin aussieht -- haelt den Prompt sonst schlank.

    ``available_from`` verschiebt das Suchfenster hinter eine laufende Abwesenheit.
    Notwendig, weil Ferien in ``capacity_time_off`` stehen und nicht zwingend als
    Kalendertermin -- ``find_free_slots`` haelt die Tage sonst faelschlich fuer frei
    und der Entwurf bietet Termine mitten in den Ferien an.
    """
    if not _looks_like_scheduling(subject or "", preview or ""):
        return ""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Europe/Zurich"))
    von = now.date()
    if available_from and available_from > von:
        von = available_from
        now = datetime.combine(von, now.time(), tzinfo=now.tzinfo)
    start = f"{von.isoformat()}T08:00:00"
    end = (now + timedelta(days=10)).strftime("%Y-%m-%dT19:00:00")
    return (
        f'3a. Diese Mail betrifft eine Terminfrage. Rufe find_free_slots(start="{start}", '
        f'end="{end}", duration_minutes=60) auf und schlage **2-3 konkrete freie '
        "Zeitfenster** in natürlicher Sprache vor (Wochentag + Datum + Uhrzeit, "
        "Europe/Zurich). Biete zusätzlich als Alternative die Terminseite "
        "https://innosmith.ch/termin/ an. Erfinde NIEMALS Slots -- nutze nur echte "
        "Rückgaben von find_free_slots. Bei Fehler/keinen freien Slots: nur den "
        "Terminseiten-Link anbieten.\n"
    )


def _group_absence_ranges(days: list[date]) -> list[tuple[date, date]]:
    """Fasst einzelne Abwesenheitstage zu Spannen zusammen, ueber Wochenenden hinweg.

    ``capacity_time_off`` haelt eine Zeile pro Tag und enthaelt nur Arbeitstage. Eine
    Ferienwoche Mo-Fr plus die Folgewoche erscheint daher als zwei Bloecke mit einer
    Luecke am Wochenende. Ohne Bruecke wuerde daraus «bis Freitag» statt «bis in zwei
    Wochen» -- also wird eine Luecke uebersprungen, wenn sie ausschliesslich aus
    Samstag/Sonntag besteht.
    """
    if not days:
        return []
    ordered = sorted(set(days))
    ranges: list[tuple[date, date]] = []
    start = prev = ordered[0]
    for current in ordered[1:]:
        gap = [prev + timedelta(days=i) for i in range(1, (current - prev).days)]
        if all(d.weekday() >= 5 for d in gap):
            prev = current
            continue
        ranges.append((start, prev))
        start = prev = current
    ranges.append((start, prev))
    return ranges


async def _absence_ranges(horizon_days: int = 21) -> list[tuple[date, date]]:
    """Anthonys kommende Abwesenheitsspannen aus ``capacity_time_off``.

    Best-effort: liefert bei Fehlern oder fehlenden Eintraegen eine leere Liste, damit
    der Entwurf nie an fehlendem Kontext scheitert.
    """
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Zurich")).date()
    try:
        async with async_session() as db:
            rows = await db.execute(
                select(CapacityTimeOff.date, CapacityTimeOff.type)
                .where(
                    CapacityTimeOff.date >= today - timedelta(days=7),
                    CapacityTimeOff.date <= today + timedelta(days=horizon_days),
                )
                .order_by(CapacityTimeOff.date)
            )
            entries = rows.all()
    except Exception:  # noqa: BLE001 - Kontext ist Beigabe, darf den Entwurf nie stoppen
        logger.warning("Abwesenheitskontext konnte nicht geladen werden")
        return []
    # Feiertage sind kein Abwesenheitsgrund, den man in einer Antwort erwaehnt.
    days = [d for d, kind in entries if kind in ("ferien", "krank", "sonstiges")]
    return [r for r in _group_absence_ranges(days) if r[1] >= today]


def _first_available_day(ranges: list[tuple[date, date]], today: date) -> date | None:
    """Erster Arbeitstag, an dem Anthony wieder verfuegbar ist.

    None, wenn er heute da ist. Wochenenden und direkt anschliessende Abwesenheiten
    werden uebersprungen -- sonst wuerde als Rueckkehrtag ein Samstag oder ein
    weiterer Ferientag vorgeschlagen.
    """
    aktuell = next((r for r in ranges if r[0] <= today <= r[1]), None)
    if aktuell is None:
        return None
    tag = aktuell[1] + timedelta(days=1)
    for _ in range(60):  # harte Schranke gegen Endlosschleifen
        if tag.weekday() >= 5:
            tag += timedelta(days=1)
        elif any(a <= tag <= b for a, b in ranges):
            tag += timedelta(days=1)
        else:
            return tag
    return tag


def _build_absence_block(ranges: list[tuple[date, date]], today: date) -> str:
    """Abwesenheiten als Pflichtkontext fuer Antwort-Entwuerfe.

    Die Ferien sind in ``capacity_time_off`` erfasst (die zwei Wochen vom 13.-24. Juli
    2026 standen dort vollstaendig drin) -- der Entwurfs-Prompt hat die Tabelle nur
    nie gelesen. Entsprechend entstanden Antworten, die die Ferien nicht erwaehnten
    und Termine mitten in die Abwesenheit legten.
    """
    if not ranges:
        return ""

    def _fmt(a: date, b: date) -> str:
        if a == b:
            return f"{_WEEKDAYS_DE[a.weekday()]}, {a.strftime('%d.%m.%Y')}"
        return (
            f"{_WEEKDAYS_DE[a.weekday()]}, {a.strftime('%d.%m.')} bis "
            f"{_WEEKDAYS_DE[b.weekday()]}, {b.strftime('%d.%m.%Y')}"
        )

    aktuell = next((r for r in ranges if r[0] <= today <= r[1]), None)
    lines = [f"- {_fmt(a, b)}" for a, b in ranges]
    kern = "\n".join(lines)

    if aktuell:
        return (
            "## ABWESENHEIT VON ANTHONY (Pflichtkontext)\n\n"
            f"**Anthony ist HEUTE abwesend** -- {_fmt(*aktuell)}.\n\n"
            f"Geplante Abwesenheiten im Horizont:\n{kern}\n\n"
            "Der Entwurf MUSS das berücksichtigen:\n"
            "- Die Abwesenheit offen ansprechen, ohne sich zu entschuldigen "
            "(«ich bin bis [Datum] abwesend»).\n"
            "- Eine realistische Erwartung setzen, wann Anthony antwortet bzw. handelt "
            "-- keine Zusage für die Abwesenheitszeit.\n"
            "- Termine NIE in die Abwesenheit legen. Freie Slots erst ab dem ersten "
            "Arbeitstag danach vorschlagen.\n"
            "- Bei Dringlichkeit auf den Zeitpunkt der Rückkehr verweisen statt auf ein "
            "vages «bald».\n\n---\n\n"
        )
    return (
        "## ABWESENHEIT VON ANTHONY (Pflichtkontext)\n\n"
        f"Anthony ist aktuell verfügbar. Geplante Abwesenheiten:\n{kern}\n\n"
        "Schlage KEINE Termine innerhalb dieser Zeiträume vor. Liegt ein Termin- oder "
        "Lieferwunsch darin, weise aktiv darauf hin und biete Alternativen davor oder "
        "danach an.\n\n---\n\n"
    )


async def _build_project_routing_hint(from_addr: str) -> str:
    """Gelerntes Projekt-Routing als weichen Prompt-Hinweis ("korrekte Zuweisung").

    Wertet die impliziten Korrektursignale (``task_moved``) dieses Absenders aus:
    Wenn agent-erzeugte Tasks dieses Kontakts wiederholt ins selbe Projekt
    verschoben wurden, bevorzugt der Agent kuenftig dieses Projekt fuer
    ``suggested_project``. Bewusst nicht-destruktiv -- die Task bleibt
    ``needs_review``, der Agent darf inhaltlich abweichen. Best-effort: ohne
    genuegend Signale (Schwelle: ``agent_reflection_min_occurrences``) faellt der
    Block ersatzlos weg.
    """
    if not from_addr:
        return ""
    try:
        import uuid as _uuid
        from collections import Counter as _Counter

        threshold = max(2, get_settings().agent_reflection_min_occurrences)
        async with async_session() as db:
            rows = await db.execute(
                select(AgentFeedback.corrected).where(
                    AgentFeedback.feedback_type == "task_moved",
                    func.lower(AgentFeedback.sender_email) == from_addr.lower(),
                    AgentFeedback.user_id == await system_principal_id(db),
                )
            )
            targets: _Counter = _Counter()
            for (corrected,) in rows.all():
                pid = (corrected or {}).get("project_id")
                if pid:
                    targets[str(pid)] += 1
            if not targets:
                return ""
            top_pid, top_count = targets.most_common(1)[0]
            if top_count < threshold:
                return ""
            try:
                proj = await db.get(Project, _uuid.UUID(top_pid))
            except (ValueError, TypeError):
                return ""
        if proj is None or getattr(proj, "status", None) == "archived":
            return ""
        return (
            "\n---\n\n## GELERNTES PROJEKT-ROUTING (weicher Hinweis)\n"
            f"Aufgaben von {from_addr} hat der Berater bereits {top_count}x ins "
            f'Projekt "{proj.name}" verschoben. Bevorzuge dieses Projekt fuer '
            "suggested_project, sofern der Inhalt nicht klar zu einem anderen "
            "Projekt gehoert.\n"
        )
    except Exception:  # noqa: BLE001 - best-effort, darf den Prompt-Bau nie stoppen
        logger.warning("Projekt-Routing-Hinweis konnte nicht erzeugt werden")
        return ""


async def _build_thread_task_hint(meta: dict) -> str:
    """Weicher Thread-/Konsistenz-Hinweis: existiert bereits ein offener Task zur
    selben Sache (gleicher Thread oder Absender+Betreff), wird der Agent darauf
    hingewiesen, KEINEN doppelten Task zu erzeugen. Die harte Garantie liegt
    weiterhin in der Dedup-Logik des Post-Processings (``_find_duplicate_open_task``);
    dieser Hinweis reduziert das Rauschen bereits im Prompt und haelt die
    Klassifikation ueber einen Thread hinweg konsistent. Best-effort.
    """
    try:
        async with async_session() as db:
            dup = await _find_duplicate_open_task(db, meta)
            if dup is None:
                return ""
            proj = await db.get(Project, dup.project_id) if dup.project_id else None
        proj_txt = f' (Projekt "{proj.name}")' if proj is not None else ""
        return (
            "\n---\n\n## BEREITS OFFENER TASK ZU DIESER SACHE (KONSISTENZ)\n"
            f'Es existiert bereits ein offener Task: "{dup.title}"{proj_txt}. '
            "Erstelle KEINEN doppelten Task. Handelt es sich um dieselbe Sache, "
            "genuegt fyi -- das Backend dockt die neue Meldung automatisch als "
            "Checklisten-Eintrag an den bestehenden Task an.\n"
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Thread-Task-Hinweis konnte nicht erzeugt werden")
        return ""


async def _build_triage_prompt(job: AgentJob) -> str:
    """Baut den Prompt für einen email_triage Job aus Metadata.

    Hermes-native: Der Agent laedt den ``email-triage``- und ``email-style``-Skill
    selbst via ``skill_view`` (Progressive Disclosure). Nur falls die nativen Skills
    (noch) nicht auf der Platte liegen, wird der Datei-Inhalt als Fallback injiziert.
    """
    skill_native = _triage_skill_available()
    style_native = _style_skill_available()
    skill_text = "" if skill_native else _load_triage_skill()
    style_text = "" if style_native else _load_style_profile()
    projects_context = await _load_projects_context()

    custom_triage_prompt = ""
    try:
        async with async_session() as db:
            owner_settings = await get_owner_settings(db)
        custom_triage_prompt = (owner_settings.get("triage_prompt") or "").strip()
    except Exception:
        logger.warning("Konnte triage_prompt nicht aus User-Settings laden")

    meta = job.metadata_json or {}
    email_id = meta.get("email_message_id", "")
    subject = meta.get("subject", "")
    from_addr = meta.get("from_address", "")
    from_name = meta.get("from_name", "")
    preview = meta.get("body_preview", "")
    inference = meta.get("inference_classification", "")
    conversation_id = meta.get("conversation_id", "")
    recipient_type = meta.get("recipient_type", "unknown")
    forced_class = meta.get("forced_class")
    correction_reason = meta.get("correction_reason") or ""
    recall_block = await _build_recall_block(meta)
    two_pass = get_settings().two_pass_draft
    # Im Zwei-Pass-Modus schreibt ein separater Lauf den Entwurf -> hier nur der
    # Triage-Regel-Kontext. Im Einpass-Modus laufen Triage + Draft zusammen.
    rules_block = await _build_rules_block("triage") if two_pass else await _build_rules_block("triage", "draft")
    sender_style_block = await _build_sender_style_block(from_addr)
    routing_hint = await _build_project_routing_hint(from_addr)
    thread_task_hint = await _build_thread_task_hint(meta)

    correction_block = ""
    if forced_class:
        artefakt = "einen Antwort-Entwurf (auto_reply)" if forced_class == "auto_reply" else "eine Aufgabe (task)"
        correction_block = (
            "## ⚠️ KORREKTUR DES BERATERS (HÖCHSTE PRIORITÄT)\n\n"
            f"Der Berater hat entschieden: Diese E-Mail MUSS als **{forced_class}** behandelt werden "
            f"-> erzeuge {artefakt}.\n"
            "→ Klassifiziere NICHT neu und überschreibe diese Entscheidung NICHT.\n"
            "→ Lade dennoch die Pflicht-Kontexte (Thread, Absender-History, -Profil) "
            "und nutze bei auto_reply den Stil-Anker (search_my_replies), bevor du den "
            "Artefakt erzeugst.\n"
            f"→ Setze triage_class im JSON-Block zwingend auf \"{forced_class}\".\n"
            + (f"→ Begründung des Beraters: {correction_reason}\n" if correction_reason else "")
            + "\n---\n\n"
        )

    thread_hint = ""
    if conversation_id:
        thread_hint = f"""
**Konversations-ID:** {conversation_id}
→ Lade den Thread mit get_thread("{conversation_id}") für vollständigen Kontext.
→ Lade die Absender-History mit search_sender_history("{from_addr}") um Kommunikationsmuster zu erkennen.
"""

    # Fakt aus dem Umschlag (RFC 3834), keine Textdeutung: der Absender-Server
    # deklariert selbst, dass die Mail automatisch erzeugt wurde. Wie das zu bewerten
    # ist, entscheidet das LLM -- eine Messung am Postfach zeigte, dass der Header
    # Autoresponder nicht von handlungsrelevanter Maschinenpost trennt (eine
    # Lieferantenrechnung trug denselben Wert wie eine Abwesenheitsnotiz).
    auto_submitted = (meta.get("auto_submitted") or "").strip()
    auto_hint = ""
    if auto_submitted:
        auto_hint = (
            f"\n**Auto-Submitted-Header:** `{auto_submitted}` -- die Mail wurde laut "
            "Absender-Server maschinell erzeugt.\n"
            "→ Auf eine automatische Antwort (z. B. eine fremde Abwesenheitsnotiz) wird "
            "NIE geantwortet: kein `auto_reply`. Ob daraus eine Aufgabe entsteht, "
            "entscheidet allein der Inhalt -- eine maschinell verschickte Rechnung oder "
            "Frist bleibt handlungsrelevant.\n"
        )

    recipient_hint = ""
    if recipient_type == "cc":
        recipient_hint = (
            "\n⚠️ **ACHTUNG: Anthony ist bei dieser E-Mail NUR im CC, NICHT im TO.**\n"
            "→ Beachte die CC-Regeln (Abschnitt 2 in references/triage-rules.md)!\n"
            "→ Default: triage_class=fyi, KEIN auto_reply, KEIN task — "
            "es sei denn, Anthony wird im Body direkt angesprochen.\n"
        )

    # Skill-Sektion: nativ via skill_view (Default) oder Datei-Fallback.
    if skill_native:
        skill_section = (
            "## TRIAGE-SKILL (NATIV LADEN)\n\n"
            "Du hast einen email_triage Job. Lade ZUERST den Skill und befolge ihn strikt:\n"
            "→ **skill_view(name='email-triage')**\n"
            "Er enthält den vollständigen Ablauf, die Prioritätsstufen, CC-Regeln, die "
            "auto_reply-Schwelle, die Move-Ordner und den Pflicht-JSON-Block. Lies bei "
            "Bedarf die referenzierten Dateien (references/triage-rules.md für Detail-Regeln "
            "und das JSON-Schema, references/examples.md für Entwurfs-Vorbilder)."
        )
    else:
        skill_section = f"## TRIAGE-INSTRUKTIONEN (STRIKT befolgen!)\n\n{skill_text}"

    # Schreibstil-Sektion: nativ via skill_view (Default) oder Datei-Fallback.
    # Im Zwei-Pass-Modus entfaellt sie hier -- der separate Schreib-Pass laedt den
    # Stil-Kanon mit vollem Budget; die Klassifikation bleibt schlank.
    if two_pass:
        style_section = ""
    elif style_native:
        style_section = (
            "\n---\n\n## SCHREIBSTIL (bei jedem Antwort-Entwurf)\n\n"
            "Bevor du einen auto_reply-Draft formulierst, lade den persönlichen "
            "Schreibstil-Kanon von Anthony und halte dich strikt daran:\n"
            "→ **skill_view(name='email-style')**\n"
        )
    elif style_text:
        style_section = (
            "\n---\n\n## SCHREIBSTIL (VERBINDLICH für jeden Antwort-Entwurf)\n\n"
            "Wenn du einen Draft (auto_reply) formulierst, halte dich strikt an den "
            "folgenden persönlichen Schreibstil-Kanon von Anthony Smith. Ziel: Anthony "
            f"muss sich im Entwurf wiedererkennen.\n\n{style_text}\n"
        )
    else:
        style_section = ""

    # Draft-Schritt: im Zwei-Pass-Modus erstellt der Klassifikations-Lauf KEINEN
    # Entwurf (das uebernimmt der separate Schreib-Pass), sonst im selben Loop.
    if two_pass:
        draft_step = (
            "7. Erstelle KEINEN Antwort-Entwurf. Klassifiziere nur -- bei auto_reply "
            "schreibt das Backend den Entwurf anschliessend in einem separaten, "
            "fokussierten Schreib-Pass. Das Backend erzwingt die Thread-Zugehörigkeit "
            "und erstellt bei task die Aufgabe automatisch."
        )
    else:
        draft_step = (
            "7. Erstelle Draft falls auto_reply. WICHTIG: Rufe VORHER "
            f'search_my_replies("{from_addr}") auf und nutze die letzten von Anthony '
            "gesendeten Antworten an diesen Kontakt als Ton-/Register-Kalibrierung "
            "(orientiere dich an Ton, Länge, Anrede und Schlussformel, schreibe aber "
            "natürlich neu, kopiere nicht wörtlich). PFLICHT: Übergib bei create_draft "
            f'IMMER reply_to_id="{email_id}", damit die Antwort als "Allen antworten" '
            "im selben Thread landet (NIEMALS einen neuen Thread starten). Empfänger "
            "NICHT manuell überschreiben — die Antwort übernimmt die korrekten "
            "Empfänger (To + CC der Diskussion) automatisch. (Hinweis: Das Backend "
            "erzwingt die Thread-Zugehörigkeit ohnehin deterministisch; ein neuer "
            "Thread wird automatisch korrigiert. Bei task übernimmt das Backend die "
            "Task-Erstellung automatisch.)"
        )

    return f"""{correction_block}{skill_section}

---

{projects_context}
{routing_hint}{thread_task_hint}{recall_block}{rules_block}
---

## AKTUELLER JOB

Du hast einen email_triage Job erhalten. Führe den kompletten Triage-Ablauf gemäss dem email-triage-Skill durch.

**Job-ID:** {job.id}
**E-Mail Message-ID:** {email_id}
**Betreff:** {subject}
**Von:** {from_name} <{from_addr}>
**Empfänger-Typ:** {recipient_type} {"(Anthony ist direkter Empfänger im TO)" if recipient_type == "to" else "(Anthony ist NUR im CC)" if recipient_type == "cc" else "(nicht eindeutig bestimmbar)"}
**Microsoft Inference:** {inference}
**Body-Vorschau:** {preview[:300]}
{auto_hint}{recipient_hint}{thread_hint}
{style_section}{sender_style_block}
## PFLICHT-AUFRUFE VOR JEDER KLASSIFIKATION UND DRAFT-ERSTELLUNG

Du MUSST die folgenden drei Kontext-Quellen laden, BEVOR du klassifizierst oder einen Draft erstellst:
1. **get_thread("{conversation_id or ''}")** -- Thread-Kontext laden (PFLICHT falls conversation_id vorhanden)
2. **search_sender_history("{from_addr}")** -- Absender-History laden (IMMER PFLICHT)
3. **get_sender_profile("{from_addr}")** -- Absender-Profil laden (IMMER PFLICHT)

Erstelle NIEMALS einen Draft ohne diese drei Kontext-Quellen geladen zu haben!

---

WICHTIG: Befolge die Prioritätsreihenfolge (Stufe 1 → Stufe 2 → Stufe 3) STRIKT.
- Prüfe ZUERST ob Stufe 1 (Signale) zutrifft.
- Prüfe DANN ob Stufe 2 (System) zutrifft.
- Nur wenn weder Stufe 1 noch 2 passen, wende Stufe 3 (Standardregeln) an.

Führe jetzt den Triage-Ablauf durch:
1. Lies die E-Mail mit get_email("{email_id}"). Falls hasAttachments=true und Bildinhalt für die Einordnung relevant sein könnte (Screenshot, gescanntes Dokument, Bild-Newsletter), rufe get_email_attachments("{email_id}") auf und werte jeden Bild-Anhang mit vision_analyze(image_url=<path>, user_prompt="Beschreibe den Inhalt für die E-Mail-Triage") aus.
2. Lies die Kategorien mit get_email_categories("{email_id}")
3. Lade Thread-Kontext, Absender-History und Absender-Profil (PFLICHT!)
4. Klassifiziere gemäss der Prioritätsreihenfolge
5. Setze die Outlook-Kategorie
6. Verschiebe bei Bedarf (System/Newsletter/Junk/Kalender)
{draft_step}
8. Gib den PFLICHT-JSON-Block aus (Schema im Skill bzw. references/triage-rules.md)
9. Aktualisiere das Absender-Profil mit update_sender_profile (siehe Skill)

Status und Output werden automatisch aus deiner finalen Antwort gespeichert -- rufe update_agent_job NICHT selbst auf.
""" + (f"\n\n## ZUSÄTZLICHE BENUTZER-REGELN (haben Vorrang!)\n{custom_triage_prompt}" if custom_triage_prompt else "")


async def _build_draft_prompt(
    meta: dict,
    parsed: dict | None = None,
    dossier: str | None = None,
    researched: bool = False,
) -> str:
    """Baut den fokussierten Schreib-Prompt fuer den Zwei-Pass-Draft.

    Einzige Aufgabe: den besten Antwort-Entwurf in Anthonys Stimme schreiben --
    getrennt von der Klassifikation, ohne JSON-/Move-/Task-Druck. Der Prompt
    enthaelt den vollstaendigen E-Mail-Body (server-seitig geladen), das Briefing
    aus der Klassifikation (``parsed``), gelernte Stil-Anker sowie Kontext (Profil,
    Regeln, Lektionen, Datum) und erstellt den Entwurf mit erzwungenem
    ``reply_to_id`` im selben Thread.

    ``dossier`` ist das Ergebnis des Sammel-Laufs (Pass 2a). ``researched``
    unterscheidet die zwei Faelle, die sonst gleich aussaehen: wurde recherchiert und
    nichts gefunden, mahnt der Prompt zur Zurueckhaltung; wurde gar nicht recherchiert
    (Terminanfrage, kein Substanzbedarf), entfaellt der Abschnitt ersatzlos -- ein
    "nichts gefunden" waere dort schlicht falsch.
    """
    email_id = meta.get("email_message_id", "")
    from_addr = meta.get("from_address", "")
    from_name = meta.get("from_name", "")
    subject = meta.get("subject", "")
    conversation_id = meta.get("conversation_id", "")
    preview = (meta.get("body_preview") or "")[:300]

    style_native = _style_skill_available()
    if style_native:
        style_section = (
            "## SCHREIBSTIL (ZUERST laden und strikt befolgen)\n"
            "→ **skill_view(name='email-style')** -- natürliche Stimme, Anrede-/"
            "Register-Spiegelung, Tonalitätsstufen, Self-Review.\n"
        )
    else:
        style_text = _load_style_profile()
        style_section = (
            "## SCHREIBSTIL (VERBINDLICH)\n\n"
            f"{style_text}\n" if style_text else ""
        )
    sender_style_block = await _build_sender_style_block(from_addr)
    rules_block = await _build_rules_block("draft")
    recall_block = await _build_recall_block(meta)
    anchors_block = await _build_style_anchor_block(meta)
    from zoneinfo import ZoneInfo

    heute = datetime.now(ZoneInfo("Europe/Zurich")).date()
    abwesenheiten = await _absence_ranges()
    absence_block = _build_absence_block(abwesenheiten, heute)
    calendar_step = _build_calendar_draft_step(
        subject, preview, _first_available_day(abwesenheiten, heute)
    )

    # Vollstaendigen Body server-seitig laden (kein Verlass auf get_email-Tool).
    body_text = await _load_email_body_text(email_id)
    body_block = body_text or preview or "(kein Textinhalt verfügbar)"
    briefing_block = _build_draft_briefing(parsed)
    today = _today_context_line()
    draft_tool = _draft_tool_name()

    thread_load = (
        f'→ **get_thread("{conversation_id}")** -- vollständiger Verlauf, falls der Kontext unklar ist.\n'
        if conversation_id else ""
    )

    # Der eigentliche Schreib-Auftrag kommt aus ``draft_prompt`` -- dieselbe Quelle,
    # die auch das Offline-Eval nutzt. Vorher pflegte das Eval eine eigene Fassung
    # und mass damit ein System, das nie in Produktion lief.
    task_block = render_draft_task(
        today=today,
        email_id=email_id,
        subject=subject,
        from_name=from_name,
        from_addr=from_addr,
        body_block=body_block,
        thread_load=thread_load,
        calendar_step=calendar_step,
        draft_tool=draft_tool,
    )

    # Das Dossier steht bewusst NACH dem Briefing und VOR dem Schreibauftrag: es ist
    # Sachgrundlage, nicht Stilvorgabe, und soll unmittelbar vor dem Auftrag praesent
    # sein.
    dossier_block = render_dossier_block(dossier or "") if researched else ""

    return (
        f"{style_section}{sender_style_block}{anchors_block}{rules_block}"
        f"{recall_block}{briefing_block}{dossier_block}\n---\n\n{absence_block}{task_block}"
    )


async def _build_chat_triage_prompt(job: AgentJob) -> str:
    """Baut den Prompt für einen chat_triage Job mit Kontext."""
    meta = job.metadata_json or {}
    chat_id = meta.get("chat_id", "")
    message_id = meta.get("chat_message_id", "")
    sender = meta.get("from_name", "")
    preview = meta.get("body_preview", "")

    projects_context = await _load_projects_context()
    rules_block = await _build_rules_block("triage", "chat")

    return f"""## CHAT-TRIAGE JOB

Du hast eine neue Microsoft Teams Chat-Nachricht erhalten. Analysiere und klassifiziere sie.
Sprache: Schweizer Hochdeutsch (ss statt scharfem S, korrekte Umlaute ä/ö/ü).

{projects_context}
{rules_block}
**Job-ID:** {job.id}
**Chat-ID:** {chat_id}
**Nachricht-ID:** {message_id}
**Absender:** {sender}
**Vorschau:** {preview[:300]}

## VORGEHEN

1. Lies die vollständige Nachricht mit den verfügbaren MCP-Tools.
2. Klassifiziere zurückhaltend (fail-closed): `task` nur, wenn klar eine konkrete
   Handlung von Anthony nötig ist. Reine Infos, Bestätigungen, Small Talk -> `fyi`.
3. Bei `task`: Erstelle den Task NICHT selbst -- das Backend legt aus deinem
   JSON-Block deterministisch einen Task-Vorschlag (mit Review-Schleife) an.
   Liefere dafür Titel, Kurzbeschreibung, passendes Projekt und ggf. Deadline.
4. Bei `fyi`: Nur zur Kenntnis nehmen.

## PFLICHT: JSON-Block am Ende

Gib als Letztes einen JSON-Block aus (ohne ihn kann das Backend die Einordnung nicht speichern):

```json
{{"triage_class": "task|fyi", "confidence": 0.0, "rationale": "kurze Begründung",
  "task_title": "nur bei task", "task_description": "nur bei task",
  "suggested_project": "Projektname oder null", "deadline": "YYYY-MM-DD oder null"}}
```

Status und Output werden automatisch aus deiner finalen Antwort gespeichert -- rufe update_agent_job NICHT selbst auf.
"""


async def _post_process_chat_triage(job_id, content: str, meta: dict | None = None) -> str:
    """Schreibt die Chat-Klassifikation nach dem LLM-Lauf in ``chat_triage`` zurueck.

    Analog zur E-Mail-Triage: ohne diesen Schritt blieb ``chat_triage.triage_class``
    dauerhaft NULL (Jobs wurden zwar abgeschlossen, aber die Einordnung nie
    persistiert). Fail-closed: ohne verwertbaren JSON-Block wird ``fyi`` gesetzt.
    """
    parsed = _extract_json_block(content)
    triage_class = None
    rationale = None
    confidence = None
    if parsed is not None:
        triage_class = parsed.get("triage_class")
        if triage_class == "quick_response":
            triage_class = "auto_reply"
        rationale = parsed.get("rationale")
        confidence = parsed.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
            if confidence is not None:
                if confidence > 1:
                    confidence = confidence / 100.0
                confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = None

    if triage_class not in ("task", "fyi", "auto_reply"):
        triage_class = "fyi"

    async with async_session() as db:
        # Deterministische Task-Erstellung (Paritaet zur E-Mail-Triage): Das
        # Backend legt den Vorschlag an (needs_review), nicht der Agent selbst.
        created_task = None
        if triage_class == "task":
            try:
                created_task = await _create_chat_task(
                    db,
                    job_id,
                    meta or {},
                    task_title=(parsed or {}).get("task_title"),
                    task_description=(parsed or {}).get("task_description"),
                    suggested_project=(parsed or {}).get("suggested_project"),
                    deadline=(parsed or {}).get("deadline"),
                )
            except Exception:  # noqa: BLE001 - Klassifikation trotzdem persistieren
                logger.exception("Job %s: Chat-Task konnte nicht erstellt werden", job_id)

        suggested_action = {
            "triage_class": triage_class,
            "rationale": rationale,
            "confidence": confidence,
            "fallback": parsed is None,
        }
        if created_task is not None:
            suggested_action["task_id"] = str(created_task.id)

        await db.execute(
            update(ChatTriage)
            .where(ChatTriage.agent_job_id == job_id)
            .values(
                triage_class=triage_class,
                confidence=confidence,
                suggested_action=suggested_action,
                status="acted",
            )
        )
        # Episode fuer das episodische Gedaechtnis (Lern-Paritaet mit der
        # E-Mail-Triage): Grundlage fuer Recall bei kuenftigen Chat-Triagen.
        meta = meta or {}
        summary = (
            f"Teams-Nachricht von {meta.get('from_name') or '?'}: "
            f"'{(meta.get('body_preview') or '')[:200]}'. "
            f"Triage-Entscheid: {triage_class}"
        )
        await record_episode(
            db,
            summary=summary,
            job_type="chat_triage",
            agent_job_id=job_id,
            decision={"triage_class": triage_class, "confidence": confidence},
        )
        await db.commit()
    logger.info("Job %s: Chat-Triage -> %s (confidence=%s)", job_id, triage_class, confidence)
    return "completed"


_ACTION_ITEMS_FENCE = re.compile(r"```(?:json)?\s*(\{[^`]*\"action_items\"[^`]*\})\s*```", re.DOTALL)


async def _post_process_meeting_summary(job_id, content: str, meta: dict | None = None) -> str:
    """Persistiert das Meeting-Protokoll (ohne automatische Task-Erstellung).

    - ``protocol_md`` = LLM-Output ohne den Action-Item-JSON-Block.
    - Notification ``meeting_summary_ready`` mit Link auf den Meetings-Tab.

    Hinweis: Die automatische Erstellung von Aufgaben aus Action-Items wurde
    bewusst ausgebaut -- die Trefferqualität war noch nicht gut genug (zu viele
    manuell abzulehnende Tasks). Der Action-Item-JSON-Block wird weiterhin aus dem
    Protokoll entfernt, damit kein Roh-JSON im Text landet. Eine Reaktivierung mit
    verbesserter Logik ist möglich (siehe Git-Historie).
    """
    meta = meta or {}
    transcript_id = meta.get("meeting_transcript_id")

    protocol_md = (content or "").strip()
    m = _ACTION_ITEMS_FENCE.search(protocol_md)
    if m:
        # JSON-Block nur noch entfernen (nicht mehr in Tasks überführen).
        protocol_md = (protocol_md[: m.start()] + protocol_md[m.end():]).strip()

    created_count = 0
    async with async_session() as db:
        if transcript_id:
            try:
                record = await db.get(MeetingTranscript, uuid.UUID(transcript_id))
            except ValueError:
                record = None
            if record is not None:
                record.protocol_md = protocol_md[:64000]
                record.status = "completed"
                from app.services.notification import notify_meeting_summary_ready

                await notify_meeting_summary_ready(
                    db,
                    transcript_id=record.id,
                    subject=record.subject,
                    action_item_count=created_count,
                )
        await db.commit()

    logger.info("Job %s: Meeting-Protokoll gespeichert (Task-Erstellung deaktiviert)", job_id)
    return "completed"


def _format_task_context(task: Task) -> str:
    """Baut den vollständigen Auftragskontext einer Task für den Agenten.

    Nutzt ausschliesslich bestehende Task-Relationen (Titel, Beschreibung,
    Checkliste, Anhänge, Tags, externe Referenzen) -- keine neuen Attribute.
    Leere Bereiche werden weggelassen, damit der Prompt nicht verrauscht.
    """
    parts: list[str] = [f"**{task.title}**"]
    if task.description:
        parts.append(task.description)

    # Checkliste als konkrete Teilschritte (offen vs. erledigt, in Reihenfolge)
    items = sorted(task.checklist_items or [], key=lambda c: c.position)
    if items:
        offen = sum(1 for c in items if not c.is_checked)
        lines = [f"- [{'x' if c.is_checked else ' '}] {c.text}" for c in items]
        parts.append(
            f"### Checkliste ({offen} offen / {len(items)} total)\n" + "\n".join(lines)
        )

    # Anhänge: Dateinamen + Hinweis. Die extrahierten Textinhalte werden vom
    # Aufrufer (_build_generic_prompt) separat als 'Anhang-Inhalte' eingebettet.
    attachments = task.attachments or []
    if attachments:
        lines = [
            f"- {a.filename} ({a.mime_type or 'unbekannt'}) → {a.filepath}"
            for a in attachments
        ]
        parts.append(
            "### Anhänge\n"
            "Die extrahierten Textinhalte der Anhänge stehen unten unter "
            "'Anhang-Inhalte'. Bilder analysierst du bei Bedarf mit vision_analyze, "
            "OneDrive-Dateien (onedrive://) lädst du bei Bedarf mit download_file "
            "nach:\n" + "\n".join(lines)
        )

    # Tags als Themen-/Kategorie-Hinweis
    tags = task.tags or []
    if tags:
        parts.append("**Tags:** " + ", ".join(t.name for t in tags))

    # Externe Referenzen: gezielt per MCP nachladbar
    refs: list[str] = []
    if task.email_message_id:
        refs.append(f"E-Mail message_id={task.email_message_id} (Graph-MCP)")
    if task.email_conversation_id:
        refs.append(f"E-Mail conversation_id={task.email_conversation_id} (Graph-MCP)")
    if task.calendar_event_id:
        refs.append(f"Kalender event_id={task.calendar_event_id} (Graph-MCP)")
    if task.pipedrive_deal_id:
        refs.append(f"Pipedrive deal_id={task.pipedrive_deal_id} (Pipedrive-MCP)")
    if task.pipedrive_person_id:
        refs.append(f"Pipedrive person_id={task.pipedrive_person_id} (Pipedrive-MCP)")
    if refs:
        parts.append(
            "### Verknüpfte Referenzen\n"
            "Lade den Kontext bei Bedarf gezielt per MCP nach:\n"
            + "\n".join(f"- {r}" for r in refs)
        )

    return "\n\n".join(parts)


async def _resolve_task_attachment_context(task: Task) -> str:
    """Extrahiert die Inhalte der Task-Anhänge als `<attached_files>`-Block.

    Lokale Uploads (Pfad unter `/uploads/...`) und OneDrive-Referenzen
    (`onedrive://{item_id}`) werden via `context_resolver` aufgelöst, damit der
    Agent die Dokumentinhalte direkt im Prompt vorfindet statt nur Metadaten.
    Bilder/nicht-Text-Formate werden vom Resolver entsprechend markiert.
    """
    attachments = task.attachments or []
    if not attachments:
        return ""

    sources: list[dict] = []
    for a in attachments:
        path = a.filepath or ""
        if path.startswith("onedrive://"):
            sources.append({
                "type": "onedrive_file",
                "item_id": path[len("onedrive://"):],
                "name": a.filename,
            })
        elif path.startswith("/uploads/"):
            sources.append({
                "type": "local_upload",
                "upload_id": path[len("/uploads/"):],
                "name": a.filename,
            })

    if not sources:
        return ""

    from app.services.context_resolver import resolve_context_sources

    graph_client = None
    if any(s["type"].startswith("onedrive") for s in sources):
        from app.services.graph import get_graph_client

        graph_client = get_graph_client()

    try:
        ctx = await resolve_context_sources(sources, graph_client)
        return ctx.to_llm_context()
    except Exception:  # noqa: BLE001 - best-effort, darf den Job nie blockieren
        logger.exception("Auflösung der Task-Anhänge fehlgeschlagen")
        return ""


async def _build_generic_prompt(job: AgentJob) -> str:
    """Baut einen kontextreichen Prompt für generische AgentJobs."""
    meta = job.metadata_json or {}
    projects_context = await _load_projects_context()

    skill_hint = ""
    skill_name = meta.get("skill")
    canonical_skill = _SKILL_NAME_ALIASES.get(skill_name, skill_name) if skill_name else None
    if canonical_skill:
        native_path = HERMES_HOME / "skills" / canonical_skill / "SKILL.md"
        legacy_path = HERMES_HOME / "skills" / f"{skill_name}.md"
        if native_path.exists():
            # Hermes-native: Agent laedt den Skill selbst (Progressive Disclosure).
            skill_hint = (
                f"\n## SKILL (NATIV LADEN)\n\nLade zuerst den Skill und befolge ihn strikt:\n"
                f"→ **skill_view(name='{canonical_skill}')**\n"
            )
        elif legacy_path.exists():
            skill_hint = f"\n## SKILL-INSTRUKTIONEN\n\n{legacy_path.read_text(encoding='utf-8')}\n"

    style_hint = ""
    if canonical_skill in ("quick-response", "email-triage"):
        if _style_skill_available():
            style_hint = (
                "\n## SCHREIBSTIL (bei jedem Antwort-Entwurf)\n\n"
                "Bevor du einen Antwort-Entwurf formulierst, lade den persönlichen "
                "Schreibstil-Kanon: → **skill_view(name='email-style')**\n"
            )
        else:
            style_text = _load_style_profile()
            if style_text:
                style_hint = (
                    "\n## SCHREIBSTIL (VERBINDLICH für jeden Antwort-Entwurf)\n\n"
                    "Halte dich strikt an den folgenden persönlichen Schreibstil-Kanon "
                    "von Anthony Smith. Ziel: Anthony muss sich im Entwurf wiedererkennen.\n\n"
                    f"{style_text}\n"
                )

    description = meta.get("description") or meta.get("prompt")
    # Tasks, die im Cockpit/Board dem Agenten zugewiesen werden, tragen den Auftrag
    # in Titel + Beschreibung der verknüpften Task (nicht in metadata_json). Ohne das
    # Nachladen bekäme der Agent einen leeren Auftrag ("nichts zu tun"). Wir laden den
    # gesamten vorhandenen Task-Kontext (Checkliste, Anhänge, Tags, Referenzen) nach.
    if not description and job.task_id:
        async with async_session() as db:
            task = (
                await db.execute(
                    select(Task)
                    .options(
                        selectinload(Task.checklist_items),
                        selectinload(Task.attachments),
                        selectinload(Task.tags),
                    )
                    .where(Task.id == job.task_id)
                )
            ).scalar_one_or_none()
        if task:
            description = _format_task_context(task)
            attached = await _resolve_task_attachment_context(task)
            if attached:
                description += "\n\n### Anhang-Inhalte\n\n" + attached
    if not description:
        description = str(meta)

    # Leitregeln je nach Job-Typ: Chat-Agent -> 'chat', E-Mail-Versand -> 'draft',
    # delegierte Tasks/sonstige Jobs -> 'task'. 'general' wirkt immer mit
    # (siehe _build_rules_block) -- frueher fiel der Default faelschlich auf
    # 'triage' zurueck, obwohl Task-Jobs keine Triage sind.
    _rule_contexts = {
        "chat_agent": ("chat",),
        "send_email": ("draft",),
    }.get(job.job_type or "", ("task",))
    rules_block = await _build_rules_block(*_rule_contexts)

    # Gelernte Lektionen aus frueheren Jobs desselben Typs (Lern-Paritaet mit
    # der E-Mail-Triage). Query aus dem Auftragstext statt E-Mail-Metadaten.
    recall_block = await _build_recall_block(
        meta, job_type=job.job_type or "task", query=str(description)[:400],
    )

    return f"""## AGENT-JOB

Heute ist {_today_context_line()} (Europe/Zurich).

{projects_context}
{skill_hint}{style_hint}{rules_block}{recall_block}

**Job-ID:** {job.id}
**Job-Typ:** {job.job_type or 'generic'}
**Auftrag:** {description}

Führe den Auftrag aus und gib dein **vollständiges** Ergebnis direkt als finale Antwort aus -- formatiert als Markdown (Überschriften, Listen, Fettungen, wo sinnvoll). Deine Antwort selbst ist das gespeicherte Resultat; es gibt keinen separaten Speicherort und keine "Kurzfassung". Rufe update_agent_job NICHT selbst auf -- Status und Output werden automatisch aus deiner finalen Antwort gespeichert.
"""


# ── Briefing-Prompt (Daily/Weekly/Monthly) ───────────────

_BRIEFING_INSTRUCTIONS: dict[str, str] = {
    "daily_briefing": (
        "Erstelle das **Tagesbriefing** — ein Wächter für das, was sonst durchrutscht. "
        "Maximal ~150 Wörter.\n\n"
        "Anthony kennt seine Aufgaben und sieht seinen Kalender. Er braucht KEINE "
        "Priorisierung, KEINE Top-3-Liste und keine Wiederholung von Terminen, "
        "Fälligkeiten oder Freigaben — das alles steht im Cockpit. Dein einziger "
        "Auftrag: die Randnotizen sichtbar machen, die zwischen den Terminen "
        "verschwinden.\n\n"
        "Schreibe eine kurze Liste. Pro Punkt EIN Satz mit der konkreten Handlung "
        "(«Bei X nachfassen», «Protokoll zu Y erstellen»). Ordne nach Dringlichkeit: "
        "was heute passieren muss, steht oben.\n\n"
        "Wenn die Datenlage keine Auffälligkeiten enthält, schreibe genau einen Satz: "
        "dass nichts liegt. Erfinde keine Sektionen, um Länge zu erzeugen."
    ),
    "weekly_briefing": (
        "Erstelle das **Wochenbriefing** — ein Planungsinstrument für die kommende Woche, "
        "kein Statusreport. Maximal ~300 Wörter.\n"
        "1. **Plan vs. Ist**: Wo die Abweichung über 30% liegt. Bei Mehraufwand nennst du "
        "den Geldwert aus der Datenlage und stellst die Frage, ob er verrechnet ist — das "
        "ist der wichtigste Punkt des Briefings. Rechne NICHT selbst, der Betrag steht da.\n"
        "2. **Planungslücken**: Wo sind Stunden geplant, aber keine oder zu wenige Aufgaben "
        "erfasst? Benenne jede Lücke direkt — dort fehlt die Planung, nicht die Zeit.\n"
        "3. **Liegengeblieben**: Überfälliges, je mit dem konkreten nächsten Schritt.\n"
        "4. **Slot-Vorschläge**: Ordne den freien Kalenderfenstern konkrete Aufgaben zu "
        "(Fälligkeit und Vorbereitungsbedarf zuerst). Was in der übernächsten Woche ansteht "
        "und Vorlauf braucht, gehört in die Fenster DIESER Woche. Als Vorschlag "
        "kennzeichnen — Anthony entscheidet und bucht.\n"
        "5. **Risiken**: Überbuchung, Deadline-Kollisionen — nur echte, keine hypothetischen."
    ),
    "monthly_briefing": (
        "Erstelle das **Monatsbriefing** — die Vorbereitung von Anthonys Monatsplanung. "
        "Maximal ~350 Wörter.\n\n"
        "Es geht NICHT um Geschäftszahlen: Umsatz, Liquidität, Debitoren, Pipeline und "
        "Kapazitätsauslastung prüft Anthony in den dafür gebauten Ansichten. Nenne keine "
        "solchen Zahlen, auch nicht schätzungsweise.\n"
        "1. **Blick nach vorne — was steht an**: Was prägt den kommenden Monat (verfügbare "
        "Arbeitstage, Abwesenheiten, Reisen, grosse Termine) und welche Fristen fallen "
        "hinein.\n"
        "2. **Was fehlt**: Projekte mit eingeplanter Kapazität, aber ohne (genügend) "
        "erfasste Aufgaben — dort muss die Planung JETZT beginnen. Jedes einzeln benennen, "
        "mit dem Datum der ersten Allokation.\n"
        "3. **Vorlauf**: Was erst nach dem kommenden Monat fällig ist, aber jetzt begonnen "
        "werden muss — insbesondere vor Abwesenheiten.\n"
        "4. **Rückblick** (kurz, max. 4 Sätze): Was liegengeblieben ist und wo Projekte "
        "still standen. Benenne Stillstand beim Namen, ohne zu dramatisieren.\n"
        "5. **Empfehlungen** (max. 5): jede mit konkretem Datum, bis wann sie angegangen "
        "sein muss."
    ),
}


async def _build_briefing_prompt(job: AgentJob) -> str:
    """Baut den Prompt für Briefing-Jobs: injizierter Datenkontext + Syntheseauftrag.

    Der komplette Zahlen-Kontext kommt deterministisch aus ``briefing_data``
    (metadata_json.context_markdown) -- das Modell synthetisiert nur noch.
    """
    meta = job.metadata_json or {}
    briefing_type = meta.get("briefing_type") or job.job_type or "daily_briefing"
    context_md = meta.get("context_markdown") or "(Kein Datenkontext verfügbar)"
    instructions = _BRIEFING_INSTRUCTIONS.get(briefing_type, _BRIEFING_INSTRUCTIONS["daily_briefing"])

    rules_block = await _build_rules_block("general")

    return f"""## BRIEFING-AUFTRAG

Heute ist {_today_context_line()} (Europe/Zurich).

Du bist Anthonys persönlicher Assistent und erstellst sein Briefing.
{rules_block}
{context_md}

## AUFTRAG

{instructions}

## VERBINDLICHE REGELN

- Verwende AUSSCHLIESSLICH Zahlen und Fakten aus der obigen Datenlage. Erfinde NICHTS.
- Übernimm Zahlen WÖRTLICH (1:1) aus der Datenlage — rechne NIE um, aggregiere NIE
  selbst, bilde keine eigenen Summen oder Differenzen.
- Übernimm Zeitangaben genau so, wie sie dastehen. Kalenderwochen stehen mit Nummer
  UND Datumsspanne in den Sektionstiteln — leite die Wochennummer NIE aus einem Datum
  ab. Verwechsle Stundenangaben NIE mit Tagesangaben.
- Behaupte KEINE Zeiträume oder Verläufe, die nicht explizit in der Datenlage stehen
  (z. B. NICHT «liegt seit einer Woche», wenn dort nur «offen» steht).
- Sektionen ohne Inhalt komplett weglassen — schreibe NICHT «keine Auffälligkeiten».
- Profilwissen über Anthony (Gewohnheiten, bevorzugte Tagesstruktur) dient NUR als
  Kontext für Empfehlungen (z. B. Fokusarbeit morgens einplanen). Behaupte NIE
  Tagesabläufe oder Routinen als Fakt — für den konkreten Tag zählt ausschliesslich
  der Kalender in der Datenlage.
- Sei direkt und benenne unbequeme Muster beim Namen (z. B. wiederholte Ausfälle
  desselben Projekts, wachsender Rückstand, unrealistische Planung) — faktenbasiert,
  ohne Dramatisierung.
- Sektionen ohne Daten lässt du weg. Als «Quelle nicht konfiguriert» oder «nicht
  erreichbar» markierte Quellen erwähnst du gesammelt in EINEM Satz am Ende.
- Schreibe auf Deutsch (Schweizer Rechtschreibung: ss statt ß), direkt und knapp.
  Keine Floskeln, keine Einleitung wie «Gerne erstelle ich...».
- Du brauchst KEINE Tools aufzurufen -- alle Daten stehen oben. Gib das fertige
  Briefing direkt als finale Antwort aus.

## FORMAT (zuletzt geprüft, deshalb hier)

Kurze Listen und **Fettdruck** für das Wichtigste. `##`-Überschriften nur, wenn der
Auftrag oben mehrere Sektionen verlangt — bei einer einzigen Liste keine Überschrift.
Schreibe KEINE Markdown-Tabelle — kein `|`-Zeichen als Spaltentrenner, in keiner
Sektion, auch nicht für Slot-Vorschläge oder Gegenüberstellungen. Ein Briefing mit
Tabelle ist unbrauchbar und gilt als nicht erfüllter Auftrag.
"""


# ── Meeting-Protokoll-Prompt ─────────────────────────────

async def _build_meeting_summary_prompt(job: AgentJob) -> str:
    """Prompt für ``meeting_summary``-Jobs: Transkript-Kontext + Protokollauftrag.

    Lange Transkripte werden vorab per Map-Reduce (Chunk-Zusammenfassungen,
    lokales Modell) verdichtet -- das Original bleibt unverändert in der DB.
    """
    from app.services.meetings import DIRECT_PROMPT_MAX_CHARS, summarize_transcript_chunks

    meta = job.metadata_json or {}
    transcript_id = meta.get("meeting_transcript_id")
    record = None
    if transcript_id:
        async with async_session() as db:
            record = await db.get(MeetingTranscript, uuid.UUID(transcript_id))

    if record is None or not (record.transcript_text or "").strip():
        return (
            "## MEETING-PROTOKOLL\n\nEs liegt kein Transkript-Text vor. Antworte mit "
            "einem kurzen Hinweis, dass das Transkript fehlt -- erfinde keinen Inhalt."
        )

    subject = record.subject or "(ohne Betreff)"
    when = record.started_at.strftime("%d.%m.%Y %H:%M") if record.started_at else "?"
    text = record.transcript_text
    if len(text) > DIRECT_PROMPT_MAX_CHARS:
        logger.info(
            "Job %s: Transkript %d Zeichen -> Map-Reduce-Verdichtung", job.id, len(text)
        )
        text = await summarize_transcript_chunks(text)
        context_label = "Verdichtete Abschnitts-Zusammenfassungen des Transkripts"
    else:
        context_label = "Vollständiges Transkript (sprecher-attribuiert)"

    rules_block = await _build_rules_block("general")

    return f"""## MEETING-PROTOKOLL ERSTELLEN

Heute ist {_today_context_line()}.

**Meeting:** {subject}
**Zeitpunkt:** {when}
{rules_block}
## {context_label}

{text}

## AUFTRAG

Erstelle ein strukturiertes Meeting-Protokoll (Deutsch, Schweizer Rechtschreibung:
ss statt ß) mit diesen Sektionen:

1. **Teilnehmende** (aus den Sprechernamen)
2. **Zusammenfassung** (3-5 Sätze: Anlass, Kernergebnis)
3. **Besprochene Themen** (pro Thema 2-4 Stichpunkte mit den relevanten Details)
4. **Entscheidungen** (klar getroffene Entscheide, mit wer/was)
5. **Offene Punkte** (unentschieden, vertagt, Klärungsbedarf)

## PFLICHT: Action-Items als JSON-Block am Ende

Gib als Letztes einen JSON-Block mit den konkreten Aufgaben aus, die sich aus dem
Meeting ergeben (nur echte Zusagen/Handlungen, im Zweifel weglassen):

```json
{{"action_items": [{{"title": "kurzer Task-Titel", "description": "1-2 Sätze Kontext",
  "owner": "Name oder 'Anthony'", "deadline": "YYYY-MM-DD oder null",
  "suggested_project": "Projektname oder null"}}]}}
```

Verwende AUSSCHLIESSLICH Informationen aus dem Transkript. Erfinde nichts.
Du brauchst keine Tools -- gib das Protokoll direkt als finale Antwort aus.
"""


# ── Post-Processing (framework-agnostisch) ───────────────

def _loads_lenient(raw: str) -> dict | None:
    """Parst einen JSON-Objekt-String tolerant.

    Lokale Modelle liefern den Pflicht-Block oft leicht abweichend: Code-Fence-
    Reste, trailing commas oder Python-Stil mit einfachen Anfuehrungszeichen.
    Diese Funktion versucht der Reihe nach: striktes JSON, JSON ohne trailing
    commas, und als letzter Ausweg ``ast.literal_eval`` (Python-Dict-Literal).
    Gibt nur dict zurueck (sonst None).
    """
    if not raw:
        return None
    s = raw.strip().strip("`").strip()
    # 1) Striktes JSON (deckt true/false/null korrekt ab).
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    # 2) Trailing commas vor schliessender Klammer entfernen.
    try:
        obj = json.loads(re.sub(r",(\s*[}\]])", r"\1", s))
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    # 3) Python-Dict-Literal (einfache Quotes). literal_eval kennt kein
    #    true/false/null -> nur als letzter Ausweg, daher zuvor mappen.
    try:
        py = re.sub(r"\btrue\b", "True", s)
        py = re.sub(r"\bfalse\b", "False", py)
        py = re.sub(r"\bnull\b", "None", py)
        obj = ast.literal_eval(py)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError, TypeError):
        pass
    return None


def _iter_balanced_objects(text: str):
    """Liefert alle klammer-balancierten ``{...}``-Teilstrings (String-/Escape-sicher).

    Im Gegensatz zu einer flachen Regex erfasst dies auch Objekte mit
    verschachtelten Feldern (z. B. ``categories``-Arrays oder Sub-Objekte).
    """
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : i + 1]
                    start = -1


def _extract_json_block(content: str) -> dict | None:
    """Extrahiert den Triage-JSON-Block robust aus dem LLM-Output.

    Lokale Modelle formatieren den Pflicht-Block inkonsistent: Fence fehlt,
    Felder sind verschachtelt, die Reihenfolge weicht ab oder es steht Prosa
    drumherum. Frueher griff nur eine sehr enge Regex (Fence ODER flaches
    ``{...}`` mit ``label`` UND ``triage_class`` ohne Verschachtelung) -- ~11%
    der Jobs fielen deshalb still durch (keine Klasse persistiert).

    Diese Implementierung scannt alle klammer-balancierten Objekte, parst sie
    tolerant und waehlt das **letzte** valide Objekt mit ``triage_class`` (der
    Abschluss-Block steht in der Regel am Ende der Antwort). Gibt None zurueck,
    wenn nichts Verwertbares vorhanden ist -- der Aufrufer eskaliert dann
    (Retry/Fallback), statt still zu verwerfen.
    """
    if not content:
        return None

    candidates: list[dict] = []
    for raw in _iter_balanced_objects(content):
        obj = _loads_lenient(raw)
        if isinstance(obj, dict):
            candidates.append(obj)

    if not candidates:
        return None

    for obj in reversed(candidates):
        if "triage_class" in obj:
            return obj
    # Kein Objekt mit triage_class -> bestmoegliches letztes Objekt (Best-Effort).
    return candidates[-1]


def _match_project(suggested_name: str | None, projects: list) -> tuple | None:
    """Matched einen Projektnamen gegen die DB-Projekte (case-insensitive contains)."""
    if not suggested_name or not projects:
        return None
    name_lower = suggested_name.lower()
    for p in projects:
        if name_lower in p.name.lower() or p.name.lower() in name_lower:
            return p
    return None


def _determine_pipeline_column(deadline_str: str | None) -> str | None:
    """Bestimmt die Pipeline-Spalte basierend auf der Deadline."""
    if not deadline_str:
        return PIPELINE_COLUMNS["this_week"]
    try:
        deadline = date.fromisoformat(deadline_str)
    except ValueError:
        return PIPELINE_COLUMNS["this_week"]

    today = date.today()
    delta = (deadline - today).days

    if delta <= 1:
        return PIPELINE_COLUMNS["focus"]
    if delta <= 7:
        return PIPELINE_COLUMNS["this_week"]
    if delta <= 14:
        return PIPELINE_COLUMNS["next_week"]
    return PIPELINE_COLUMNS["this_month"]


async def _build_graph_client():
    """Baut einen GraphClient aus den Settings (oder None, wenn nicht konfiguriert)."""
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return None
    import sys as _sys

    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
    from graph_client import GraphClient, GraphConfig  # noqa: E402

    return GraphClient(GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    ))


_WEEKDAYS_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag",
]


def _today_context_line() -> str:
    """Heutiges Datum + Wochentag in Europe/Zurich (fuer terminbezogene Antworten)."""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Europe/Zurich"))
    return f"{_WEEKDAYS_DE[now.weekday()]}, {now.strftime('%d.%m.%Y')}"


async def _load_email_body_text(email_id: str, cap: int = 4000) -> str:
    """Laedt den vollstaendigen E-Mail-Body server-seitig (HTML->Text, gekappt).

    Wird direkt in den Draft-Prompt eingebettet, damit der Schreib-Pass den echten
    Inhalt kennt, ohne auf einen (fehleranfaelligen) get_email-Tool-Call angewiesen
    zu sein -- verhindert Halluzinationen bei abgekuerztem Vorgehen. Der zitierte
    Original-Thread wird fuer einen fokussierten Prompt entfernt. Best-effort:
    liefert "" bei fehlender Graph-Konfiguration oder Fehler.
    """
    if not email_id:
        return ""
    client = await _build_graph_client()
    if client is None:
        return ""
    try:
        from app.services.learning import html_to_text, strip_quoted_history

        msg = await client.get_email(email_id)
        body = msg.get("body", {}) or {}
        raw = body.get("content") or msg.get("bodyPreview") or ""
        text_body = html_to_text(raw) if raw else ""
        # Zitierten Verlauf abtrennen (der eigentliche neue Inhalt zaehlt); faellt
        # der Trim leer aus (Marker am Anfang), bleibt der volle Text erhalten.
        text_body = strip_quoted_history(text_body) or text_body
        if len(text_body) > cap:
            text_body = text_body[:cap].rstrip() + " […]"
        return text_body
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Draft: voller Body konnte nicht geladen werden (email_id=%s)", str(email_id)[:40])
        return ""
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def _build_draft_briefing(parsed: dict | None) -> str:
    """Reicht die Begruendung des Klassifikations-Passes als Antwort-Briefing weiter.

    Pass 1 hat die volle Mail gelesen und begruendet (``rationale``), warum geantwortet
    wird -- dieses Signal geht sonst verloren. Rein und damit testbar.
    """
    parsed = parsed or {}
    rationale = (parsed.get("rationale") or "").strip()
    label = (parsed.get("label") or "").strip()
    if not rationale and not label:
        return ""
    lines: list[str] = []
    if label:
        lines.append(f"- Einordnung: {label}")
    if rationale:
        lines.append(f"- Weshalb eine Antwort nötig ist: {rationale}")
    return (
        "\n---\n\n## BRIEFING AUS DER KLASSIFIKATION (das soll die Antwort leisten)\n"
        + "\n".join(lines) + "\n"
    )


async def _delete_draft(draft_id: str) -> None:
    """Entfernt einen Entwurf aus Outlook. Best-effort, darf den Job nie stoppen.

    Wird gebraucht, wenn ein Entwurf verworfen wird (etwa ein Platzhalter aus dem
    Klassifikations-Lauf): ohne Loeschung bliebe er als Geist-Entwurf in Outlook
    stehen, ohne Bezug zu einem Job und ohne dass ihn jemand vermisst.
    """
    client = await _build_graph_client()
    if client is None:
        return
    try:
        await client.delete_message(draft_id)
        logger.info("Entwurf verworfen (draft_id=%s)", str(draft_id)[:40])
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning(
            "Entwurf konnte nicht geloescht werden (draft_id=%s)", str(draft_id)[:40]
        )
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def _snapshot_agent_draft(draft_id: str) -> dict | None:
    """Liest den vom Agenten erstellten Entwurf (Body + Empfaenger + conversationId).

    Dient als Original-Referenz fuer den spaeteren Stil-Diff (Lernsignal). Best-effort.
    """
    client = await _build_graph_client()
    if client is None:
        return None
    try:
        msg = await client.get_email(draft_id)
        body = msg.get("body", {}) or {}
        return {
            "body_html": body.get("content") if body.get("contentType") == "html" else msg.get("bodyPreview"),
            "conversation_id": msg.get("conversationId"),
            "to": [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])],
            "cc": [r.get("emailAddress", {}).get("address", "") for r in msg.get("ccRecipients", [])],
        }
    except Exception:  # noqa: BLE001 - best-effort, darf Job nicht stoppen
        logger.warning("Draft-Snapshot fehlgeschlagen (draft_id=%s)", str(draft_id)[:40])
        return None
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def _finalize_email_state(
    meta: dict,
    label: str | None,
    moved_id: str | None,
    *,
    triage_class: str | None = None,
    needs_review: bool = False,
) -> None:
    """Der EINZIGE Schreibpfad auf den Outlook-Zustand nach der Triage.

    Das LLM klassifiziert, diese Funktion mutiert. Der Triage-Agent hat die
    zustandsveraendernden Graph-Tools nicht mehr im Toolset (siehe
    ``_TRIAGE_MCP_SERVERS`` und ``GRAPH_TOOL_MODE`` im Graph-MCP-Server) -- vorher
    schrieb er unvalidierte Strings direkt nach Outlook, was zu 80 erfundenen
    Kategorien und zu Moves echter Kundenmails fuehrte.

    Drei Schritte, in dieser Reihenfolge zwingend:

    1. **Move** nach ``move_target`` (Label + ``fyi`` + ``inferenceClassification
       == other``). Zuerst, weil ein Move die Graph-ID aendert -- danach zaehlt nur
       noch die neue ID.
    2. **Kategorie** aus dem validierten Label, IMMER gesetzt (nicht mehr nur als
       Luecken-Fueller). Genau hier entstand der Hauptmangel: 64 % der Mails
       blieben ohne ``label``, und ``Finanzen`` wurde faktisch nie vergeben.
    3. **``mark_as_unread``** als allerletzte Aktion -- immer. Ein
       ``set_categories``-PATCH kippt ``isRead`` in Exchange auf ``true``; nur wenn
       das ungelesen-Setzen zuletzt laeuft, bleibt die Mail sichtbar neu.

    ``moved_id`` deckt den Altfall ab, dass die ID bereits durch einen fremden
    Move gewandert ist. Best-effort und 404-tolerant (CC-only-Mails / veraltete
    IDs duerfen den Job nie stoppen).
    """
    final_mid = moved_id or meta.get("email_message_id")
    if not final_mid:
        return

    client = await _build_graph_client()
    if client is None:
        return
    try:
        # Schritt 1: Move -- deterministisch aus der Klassifikation, nicht vom LLM.
        target = move_target(
            label,
            triage_class,
            meta.get("inference_classification"),
            needs_review=needs_review,
        )
        if target and not moved_id:
            try:
                result = await client.move_to_folder(final_mid, target)
                new_mid = (result or {}).get("id")
                if new_mid:
                    final_mid = new_mid
                logger.info(
                    "Finalize: Mail nach '%s' verschoben (label=%s, fyi + other)",
                    target, label,
                )
            except ValueError:
                # get_or_create_folder wirft ValueError, wenn der Ordner fehlt.
                logger.warning("Finalize: Zielordner '%s' existiert nicht -- kein Move", target)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                logger.warning("Finalize: Move nach '%s' fehlgeschlagen (HTTP %s)", target, status)

        # Schritt 2: Kategorie IMMER aus dem validierten Label setzen.
        if label and label != NO_CATEGORY:
            try:
                await client.set_categories(final_mid, [label])
                logger.info("Finalize: Kategorie '%s' gesetzt", label)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 404:
                    logger.info("Finalize: Kategorie nicht setzbar (404, z. B. CC-only/veraltete ID)")
                else:
                    logger.warning("Finalize: Kategorie-Schritt fehlgeschlagen (HTTP %s)", status)

        # Schritt 3: IMMER und als letzte Aktion -- Mail auf ungelesen zuruecksetzen.
        try:
            await client.mark_as_unread(final_mid)
            logger.info("Finalize: Mail auf ungelesen gesetzt (mid=%s)", str(final_mid)[:40])
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                logger.info("Finalize: ungelesen nicht setzbar (404, z. B. CC-only/veraltete ID)")
            else:
                logger.warning("Finalize: ungelesen-Schritt fehlgeschlagen (HTTP %s)", status)
    except Exception:  # noqa: BLE001 - Finalisierung darf den Job nie stoppen
        logger.warning("Finalize: unerwarteter Fehler (mid=%s)", str(final_mid)[:40])
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def apply_label_category(message_id: str, label: str) -> bool:
    """Setzt eine korrigierte Kategorie in Outlook -- ohne Move, ohne Statusverlust.

    Fuer die manuelle Label-Korrektur aus dem Cockpit. Bewusst KEIN Move: eine
    Menschenkorrektur soll nichts wegraeumen (verschoben heisst aus dem Blick). Der
    Gelesen-Status wird erhalten, weil ein ``set_categories``-PATCH ihn in Exchange
    auf ``true`` kippt.

    Returns True, wenn die Kategorie gesetzt wurde.
    """
    client = await _build_graph_client()
    if client is None:
        return False
    try:
        was_unread = False
        try:
            state = await client.get_email_categories(message_id)
            was_unread = (state or {}).get("isRead") is False
        except httpx.HTTPStatusError:
            logger.info("Label-Korrektur: Zustand nicht lesbar (mid=%s)", str(message_id)[:40])

        await client.set_categories(message_id, [label])
        if was_unread:
            try:
                await client.mark_as_unread(message_id)
            except httpx.HTTPStatusError:
                logger.warning("Label-Korrektur: ungelesen nicht wiederherstellbar")
        logger.info("Label-Korrektur: Kategorie '%s' gesetzt (mid=%s)", label, str(message_id)[:40])
        return True
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logger.warning("Label-Korrektur fehlgeschlagen (HTTP %s)", status)
        return False
    except Exception:  # noqa: BLE001 - Korrektur darf den Request nie sprengen
        logger.warning("Label-Korrektur: unerwarteter Fehler (mid=%s)", str(message_id)[:40])
        return False
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def _normalize_conversation_id(value: str | None) -> str:
    """conversationId fuer den Vergleich normalisieren (None/Leerstring -> '')."""
    return (value or "").strip()


async def _ensure_draft_in_thread(
    draft_id: str,
    email_message_id: str,
    snapshot: dict | None,
) -> tuple[str, dict | None]:
    """Garantiert deterministisch, dass der Entwurf im Original-Thread liegt.

    Der Agent SOLL bei ``create_draft`` ``reply_to_id`` setzen, damit die Antwort
    via ``createReplyAll`` im selben Thread (gleiche ``conversationId``) landet und
    die korrekten Empfaenger uebernimmt. Verlaesst sich aber das LLM nicht darauf,
    entsteht ein NEUER Thread (``POST /messages``) -- Anthony und die Empfaenger
    sehen die urspruengliche Diskussion dann nicht.

    Diese Funktion prueft die ``conversationId`` des Entwurfs gegen den Original-
    Thread (ground truth via ``get_email``) und repariert bei Abweichung: Der
    Agent-Body wird in einen korrekten Reply-All-Entwurf uebernommen, der falsche
    Entwurf geloescht. Gibt ``(draft_id, snapshot)`` zurueck -- bei Reparatur die
    neuen Werte. Best-effort: Fehler duerfen den Job nicht stoppen.
    """
    if not (draft_id and email_message_id and snapshot):
        return draft_id, snapshot

    client = await _build_graph_client()
    if client is None:
        return draft_id, snapshot
    try:
        original = await client.get_email(email_message_id)
        original_conv = _normalize_conversation_id(original.get("conversationId"))
        draft_conv = _normalize_conversation_id(snapshot.get("conversation_id"))

        # Original-conversationId unbekannt -> keine verlaessliche Aussage moeglich.
        if not original_conv:
            logger.warning(
                "Thread-Check: Original-conversationId fehlt (email_message_id=%s), "
                "ueberspringe Reparatur",
                str(email_message_id)[:40],
            )
            return draft_id, snapshot

        if draft_conv == original_conv:
            return draft_id, snapshot

        # Abweichung -> Agent hat einen neuen/falschen Thread erzeugt. Reparieren.
        logger.warning(
            "Thread-Check: Entwurf liegt im falschen Thread (draft_conv=%s != "
            "original_conv=%s), erstelle Reply-All im Original-Thread neu",
            draft_conv or "<leer>", original_conv,
        )
        body_html = snapshot.get("body_html") or ""
        subject = original.get("subject") or ""
        from_addr = (
            original.get("from", {}).get("emailAddress", {}).get("address")
            or original.get("sender", {}).get("emailAddress", {}).get("address")
            or ""
        )
        fixed = await client.create_draft(
            subject=subject,
            body_html=body_html,
            to_recipients=[from_addr] if from_addr else [],
            reply_to_id=email_message_id,
            reply_all=True,
        )
        new_draft_id = fixed.get("id")
        if not new_draft_id:
            logger.warning("Thread-Reparatur fehlgeschlagen: kein neue draft_id erhalten")
            return draft_id, snapshot

        # Falschen Entwurf entfernen (best-effort).
        try:
            await client.delete_message(draft_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Thread-Reparatur: falschen Entwurf konnte nicht geloescht werden "
                "(draft_id=%s)", str(draft_id)[:40],
            )

        new_snapshot = await _snapshot_agent_draft(new_draft_id)
        logger.info(
            "Thread-Reparatur erfolgreich: neue draft_id=%s im Original-Thread",
            str(new_draft_id)[:40],
        )
        return new_draft_id, (new_snapshot or snapshot)
    except Exception:  # noqa: BLE001 - darf den Job nie stoppen
        logger.warning(
            "Thread-Check fehlgeschlagen (draft_id=%s)", str(draft_id)[:40]
        )
        return draft_id, snapshot
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def _json_retry_prompt(prev: str) -> str:
    """Strikter Nachfass-Prompt: erzwingt NUR den Pflicht-JSON-Block.

    Wird genau einmal ausgefuehrt, wenn der erste Lauf keinen verwertbaren Block
    lieferte. Leitet die Werte aus der bereits erstellten Analyse ab, ohne erneut
    Tools zu bemuehen.
    """
    tail = (prev or "")[-4000:]
    return (
        "Deine vorherige Antwort enthielt keinen maschinenlesbaren Pflicht-JSON-Block. "
        "Gib jetzt AUSSCHLIESSLICH den JSON-Block aus -- kein Text davor oder danach, "
        "keine weiteren Tool-Aufrufe -- in einem ```json ... ``` Codeblock.\n"
        "Pflichtfelder: triage_class (genau einer von \"auto_reply\", \"task\", \"fyi\"), "
        "label, reply_expected (true/false), confidence (Zahl 0..1), rationale. "
        "Bei task zusaetzlich: task_title, task_description, suggested_project, deadline.\n\n"
        "Leite die Werte aus deiner bisherigen Analyse ab:\n---\n" + tail + "\n---\n"
    )


_INTERNAL_NOISE_RE = re.compile(
    r"(?:\b404\b|\b400\b|HTTPStatusError|Bad Request|Not Found|createReply(?:All)?|"
    r"per Graph[- ]?API|via Graph[- ]?API|Graph[- ]?API nicht|"
    r"l(?:ä|ae)sst sich (?:nicht|per|via)|liess sich (?:nicht|per|via))",
    re.IGNORECASE,
)


def _strip_internal_notes(text: str | None) -> str | None:
    """Entfernt interne API-/Fehler-Diagnosen aus nutzersichtbarem Text.

    Der Agent schreibt gelegentlich technische Hinweise (404, HTTPStatusError,
    createReplyAll, "via Graph API nicht lesbar") in task_description/Rationale.
    Solche Sätze lesen sich im Cockpit wie ein Fehlschlag ("ging nicht") und
    gehören nicht in die nutzersichtbare Aufgabe -- sie werden satzweise entfernt.

    Zeilenumbrüche und Absatzstruktur bleiben erhalten (Listen, Leerzeilen), damit
    Markdown im Frontend lesbar gerendert wird. Früher kollabierte die Funktion alle
    Zeilen zu einem Fliesstext -- Aufzählungen im Cockpit waren dadurch unlesbar.
    """
    if not text:
        return text
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        # Einrückung für verschachtelte Listen bewahren; Trailing-Spaces entfernen.
        # Komplett leere Zeilen bleiben als Absatztrenner erhalten.
        indent_len = len(raw_line) - len(raw_line.lstrip(" \t"))
        indent = raw_line[:indent_len]
        content = raw_line[indent_len:].rstrip()
        if not content:
            out_lines.append("")
            continue
        # Rauschen satzweise *innerhalb* der Zeile entfernen.
        parts = re.split(r"(?<=[.!?])\s+", content)
        kept = [p for p in parts if p.strip() and not _INTERNAL_NOISE_RE.search(p)]
        if not kept:
            continue
        cleaned_line = " ".join(s.strip() for s in kept)
        out_lines.append(indent + cleaned_line)
    cleaned = "\n".join(out_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or None


_SUBJECT_PREFIX_RE = re.compile(r"^(?:\s*(?:re|aw|fw|wg|fwd|antw| w)\s*:\s*)+", re.IGNORECASE)


def _normalize_subject(subject: str | None) -> str:
    """Normalisiert einen Betreff fuer den Duplikat-Vergleich.

    Entfernt wiederholte Antwort-/Weiterleitungs-Praefixe (RE:/AW:/FW:/WG: ...)
    und kollabiert Whitespace -- damit praktisch identische Betreffzeilen
    (z. B. wiederkehrende Fehler-Mails) als gleich erkannt werden.
    """
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = _SUBJECT_PREFIX_RE.sub("", s.strip())
    return re.sub(r"\s+", " ", s).strip().lower()


async def _find_duplicate_open_task(db, meta: dict) -> Task | None:
    """Sucht einen bereits offenen Task zur selben Sache (Konversation oder Absender+Betreff).

    Verhindert, dass aus vielen praktisch identischen E-Mails (z. B. wiederkehrende
    System-Fehlermeldungen) immer wieder neue, redundante Tasks entstehen. Gibt den
    aeltesten passenden **offenen** (nicht erledigten) Task zurueck oder ``None``.
    """
    conv = meta.get("conversation_id")
    from_addr = (meta.get("from_address") or "").strip().lower()
    norm_subject = _normalize_subject(meta.get("subject"))

    # Schneller Pfad: gleicher Thread hat bereits einen offenen Task.
    if conv:
        res = await db.execute(
            select(Task)
            .where(Task.email_conversation_id == conv, Task.is_completed.is_(False))
            .order_by(Task.created_at)
            .limit(1)
        )
        dup = res.scalar_one_or_none()
        if dup is not None:
            return dup

    # Absender + normalisierter Betreff: faengt wiederkehrende, praktisch
    # identische Mails, die je eine eigene Konversation haben (z. B. n8n-Alerts).
    if from_addr and norm_subject:
        res = await db.execute(
            select(Task, EmailTriage.subject)
            .join(EmailTriage, EmailTriage.message_id == Task.email_message_id)
            .where(
                Task.is_completed.is_(False),
                Task.email_message_id.isnot(None),
                func.lower(EmailTriage.from_address) == from_addr,
            )
            .order_by(Task.created_at)
            .limit(100)
        )
        for task, subj in res.all():
            if _normalize_subject(subj) == norm_subject:
                return task
    return None


async def _was_suggestion_dismissed(db, meta: dict, days: int = 14) -> bool:
    """True, wenn ein praktisch identischer Task-Vorschlag kürzlich verworfen wurde.

    Beim Verwerfen (dismiss-review) wird der Task gelöscht -- der Dedupe über
    offene Tasks greift dann nicht mehr. Der Marker ``task_dismissed`` auf der
    Quell-Triage (gleicher Absender + normalisierter Betreff) verhindert, dass
    z. B. der nächste identische n8n-Alert denselben Vorschlag wieder hochspült.
    """
    from_addr = (meta.get("from_address") or "").strip().lower()
    norm_subject = _normalize_subject(meta.get("subject"))
    if not from_addr or not norm_subject:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    res = await db.execute(
        select(EmailTriage.subject)
        .where(
            func.lower(EmailTriage.from_address) == from_addr,
            EmailTriage.created_at >= cutoff,
            EmailTriage.suggested_action["task_dismissed"].as_boolean() == True,  # noqa: E712
        )
        .limit(100)
    )
    for (subj,) in res.all():
        if _normalize_subject(subj) == norm_subject:
            return True
    return False


async def _append_duplicate_note(db, task: Task, meta: dict) -> None:
    """Dockt eine weitere Meldung als Checklisten-Eintrag an einen offenen Task an."""
    subj = meta.get("subject") or "(kein Betreff)"
    when = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")
    pos_res = await db.execute(
        select(ChecklistItem.position)
        .where(ChecklistItem.task_id == task.id)
        .order_by(ChecklistItem.position.desc())
        .limit(1)
    )
    next_pos = (pos_res.scalar_one_or_none() or 0) + 1
    db.add(
        ChecklistItem(
            task_id=task.id,
            text=f"Weitere Meldung am {when}: {subj}"[:500],
            is_checked=False,
            position=next_pos,
        )
    )
    task.updated_at = datetime.now(timezone.utc)
    await db.flush()


async def _create_review_task(
    db,
    job_id,
    *,
    title: str,
    description: str,
    suggested_project: str | None,
    deadline: str | None,
    email_conversation_id: str | None = None,
    meeting_transcript_id: uuid.UUID | None = None,
) -> Task | None:
    """Gemeinsamer Kern: legt einen Task-Vorschlag (``needs_review=True``) an.

    Genutzt von Chat-Triage und Meeting-Nachbereitung (Paritaet zur E-Mail-
    Triage): Projekt-Matching, erste Board-Spalte, Pipeline-Spalte nach
    Deadline. Dedupe: gleicher offener Titel -> kein neuer Task.
    """
    dup_res = await db.execute(
        select(Task)
        .where(func.lower(Task.title) == title.lower(), Task.is_completed.is_(False))
        .limit(1)
    )
    if dup_res.scalar_one_or_none() is not None:
        logger.info("Job %s: Task-Duplikat (Titel '%s') -- kein neuer Task", job_id, title[:60])
        return None

    proj_result = await db.execute(
        select(Project).where(Project.status != "archived").order_by(Project.name)
    )
    projects = list(proj_result.scalars().all())
    matched_project = _match_project(suggested_project, projects)
    if not matched_project and projects:
        matched_project = projects[0]
    if not matched_project:
        logger.warning("Job %s: Kein Projekt fuer Task-Vorschlag vorhanden", job_id)
        return None

    col_result = await db.execute(
        select(BoardColumn)
        .where(BoardColumn.project_id == matched_project.id)
        .order_by(BoardColumn.position)
        .limit(1)
    )
    first_col = col_result.scalar_one_or_none()
    if not first_col:
        logger.warning("Job %s: Projekt '%s' hat keine Board-Spalte", job_id, matched_project.name)
        return None

    pipeline_col_id = _determine_pipeline_column(deadline)
    due_date = None
    if deadline:
        try:
            due_date = date.fromisoformat(deadline)
        except ValueError:
            pass

    max_pos_result = await db.execute(
        select(Task.board_position)
        .where(Task.board_column_id == first_col.id)
        .order_by(Task.board_position.desc())
        .limit(1)
    )
    next_pos = (max_pos_result.scalar_one_or_none() or 0) + 1

    new_task = Task(
        title=title[:200],
        description=description,
        project_id=matched_project.id,
        board_column_id=first_col.id,
        board_position=next_pos,
        pipeline_column_id=pipeline_col_id,
        due_date=due_date,
        email_conversation_id=email_conversation_id,
        meeting_transcript_id=meeting_transcript_id,
        needs_review=True,
        assignee="me",
    )
    db.add(new_task)
    await db.flush()
    logger.info(
        "Job %s: Task-Vorschlag '%s' in Projekt '%s' erstellt",
        job_id, new_task.title, matched_project.name,
    )
    return new_task


async def _create_chat_task(
    db,
    job_id,
    meta: dict,
    *,
    task_title: str | None,
    task_description: str | None,
    suggested_project: str | None,
    deadline: str | None,
) -> Task | None:
    """Legt aus einer triagierten Teams-Nachricht einen Task-Vorschlag an."""
    title = (task_title or "").strip() or f"Teams: {(meta.get('body_preview') or 'Nachricht')[:80]}"
    from_name = meta.get("from_name") or "?"
    preview = (meta.get("body_preview") or "").strip()
    base_desc = _strip_internal_notes(task_description) or "Erstellt aus Teams-Nachricht."
    source_block = f"\n\n---\n**Quelle:** Teams-Chat von {from_name}"
    if preview:
        source_block += f"\n> {preview[:400]}"

    new_task = await _create_review_task(
        db,
        job_id,
        title=title,
        description=base_desc + source_block,
        suggested_project=suggested_project,
        deadline=deadline,
    )
    if new_task is not None:
        await notify_chat_triage_task(
            db,
            task_id=new_task.id,
            task_title=new_task.title,
            from_name=meta.get("from_name"),
        )
    return new_task


async def _create_email_task(
    db,
    job_id,
    meta: dict,
    *,
    task_title: str,
    task_description: str | None,
    suggested_project: str | None,
    deadline: str | None,
    reply_expected: bool = False,
) -> Task | None:
    """Legt aus einer triagierten E-Mail eine Task an (geteilt von Normal- + Fallback-Pfad).

    Waehlt das passende Projekt (oder das erste), die erste Board-Spalte und die
    Pipeline-Spalte nach Deadline. Verknuepft ``email_message_id`` /
    ``email_conversation_id`` und setzt ``needs_review=True``. Gibt die Task
    zurueck oder None, wenn kein Projekt/keine Spalte existiert.

    Duplikat-Schutz: Existiert bereits ein offener Task zur selben Sache (gleiche
    Konversation oder Absender+Betreff), wird KEIN neuer Task erstellt. Stattdessen
    wird die neue Meldung als Checklisten-Eintrag angedockt und die zugehoerige
    ``email_triage`` als dedupliziert (fyi) markiert.
    """
    # Verworfene Vorschläge respektieren: Hat der Berater denselben Vorschlag
    # (Absender + Betreff) kürzlich weggeklickt, wird KEIN neuer Task erstellt.
    if await _was_suggestion_dismissed(db, meta):
        if job_id is not None:
            await db.execute(
                update(EmailTriage)
                .where(EmailTriage.agent_job_id == job_id)
                .values(
                    triage_class="fyi",
                    reply_expected=False,
                    suggested_action={
                        "label": "Verworfen",
                        "triage_class": "fyi",
                        "suppressed_by_dismissal": True,
                        "rationale": (
                            "Praktisch identischer Task-Vorschlag wurde kürzlich "
                            "verworfen -- kein erneuter Vorschlag."
                        ),
                    },
                    status="acted",
                )
            )
        logger.info(
            "Job %s: Task-Vorschlag unterdrückt (kürzlich verworfen: '%s')",
            job_id, (meta.get("subject") or "")[:60],
        )
        return None

    dup = await _find_duplicate_open_task(db, meta)
    if dup is not None:
        await _append_duplicate_note(db, dup, meta)
        if job_id is not None:
            await db.execute(
                update(EmailTriage)
                .where(EmailTriage.agent_job_id == job_id)
                .values(
                    triage_class="fyi",
                    reply_expected=False,
                    suggested_action={
                        "label": "Duplikat",
                        "triage_class": "fyi",
                        "deduplicated": True,
                        "duplicate_of": str(dup.id),
                        "rationale": (
                            f"Bereits als offene Aufgabe erfasst ('{(dup.title or '')[:60]}'). "
                            "Als weitere Meldung angedockt -- kein neuer Task."
                        ),
                    },
                    status="acted",
                )
            )
        logger.info(
            "Job %s: Duplikat erkannt -> an offenen Task %s angedockt (kein neuer Task)",
            job_id, dup.id,
        )
        return dup

    proj_result = await db.execute(
        select(Project).where(Project.status != "archived").order_by(Project.name)
    )
    projects = list(proj_result.scalars().all())
    matched_project = _match_project(suggested_project, projects)
    if not matched_project and projects:
        matched_project = projects[0]
    if not matched_project:
        logger.warning("Job %s: Kein Projekt fuer Task vorhanden", job_id)
        return None

    col_result = await db.execute(
        select(BoardColumn)
        .where(BoardColumn.project_id == matched_project.id)
        .order_by(BoardColumn.position)
        .limit(1)
    )
    first_col = col_result.scalar_one_or_none()
    if not first_col:
        logger.warning("Job %s: Projekt '%s' hat keine Board-Spalte", job_id, matched_project.name)
        return None

    pipeline_col_id = _determine_pipeline_column(deadline)
    due_date = None
    if deadline:
        try:
            due_date = date.fromisoformat(deadline)
        except ValueError:
            pass

    max_pos_result = await db.execute(
        select(Task.board_position)
        .where(Task.board_column_id == first_col.id)
        .order_by(Task.board_position.desc())
        .limit(1)
    )
    next_pos = (max_pos_result.scalar_one_or_none() or 0) + 1

    # Nutzersichtbare Beschreibung saeubern (interne API-/Fehler-Diagnosen raus).
    # Kein Quell-E-Mail-Block mehr: Absender und Betreff zeigt die Detailansicht aus
    # den Task-Feldern, die Originalmail liest man dort inline ueber die
    # conversation_id. Der Block war eine dritte Kopie derselben Angaben und kostete
    # in jeder Aufgabe rund acht Zeilen plus eine mehrzeilig umbrechende URL.
    base_desc = _strip_internal_notes(task_description) or f"Erstellt aus E-Mail: {meta.get('subject', '')}"

    new_task = Task(
        title=task_title,
        description=base_desc,
        project_id=matched_project.id,
        board_column_id=first_col.id,
        board_position=next_pos,
        pipeline_column_id=pipeline_col_id,
        email_message_id=meta.get("email_message_id"),
        email_conversation_id=meta.get("conversation_id"),
        due_date=due_date,
        needs_review=True,
        assignee="me",
    )
    db.add(new_task)
    await db.flush()
    await notify_task_suggested(
        db,
        task_id=new_task.id,
        task_title=task_title,
        from_email=meta.get("from_address"),
    )
    logger.info(
        "Job %s: Task erstellt '%s' in Projekt '%s' (reply_expected=%s)",
        job_id, task_title, matched_project.name, reply_expected,
    )
    return new_task


async def _structured_triage_reask(meta: dict, content: str) -> dict | None:
    """Tool-freier, parse-garantierter Klassifikations-Call (Structured Output).

    Rettungspfad, wenn der agentische Triage-Loop keinen verwertbaren JSON-Block
    lieferte: EIN direkter Ollama-Call mit ``response_format={"type":"json_object"}``
    (parse-garantiert) leitet die finale Klassifikation aus Betreff/Absender/Vorschau
    plus der bisherigen Agenten-Analyse ab. Bewusst OHNE Agent/Tools -- so gibt es
    keine Kollision mit dem Tool-Calling (``request_overrides`` wuerde sonst jeden
    Turn erzwingen und Tool-Aufrufe brechen).

    Best-effort: gibt None zurueck, wenn deaktiviert, das Triage-Modell nicht lokal
    ist oder der Call scheitert -- der Aufrufer faellt dann fail-closed zurueck.
    """
    cfg = get_settings()
    if not cfg.triage_structured_fallback or not _is_local_model(cfg.triage_model):
        return None
    subject = meta.get("subject", "")
    from_addr = meta.get("from_address", "")
    preview = (meta.get("body_preview") or "")[:500]
    analysis = (content or "")[-1500:]
    labels_hint = "|".join(TRIAGE_LABELS)
    schema_hint = (
        f'{{"rationale": "kurz", "label": "{labels_hint}", '
        '"triage_class": "task|auto_reply|fyi", "reply_expected": true|false, '
        '"confidence": 0.0}'
    )
    system_msg = (
        "Du bist ein E-Mail-Triage-Klassifikator. Gib AUSSCHLIESSLICH ein JSON-Objekt "
        f"nach diesem Schema zurueck: {schema_hint}. Begruende ZUERST (rationale), dann "
        "klassifiziere. triage_class ist genau eines von task, auto_reply, fyi. "
        "label ist genau EINES der vorgegebenen Labels -- erfinde keine neuen. "
        "Terminzusagen/reine Infos sind fyi. Im Zweifel fyi."
    )
    user_msg = (
        f"Betreff: {subject}\nVon: {from_addr}\nVorschau: {preview}\n\n"
        f"Bisherige (ggf. unstrukturierte) Analyse des Agenten:\n{analysis}\n\n"
        "Leite daraus die finale Klassifikation als JSON ab."
    )
    model = cfg.triage_model.removeprefix("ollama/")
    url = f"{cfg.ollama_base_url.rstrip('/')}/v1/chat/completions"
    base_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
        "stream": False,
    }

    async def _call(response_format: dict) -> dict | None:
        payload = {**base_payload, "response_format": response_format}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url, json=payload, headers={"Authorization": "Bearer ollama"}
            )
            resp.raise_for_status()
            data = resp.json()
        msg = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        parsed = _loads_lenient(msg)
        if isinstance(parsed, dict) and parsed.get("triage_class") in ("task", "auto_reply", "fyi"):
            return parsed
        return None

    # 1) Schema-constrained Decoding (Best Practice): erzwingt gueltiges JSON mit
    #    triage_class-Enum. Das rationale-Feld steht ZUERST -> das Modell committet
    #    erst die Begruendung, dann die Klasse ("reasoning before answer").
    json_schema_rf = {
        "type": "json_schema",
        "json_schema": {
            "name": "email_triage",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "string"},
                    "label": {"type": "string", "enum": list(TRIAGE_LABELS)},
                    "triage_class": {
                        "type": "string",
                        "enum": ["task", "auto_reply", "fyi"],
                    },
                    "reply_expected": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["rationale", "label", "triage_class", "reply_expected"],
                "additionalProperties": False,
            },
        },
    }
    try:
        parsed = await _call(json_schema_rf)
        if parsed is not None:
            return parsed
    except Exception:  # noqa: BLE001 - z.B. 400 bei aelterer Ollama-Version ohne json_schema
        logger.info(
            "Structured-Reask: json_schema nicht unterstuetzt, Graceful-Fallback json_object"
        )

    # 2) Graceful-Fallback auf das schwaechere json_object-Mode (aeltere Ollama-
    #    Versionen), damit ein fehlendes json_schema-Feature keinen Hard-Break gibt.
    try:
        return await _call({"type": "json_object"})
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Structured-Triage-Reask fehlgeschlagen")
    return None


async def _fallback_unparsed_triage(job_id, meta: dict, moved_id: str | None = None) -> str:
    """Sicherheitsnetz, wenn der LLM keinen verwertbaren Triage-Block lieferte.

    Fail-closed (Best Practice): Bei Unsicherheit wird NICHT gehandelt. Die E-Mail
    wird als ``fyi`` mit ``needs_review``-Marker eingeordnet und bleibt -- via
    ``_finalize_email_state`` -- ungelesen in der Inbox sichtbar. Es wird KEIN
    Auto-Task mehr erstellt: ein faelschlich angelegter Task (z. B. aus einer
    blossen Terminzusage) ist teurer und nerviger als eine sichtbare Mail, die
    der Mensch in der Inbox ohnehin sieht und bei Bedarf manuell einordnet.
    """
    logger.warning(
        "Job %s: Kein verwertbarer JSON-Block -- fail-closed auf fyi/needs_review (kein Auto-Task)",
        job_id,
    )
    async with async_session() as db:
        await db.execute(
            update(EmailTriage)
            .where(EmailTriage.agent_job_id == job_id)
            .values(
                triage_class="fyi",
                reply_expected=False,
                confidence=None,
                suggested_action={
                    "label": "Unklar",
                    "triage_class": "fyi",
                    "needs_review": True,
                    "rationale": (
                        "Agent lieferte keinen strukturierten Triage-Block. Die E-Mail "
                        "bleibt zur manuellen Sichtung ungelesen in der Inbox -- kein Auto-Task."
                    ),
                    "fallback": True,
                },
                status="acted",
            )
        )
        await db.commit()
    # Auch im Fallback deterministisch finalisieren: Sentinel NO_CATEGORY
    # ueberspringt das Kategorie-Setzen (kein Raten einer Outlook-Kategorie),
    # die Mail wird aber auf ungelesen zurueckgesetzt und bleibt sichtbar.
    # needs_review verhindert jeden Move -- was nicht verstanden wurde, bleibt liegen.
    await _finalize_email_state(
        meta, NO_CATEGORY, moved_id, triage_class="fyi", needs_review=True
    )
    return "completed"


async def _post_process_triage(
    job_id,
    content: str,
    meta: dict,
    captured_draft_id: str | None = None,
    tools_used: list[str] | None = None,
    moved_id: str | None = None,
) -> str:
    """Deterministische Post-Processing-Logik nach LLM-Klassifikation.

    ``captured_draft_id`` ist die echte Outlook-Draft-ID, die der Worker direkt aus
    dem ``create_draft``-Tool-Ergebnis erfasst hat (ground truth). Sie hat IMMER
    Vorrang vor einer im JSON-Block gemeldeten ID -- letztere wird vom LLM bei
    langen Graph-IDs regelmaessig verstuemmelt und ist deshalb nicht vertrauenswuerdig.

    ``tools_used`` (tatsaechlich aufgerufene Tools) dient dem Kontext-Gate: ein
    ``auto_reply`` ohne geladene Pflicht-Kontexte (Thread/History/Profil/Stil-Anker)
    wird auf ``task`` heruntergestuft, da solche Entwuerfe erfahrungsgemaess
    tonal/inhaltlich unbrauchbar sind.
    """
    parsed = _extract_json_block(content)
    if parsed is None:
        # Rettung vor dem Fail-Closed: EIN tool-freier, parse-garantierter
        # Klassifikations-Call (nur wenn aktiviert + lokales Modell).
        parsed = await _structured_triage_reask(meta, content)
        if parsed is not None:
            logger.info(
                "Job %s: Klassifikation via Structured-Fallback gerettet (%s)",
                job_id,
                parsed.get("triage_class"),
            )
            parsed.setdefault("task_title", meta.get("subject"))
        else:
            return await _fallback_unparsed_triage(job_id, meta, moved_id)

    triage_class = parsed.get("triage_class")
    # Label gegen das kanonische Vokabular pruefen. Fail-closed: ein erfundenes
    # Label wird NICHT zurechtgebogen, sondern zu 'Unklar' mit needs_review --
    # sichtbar in der Inbox und korrigierbar (statt still falsch kategorisiert).
    label = normalize_label(parsed.get("label"))
    label_invalid = label is None
    if label_invalid:
        raw_label = parsed.get("label")
        logger.warning(
            "Job %s: Label %r nicht im Vokabular -- fail-closed auf '%s' (needs_review)",
            job_id, str(raw_label)[:60], FALLBACK_LABEL,
        )
        label = FALLBACK_LABEL
        parsed["label"] = FALLBACK_LABEL
        parsed["label_rejected"] = str(raw_label)[:120] if raw_label else None
        parsed["needs_review"] = True
    else:
        parsed["label"] = label
    # Echte Draft-ID aus dem Tool-Ergebnis ist die einzige verlaessliche Quelle.
    # Die vom Modell im JSON gemeldete ID wird NICHT als ID-Quelle genutzt.
    draft_id = captured_draft_id
    llm_claimed_draft = bool(parsed.get("draft_id"))
    if llm_claimed_draft and not captured_draft_id:
        logger.warning(
            "Job %s: LLM meldet draft_id, aber kein echtes create_draft-Tool-Ergebnis "
            "erfasst -- ID wird verworfen (kein verlaesslicher Entwurf).",
            job_id,
        )
    deadline = parsed.get("deadline")
    task_title = parsed.get("task_title")
    task_description = parsed.get("task_description")
    suggested_project = parsed.get("suggested_project")
    rationale = parsed.get("rationale")
    reply_expected = bool(parsed.get("reply_expected", False))

    # Sicherheitsgrad der Einschaetzung (0..1). Optional vom LLM geliefert; auf
    # gueltigen Bereich begrenzen, damit das Frontend ein verlaessliches Signal
    # (ConfidenceBadge) anzeigen kann.
    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
        if confidence is not None:
            if confidence > 1:  # toleriere Prozentangaben (z. B. 85)
                confidence = confidence / 100.0
            confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = None

    if triage_class == "quick_response":
        triage_class = "auto_reply"
    elif triage_class == "board_task":
        triage_class = "task"
        reply_expected = True
    elif triage_class == "bedenkzeit":
        triage_class = "task"

    # Berater-Korrektur erzwingen: Eine vom Menschen vorgegebene Klasse hat Vorrang
    # vor der (ggf. abweichenden) Selbst-Klassifikation des Agenten.
    forced_class = meta.get("forced_class")
    if forced_class in ("auto_reply", "task", "fyi") and triage_class != forced_class:
        logger.info(
            "Job %s: forced_class=%s erzwingt Korrektur (Agent wollte %s)",
            job_id, forced_class, triage_class,
        )
        triage_class = forced_class

    # Bei erzwungener Klasse den Draft-basierten Auto-Switch unterdruecken, damit
    # eine bewusst gewollte 'task'-Korrektur nicht zurueck auf auto_reply kippt.
    if draft_id and triage_class != "auto_reply" and forced_class != "task":
        logger.warning("Job %s: draft_id vorhanden aber triage_class=%s, korrigiere zu auto_reply", job_id, triage_class)
        triage_class = "auto_reply"

    # Zwei-Pass-Entwurf: Sobald auto_reply feststeht, schreibt ein separater,
    # fokussierter Schreib-Pass den Draft mit Prosa-Sampling. Scheitert er, greift
    # unten der Fail-closed-Pfad. forced_class='task' bleibt unberührt.
    #
    # Der Schreib-Pass laeuft IMMER -- frueher nur bei fehlender draft_id. Diese
    # Bedingung war der Kern des Vorfalls vom 03.08.2026: der Klassifikations-Lauf
    # hatte entgegen dem Prompt einen Platzhalter erstellt (nur Anrede und Gruss),
    # und dessen blosse Existenz unterdrueckte den echten Schreib-Pass. Ein Entwurf
    # aus Pass 1 ist per Definition ungewollt und wird verworfen.
    if triage_class == "auto_reply" and get_settings().two_pass_draft:
        stale_draft_id = draft_id
        draft_id = await _generate_reply_draft(meta, parsed)
        if draft_id:
            # Tools des Schreib-Passes fürs Kontext-Gate mitzählen (get_email/
            # get_thread/search_my_replies liefen erst jetzt).
            tools_used = sorted(_job_tool_names)
            logger.info("Job %s: Zwei-Pass-Draft erstellt (draft_id=%s)", job_id, draft_id)
            if stale_draft_id and stale_draft_id != draft_id:
                logger.warning(
                    "Job %s: Entwurf aus dem Klassifikations-Lauf verworfen "
                    "(der Triage-Agent sollte create_draft gar nicht haben)", job_id,
                )
                await _delete_draft(stale_draft_id)
        else:
            logger.warning("Job %s: Zwei-Pass-Draft lieferte keinen Entwurf", job_id)
            # Ohne neuen Entwurf zaehlt ein evtl. vorhandener Pass-1-Entwurf nicht
            # als Ersatz: er ist die Fehlerquelle, nicht die Rueckfallebene.
            if stale_draft_id:
                await _delete_draft(stale_draft_id)

    if triage_class == "auto_reply" and not draft_id:
        # Fail-closed (Best Practice): ohne echten Entwurf wird NICHT als Task
        # gehandelt, sondern als fyi belassen. Die Mail bleibt via
        # _finalize_email_state ungelesen in der Inbox sichtbar -- ein
        # faelschlich erstellter Task ist teurer als eine sichtbare Mail.
        logger.warning("Job %s: auto_reply ohne draft_id -> fyi (fail-closed, kein Auto-Task)", job_id)
        triage_class = "fyi"

    # Kontext-Gate (NICHT-destruktiv): Ein bereits ERSTELLTER Entwurf wird NIE
    # mehr verworfen. Frueher wurde bei fehlendem Pflicht-Kontext auf 'task'
    # heruntergestuft und draft_id genullt -- das liess gute Entwuerfe in Outlook
    # verwaisen und zeigte im Cockpit nur eine Task mit "ging nicht"-Notiz. Jeder
    # Entwurf bleibt jetzt als auto_reply zur HITL-Freigabe sichtbar; fehlender
    # Kontext senkt nur die angezeigte Confidence und wird INTERN vermerkt (nicht
    # im nutzersichtbaren Text). forced_class bleibt unberuehrt.
    gate_internal_note: str | None = None
    if (
        triage_class == "auto_reply"
        and draft_id
        and tools_used is not None
        and forced_class != "auto_reply"
    ):
        grade = _compute_self_grade(meta, {"draft_id": draft_id}, list(tools_used))
        if grade["missing"]:
            logger.info(
                "Job %s: auto_reply mit unvollstaendigem Pflicht-Kontext %s -- "
                "Entwurf bleibt erhalten, Confidence gesenkt (kein Downgrade)",
                job_id, grade["missing"],
            )
            capped = 0.4
            confidence = capped if confidence is None else min(confidence, capped)
            gate_internal_note = (
                "Entwurf ohne vollstaendigen Pflicht-Kontext erstellt "
                f"(fehlend: {', '.join(grade['missing'])}) -- vor Freigabe pruefen."
            )

    if triage_class == "task" and not task_title:
        task_title = meta.get("subject", "E-Mail Triage (kein Titel)")
        logger.warning("Job %s: task ohne task_title, verwende Subject: %s", job_id, task_title)

    # Low-Confidence-Gate (Best-Practice-Audit-Bucket): Eine Klassifikation mit
    # geringer Sicherheit wird zur menschlichen Sichtung markiert, statt still
    # durchzugehen. Nicht-destruktiv -- die Klasse bleibt, nur das needs_review-
    # Signal wird gesetzt, damit das Cockpit solche Faelle hervorhebt.
    low_conf_threshold = get_settings().triage_low_confidence_threshold
    needs_review = confidence is not None and confidence < low_conf_threshold
    if needs_review:
        logger.info(
            "Job %s: Confidence %.2f < %.2f -- als needs_review markiert",
            job_id, confidence, low_conf_threshold,
        )

    logger.info(
        "Job %s: JSON parsed -- label=%s, triage_class=%s, reply_expected=%s, draft_id=%s",
        job_id, label, triage_class, reply_expected, draft_id,
    )

    async with async_session() as db:
        await db.execute(
            update(EmailTriage)
            .where(EmailTriage.agent_job_id == job_id)
            .values(
                triage_class=triage_class,
                reply_expected=reply_expected,
                confidence=confidence,
                suggested_action={
                    "label": label,
                    "triage_class": triage_class,
                    "reply_expected": reply_expected,
                    "deadline": deadline,
                    "task_title": task_title,
                    "suggested_project": suggested_project,
                    "draft_id": draft_id,
                    "rationale": rationale,
                    "confidence": confidence,
                    "needs_review": needs_review,
                },
                status="acted" if triage_class != "auto_reply" else "processing",
            )
        )

        final_status = "completed"

        if triage_class == "task" and task_title:
            await _create_email_task(
                db,
                job_id,
                meta,
                task_title=task_title,
                task_description=task_description,
                suggested_project=suggested_project,
                deadline=deadline,
                reply_expected=reply_expected,
            )

        elif triage_class == "auto_reply" and draft_id:
            job_result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
            job = job_result.scalar_one_or_none()
            if job:
                existing_meta = dict(job.metadata_json or {})
                existing_meta["draft_id"] = draft_id
                # Lesbares "Warum" + Sicherheit fuer die Freigabe-Karte mitgeben,
                # damit das Frontend nicht den rohen Trace interpretieren muss.
                if rationale:
                    existing_meta["rationale"] = rationale
                if confidence is not None:
                    existing_meta["confidence"] = confidence
                if gate_internal_note:
                    existing_meta["context_warning"] = gate_internal_note
                existing_meta["summary"] = (
                    rationale
                    or f"Antwort-Entwurf für '{meta.get('subject') or '(kein Betreff)'}' vorbereitet."
                )
                # Original-Entwurf als Referenz fuer den spaeteren Stil-Diff snapshotten.
                snapshot = await _snapshot_agent_draft(draft_id)
                # Deterministisch erzwingen, dass der Entwurf im Original-Thread
                # liegt (Reply-All) -- unabhaengig davon, ob das LLM reply_to_id
                # gesetzt hat. Repariert ggf. die draft_id + Snapshot.
                draft_id, snapshot = await _ensure_draft_in_thread(
                    draft_id, meta.get("email_message_id", ""), snapshot
                )
                existing_meta["draft_id"] = draft_id
                if snapshot:
                    existing_meta["original_draft_html"] = snapshot.get("body_html")
                    existing_meta["draft_conversation_id"] = snapshot.get("conversation_id")
                    existing_meta["draft_to"] = snapshot.get("to")
                    existing_meta["draft_cc"] = snapshot.get("cc")
                    # Struktureller Platzhalter-Test: fehlt zwischen Anrede und
                    # Schlussformel jeder Inhalt, ist der Entwurf wertlos. Der
                    # Entwurf bleibt trotzdem in der Freigabe (nichts wird still
                    # verworfen), aber sichtbar markiert und mit gedeckelter
                    # Confidence -- sonst meldet das Cockpit 0.9 fuer zwei Grusszeilen.
                    if not has_content_between_greeting_and_closing(
                        snapshot.get("body_html") or ""
                    ):
                        logger.warning(
                            "Job %s: Entwurf ohne Inhaltsteil (nur Anrede/Gruss)", job_id
                        )
                        existing_meta["draft_quality"] = "placeholder"
                        existing_meta["context_warning"] = (
                            "Der Entwurf enthält keinen Inhalt zwischen Anrede und "
                            "Schlussformel -- bitte vor dem Senden prüfen."
                        )
                        if confidence is None or confidence > 0.3:
                            existing_meta["confidence"] = 0.3
                    else:
                        _apply_grounding_check(
                            existing_meta, snapshot.get("body_html") or "", job_id
                        )
                job.metadata_json = existing_meta
            final_status = "awaiting_approval"
            await notify_agent_awaiting_approval(
                db, job_id=job_id, subject=meta.get("subject"),
            )

        # Episode fuer das episodische Gedaechtnis ablegen (Recall-Basis).
        if triage_class:
            from_name = meta.get("from_name") or ""
            from_address = meta.get("from_address") or ""
            subject = meta.get("subject") or "(kein Betreff)"
            summary = (
                f"E-Mail von {from_name} <{from_address}>: '{subject}'. "
                f"Triage-Entscheid: {triage_class}"
                + (", Antwort erwartet" if reply_expected else "")
            )
            await record_episode(
                db,
                summary=summary,
                job_type="email_triage",
                agent_job_id=job_id,
                sender_email=from_address or None,
                decision={
                    "triage_class": triage_class,
                    "reply_expected": bool(reply_expected),
                    "draft_id": draft_id,
                },
            )

        await db.commit()

    # Deterministische Outlook-Finalisierung NACH der DB-Transaktion (reine Netz-
    # I/O): Move gemaess Politik, Kategorie aus dem validierten Label, Mail immer
    # auf ungelesen. Laeuft fuer alle Klassen (task/auto_reply/fyi).
    await _finalize_email_state(
        meta,
        label,
        moved_id,
        triage_class=triage_class,
        needs_review=label_invalid,
    )

    return final_status


# ── Runtime-Initialisierung ──────────────────────────────

def _is_local_model(sel: str) -> bool:
    """True, wenn das Modell lokal ueber Ollama laeuft (Default oder ``ollama/*``)."""
    return not sel or sel in ("nanobot", "hermes") or sel.startswith("ollama/")


# Cloud-Provider (z. B. OpenAI) begrenzen die Anzahl Tools pro Request auf 128.
# Lokales Ollama kennt dieses Limit nicht. Gilt als Sicherheitsnetz fuer den
# Cloud-Pfad (aktuell 113 MCP-Tools insgesamt, also unkritisch).
CLOUD_TOOL_LIMIT = 128


def get_configured_server_keys() -> list[str]:
    """Liest die konfigurierten MCP-Server-Keys aus ``~/.hermes/config.yaml``.

    Die Keys (z. B. ``graph``, ``bexio``) sind zugleich die Hermes-Toolset-Aliase
    (``validate_toolset`` akzeptiert Aliase), die in ``enabled_toolsets`` genutzt
    werden, um einzelne MCP-Server gezielt freizugeben.
    """
    import yaml

    config_path = HERMES_HOME / "config.yaml"
    try:
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return list((config.get("mcp_servers") or {}).keys())
    except Exception:
        logger.exception("MCP-Server-Keys konnten nicht aus config.yaml gelesen werden")
        return []


def expand_graph_admin(servers: list[str]) -> list[str]:
    """Koppelt ``graphAdmin`` an ``graph`` (nur fuer Chat-Kontexte).

    Der Graph-Server ist zweimal registriert: ``graph`` ohne und ``graphAdmin``
    mit den zustandsveraendernden Tools (siehe ``hermes_config.build_config_dict``).
    Fuer den Nutzer bleibt das eine Auswahl -- wer im Chat «Graph» aktiviert, will
    auch verschieben und kategorisieren koennen. Die Triage nutzt diese Funktion
    bewusst NICHT.
    """
    if "graph" in servers and "graphAdmin" not in servers:
        return [*servers, "graphAdmin"]
    return servers


def resolve_cloud_toolsets(enabled_servers: list[str] | None) -> list[str]:
    """Validiert die gewuenschten MCP-Server gegen die Konfiguration.

    Gibt die Toolset-Namen (= Server-Aliase) zurueck, die fuer ein Cloud-Modell
    freigegeben werden. Unbekannte/nicht konfigurierte Server werden verworfen.
    Eine leere Liste bedeutet Default-Deny (keine MCP-Tools).
    """
    configured = set(get_configured_server_keys())
    requested = expand_graph_admin(list(enabled_servers or []))
    return [s for s in requested if s in configured]


# Kuratierte Allowlist der Hermes-Core-Toolsets fuer lokale Agenten (Worker + Chat).
# Bewusst OHNE rohe Host-Ausfuehrung: KEIN terminal/process, KEIN file-write,
# KEIN browser, KEIN execute_code (Host), KEIN delegation/messaging/cronjob/
# image_gen/tts/homeassistant/rl. Riskante bzw. ausfuehrende Aktionen laufen
# ausschliesslich ueber die gekapselten MCP-Server (scripts = registrierte Docker-
# Scripts, sandbox = isolierter Docker) mit HITL-Gate. So bleibt die Sandbox-/
# No-Host-Execution-Philosophie gewahrt, waehrend die agentischen Faehigkeiten
# (Wissen, Lernen, Recall, Rueckfragen, Vision, Web-Recherche) voll nutzbar sind.
LOCAL_CORE_TOOLSETS: list[str] = [
    "skills",          # skills_list, skill_view, skill_manage
    "memory",          # memory: deklaratives Langzeitwissen schreiben/lesen
    "session_search",  # frühere Gespräche durchsuchen (Kontinuität)
    "clarify",         # strukturierte HITL-Rückfragen
    "todo",            # Mehrschritt-Planung
    "vision",          # vision_analyze: E-Mail-Anhänge/Screenshots verstehen
    "web",             # web_search + web_extract: agentische Recherche (statt Eigenbau)
]

# Fallback-Server-Keys, falls config.yaml (noch) nicht lesbar ist. Deckt sich mit
# build_config_dict() in hermes_config.py.
_KNOWN_MCP_SERVERS: list[str] = [
    "taskpilot", "capacity", "graph", "graphAdmin", "pipedrive", "toggl", "bexio",
    "signa", "invoiceinsight", "scripts", "sandbox", "contentConverter",
]


def build_local_allowlist(include_delegation: bool = False) -> list[str]:
    """Allowlist fuer lokale Agenten: kuratierte Core-Toolsets + konfigurierte MCP-Server.

    ``include_delegation`` aktiviert das ``delegation``-Toolset (``delegate_task``):
    nur fuer den interaktiven Chat-Agenten (InnoPilot) gedacht, der Research-/
    Dokument-Jobs in Subagenten zerlegen kann. Subagenten erben dieselbe gehaertete
    Allowlist (kein Host-Shell), und externe Ausgaben bleiben HITL-pflichtig. Der
    fokussierte Triage-Worker bekommt KEINE Delegation (kein Subagenten-Spawn).

    Ersetzt das fruehere ``enabled_toolsets=None`` (volles Core-Toolkit inkl. Host-
    Shell). Die MCP-Server-Keys sind zugleich Toolset-Aliase (siehe
    ``resolve_cloud_toolsets``); fehlt die Config, greift ``_KNOWN_MCP_SERVERS``.
    """
    servers = get_configured_server_keys() or _KNOWN_MCP_SERVERS
    core = [*LOCAL_CORE_TOOLSETS, "delegation"] if include_delegation else LOCAL_CORE_TOOLSETS
    return [*core, *servers]


# MCP-Server, die die Triage tatsaechlich braucht: E-Mail/Teams lesen und
# Entwuerfe schreiben (graph) sowie Tasks/Profile/History (taskpilot). Alle
# anderen Server (CRM, Buchhaltung, Zeiterfassung, SIGNA, Sandbox, ...) sind fuer
# die Klassifikation irrelevanter Prompt-Ballast fuer das lokale Modell.
#
# ``graphAdmin`` ist hier bewusst NICHT enthalten: die zustandsveraendernden
# Graph-Tools (Kategorien, Move, Versand, gelesen-Markierung) liegen dort, und
# der Triage-Agent soll den Outlook-Zustand nicht selbst mutieren. Er
# klassifiziert, das Backend schreibt (siehe ``_finalize_email_state``).
_TRIAGE_MCP_SERVERS: list[str] = ["graph", "taskpilot"]


def build_triage_allowlist() -> list[str]:
    """Reduzierte Allowlist fuer Triage-Jobs (Tool-Scoping, Paket C).

    Core-Toolsets ohne ``web`` (Datenminimierung: der Triage-Agent soll keine
    E-Mail-Inhalte in externe Suchanfragen packen koennen -- die Triage-Prompts
    weisen Websuche ohnehin nie an) plus nur die zwei fachlich noetigen
    MCP-Server. Reduziert die Tool-Definitionen im Kontext deutlich und
    verbessert die Tool-Wahl des lokalen Modells.
    """
    core = [t for t in LOCAL_CORE_TOOLSETS if t != "web"]
    configured = set(get_configured_server_keys() or _KNOWN_MCP_SERVERS)
    servers = [s for s in _TRIAGE_MCP_SERVERS if s in configured]
    return [*core, *servers]


# Fachsysteme fuer den Recherche-Lauf (Pass 2a). Der Sammel-Lauf teilte bisher die
# Allowlist der Klassifikation -- er sah also nur Graph und die TaskPilot-DB und
# musste Fragen zu Stunden, Rechnungs- oder Angebotsstand aus dem Mailarchiv
# beantworten. Genau daraus entstand am 03.08.2026 die veraltete Budgetzahl.
#
# ``schmal`` deckt den beobachteten Fehlerfall ab, ``breit`` gibt dem Agenten
# dieselben Fachsysteme, die auch der Mensch nutzt (Leitprinzip Team-Modell). Was
# besser traegt, ist eine Messfrage: mehr Werkzeugdefinitionen kosten Kontext und
# koennen die Werkzeugwahl eines kleinen Modells verschlechtern. Deshalb ein Flag
# statt einer Annahme.
_GATHER_MCP_SERVERS_NARROW: list[str] = ["graph", "taskpilot", "capacity"]
_GATHER_MCP_SERVERS_WIDE: list[str] = [
    "graph", "taskpilot", "capacity", "toggl", "pipedrive", "bexio", "signa",
]


def build_gather_allowlist(wide: bool | None = None) -> list[str]:
    """Allowlist fuer den Recherche-Lauf. ``web`` bleibt aus (Datenminimierung)."""
    if wide is None:
        wide = get_settings().draft_context_wide_tools
    core = [t for t in LOCAL_CORE_TOOLSETS if t != "web"]
    wanted = _GATHER_MCP_SERVERS_WIDE if wide else _GATHER_MCP_SERVERS_NARROW
    configured = set(get_configured_server_keys() or _KNOWN_MCP_SERVERS)
    return [*core, *[s for s in wanted if s in configured]]


def count_tools(enabled_toolsets: list[str] | None) -> int:
    """Anzahl der Tool-Definitionen fuer eine gegebene Toolset-Auswahl.

    Setzt eine erfolgte MCP-Discovery (``ensure_runtime_ready``) voraus.
    """
    try:
        from model_tools import get_tool_definitions

        return len(get_tool_definitions(enabled_toolsets=enabled_toolsets, quiet_mode=True))
    except Exception:
        logger.exception("Tool-Anzahl konnte nicht ermittelt werden")
        return 0


def _build_worker_agent(
    enabled_toolsets: list[str] | None = None,
    session_id: str = "taskpilot-worker",
):
    """Konstruiert einen persistenten Worker-AIAgent (laeuft im Thread).

    ``enabled_toolsets`` erlaubt job-typ-spezifisches Tool-Scoping (z. B. die
    reduzierte Triage-Allowlist); Default ist die volle lokale Allowlist.
    """
    from run_agent import AIAgent

    cfg = get_settings()
    model = cfg.triage_model.removeprefix("ollama/")
    # Worker nutzt per Default ein lokales Modell (voller Zugriff). Falls jemand
    # ein Cloud-Triage-Modell konfiguriert, gilt Default-Deny wie im Chat:
    # keine MCP-Tools, kein Memory/USER-Profil, keine Kontextdateien.
    if _is_local_model(cfg.triage_model):
        base_url = f"{cfg.ollama_base_url.rstrip('/')}/v1"
        api_key = "ollama"
        # Härtung: explizite Allowlist statt None (= volles Host-Toolkit).
        if enabled_toolsets is None:
            enabled_toolsets = build_local_allowlist()
        skip_memory = False
        skip_context_files = False
    else:
        base_url = f"{cfg.litellm_base_url.rstrip('/')}/v1"
        api_key = "sk-litellm-local"
        model = cfg.triage_model
        enabled_toolsets = []
        skip_memory = True
        skip_context_files = True

    return AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider="custom",
        api_mode="chat_completions",
        model=model,
        enabled_toolsets=enabled_toolsets,
        skip_memory=skip_memory,
        skip_context_files=skip_context_files,
        max_iterations=90,
        tool_delay=0.0,
        quiet_mode=True,
        # Hermes-native: Trajektorien persistieren (Grundlage fuer Inspektion +
        # spaeteres Fine-Tuning/Lernen). Best-effort in Hermes, schreibt JSONL.
        save_trajectories=True,
        session_id=session_id,
        reasoning_callback=_on_reasoning,
        tool_start_callback=_on_tool_start,
        tool_complete_callback=_on_tool_complete,
    )


async def ensure_runtime_ready() -> bool:
    """Stellt sicher, dass Config geschrieben, Env gesetzt und MCP-Tools registriert sind.

    Idempotent — wird von Worker und Chat-Agent genutzt. Gibt True zurueck, wenn
    die Runtime bereit ist.
    """
    global _runtime_ready
    if _runtime_ready:
        return True

    async with _get_runtime_lock():
        if _runtime_ready:
            return True

        os.environ["HERMES_HOME"] = str(HERMES_HOME)
        try:
            write_hermes_config()
            await populate_hermes_env()
        except Exception:
            logger.exception("Hermes-Config/Env konnte nicht vorbereitet werden")
            return False

        # Trajektorien an definierten Ort buendeln (nach Config, vor Agent-Bau).
        _install_trajectory_path_shim()

        try:
            from tools.mcp_tool import discover_mcp_tools

            tool_names = await asyncio.to_thread(discover_mcp_tools)
            logger.info("Hermes MCP-Discovery: %d Tools registriert", len(tool_names or []))
        except Exception:
            logger.exception("Hermes MCP-Discovery fehlgeschlagen")
            return False

        _runtime_ready = True
        return True


def build_chat_agent(
    model: str | None,
    *,
    preset: str = "agent",
    enabled_servers: list[str] | None = None,
    include_memory: bool = False,
    temperature: float | None = None,
    on_text=None,
    on_reasoning=None,
    on_tool_start=None,
    on_tool_complete=None,
    clarify_callback=None,
    session_id: str | None = None,
):
    """Konstruiert einen AIAgent fuer den interaktiven Chat (InnoPilot).

    Jede Chat-Anfrage bekommt eine eigene Instanz mit eigenen Callbacks
    (Streaming + Thinking + Tools), damit parallele Anfragen sich nicht
    gegenseitig stoeren. MCP-Tools stammen aus der globalen Registry
    (``ensure_runtime_ready`` muss vorher gelaufen sein).

    Presets:
    - ``agent``: voller InnoPilot (MCP-Tools nach Grounding-Politik). Deckt den
      vereinheitlichten Agent-Modus ab (Chat, Tools, Web, Code-Sandbox).
    - ``chat``: reiner Konversationsmodus auf derselben Hermes-Runtime —
      KEINE Tools (weder Core noch MCP), aber Session-Kompression, Memory-
      Injektion (lokal) und ``conversation_history``-Handling. Wird noch vom
      Deep-Research-Pfad (nicht-Gemini, z. B. Perplexity) genutzt.

    Modell-Routing: ``ollama/*`` (und Default) -> Ollama ``/v1`` lokal;
    Cloud-Modelle (``openai/*``, ``anthropic/*``, ``gemini/*``) -> LiteLLM-Proxy.

    Grounding-Politik (Datenschutz):
    - Lokales Modell: voller Zugriff per Default (alle MCP-Tools, Memory/USER-
      Profil, Kontextdateien). Optional kann der Nutzer via ``enabled_servers``
      (nicht-leere Liste) einzelne MCP-Server einschraenken; leer/None = alles.
      Daten bleiben lokal.
    - Cloud-Modell: Default-Deny. Nur explizit per ``enabled_servers``
      freigegebene MCP-Server sind verfuegbar; Memory/USER-Profil nur bei
      ``include_memory=True``; Kontextdateien (SOUL/AGENTS) bleiben aus.
    """
    from run_agent import AIAgent

    cfg = get_settings()
    sel = (model or "").strip()
    if _is_local_model(sel):
        base_url = f"{cfg.ollama_base_url.rstrip('/')}/v1"
        api_key = "ollama"
        resolved_model = sel.removeprefix("ollama/") or cfg.triage_model.removeprefix("ollama/")
        # Lokal: voller Kontext. Default = gehärtete Allowlist + Delegation. Ist im
        # Grounding eine explizite Server-Auswahl gesetzt (nicht-leere Liste), wird
        # sie respektiert: Core-Toolsets + nur die aktivierten MCP-Server. Leer/None
        # bedeutet voller Zugriff (Default) — Daten bleiben ohnehin lokal.
        if enabled_servers:
            configured = set(get_configured_server_keys() or _KNOWN_MCP_SERVERS)
            servers = [s for s in expand_graph_admin(list(enabled_servers)) if s in configured]
            enabled_toolsets = [*LOCAL_CORE_TOOLSETS, "delegation", *servers]
        else:
            enabled_toolsets = build_local_allowlist(include_delegation=True)
        skip_memory = False
        skip_context_files = False
    else:
        base_url = f"{cfg.litellm_base_url.rstrip('/')}/v1"
        api_key = "sk-litellm-local"
        resolved_model = sel
        # Cloud: Default-Deny, nur explizit freigegebene MCP-Server.
        enabled_toolsets = resolve_cloud_toolsets(enabled_servers)
        skip_memory = not include_memory
        skip_context_files = True

    if preset == "chat":
        # Reiner Chat: keinerlei Tools — das Modell soll antworten, nicht agieren.
        enabled_toolsets = []
        # Kontextdateien (SOUL/AGENTS) sind Agenten-Identitaet; im Plain-Chat aus.
        skip_context_files = True

    request_overrides = None
    if temperature is not None:
        request_overrides = {"temperature": float(temperature)}

    return AIAgent(
        base_url=base_url,
        api_key=api_key,
        provider="custom",
        api_mode="chat_completions",
        model=resolved_model,
        enabled_toolsets=enabled_toolsets,
        skip_memory=skip_memory,
        skip_context_files=skip_context_files,
        max_iterations=90,
        tool_delay=0.0,
        quiet_mode=True,
        save_trajectories=True,
        session_id=session_id or "taskpilot-chat",
        request_overrides=request_overrides,
        stream_delta_callback=on_text,
        reasoning_callback=on_reasoning,
        tool_start_callback=on_tool_start,
        tool_complete_callback=on_tool_complete,
        clarify_callback=clarify_callback,
    )


async def _init_agent():
    """Initialisiert den persistenten Worker-Agent (nach Runtime-Setup)."""
    global _agent
    if _agent is not None:
        return _agent
    if not await ensure_runtime_ready():
        return None
    try:
        _agent = await asyncio.to_thread(_build_worker_agent)
        logger.info("Hermes Worker-AIAgent initialisiert (Modell: %s)", _agent.model)
    except Exception:
        logger.exception("Hermes Worker-AIAgent-Initialisierung fehlgeschlagen")
        _agent = None
    return _agent


def _build_cloud_job_agent(model: str):
    """Ephemerer Agent fuer einen Job mit Cloud-``llm_override``.

    Grounding-Politik wie im Chat: Cloud = Default-Deny (keine MCP-Tools, kein
    Memory/USER-Profil, keine Kontextdateien). Der Task-Kontext steht vollstaendig
    im Prompt (siehe ``_build_generic_prompt``) -- Schreib-/Analyseauftraege sind
    damit trotzdem moeglich. Routing via LiteLLM-Proxy.
    """
    from run_agent import AIAgent

    cfg = get_settings()
    return AIAgent(
        base_url=f"{cfg.litellm_base_url.rstrip('/')}/v1",
        api_key="sk-litellm-local",
        provider="custom",
        api_mode="chat_completions",
        model=model,
        enabled_toolsets=[],
        skip_memory=True,
        skip_context_files=True,
        max_iterations=30,
        tool_delay=0.0,
        quiet_mode=True,
        save_trajectories=True,
        session_id="taskpilot-worker-cloud",
        reasoning_callback=_on_reasoning,
        tool_start_callback=_on_tool_start,
        tool_complete_callback=_on_tool_complete,
    )


async def _init_triage_agent():
    """Initialisiert den reduzierten Triage-Agent (Tool-Scoping, Paket C).

    Eigene persistente Instanz mit der schlanken Triage-Allowlist (Core ohne
    ``web`` + graph + taskpilot). Best-effort: schlaegt der Bau fehl, faellt der
    Worker-Loop auf den vollen Agenten zurueck.
    """
    global _triage_agent
    if _triage_agent is not None:
        return _triage_agent
    if not await ensure_runtime_ready():
        return None
    try:
        _triage_agent = await asyncio.to_thread(
            _build_worker_agent, build_triage_allowlist(), "taskpilot-worker-triage",
        )
        logger.info(
            "Hermes Triage-AIAgent initialisiert (reduzierte Toolsets: %s)",
            build_triage_allowlist(),
        )
    except Exception:
        logger.exception("Hermes Triage-AIAgent-Initialisierung fehlgeschlagen")
        _triage_agent = None
    return _triage_agent


async def _init_gather_agent():
    """Initialisiert den Recherche-Agenten (Pass 2a) mit eigener Allowlist.

    Bisher liehen sich Klassifikation und Recherche denselben Agenten. Das war
    bequem und fachlich falsch: der Lauf, der recherchieren soll, hatte keinen
    Zugang zu den Fachsystemen. Eigene Instanz, damit der Umfang unabhaengig von
    der Klassifikation einstellbar ist. Best-effort mit Rueckfall auf den
    Triage-Agenten.
    """
    global _gather_agent
    if _gather_agent is not None:
        return _gather_agent
    if not await ensure_runtime_ready():
        return None
    try:
        allow = build_gather_allowlist()
        _gather_agent = await asyncio.to_thread(
            _build_worker_agent, allow, "taskpilot-worker-gather",
        )
        logger.info("Hermes Recherche-AIAgent initialisiert (Toolsets: %s)", allow)
    except Exception:
        logger.exception("Hermes Recherche-AIAgent-Initialisierung fehlgeschlagen")
        _gather_agent = None
    return _gather_agent


def _draft_sampling_overrides(disable_thinking: bool = True, *, local: bool = True) -> dict:
    """Prosa-Sampling fuer den Schreib-Pass (Qwen-3.6-Empfehlung fuer Non-Thinking).

    ``temperature``/``top_p``/``presence_penalty`` gehen als Standard-Chat-Parameter,
    ``top_k`` provider-spezifisch via ``extra_body``.

    Thinking wird doppelt abgeschaltet, weil kein Parameter bei allen Providern
    wirkt: ``chat_template_kwargs.enable_thinking`` versteht vLLM, ``reasoning_effort``
    versteht Ollama. Ollama ignoriert das erstere stillschweigend -- ohne
    ``reasoning_effort`` erzeugt der Schreib-Pass mehrere Tausend Reasoning-Tokens
    fuer eine E-Mail von 150 und kann das Token-Budget komplett aufbrauchen (leere
    Antwort). ``reasoning_effort="none"`` ist kein Standard-OpenAI-Wert, darum nur
    fuer lokale Modelle (``local=False`` bei Cloud-Override).
    """
    cfg = get_settings()
    extra: dict = {"top_k": cfg.draft_top_k}
    out: dict = {
        "temperature": cfg.draft_temperature,
        "top_p": cfg.draft_top_p,
        "presence_penalty": cfg.draft_presence_penalty,
    }
    if disable_thinking:
        extra["chat_template_kwargs"] = {"enable_thinking": False}
        if local and cfg.draft_reasoning_effort:
            out["reasoning_effort"] = cfg.draft_reasoning_effort
    out["extra_body"] = extra
    return out


def _run_agent_sync(
    agent,
    prompt: str,
    disable_thinking: bool,
    overrides: dict | None = None,
    max_iterations: int | None = None,
) -> str:
    """Synchroner Agent-Lauf (im Thread). Gibt den finalen Antworttext zurueck.

    ``disable_thinking`` setzt ``extra_body.chat_template_kwargs``; standardmaessig
    False (Thinking an). ``overrides`` erlaubt vollstaendige ``request_overrides``
    (z. B. Prosa-Sampling im Draft-Pass) und hat Vorrang -- der Aufrufer ist dann
    fuer den Thinking-Schalter zustaendig.

    ``max_iterations`` begrenzt die Werkzeug-Runden fuer diesen einen Lauf. Noetig,
    weil eine Rundengrenze im Prompt nur eine Bitte ist: im Live-Test vom 03.08.2026
    wiederholte das Modell dieselbe Suchanfrage dreimal und lieferte nie ein
    Ergebnis, obwohl der Prompt hoechstens fuenf Suchvorgaenge erlaubte. Hermes
    fordert beim Erreichen der Grenze selbsttaetig eine werkzeuglose Zusammenfassung
    an -- aus der Endlosschleife wird so ein verwertbares Resultat.

    Achtung: Der ``chat_template_kwargs``-Fallback hier wirkt nur bei Providern wie
    vLLM. Ollama ignoriert ihn (siehe ``_draft_sampling_overrides``), das dortige
    Gegenmittel ``reasoning_effort`` ist hier bewusst nicht gesetzt, weil die
    Locality des Agenten an dieser Stelle nicht bekannt ist und der Wert "none" von
    Cloud-Providern abgelehnt wuerde. Aufrufer, die Thinking verlaesslich abschalten
    muessen, geben ``overrides`` aus ``_draft_sampling_overrides`` mit.
    """
    prev_overrides = getattr(agent, "request_overrides", None)
    prev_iterations = getattr(agent, "max_iterations", None)
    req: dict | None = None
    if overrides is not None:
        req = dict(overrides)
    elif disable_thinking:
        req = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    if req is not None:
        agent.request_overrides = req
    if max_iterations is not None:
        agent.max_iterations = max_iterations
    try:
        result = agent.run_conversation(prompt, system_message=WORKER_SYSTEM_PROMPT)
    finally:
        if req is not None:
            agent.request_overrides = prev_overrides
        if max_iterations is not None and prev_iterations is not None:
            agent.max_iterations = prev_iterations

    if isinstance(result, dict):
        return str(result.get("final_response") or "")
    return str(result or "")


def _sender_org_hint(from_addr: str) -> str:
    """Firmenkennung aus der Absenderdomäne (ohne TLD und ohne Freemailer).

    Dient als Suchbaustein im Sammel-Lauf: «swissbankers» aus
    ``franziska.koenig@swissbankers.ch``. Bei Freemail-Adressen leer, weil
    «gmail» als Suchbegriff nur Rauschen erzeugt.
    """
    domain = (from_addr or "").split("@")[-1].strip().lower()
    if not domain or "." not in domain:
        return ""
    label = domain.split(".")[0]
    if label in _FREEMAIL_LABELS:
        return ""
    return label


_FREEMAIL_LABELS: frozenset[str] = frozenset({
    "gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "gmx",
    "bluewin", "icloud", "me", "protonmail", "proton", "web", "t-online",
})


def _context_need(meta: dict, parsed: dict | None) -> str:
    """Bestimmt, ob diese Mail eine Fachrecherche braucht: ``none``/``calendar``/``substance``.

    Konditional statt pauschal -- eine reine Terminanfrage («Wann hast du Zeit?»)
    braucht den Kalender und sonst nichts; eine Recherche dazu kostet nur Zeit und
    kann fachfremden Kontext einschleppen.

    Reihenfolge: eine ausdrueckliche Angabe aus Pass 1 hat Vorrang, danach greift die
    bestehende Termin-Heuristik, sonst ``substance``. Der Default ist bewusst
    ``substance``: eine ueberfluessige Recherche kostet Zeit, eine ausgelassene kostet
    den Entwurf. Rein und damit testbar.
    """
    stated = str(((parsed or {}).get("context_need") or "")).strip().lower()
    if stated in ("none", "calendar", "substance"):
        return stated
    if _looks_like_scheduling(meta.get("subject") or "", meta.get("body_preview") or ""):
        return "calendar"
    return "substance"


def _gather_sampling_overrides() -> dict:
    """Sampling fuer den Sammel-Lauf: niedrige Temperatur, kein Thinking.

    Der Sammel-Lauf waehlt Werkzeuge und traegt Fakten zusammen -- dort schadet
    das Prosa-Sampling des Schreib-Passes (temp 0.7), weil es die Query-Wahl
    verrauscht. Thinking bleibt aus: gemessen am 03.08.2026 ruft das Modell die
    Suche auch ohne Reasoning zuverlaessig auf (5 von 5), und Reasoning-Deltas
    kosten hier nur Zeit.
    """
    cfg = get_settings()
    out: dict = {
        "temperature": cfg.draft_context_temperature,
        "top_p": cfg.draft_top_p,
        "extra_body": {"top_k": cfg.draft_top_k, "chat_template_kwargs": {"enable_thinking": False}},
    }
    if cfg.draft_reasoning_effort:
        out["reasoning_effort"] = cfg.draft_reasoning_effort
    return out


async def _build_gather_prompt(meta: dict, parsed: dict | None = None) -> str:
    """Baut den Rechercheauftrag fuer Pass 2a (Sammeln, nicht Schreiben)."""
    from_addr = meta.get("from_address", "")
    subject = meta.get("subject", "")
    conversation_id = meta.get("conversation_id", "")
    body_text = await _load_email_body_text(meta.get("email_message_id", ""))
    body_block = body_text or (meta.get("body_preview") or "")[:300] or "(kein Textinhalt verfügbar)"

    extra_tools = ""
    if conversation_id:
        extra_tools = (
            f'   Der bisherige Verlauf ist mit **get_thread("{conversation_id}")** '
            "abrufbar, falls die Vorgeschichte für das Verständnis nötig ist.\n"
        )

    return render_gather_task(
        today=_today_context_line(),
        subject=subject,
        from_name=meta.get("from_name", ""),
        from_addr=from_addr,
        body_block=body_block,
        briefing_block=_build_draft_briefing(parsed),
        sender_org=_sender_org_hint(from_addr),
        topic_hint=subject,
        max_rounds=_GATHER_MAX_ROUNDS,
        extra_tools=extra_tools,
        extra_systems=_gather_extra_systems(),
    )


def _gather_extra_systems() -> str:
    """Systemkarten-Zeilen fuer die Fachsysteme des breiten Umfangs.

    Nur nennen, was der Agent auch aufrufen kann -- ein Verweis auf ein Werkzeug,
    das nicht in seiner Allowlist steht, produziert nur Fehlversuche.
    """
    if not get_settings().draft_context_wide_tools:
        return ""
    return (
        "- Verkaufschancen, Deals, Angebotsstand → **search_crm**\n"
        "- Rechnungen und Offerten → **search_invoices**\n"
        "- Erfasste Zeit auf Toggl-Ebene → **search_time_entries**\n"
    )


# Obergrenze der Recherche-Runden. Die Literatur zu agentischem Retrieval nennt
# 3-5 Runden als Bereich, in dem der Nutzen noch die Latenz rechtfertigt; darueber
# hinaus wiederholt das Modell meist nur Varianten derselben Anfrage.
_GATHER_MAX_ROUNDS = 5


async def _gather_draft_context(meta: dict, parsed: dict | None = None) -> str | None:
    """Pass 2a: lokaler, agentischer Sammel-Lauf vor dem Schreiben.

    Laeuft bewusst LOKAL und mit echten Namen -- Retrieval funktioniert nur so.
    Liefert ein Markdown-Dossier oder ``None``. Best-effort: scheitert die
    Recherche, schreibt der Draft-Pass wie bisher ohne Fachkontext weiter.

    Der Sammel-Agent hat eine eigene Allowlist (``build_gather_allowlist``): Zugang
    zu den Fachsystemen, aber ohne ``web`` und ohne ``graphAdmin``. Er kann also
    suchen und lesen, aber weder Entwuerfe erstellen noch den Outlook-Zustand
    veraendern. Rueckfall auf Triage- bzw. Worker-Agent, falls der Bau scheitert.
    """
    agent = await _init_gather_agent() or _triage_agent or _agent
    if agent is None:
        logger.warning("Kontext-Recherche: kein Agent verfügbar")
        return None
    try:
        prompt = await _build_gather_prompt(meta, parsed)
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Kontext-Recherche: Prompt-Bau fehlgeschlagen")
        return None
    try:
        dossier = await asyncio.to_thread(
            _run_agent_sync,
            agent,
            prompt,
            True,
            _gather_sampling_overrides(),
            _GATHER_MAX_ROUNDS + 1,
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Kontext-Recherche: Sammel-Lauf fehlgeschlagen")
        return None
    text = (dossier or "").strip()
    if not text:
        return None
    cap = get_settings().draft_context_max_chars
    if len(text) > cap:
        text = text[:cap].rstrip() + " […]"
    logger.info(
        "Kontext-Recherche abgeschlossen: %d Zeichen Dossier, %d Quellen",
        len(text), len(_job_context_sources),
    )
    return text


async def _generate_reply_draft(meta: dict, parsed: dict | None = None) -> str | None:
    """Zweiter Pass (nur bei ``two_pass_draft``): Kontext sammeln, dann schreiben.

    Ablauf:

    1. **Pass 2a (lokal, agentisch):** ``_gather_draft_context`` recherchiert mit
       echten Namen den Fachkontext und verdichtet ihn zu einem Dossier.
    2. **Pass 2b (schreiben):** Lokal per Werkzeug ``create_draft`` -- oder, wenn
       ``draft_model`` ein Cloud-Modell nennt, werkzeuglos auf Basis des
       ANONYMISIERTEN Prompts, mit anschliessender Deanonymisierung und
       server-seitiger Entwurfserstellung.

    ``parsed`` reicht das Briefing (rationale/label) aus Pass 1 weiter. Best-effort:
    liefert die echte Draft-ID oder ``None``. Bei ``None`` greift im Post-Processing
    der bestehende Fail-closed-Pfad (auto_reply ohne Draft -> fyi).
    """
    global _job_created_draft_id
    if _agent is None:
        logger.warning("Zwei-Pass-Draft: kein Worker-Agent verfügbar")
        return None

    cfg = get_settings()
    _job_context_sources.clear()
    dossier: str | None = None
    need = _context_need(meta, parsed)
    researched = cfg.draft_context_research and need == "substance"
    if researched:
        dossier = await _gather_draft_context(meta, parsed)
        _tag_trace_pass("gather")
    else:
        logger.info("Kontext-Recherche übersprungen (context_need=%s)", need)

    try:
        prompt = await _build_draft_prompt(meta, parsed, dossier, researched)
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Zwei-Pass-Draft: Prompt-Bau fehlgeschlagen")
        return None

    # Der Schreib-Prompt ist der wichtigste Beleg: Mailtext, Thread-Block, Stil-Anker
    # und Dossier stehen darin. Was der Entwurf behauptet, muss hier oder in einem
    # Tool-Ergebnis stehen.
    _record_evidence(prompt)

    draft_model = await _resolve_draft_model()
    if draft_model and not _is_local_model(draft_model):
        return await _write_draft_with_cloud_model(meta, prompt, draft_model)

    # Nur den in DIESEM Pass erstellten Entwurf erfassen.
    _job_created_draft_id = None
    try:
        await asyncio.to_thread(
            _run_agent_sync, _agent, prompt, True, _draft_sampling_overrides(True)
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Zwei-Pass-Draft: Schreib-Pass fehlgeschlagen")
        return None
    return _job_created_draft_id


async def _resolve_draft_model() -> str:
    """Schreib-Modell fuer Pass 2b aus den Owner-Settings (leer = lokal).

    Best-effort: ist die DB nicht erreichbar, bleibt der Schreib-Pass lokal. Das ist
    die sichere Richtung -- ein Ausfall darf nie dazu fuehren, dass Text ungeplant an
    ein oeffentliches Modell geht.
    """
    from app.services.llm_defaults import get_draft_model

    try:
        async with async_session() as db:
            return await get_draft_model(db)
    except Exception:  # noqa: BLE001
        logger.warning("Schreib-Modell nicht aus Settings lesbar -- lokaler Pass")
        return ""


async def _anonymize_for_cloud(text: str) -> tuple[str, str]:
    """Maskiert Personen-/Firmenbezüge vor dem Versand an ein Cloud-Modell.

    Nutzt dieselbe Strecke wie die Finanzanalyse (contentConverter + Mapping-Store),
    damit es genau eine Anonymisierungs-Implementierung im System gibt. Gibt
    ``(maskierter_text, session_id)`` zurueck. Wirft bei Fehlschlag -- der Aufrufer
    faellt dann auf das lokale Modell zurueck (fail-closed).
    """
    from ai9 import content_converter as cc
    from ai9 import mapping_store

    result = await cc.call_tool(
        "anonymize_content",
        text=text,
        entities=",".join(_CLOUD_ANON_ENTITIES),
        language="de",
    )
    if not isinstance(result, dict):
        raise RuntimeError("Anonymisierung lieferte kein Mapping")
    anon = result.get("anonymized_text") or ""
    if not anon:
        raise RuntimeError("Anonymisierung lieferte leeren Text")
    session_id, _diff = mapping_store.store_mapping(result.get("mapping_keys", {}))
    return anon, session_id


async def _deanonymize_from_cloud(text: str, session_id: str) -> str:
    """Setzt die Originalwerte in der Cloud-Antwort wieder ein.

    Fehlt das Mapping (TTL abgelaufen, Backend-Neustart), wird geworfen statt den
    maskierten Text zurueckzugeben. Sonst entstuende ein Entwurf, in dem die
    Tarnnamen stehen -- und die sind echte, plausible Namen, kein sichtbarer Fehler.
    """
    from ai9 import content_converter as cc
    from ai9 import mapping_store

    keys = mapping_store.get_mapping_keys(session_id)
    if not keys:
        raise RuntimeError("Anonymisierungs-Mapping nicht mehr verfuegbar")
    result = await cc.call_tool("deanonymize_content", text=text, mapping_keys=keys)
    return result if isinstance(result, str) else str(result)


def _residual_pseudonyms(text: str, session_id: str) -> list[str]:
    """Deckt Tarnnamen auf, die die Ruecksetzung nicht erwischt hat.

    Die Maskierung arbeitet nicht mit Platzhaltern, sondern mit ERSATZNAMEN: aus
    «Gabriel» wird «Senad Weibel», aus «InnoSmith GmbH» wird «Hess & Partner»
    (geprueft am 04.08.2026 gegen das echte contentConverter-Modell). Das schuetzt
    die Fluessigkeit des Textes, birgt aber ein Risiko, das Platzhalter nicht
    haetten: schreibt das Modell den Ersatznamen verkuerzt oder gebeugt («Hoi
    Senad»), findet die Ruecksetzung ihn nicht -- und im Entwurf an einen echten
    Kunden steht ein fremder, voellig plausibler Name.

    Geprueft wird deshalb der ganze Ersatzname und -- nur bei Personen -- jeder
    Namensteil. Bei Firmen bleibt es beim ganzen String, weil Bestandteile wie
    «Partner» oder «Gruppe» sonst Fehlalarme ausloesen.
    """
    from ai9 import mapping_store

    keys = mapping_store.get_mapping_keys(session_id) or {}
    mappings = keys.get("mappings") or {}
    entity_types = keys.get("entity_types") or {}
    lowered = (text or "").lower()

    hits: list[str] = []
    for fake in mappings:
        fake_str = str(fake).strip()
        if not fake_str:
            continue
        if fake_str.lower() in lowered:
            hits.append(fake_str)
            continue
        if str(entity_types.get(fake) or "").upper() != "PERSON":
            continue
        for part in re.findall(r"[^\W\d_]{4,}", fake_str, re.UNICODE):
            if part.lower() in lowered:
                hits.append(part)
                break
    return hits


async def _write_draft_locally(prompt: str) -> str | None:
    """Schreib-Pass mit dem lokalen Modell (Rueckfall des Cloud-Pfads)."""
    global _job_created_draft_id

    _job_created_draft_id = None
    try:
        await asyncio.to_thread(
            _run_agent_sync, _agent, prompt, True, _draft_sampling_overrides(True)
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Lokaler Schreib-Pass fehlgeschlagen")
        return None
    return _job_created_draft_id


# Entitaeten analog zur Finanzanalyse-Pipeline (contentConverter-Konvention).
_CLOUD_ANON_ENTITIES = ["PERSON", "ORG", "LOCATION", "EMAIL", "PHONE", "IBAN"]


async def _write_draft_with_cloud_model(
    meta: dict, prompt: str, model: str
) -> str | None:
    """Pass 2b mit Cloud-Modell: anonymisiert, werkzeuglos, deanonymisiert.

    Die automatische Barriere ist hier nicht abschaltbar: E-Mail-Entwuerfe entstehen
    unbeaufsichtigt, der Kontext ist vorher nicht pruefbar. Schlaegt die
    Anonymisierung fehl, wird NICHT an die Cloud gesendet, sondern auf das lokale
    Modell zurueckgefallen (fail-closed).

    Werkzeuglos ist Absicht, nicht Einschraenkung: ein maskiertes Modell wuerde mit
    Platzhaltern suchen ("PERSON_1 KreditorenBot") und nichts finden. Das Sammeln
    ist deshalb bereits in Pass 2a passiert; das Cloud-Modell bekommt das Ergebnis
    und schreibt daraus. Den Entwurf legt das Backend deterministisch an -- damit
    ist die Thread-Zugehoerigkeit ohnehin garantiert.
    """
    global _job_created_draft_id

    try:
        anon_prompt, session_id = await _anonymize_for_cloud(prompt)
    except Exception as exc:  # noqa: BLE001 - fail-closed auf lokal
        logger.warning(
            "Cloud-Entwurf: Anonymisierung fehlgeschlagen (%s) -- lokaler Schreib-Pass",
            exc,
        )
        return await _write_draft_locally(prompt)

    try:
        cloud_agent = await asyncio.to_thread(_build_cloud_job_agent, model)
        text = await asyncio.to_thread(
            _run_agent_sync,
            cloud_agent,
            f"{anon_prompt}\n\n{_CLOUD_WRITER_SUFFIX}",
            True,
            _draft_sampling_overrides(True, local=False),
        )
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Cloud-Entwurf: Schreib-Lauf fehlgeschlagen (Modell %s)", model)
        return None

    if not (text or "").strip():
        logger.warning("Cloud-Entwurf: leere Antwort von %s", model)
        return None

    try:
        text = await _deanonymize_from_cloud(text, session_id)
    except Exception:  # noqa: BLE001 - lieber kein Entwurf als ein maskierter
        logger.exception("Cloud-Entwurf: De-Anonymisierung fehlgeschlagen -- verworfen")
        return None

    residual = _residual_pseudonyms(text, session_id)
    if residual:
        logger.warning(
            "Cloud-Entwurf: Tarnnamen nach Ruecksetzung noch im Text (%s) -- "
            "lokaler Schreib-Pass statt Entwurf mit fremdem Namen",
            residual[:5],
        )
        return await _write_draft_locally(prompt)

    draft_id = await _create_reply_draft_from_text(meta.get("email_message_id", ""), text)
    if draft_id:
        _job_created_draft_id = draft_id
        _job_tool_names.add("cloud_draft_writer")
        logger.info("Cloud-Entwurf erstellt (Modell %s, draft_id=%s)", model, draft_id[:40])
    return draft_id


_CLOUD_WRITER_SUFFIX = (
    "WICHTIG: Du hast in diesem Lauf KEINE Werkzeuge. Der gesamte Kontext steht oben. "
    "Gib ausschliesslich den fertigen E-Mail-Text aus -- Anrede, Inhalt, Schlussformel. "
    "Keine Betreffzeile, keine Erklärungen, keine Meta-Kommentare, kein Markdown-"
    "Codeblock.\n"
    "Alle Personen- und Firmennamen im Kontext sind zum Schutz der Daten durch "
    "ANDERE Namen ersetzt. Übernimm jeden Namen exakt und vollständig so, wie er "
    "oben steht -- nie verkürzt, nie nur den Vornamen, nie gebeugt. Nach dem "
    "Schreiben werden diese Namen automatisch durch die echten ersetzt; das "
    "funktioniert nur bei wörtlicher Übernahme.\n"
    "Nenne ausserdem keine Zahl, Adresse oder Bezeichnung, die nicht oben steht. "
    "Fehlt eine Angabe, frage im Text danach, statt einen Wert einzusetzen."
)


def _plain_text_to_html(text: str) -> str:
    """Wandelt den Entwurfstext in schlichtes HTML (Absätze, Zeilenumbrüche)."""
    from html import escape

    blocks = [b.strip() for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    if not blocks:
        return ""
    return "".join(
        "<p>" + "<br>".join(escape(line) for line in block.splitlines()) + "</p>"
        for block in blocks
    )


async def _create_reply_draft_from_text(email_id: str, text: str) -> str | None:
    """Legt einen Reply-Entwurf im Original-Thread an (server-seitig, ohne LLM-Tool).

    Wird vom werkzeuglosen Cloud-Schreibpfad genutzt. ``createReplyAll`` uebernimmt
    Empfaenger und Betreff aus der Originalmail -- damit ist die Thread-Zugehoerigkeit
    garantiert und es gibt keine vom Modell erfundenen Adressaten.
    """
    if not email_id:
        return None
    body_html = _plain_text_to_html(text)
    if not body_html:
        return None
    client = await _build_graph_client()
    if client is None:
        logger.warning("Cloud-Entwurf: Graph nicht konfiguriert")
        return None
    try:
        created = await client.create_draft(
            subject="",
            body_html=body_html,
            to_recipients=[],
            reply_to_id=email_id,
            reply_all=True,
        )
        return (created or {}).get("id")
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("Cloud-Entwurf: Entwurf konnte nicht angelegt werden")
        return None
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def _enforce_autonomy_status(meta: dict, content: str) -> str:
    """Bestimmt den End-Status nach Autonomie-Stufe statt per Text-Heuristik.

    - **L0 Block**: nicht ausführen -> ``blocked`` (Defensive; L0 sollte den
      Worker gar nicht erreichen, da Trigger/Scheduler L0 sperren).
    - **L1 Freigabe**: immer ``awaiting_approval`` (Entwurf, nie Auto-Versand).
    - **L2 Melden**: ``completed`` + post-hoc Benachrichtigung.
    - **L3 Auto**: autonom ``completed``.

    Ohne Autonomie-Kontext (Alt-Jobs/sonstige Typen) gilt die bisherige
    Heuristik (``awaiting_approval``, wenn das Modell es signalisiert).
    """
    autonomy = (meta or {}).get("autonomy_level")
    if autonomy == "L0":
        return "blocked"
    if autonomy == "L1":
        return "awaiting_approval"
    if autonomy in ("L2", "L3"):
        return "completed"
    return "awaiting_approval" if "awaiting_approval" in content.lower() else "completed"


def _local_override_request(
    meta: dict, agent_model: str | None, disable_thinking: bool
) -> dict | None:
    """Berechnet die ``request_overrides`` fuer einen lokalen LLM-Override.

    Gibt None zurueck, wenn kein Override gesetzt, das Override ein Cloud-Modell
    ist (laeuft ueber einen eigenen Agenten) oder das Modell dem Agent-Default
    entspricht. Rein und damit unabhaengig testbar.
    """
    override_model = (meta or {}).get("llm_override") or ""
    # Platzhalter ('hermes'/'nanobot') sind kein echtes Modell -> kein Override.
    if not override_model or override_model in ("hermes", "nanobot"):
        return None
    if not _is_local_model(override_model):
        return None
    resolved = override_model.removeprefix("ollama/")
    if not resolved or resolved == agent_model:
        return None
    overrides: dict = {"model": resolved}
    if disable_thinking:
        overrides["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        # Hier ist das Modell per Definition lokal (oben via _is_local_model geprueft),
        # darum zusaetzlich der Schalter, den Ollama tatsaechlich auswertet.
        effort = get_settings().draft_reasoning_effort
        if effort:
            overrides["reasoning_effort"] = effort
    return overrides


async def _process_job(agent, job_id, job_type: str, prompt: str, meta: dict) -> None:
    """Verarbeitet einen einzelnen AgentJob via Hermes AIAgent."""
    global _job_trace, _job_created_draft_id, _job_moved_message_id
    logger.info("Starte Job %s (type=%s)", job_id, job_type)

    async with async_session() as db:
        await db.execute(
            update(AgentJob)
            .where(AgentJob.id == job_id)
            .values(status="running", started_at=datetime.now(timezone.utc))
        )
        await db.commit()

    _job_trace = []
    _job_created_draft_id = None
    _job_moved_message_id = None
    _job_tool_names.clear()
    _job_context_sources.clear()
    _job_evidence.clear()
    disable_thinking = _thinking_disabled(job_type, meta.get("skill"))

    # Briefings sind reine Prosa-Synthese (alle Daten stehen im Prompt): sie laufen
    # mit Prosa-Sampling (temp 0.7 etc.) und ohne Thinking — identisch zum
    # Draft-Schreib-Pass. Das Default-Sampling produzierte nachweislich
    # verstümmelte Zahlen und schwachen Stil.
    is_briefing_job = job_type in ("daily_briefing", "weekly_briefing", "monthly_briefing")
    if is_briefing_job:
        disable_thinking = True

    # Pro-Task-LLM-Override (lokal): Modellwechsel fuer diesen Job via
    # request_overrides (Leitprinzip 3: LLM-Kontrolle pro Task). Cloud-Overrides
    # laufen ueber einen eigenen Agenten (siehe _worker_loop/_build_cloud_job_agent).
    overrides = _local_override_request(meta, getattr(agent, "model", None), disable_thinking)
    if overrides:
        logger.info("Job %s: LLM-Override aktiv -> %s", job_id, overrides.get("model"))

    if is_briefing_job:
        # Locality am Override-Selektor bestimmen, nicht an ``agent.model``: dort steht
        # bei lokalen Modellen der aufgeloeste Ollama-Name ohne ``ollama/``-Prefix.
        prose = _draft_sampling_overrides(
            True, local=_is_local_model(str(meta.get("llm_override") or ""))
        )
        if overrides:
            # Modell-Override behalten, Prosa-Sampling ergänzen (extra_body mergen).
            merged_extra = {**prose.get("extra_body", {}), **(overrides.get("extra_body") or {})}
            overrides = {**prose, **overrides, "extra_body": merged_extra}
        else:
            overrides = prose

    # Token-Verbrauch pro Job messen: der persistente Agent zaehlt kumulativ
    # (session_total_tokens) -- die Differenz vor/nach dem Lauf ist der Verbrauch
    # dieses Jobs. Grundlage fuer Kosten-/Kontext-Observability im Cockpit.
    tokens_before = int(getattr(agent, "session_total_tokens", 0) or 0)

    try:
        content = await asyncio.to_thread(
            _run_agent_sync, agent, prompt, disable_thinking, overrides
        )
        # Echte Draft-ID aus dem Tool-Ergebnis (ground truth) an das Post-Processing
        # weiterreichen -- die vom LLM gemeldete ID wird bewusst ignoriert.
        captured_draft_id = _job_created_draft_id
        captured_moved_id = _job_moved_message_id
        logger.info("Job %s abgeschlossen: %s", job_id, content[:200])

        if job_type == "email_triage":
            # Zuverlaessigkeit: Liefert der erste Lauf keinen verwertbaren JSON-Block,
            # genau EIN strikter Nachfass-Prompt, bevor das Fallback-Netz greift.
            if _extract_json_block(content) is None:
                logger.info("Job %s: kein JSON-Block -- strikter Nachfass-Lauf", job_id)
                retry = await asyncio.to_thread(
                    _run_agent_sync, agent, _json_retry_prompt(content), disable_thinking
                )
                if retry and _extract_json_block(retry) is not None:
                    content = f"{content}\n\n{retry}"
            # Events des Klassifikations-Laufs markieren, bevor der Schreib-Pass
            # weiterschreibt -- sonst ist im Cockpit nicht unterscheidbar, welcher
            # Pass welches Tool aufgerufen hat.
            _tag_trace_pass("classify")
            # tools_used aus der ungekappten Tool-Namen-Menge (nicht aus dem
            # 200-Event-Trace) -- sonst fehlen spaete Tools wie create_draft/
            # search_my_replies und das Kontext-Gate stuft faelschlich herunter.
            tools_used = sorted(_job_tool_names)
            status = await _post_process_triage(
                job_id, content, meta, captured_draft_id, tools_used, captured_moved_id
            )
            # Trace ERST JETZT einfrieren: der Schreib-Pass laeuft im
            # Post-Processing und war vorher grundsaetzlich nie im Trace sichtbar --
            # genau deshalb blieb der Platzhalter-Entwurf so lange unauffindbar.
            _tag_trace_pass("draft")
            trace = list(_job_trace)
            # Nach dem Post-Processing neu erfassen: ein evtl. Zwei-Pass-Schreib-Pass
            # hat weitere Tools (get_email/get_thread/search_my_replies/create_draft)
            # aufgerufen -- fuer korrekte Observability/Self-Grade mitzaehlen.
            tools_used = sorted(_job_tool_names)
        elif job_type == "chat_triage":
            trace = list(_job_trace)
            tools_used = sorted(_job_tool_names)
            status = await _post_process_chat_triage(job_id, content, meta)
        elif job_type == "meeting_summary":
            trace = list(_job_trace)
            tools_used = sorted(_job_tool_names)
            status = await _post_process_meeting_summary(job_id, content, meta)
        else:
            trace = list(_job_trace)
            tools_used = sorted(_job_tool_names)
            status = _enforce_autonomy_status(meta, content)
            # Episode auch fuer delegierte Task-/generische Jobs (Lern-Paritaet):
            # Grundlage fuer Recall, wenn aehnliche Auftraege wiederkehren.
            try:
                async with async_session() as ep_db:
                    await record_episode(
                        ep_db,
                        summary=(
                            f"Agent-Job ({job_type}): "
                            f"'{(meta.get('prompt_preview') or meta.get('description') or prompt[:200])[:300]}'. "
                            f"Ergebnis-Status: {status}"
                        ),
                        job_type=job_type,
                        agent_job_id=job_id,
                        decision={"status": status},
                        commit=True,
                    )
            except Exception:  # noqa: BLE001 - best-effort, darf den Job nie kippen
                logger.warning("Episode fuer Job %s konnte nicht gespeichert werden", job_id)

        is_briefing = job_type in ("daily_briefing", "weekly_briefing", "monthly_briefing")
        if job_type != "email_triage":
            if status == "awaiting_approval":
                async with async_session() as notif_db:
                    await notify_agent_awaiting_approval(notif_db, job_id=job_id)
                    await notif_db.commit()
            elif status == "completed" and is_briefing:
                # Briefings: eigener Notification-Typ statt generischem L2-Hinweis.
                from app.services.notification import notify_briefing_ready

                labels = {
                    "daily_briefing": "Tagesbriefing",
                    "weekly_briefing": "Wochenbriefing",
                    "monthly_briefing": "Monatsbriefing",
                }
                async with async_session() as notif_db:
                    await notify_briefing_ready(
                        notif_db, job_id=job_id, briefing_label=labels[job_type],
                    )
                    await notif_db.commit()
            elif (
                status == "completed"
                and (meta or {}).get("autonomy_level") == "L2"
                and job_type != "meeting_summary"  # eigene Notification im Post-Process
            ):
                # L2 'Melden': autonom ausgeführt, Mensch post-hoc informieren.
                async with async_session() as notif_db:
                    await notify_agent_completed(notif_db, job_id=job_id)
                    await notif_db.commit()

        async with async_session() as db:
            job_result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
            job = job_result.scalar_one_or_none()
            new_meta = dict((job.metadata_json if job else None) or meta)
            new_meta["trace"] = trace
            new_meta["tools_used"] = tools_used
            # Provenance: worauf sich ein Entwurf stuetzt, gehoert an die Freigabe.
            # Die Kundeneingrenzung laeuft bewusst ueber diese Sichtbarkeit und
            # nicht ueber einen harten Suchfilter -- ein Filter kostet Recall und
            # braucht Metadaten, die der Index nicht hat, waehrend ohnehin jede
            # externe Mail vor dem Versand gelesen wird (HITL L1).
            if _job_context_sources:
                new_meta["context_sources"] = list(_job_context_sources)
            if job_type == "email_triage":
                grade = _compute_self_grade(meta, new_meta, tools_used)
                new_meta["self_grade"] = grade
                if grade["missing"]:
                    logger.info(
                        "Job %s Self-Grade %.2f, fehlende Pflicht-Kontexte: %s",
                        job_id, grade["score"], grade["missing"],
                    )
            tokens_after = int(getattr(agent, "session_total_tokens", 0) or 0)
            tokens_used = tokens_after - tokens_before
            job_values = {
                "status": status,
                "output": content[:16000],
                "metadata_json": new_meta,
                "completed_at": datetime.now(timezone.utc),
            }
            if tokens_used > 0:
                job_values["tokens_used"] = tokens_used
                # Lokale Ollama-Modelle verursachen keine API-Kosten.
                job_values["cost_usd"] = 0
            await db.execute(
                update(AgentJob).where(AgentJob.id == job_id).values(**job_values)
            )
            await db.commit()

    except Exception as e:
        logger.exception("Job %s fehlgeschlagen", job_id)
        async with async_session() as db:
            await db.execute(
                update(AgentJob)
                .where(AgentJob.id == job_id)
                .values(
                    status="failed",
                    error_message=str(e)[:2000],
                    completed_at=datetime.now(timezone.utc),
                )
            )
            if job_type == "email_triage":
                await db.execute(
                    update(EmailTriage)
                    .where(EmailTriage.agent_job_id == job_id)
                    .values(status="dismissed")
                )
            elif job_type == "chat_triage":
                await db.execute(
                    update(ChatTriage)
                    .where(ChatTriage.agent_job_id == job_id)
                    .values(triage_class="fyi", status="dismissed")
                )
            elif job_type == "meeting_summary":
                await db.execute(
                    update(MeetingTranscript)
                    .where(MeetingTranscript.agent_job_id == job_id)
                    .values(status="failed", error_message=str(e)[:2000])
                )
            await db.commit()


# ── Wartung (framework-agnostisch) ───────────────────────

async def _cleanup_orphaned_drafts() -> int:
    """Schliesst awaiting_approval-Jobs ab, deren Draft in Outlook nicht mehr existiert."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
    from graph_client import GraphClient, GraphConfig

    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return 0

    async with async_session() as db:
        result = await db.execute(
            select(AgentJob).where(
                AgentJob.status == "awaiting_approval",
                AgentJob.job_type.in_(["email_triage", "send_email"]),
            )
        )
        jobs = list(result.scalars().all())

    if not jobs:
        return 0

    config = GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    )
    client = GraphClient(config)
    resolved = 0

    try:
        for job in jobs:
            meta = job.metadata_json or {}
            draft_id = meta.get("draft_id")
            if not draft_id:
                continue
            try:
                await client.get_email(draft_id)
            except httpx.HTTPStatusError as exc:
                # NUR ein echtes 404 bedeutet "Entwurf wirklich weg" (gesendet/
                # geloescht). Alles andere (5xx, Drosselung, Netz) ist transient und
                # darf eine gueltige Freigabe NICHT zerstoeren.
                if exc.response.status_code != 404:
                    logger.warning(
                        "Draft-Cleanup: transienter Fehler (%s) fuer Job %s -- bleibt awaiting_approval",
                        exc.response.status_code, job.id,
                    )
                    continue
                async with async_session() as db:
                    await db.execute(
                        update(AgentJob)
                        .where(AgentJob.id == job.id)
                        .values(
                            status="completed",
                            output=(job.output or "") + "\n\n--- Entwurf wurde in Outlook gesendet oder gelöscht. Job automatisch abgeschlossen. ---",
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                    await db.commit()
                resolved += 1
                logger.info("Draft-Cleanup: Job %s automatisch abgeschlossen (Draft 404 -- nicht mehr in Outlook)", job.id)
            except Exception:
                # Unklarer Fehler (Timeout, Verbindungsabbruch, ...) -> Job bewusst
                # NICHT abschliessen; im naechsten Zyklus erneut pruefen.
                logger.warning(
                    "Draft-Cleanup: get_email fehlgeschlagen (kein 404) fuer Job %s -- bleibt awaiting_approval",
                    job.id,
                )
                continue
    finally:
        await client.close()

    if resolved:
        logger.info("Draft-Cleanup: %d verwaiste Jobs abgeschlossen", resolved)
    return resolved


async def _reap_stale_jobs() -> int:
    """Setzt running-Jobs, die länger als STALE_TIMEOUT_MINUTES laufen, auf failed."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_TIMEOUT_MINUTES)
    async with async_session() as db:
        result = await db.execute(
            update(AgentJob)
            .where(
                AgentJob.status == "running",
                AgentJob.started_at < cutoff,
            )
            .values(
                status="failed",
                error_message=f"Timeout: Job lief über {STALE_TIMEOUT_MINUTES} Minuten ohne Abschluss",
                completed_at=datetime.now(timezone.utc),
            )
            .returning(AgentJob.id)
        )
        reaped_ids = result.scalars().all()
        if reaped_ids:
            logger.warning(
                "Reaper: %d stale running-Jobs auf failed gesetzt: %s",
                len(reaped_ids), [str(i) for i in reaped_ids],
            )
        await db.commit()
    return len(reaped_ids)


async def _resweep_unclassified_triages(limit: int = 20) -> int:
    """Holt still durchgefallene Triages zurueck in die Queue (Selbstheilung).

    E-Mails, deren Agent-Job abgeschlossen/fehlgeschlagen ist, deren
    ``email_triage`` aber ohne Klasse auf ``pending`` haengt (z. B. aus der Zeit
    vor dem robusten Parser, oder weil der LLM keinen Block lieferte), werden mit
    geklonter Metadata neu eingereiht. Ein ``resweep_count`` deckelt die
    Wiederholungen (``MAX_RESWEEP``), damit dauerhaft problematische Mails nicht
    endlos zirkulieren.
    """
    requeued = 0
    dismissed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=RESWEEP_MAX_AGE_DAYS)
    async with async_session() as db:
        rows = await db.execute(
            select(EmailTriage, AgentJob)
            .join(AgentJob, EmailTriage.agent_job_id == AgentJob.id)
            .where(
                EmailTriage.triage_class.is_(None),
                EmailTriage.status == "pending",
                AgentJob.status.in_(["completed", "failed"]),
                # Nur frische Mails -- aeltere 404en ohnehin und erzeugen nur Churn.
                EmailTriage.created_at >= cutoff,
            )
            .order_by(EmailTriage.created_at.desc())
            .limit(limit)
        )
        for triage, job in rows.all():
            meta = dict(job.metadata_json or {})
            # Ohne Message-ID ist ein Re-Run sinnlos (get_email schluege fehl).
            if not meta.get("email_message_id"):
                triage.status = "dismissed"
                dismissed += 1
                continue
            resweep_count = int(meta.get("resweep_count") or 0)
            if resweep_count >= MAX_RESWEEP:
                # Erschoepfte Wiederholungen: endgueltig schliessen, statt jeden
                # Zyklus erneut zu selektieren (kein Dauer-Churn).
                triage.status = "dismissed"
                dismissed += 1
                continue
            new_meta = {
                k: v for k, v in meta.items()
                if k not in ("trace", "tools_used", "self_grade")
            }
            new_meta["resweep_count"] = resweep_count + 1
            new_meta["resweep_of"] = str(job.id)
            new_job = AgentJob(
                user_id=job.user_id,
                task_id=None,
                job_type="email_triage",
                status="queued",
                llm_model=job.llm_model,
                metadata_json=new_meta,
            )
            db.add(new_job)
            await db.flush()
            triage.agent_job_id = new_job.id
            triage.status = "pending"
            requeued += 1
        if requeued or dismissed:
            await db.commit()
            logger.info(
                "Resweep: %d Triage(s) neu eingereiht, %d endgueltig geschlossen",
                requeued, dismissed,
            )
    return requeued


# ── Worker-Loop ──────────────────────────────────────────

async def _worker_loop() -> None:
    """Pollt nach queued Jobs und verarbeitet sie sequentiell."""
    await asyncio.sleep(3)

    agent = await _init_agent()
    if agent is None:
        logger.error("Hermes-Worker kann nicht starten (Runtime nicht verfügbar)")
        return

    logger.info("Hermes-Worker gestartet -- pollt alle %ds nach queued Jobs", POLL_INTERVAL)

    last_reap = time.monotonic()
    last_draft_cleanup = time.monotonic()
    last_resweep = time.monotonic()
    # Style-Store gleich nach dem Start einmal synchronisieren (Initial-Backfill),
    # danach im konfigurierten Intervall.
    style_interval = max(3600, get_settings().style_store_sync_interval_seconds)
    last_style_sync = time.monotonic() - style_interval - 1

    while True:
        try:
            if time.monotonic() - last_reap >= REAP_INTERVAL:
                await _reap_stale_jobs()
                last_reap = time.monotonic()

            if (
                get_settings().style_store_enabled
                and time.monotonic() - last_style_sync >= style_interval
            ):
                try:
                    from app.services.style_store import sync_style_store

                    await sync_style_store()
                except Exception:
                    logger.exception("Style-Store-Sync fehlgeschlagen")
                last_style_sync = time.monotonic()

            if time.monotonic() - last_draft_cleanup >= DRAFT_CLEANUP_INTERVAL:
                try:
                    await _cleanup_orphaned_drafts()
                except Exception:
                    logger.exception("Draft-Cleanup fehlgeschlagen")
                last_draft_cleanup = time.monotonic()

            if time.monotonic() - last_resweep >= RESWEEP_INTERVAL:
                try:
                    await _resweep_unclassified_triages()
                except Exception:
                    logger.exception("Resweep fehlgeschlagen")
                last_resweep = time.monotonic()

            async with async_session() as db:
                result = await db.execute(
                    select(AgentJob)
                    .where(AgentJob.status == "queued")
                    .order_by(AgentJob.created_at)
                    .limit(1)
                )
                job = result.scalar_one_or_none()

            if job is not None:
                meta = job.metadata_json or {}
                if job.job_type == "email_triage":
                    prompt = await _build_triage_prompt(job)
                elif job.job_type == "chat_triage":
                    prompt = await _build_chat_triage_prompt(job)
                elif job.job_type in ("daily_briefing", "weekly_briefing", "monthly_briefing"):
                    prompt = await _build_briefing_prompt(job)
                elif job.job_type == "meeting_summary":
                    prompt = await _build_meeting_summary_prompt(job)
                else:
                    prompt = await _build_generic_prompt(job)

                job_agent = agent
                # Tool-Scoping (Paket C): Triage-Jobs laufen auf dem reduzierten
                # Agenten (Core ohne web + graph + taskpilot). Fallback: voller Agent.
                if (
                    get_settings().triage_tool_scoping
                    and job.job_type in ("email_triage", "chat_triage")
                ):
                    job_agent = await _init_triage_agent() or agent
                # Cloud-LLM-Override: eigener ephemerer Agent (Default-Deny) via
                # LiteLLM-Proxy; lokale Overrides laufen via request_overrides.
                override_model = (meta.get("llm_override") or "").strip()
                if override_model and not _is_local_model(override_model):
                    try:
                        job_agent = await asyncio.to_thread(
                            _build_cloud_job_agent, override_model
                        )
                        logger.info(
                            "Job %s: Cloud-LLM-Override -> %s (Default-Deny-Toolset)",
                            job.id, override_model,
                        )
                    except Exception:
                        logger.exception(
                            "Cloud-Override-Agent (%s) fehlgeschlagen -- lokaler Fallback",
                            override_model,
                        )
                        job_agent = agent

                await _process_job(job_agent, job.id, job.job_type or "generic", prompt, meta)
            else:
                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hermes-Worker: unerwarteter Fehler")
            await asyncio.sleep(POLL_INTERVAL)


async def start_hermes_worker() -> None:
    """Startet den Hermes-Worker als Hintergrund-Task."""
    global _worker_task
    _worker_task = asyncio.create_task(_worker_loop())
    logger.info("Hermes-Worker: Hintergrund-Task gestartet")


async def stop_hermes_worker() -> None:
    """Stoppt den Hermes-Worker und gibt MCP-Verbindungen frei."""
    global _worker_task, _agent, _triage_agent
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    _worker_task = None
    _agent = None
    _triage_agent = None
    try:
        from tools.mcp_tool import shutdown_mcp_servers

        await asyncio.to_thread(shutdown_mcp_servers)
    except Exception:
        logger.warning("MCP-Server-Shutdown fehlgeschlagen (ignoriert)")
    logger.info("Hermes-Worker gestoppt")
