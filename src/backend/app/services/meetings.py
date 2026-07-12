"""Meeting-Nachbereitung: Transkript-Poller, VTT-Parser und Anonymisierung.

Pipeline:
1. Poller (alle 15 Min): beendete Teams-Meetings der letzten 24 h erkennen,
   Transkript (VTT) im Original abholen und speichern.
2. Pro neuem Transkript ein ``AgentJob(job_type='meeting_summary')`` — der
   Hermes-Worker erstellt daraus ein strukturiertes Protokoll inkl.
   Action-Item-Vorschlägen (needs_review-Tasks, HITL).
3. Optionale Anonymisierung (zweistufig): Regex-Maskierung (E-Mail/Telefon)
   plus lokale LLM-Pseudonymisierung mit konsistenter Mapping-Tabelle. Die
   Mapping-Tabelle bleibt ausschliesslich lokal (wird nie exportiert).
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import AgentJob, MeetingTranscript
from app.core.principal import system_principal_id

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.meetings")

POLL_INTERVAL_SECONDS = 900          # 15 Minuten
MEETING_LOOKBACK_HOURS = 24          # Fenster (Transkript-Erstellung) für "kürzlich"


def _get_graph_client() -> GraphClient | None:
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return None
    return GraphClient(GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    ))


# ── VTT-Parser ───────────────────────────────────────────────────────────────

_VTT_TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}:\d{2}[.,]\d{3}\s+-->\s+")
_VTT_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)</v>", re.DOTALL)
_VTT_TAG = re.compile(r"<[^>]+>")


def parse_vtt(raw: str) -> str:
    """Wandelt ein WebVTT-Transkript in sprecher-attribuierten Klartext um.

    Aufeinanderfolgende Captions desselben Sprechers werden zu einem Absatz
    zusammengeführt («Caption-Merge») — das reduziert die Länge deutlich und
    macht den Text für Mensch und LLM lesbar.
    """
    entries: list[tuple[str, str]] = []  # (speaker, text)
    for block in re.split(r"\n\s*\n", raw or ""):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if not lines or lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        # Cue-Identifier und Timestamp-Zeilen überspringen
        payload_lines = [ln for ln in lines if not _VTT_TIMESTAMP.match(ln)]
        # reiner Identifier (z. B. UUID) ohne Text
        payload = " ".join(payload_lines)
        if not payload:
            continue
        m = _VTT_VOICE.search(payload)
        if m:
            speaker = m.group(1).strip()
            text = _VTT_TAG.sub("", m.group(2)).strip()
        else:
            # Identifier-Zeilen ohne <v>-Tag: nur behalten, wenn echter Text
            text = _VTT_TAG.sub("", payload).strip()
            if not text or re.fullmatch(r"[0-9a-fA-F\-/]+", text):
                continue
            speaker = ""
        if not text:
            continue
        if entries and entries[-1][0] == speaker:
            entries[-1] = (speaker, entries[-1][1] + " " + text)
        else:
            entries.append((speaker, text))

    lines_out = []
    for speaker, text in entries:
        lines_out.append(f"**{speaker}:** {text}" if speaker else text)
    return "\n\n".join(lines_out)


def chunk_transcript(text: str, max_chars: int = 12000) -> list[str]:
    """Teilt einen langen Transkript-Text an Sprecherwechsel-Grenzen in Chunks."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in text.split("\n\n"):
        if current and current_len + len(para) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ── Poller ───────────────────────────────────────────────────────────────────

def _parse_graph_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _organizer_from_transcript(tr: dict) -> str | None:
    """Extrahiert den Organisator-Anzeigenamen aus einem getAllTranscripts-Eintrag."""
    org = ((tr.get("meetingOrganizer") or {}).get("user") or {})
    return org.get("displayName") or None


