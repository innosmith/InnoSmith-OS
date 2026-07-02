"""Follow-up-Erkennung: unbeantwortete gesendete E-Mails erkennen (deterministisch).

Täglicher Check (via Briefing-Scheduler-Loop angestossen): gesendete E-Mails,
auf die nach X Arbeitstagen (Default 5) keine Antwort eines anderen Absenders
in derselben Konversation einging, erzeugen einen ``needs_review``-Task-Vorschlag
«Nachfassen bei …» mit gesetzter ``email_conversation_id`` (damit das
EmailThreadPanel im Cockpit den ganzen Thread zeigt).

Kein LLM — reiner ``conversationId``-Abgleich. Dedupe über die Tabelle
``followup_suggestions`` (überlebt das Verwerfen des Task-Vorschlags).
"""

import json as json_mod
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models import FollowupSuggestion, Task, User

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.followup")

_TZ = ZoneInfo("Europe/Zurich")

DEFAULT_WAIT_WORKDAYS = 5
MAX_AGE_DAYS = 30          # ältere Sent-Mails nicht mehr aufgreifen
MAX_SENT_SCAN = 150        # Sicherheitsdeckel pro Lauf
MAX_NEW_SUGGESTIONS = 10   # pro Lauf höchstens N neue Vorschläge

# Adressmuster, bei denen Nachfassen sinnlos ist
_NOREPLY_RE = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|notification", re.IGNORECASE)


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


def _workdays_since(start: date, end: date) -> int:
    """Anzahl Arbeitstage (Mo–Fr) zwischen start (exkl.) und end (inkl.)."""
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_recipient(msg: dict) -> str:
    recips = msg.get("toRecipients") or []
    if not recips:
        return ""
    addr = ((recips[0].get("emailAddress") or {}).get("address")) or ""
    return addr.strip().lower()


def _has_reply(messages: list[dict], own_email: str, sent_at: datetime) -> bool:
    """True, wenn NACH dem Sendezeitpunkt eine Nachricht eines anderen Absenders kam."""
    for m in messages:
        sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        received = _parse_dt(m.get("receivedDateTime"))
        if not sender or sender == own_email or received is None:
            continue
        if received > sent_at:
            return True
    return False


_GATE_SYSTEM_PROMPT = (
    "Du beurteilst, ob bei einer von MIR (Anthony) gesendeten E-Mail ein Nachfassen "
    "sinnvoll ist, weil noch keine Antwort kam.\n\n"
    "Ein Nachfassen ist NUR sinnvoll, wenn die E-Mail eine OFFENE Frage, eine Bitte, "
    "ein Angebot oder einen zu treffenden Entscheid enthält, die eine ANTWORT oder "
    "HANDLUNG des Empfängers erwartet.\n\n"
    "NICHT sinnvoll (dann false) bei:\n"
    "- reinen Informationen/FYI, Weiterleitungen ohne Frage\n"
    "- Dankes-, Bestätigungs- oder Abschluss-Nachrichten ('passt so', 'erledigt', 'danke')\n"
    "- Termin-Zusagen/-Absagen und automatischen Kalendermeldungen "
    "('Angenommen:', 'Abgelehnt:', 'Abgesagt:', 'Aktualisiert:')\n"
    "- Newslettern, automatischen Status- oder Report-Mails\n"
    "- meinen eigenen Antworten, die eine Frage des Gegenübers beantworten, "
    "ohne selbst einen neuen offenen Punkt zu öffnen\n\n"
    "Beispiele:\n"
    "- Betreff 'Angebot Projektphase 2' / Text 'Bitte gib mir bis Ende Woche Bescheid, "
    "ob das passt.' -> true\n"
    "- Betreff 'Angenommen: Weekly' -> false\n"
    "- Betreff 'AW: Deine Frage zur Rechnung' / Text 'Ja, der Betrag stimmt so.' -> false\n\n"
    "Antworte AUSSCHLIESSLICH mit JSON: "
    '{"erwartet_antwort": true|false, "grund": "kurze Begründung"}. /no_think'
)