async def poll_meeting_transcripts() -> int:
    """Ein Poller-Durchlauf: neue Meeting-Transkripte abholen.

    Nutzt ``getAllTranscripts`` (nur vom konfigurierten User organisierte
    Meetings, GUID-basiert). Gibt die Anzahl neu gespeicherter Transkripte
    zurück. Best-effort: Fehler einzelner Transkripte (z. B. 403 vor dem
    Admin-Setup) blockieren den Rest nicht.
    """
    client = _get_graph_client()
    if client is None:
        return 0

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=MEETING_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        transcripts = await client.list_recent_transcripts(start, end)
    except Exception as e:  # noqa: BLE001 - inkl. 403 vor Admin-Freigabe
        logger.warning("Meeting-Poller: Transkripte nicht abrufbar: %s", e)
        return 0

    async with async_session() as db:
        principal_id = await system_principal_id(db)
        known = {
            row[0]
            for row in (
                await db.execute(
                    select(MeetingTranscript.transcript_id).where(
                        MeetingTranscript.user_id == principal_id
                    )
                )
            ).all()
        }

    stored = 0
    meeting_cache: dict[str, dict] = {}
    for tr in transcripts:
        transcript_id = tr.get("id")
        meeting_id = tr.get("meetingId")
        content_url = tr.get("transcriptContentUrl")
        if not transcript_id or not meeting_id or transcript_id in known or not content_url:
            continue

        try:
            raw_vtt = await client.get_transcript_content(content_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("Meeting-Poller: VTT %s nicht ladbar: %s", transcript_id, e)
            continue

        # Meeting-Metadaten (Betreff/Zeiten) best-effort nachladen und cachen
        meeting = meeting_cache.get(meeting_id)
        if meeting is None:
            try:
                meeting = await client.get_online_meeting(meeting_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("Meeting-Poller: Meeting-Details %s nicht ladbar: %s", meeting_id, e)
                meeting = {}
            meeting_cache[meeting_id] = meeting

        parsed = parse_vtt(raw_vtt)
        subject = meeting.get("subject") or "(ohne Betreff)"
        organizer = (
            ((meeting.get("participants") or {}).get("organizer") or {}).get("upn")
            or _organizer_from_transcript(tr)
        )
        started_at = _parse_graph_dt(meeting.get("startDateTime"))
        ended_at = _parse_graph_dt(meeting.get("endDateTime")) or _parse_graph_dt(tr.get("createdDateTime"))

        async with async_session() as db:
            record = MeetingTranscript(
                user_id=principal_id,
                meeting_id=meeting_id,
                transcript_id=transcript_id,
                subject=subject,
                organizer=organizer,
                started_at=started_at,
                ended_at=ended_at,
                raw_vtt=raw_vtt,
                transcript_text=parsed,
                status="processing",
            )
            db.add(record)
            await db.flush()
            job = AgentJob(
                user_id=principal_id,
                job_type="meeting_summary",
                status="queued",
                metadata_json={
                    "meeting_transcript_id": str(record.id),
                    "subject": subject,
                    "description": f"Meeting-Protokoll: {subject}",
                    "autonomy_level": "L2",
                },
            )
            db.add(job)
            await db.flush()
            record.agent_job_id = job.id
            await db.commit()

        known.add(transcript_id)
        stored += 1
        logger.info("Transkript gespeichert (%s) + AgentJob meeting_summary erzeugt", subject)

    return stored


_poller_task: asyncio.Task | None = None


async def meeting_poller_loop() -> None:
    logger.info("Meeting-Poller gestartet (Intervall: %ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            count = await poll_meeting_transcripts()
            if count:
                logger.info("Meeting-Poller: %d neue(s) Transkript(e)", count)
        except Exception:
            logger.exception("Meeting-Poller: unerwarteter Fehler")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def start_meeting_poller() -> None:
    global _poller_task
    _poller_task = asyncio.create_task(meeting_poller_loop())


async def stop_meeting_poller() -> None:
    global _poller_task
    if _poller_task and not _poller_task.done():
        _poller_task.cancel()
        try:
            await _poller_task
        except asyncio.CancelledError:
            pass
    _poller_task = None


# ── Map-Reduce für lange Transkripte ─────────────────────────────────────────

# Ab dieser Länge wird das Transkript nicht mehr direkt in den Protokoll-Prompt
# gelegt, sondern vorab per Chunk-Zusammenfassung verdichtet (Kontextbudget
# lokaler Modelle).
DIRECT_PROMPT_MAX_CHARS = 24000


async def summarize_transcript_chunks(text: str) -> str:
    """Map-Phase: fasst Transkript-Chunks einzeln zusammen (direkte Ollama-Calls).

    Die Chunk-Zusammenfassungen werden anschliessend als verdichteter Kontext in
    den Protokoll-Prompt injiziert (Reduce macht der Agent). Best-effort: schlägt
    ein Chunk fehl, wird sein Rohanfang gekürzt übernommen.
    """
    cfg = get_settings()
    model = cfg.triage_model.removeprefix("ollama/")
    url = f"{cfg.ollama_base_url.rstrip('/')}/v1/chat/completions"

    chunks = chunk_transcript(text, max_chars=12000)
    summaries: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Fasse diesen Abschnitt eines Meeting-Transkripts strukturiert "
                        "zusammen (Deutsch, Schweizer Rechtschreibung). Erhalte: Wer hat "
                        "was gesagt/entschieden, konkrete Zahlen, Termine, Zusagen und "
                        "offene Punkte. Keine Floskeln, keine Interpretation."
                    ),
                },
                {"role": "user", "content": chunk},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(url, json=payload, headers={"Authorization": "Bearer ollama"})
                resp.raise_for_status()
                data = resp.json()
            summary = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
            summaries.append(f"### Abschnitt {i}/{len(chunks)}\n\n{summary.strip()}")
        except Exception as e:  # noqa: BLE001
            logger.warning("Chunk-Zusammenfassung %d/%d fehlgeschlagen: %s", i, len(chunks), e)
            summaries.append(f"### Abschnitt {i}/{len(chunks)} (Rohauszug)\n\n{chunk[:3000]}")

    return "\n\n".join(summaries)


# ── Anonymisierung ───────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Schweizer/internationale Telefonnummern (mind. 9 Ziffern, mit Trennzeichen)
_PHONE_RE = re.compile(r"(?:\+|00)\d[\d\s/.\-()]{7,}\d|\b0\d{2}[\s/.\-]?\d{3}[\s/.\-]?\d{2}[\s/.\-]?\d{2}\b")


def mask_deterministic(text: str, mapping: dict[str, str]) -> str:
    """Stufe 1: E-Mail-Adressen und Telefonnummern per Regex maskieren."""
    counters = {"email": 0, "tel": 0}
    # Bestehende Zähler aus dem Mapping fortführen (idempotent bei Re-Runs)
    for placeholder in mapping.values():
        m = re.fullmatch(r"\[(E-Mail|Telefon) (\d+)\]", placeholder)
        if m:
            key = "email" if m.group(1) == "E-Mail" else "tel"
            counters[key] = max(counters[key], int(m.group(2)))

    def _sub(kind: str, label: str, match: re.Match) -> str:
        value = match.group(0)
        if value not in mapping:
            counters[kind] += 1
            mapping[value] = f"[{label} {counters[kind]}]"
        return mapping[value]

    text = _EMAIL_RE.sub(lambda m: _sub("email", "E-Mail", m), text)
    text = _PHONE_RE.sub(lambda m: _sub("tel", "Telefon", m), text)
    return text


async def _extract_names_llm(text: str) -> list[dict]:
    """Stufe 2a: Personen-/Firmennamen per lokalem LLM extrahieren (JSON).

    Direkter Ollama-Call ohne Agent/Tools -- schema-nah, tolerant geparst.
    Gibt eine Liste ``[{"name": ..., "type": "person"|"org"}]`` zurück.
    """
    cfg = get_settings()
    model = cfg.triage_model.removeprefix("ollama/")
    url = f"{cfg.ollama_base_url.rstrip('/')}/v1/chat/completions"

    names: dict[str, str] = {}
    for chunk in chunk_transcript(text, max_chars=10000):
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Du extrahierst Eigennamen für eine Anonymisierung. Gib "
                        'AUSSCHLIESSLICH JSON zurück: {"names": [{"name": "...", '
                        '"type": "person|org"}]}. Erfasse Personennamen (Vor-/Nachname, '
                        "auch einzeln) und Firmen-/Kundennamen. KEINE Produktnamen, "
                        "keine Orte, keine generischen Begriffe."
                    ),
                },
                {"role": "user", "content": chunk},
            ],
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(url, json=payload, headers={"Authorization": "Bearer ollama"})
                resp.raise_for_status()
                data = resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
            import json as json_mod

            parsed = json_mod.loads(content)
            for item in parsed.get("names", []):
                name = (item.get("name") or "").strip()
                ntype = "org" if item.get("type") == "org" else "person"
                if len(name) >= 2:
                    names.setdefault(name, ntype)
        except Exception as e:  # noqa: BLE001 - best-effort pro Chunk
            logger.warning("Namens-Extraktion (Chunk) fehlgeschlagen: %s", e)

    return [{"name": n, "type": t} for n, t in names.items()]


def _letter_label(index: int) -> str:
    """0 -> A, 1 -> B, …, 26 -> AA (Excel-Stil)."""
    label = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        label = chr(65 + rem) + label
    return label


def apply_pseudonyms(text: str, names: list[dict], mapping: dict[str, str]) -> str:
    """Stufe 2b: Namen deterministisch und konsistent durch Platzhalter ersetzen.

    Längste Namen zuerst (verhindert Teil-Ersetzungen), Ersetzung wortgrenzen-
    basiert und case-sensitiv. Das Mapping wächst konsistent über Re-Runs.
    """
    person_count = sum(1 for v in mapping.values() if v.startswith("Person "))
    org_count = sum(1 for v in mapping.values() if v.startswith("Firma "))

    for item in sorted(names, key=lambda x: -len(x["name"])):
        name = item["name"]
        if name in mapping:
            continue
        if item["type"] == "org":
            mapping[name] = f"Firma {_letter_label(org_count)}"
            org_count += 1
        else:
            mapping[name] = f"Person {_letter_label(person_count)}"
            person_count += 1

    for name in sorted(mapping.keys(), key=len, reverse=True):
        replacement = mapping[name]
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
        text = pattern.sub(replacement, text)
    return text


async def anonymize_meeting(record: MeetingTranscript) -> dict:
    """Erzeugt die anonymisierten Fassungen von Transkript und Protokoll.

    Returns dict mit ``anonymized_text``, ``anonymized_protocol_md`` und
    ``anonymization_map`` (Aufrufer persistiert). Zweistufig: Regex-Maskierung
    (deterministisch) + LLM-Namens-Extraktion mit konsistenter Ersetzung.
    """
    mapping: dict[str, str] = dict(record.anonymization_map or {})
    source_text = record.transcript_text or ""
    source_protocol = record.protocol_md or ""

    combined = source_text + "\n\n" + source_protocol
    names = await _extract_names_llm(combined)

    anon_text = apply_pseudonyms(mask_deterministic(source_text, mapping), names, mapping)
    anon_protocol = apply_pseudonyms(mask_deterministic(source_protocol, mapping), names, mapping)

    return {
        "anonymized_text": anon_text,
        "anonymized_protocol_md": anon_protocol,
        "anonymization_map": mapping,
    }