async def _expects_reply_llm(subject: str, body_preview: str, recipient: str) -> tuple[bool | None, str]:
    """Lokaler LLM-Gate: Erwartet die gesendete Mail eine Antwort (offene Frage/Bitte)?

    Direkter Ollama-Call (Muster wie ``meetings._extract_names_llm``). Rückgabe:
    - ``(True, grund)``  -> offene Frage/Bitte, Nachfassen sinnvoll
    - ``(False, grund)`` -> bewusst kein Nachfassen (dauerhaft als 'skipped' merken)
    - ``(None, "unklar")`` -> transienter Fehler / LLM nicht erreichbar (nicht
      persistieren, nächster Lauf versucht erneut)
    """
    cfg = get_settings()
    model = cfg.triage_model.removeprefix("ollama/")
    url = f"{cfg.ollama_base_url.rstrip('/')}/v1/chat/completions"

    user_content = (
        f"Empfänger: {recipient}\n"
        f"Betreff: {subject or '(ohne Betreff)'}\n"
        f"Auszug: {(body_preview or '').strip()[:800]}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _GATE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers={"Authorization": "Bearer ollama"})
            resp.raise_for_status()
            data = resp.json()
        content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        parsed = json_mod.loads(content)
        expects = bool(parsed.get("erwartet_antwort"))
        reason = str(parsed.get("grund") or "").strip()[:300]
        return expects, reason
    except Exception as e:  # noqa: BLE001 - transienter Fehler -> nicht persistieren
        logger.warning("Follow-up-Gate (LLM) fehlgeschlagen: %s", e)
        return None, "unklar"


async def check_followups_due() -> int:
    """Ein Follow-up-Lauf. Gibt die Anzahl neu erstellter Vorschläge zurück."""
    cfg = get_settings()
    client = _get_graph_client()
    if client is None:
        return 0
    own_email = cfg.graph_user_email.strip().lower()

    async with async_session() as db:
        owner = (
            await db.execute(select(User).where(User.role == "owner").limit(1))
        ).scalar_one_or_none()
        settings = dict((owner.settings if owner else None) or {})
    if settings.get("followup_enabled") is False:
        return 0
    try:
        wait_days = int(settings.get("followup_wait_days") or DEFAULT_WAIT_WORKDAYS)
    except (TypeError, ValueError):
        wait_days = DEFAULT_WAIT_WORKDAYS

    try:
        sent = await client.list_sent_messages(top=MAX_SENT_SCAN)
    except Exception as e:  # noqa: BLE001
        logger.warning("Follow-up-Check: Sent-Items nicht abrufbar: %s", e)
        return 0

    today = datetime.now(_TZ).date()
    min_sent = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    # Pro Konversation nur die JÜNGSTE gesendete Mail betrachten
    latest_by_conv: dict[str, dict] = {}
    for msg in sent:
        conv = msg.get("conversationId")
        sent_at = _parse_dt(msg.get("sentDateTime"))
        if not conv or sent_at is None or sent_at < min_sent:
            continue
        if conv not in latest_by_conv:
            latest_by_conv[conv] = msg  # Liste ist bereits sentDateTime desc

    async with async_session() as db:
        known_convs = {
            row[0]
            for row in (await db.execute(select(FollowupSuggestion.conversation_id))).all()
        }
        task_convs = {
            row[0]
            for row in (
                await db.execute(
                    select(Task.email_conversation_id)
                    .where(Task.email_conversation_id.isnot(None))
                )
            ).all()
        }

    created = 0
    for conv, msg in latest_by_conv.items():
        if created >= MAX_NEW_SUGGESTIONS:
            break
        if conv in known_convs or conv in task_convs:
            continue

        sent_at = _parse_dt(msg.get("sentDateTime"))
        if _workdays_since(sent_at.astimezone(_TZ).date(), today) < wait_days:
            continue

        recipient = _first_recipient(msg)
        if not recipient or recipient == own_email or _NOREPLY_RE.search(recipient):
            continue
        subject = (msg.get("subject") or "").strip()

        # LLM-Gate: Enthält die gesendete Mail eine offene Frage/Bitte, die eine
        # Antwort erwartet? Alleiniger inhaltlicher Entscheider (keine fixen Regeln).
        decision, gate_reason = await _expects_reply_llm(
            subject, msg.get("bodyPreview") or "", recipient
        )
        if decision is None:
            # Transienter Fehler / LLM nicht erreichbar -> nicht persistieren,
            # nächster Lauf versucht dieselbe Konversation erneut.
            continue
        if decision is False:
            # Bewusst kein Nachfassen -> dauerhaft als 'skipped' merken.
            async with async_session() as db:
                db.add(FollowupSuggestion(
                    conversation_id=conv,
                    subject=subject,
                    recipient=recipient,
                    sent_at=sent_at,
                    status="skipped",
                ))
                await db.commit()
            known_convs.add(conv)
            logger.info("Follow-up übersprungen (%s): %s", gate_reason or "kein offener Punkt", subject[:80])
            continue

        try:
            thread = await client.get_conversation_messages(conv, top=10)
        except Exception as e:  # noqa: BLE001
            logger.warning("Follow-up-Check: Thread %s nicht ladbar: %s", conv, e)
            continue
        if _has_reply(thread, own_email, sent_at):
            async with async_session() as db:
                db.add(FollowupSuggestion(
                    conversation_id=conv,
                    subject=subject,
                    recipient=recipient,
                    sent_at=sent_at,
                    status="answered",
                ))
                await db.commit()
            known_convs.add(conv)
            continue

        # Kein Reply nach Wartefrist -> Task-Vorschlag (needs_review, HITL)
        from app.services.hermes_worker import _create_review_task
        from app.services.notification import create_notification

        sent_str = sent_at.astimezone(_TZ).strftime("%d.%m.%Y")
        title = f"Nachfassen bei {recipient}: {subject or '(ohne Betreff)'}"
        description = (
            f"Am {sent_str} gesendete E-Mail an {recipient} ist seit über "
            f"{wait_days} Arbeitstagen unbeantwortet.\n\n"
            + (f"**Offener Punkt:** {gate_reason}\n\n" if gate_reason else "")
            + f"---\n**Quelle:** Follow-up-Erkennung (Betreff: {subject or '—'})"
        )

        async with async_session() as db:
            task = await _create_review_task(
                db,
                None,
                title=title,
                description=description,
                suggested_project=None,
                deadline=None,
                email_conversation_id=conv,
            )
            db.add(FollowupSuggestion(
                conversation_id=conv,
                task_id=task.id if task else None,
                subject=subject,
                recipient=recipient,
                sent_at=sent_at,
                status="suggested",
            ))
            if task is not None and owner is not None:
                await create_notification(
                    db,
                    user_id=owner.id,
                    type="follow_up_due",
                    title=f"Follow-up fällig: {recipient}",
                    body=f"Keine Antwort auf «{subject or '(ohne Betreff)'}» seit {wait_days} Arbeitstagen",
                    link="/",
                    source_type="task",
                    source_id=task.id,
                )
            await db.commit()
        known_convs.add(conv)
        if task is not None:
            created += 1
            logger.info("Follow-up-Vorschlag erstellt: %s", title[:100])

    return created


_last_run_date: date | None = None
RUN_HOUR = 7  # frühester täglicher Lauf (lokale Zeit)


async def maybe_run_daily_followup_check() -> int:
    """Vom Scheduler-Loop aufgerufen: führt den Check höchstens 1x pro Tag aus."""
    global _last_run_date
    now = datetime.now(_TZ)
    if now.hour < RUN_HOUR:
        return 0
    if _last_run_date == now.date():
        return 0
    _last_run_date = now.date()
    try:
        return await check_followups_due()
    except Exception:
        logger.exception("Follow-up-Check fehlgeschlagen")
        return 0
