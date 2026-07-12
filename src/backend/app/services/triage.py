"""Triage-Service: Pollt automatisch neue E-Mails (120s) und Teams-Chats (300s).

Läuft als Hintergrund-Tasks beim Backend-Start. Jede neue E-Mail wird als
AgentJob(job_type="email_triage") in die Queue geschrieben, jede neue
Chat-Nachricht als AgentJob(job_type="chat_triage"). Der Hermes-Worker pollt
die Queue (alle 10s) und verarbeitet die Jobs in-process.

E-Mail-Triage:
  1. E-Mail lesen → LLM-Klassifikation → Aktion (Draft / Task / FYI)

Chat-Triage:
  1. Chat-Nachricht lesen → LLM-Klassifikation → Aktion (Task / FYI)
  2. Meeting-Transkript-Benachrichtigungen erkennen → AgentJob(meeting_summary)
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from dateutil.parser import isoparse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import AgentJob, ChatTriage, EmailTriage
from app.core.principal import get_owner

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.triage")

# Coverage-Robustheit: Statt eines fixen "nur neueste N"-Fensters wird der
# Posteingang seitenweise durchlaufen (Pagination via ``$skip``), bis eine Mail
# aelter als der Cutoff auftaucht (Reihenfolge ist ``receivedDateTime desc``) oder
# das Seitenlimit erreicht ist. So gehen bei Bursts (z. B. 100+ Fehler-Mails in
# kurzer Zeit) keine Mails mehr verloren, die frueher unter Position 20 rutschten.
INBOX_PAGE_SIZE = 50
MAX_INBOX_PAGES = 20  # Sicherheitsdeckel: bis zu 1000 Mails pro Zyklus scannen.
MAX_NEW_EMAILS_PER_CYCLE = 200  # Rest wird im naechsten Zyklus nachgezogen.
# Kaltstart-Fenster: Mails aelter als dieser Wert werden nicht mehr aufgegriffen
# (verhindert, dass nach laengerer Downtime der ganze Posteingang neu triagiert
# wird). Grosszuegig genug, um kurze Ausfaelle zu ueberbruecken.
COLD_START_CUTOFF_HOURS = 72


async def _is_triage_enabled_in_db() -> bool:
    """Prüft triage_enabled im Owner-Settings-JSONB (Stufe 2: Runtime-Toggle)."""
    try:
        async with async_session() as db:
            owner = await get_owner(db)
            if owner is None:
                return True
            return (owner.settings or {}).get("triage_enabled", True)
    except Exception:
        logger.warning("triage_enabled konnte nicht aus DB gelesen werden, Default=True")
        return True


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


async def _get_known_message_ids(db: AsyncSession) -> set[str]:
    result = await db.execute(select(EmailTriage.message_id))
    return {row[0] for row in result.all()}


async def _fetch_new_inbox_emails(
    client: GraphClient, known_ids: set[str], cutoff: datetime
) -> list[dict]:
    """Blaettert den Posteingang seitenweise durch und sammelt neue Mails.

    Robuster Ersatz fuer das fruehere fixe ``top=20``-Fenster: Da der Posteingang
    nach ``receivedDateTime desc`` sortiert ist, wird solange paginiert, bis eine
    Mail aelter als ``cutoff`` auftaucht (danach sind alle aelter -> Stopp), eine
    Teilseite kommt (Ende des Ordners) oder das Seiten-/Mengenlimit greift. Damit
    werden auch Bursts von >20 Mails pro Zyklus vollstaendig erfasst.
    """
    new_emails: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(MAX_INBOX_PAGES):
        data = await client.list_emails(
            folder="inbox", top=INBOX_PAGE_SIZE, skip=page * INBOX_PAGE_SIZE
        )
        msgs = data.get("value", [])
        if not msgs:
            break
        reached_old = False
        for msg in msgs:
            mid = msg.get("id")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            received = msg.get("receivedDateTime")
            if received:
                try:
                    if isoparse(received) < cutoff:
                        reached_old = True
                        continue
                except (ValueError, TypeError):
                    pass
            if mid in known_ids:
                continue
            new_emails.append(msg)
        # Aeltere-als-Cutoff erreicht oder letzte (Teil-)Seite -> fertig.
        if reached_old or len(msgs) < INBOX_PAGE_SIZE:
            break
        if len(new_emails) >= MAX_NEW_EMAILS_PER_CYCLE:
            logger.info(
                "Triage: Mengenlimit (%d) erreicht -- Rest wird im naechsten Zyklus nachgezogen",
                MAX_NEW_EMAILS_PER_CYCLE,
            )
            break
    return new_emails


def _parse_received_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return isoparse(raw)
    except (ValueError, TypeError):
        return None


OWNER_EMAIL_ADDRESSES = {
    "anthony@innosmith.ch",
    "anthony@gerbersmith.ch",
    "anthony.thomas.smith@gmail.com",
    "anthony.smith@bfh.ch",
}


# Graph ``meetingMessageType``-Werte, die reine ANTWORTEN auf eine Einladung sind
# (Zusage/Absage/mit-Vorbehalt). Das sind reine Infos ohne Handlungsbedarf -> sie
# werden deterministisch (ohne LLM) als ``fyi`` behandelt + nach ``Kalender``
# verschoben. NICHT enthalten: ``meetingRequest`` (echte Einladung, ggf. Kalender-
# pruefung/Antwort noetig) und ``meetingCancelled`` (kann zeitkritisch sein) -- die
# laufen weiter durch den normalen LLM-Pfad.
MEETING_RESPONSE_TYPES = {
    "meetingAccepted",
    "meetingTentativelyAccepted",
    "meetingDeclined",
}


def is_meeting_response(email_data: dict) -> bool:
    """True, wenn die E-Mail eine reine Meeting-Antwort (Zusage/Absage) ist.

    Liest das strukturierte Graph-Feld ``meetingMessageType`` -- das ist KEINE
    Fuzzy-Entscheidung, sondern ein deterministisches Signal von Exchange. Damit
    wird der haeufigste Fehlgriff (Terminzusage -> Aufgabe) an der Wurzel verhindert,
    ohne das LLM zu bemuehen.
    """
    return (email_data.get("meetingMessageType") or "") in MEETING_RESPONSE_TYPES


async def _handle_meeting_response(db: AsyncSession, client: GraphClient, email_data: dict) -> None:
    """Deterministische Behandlung einer Meeting-Antwort: fyi + Kategorie + Move.

    Erstellt einen ``EmailTriage``-Audit-Record (``triage_class='fyi'``,
    ``status='acted'``) und verschiebt die Mail best-effort nach ``Kalender``.
    KEIN AgentJob / kein LLM -- das ist eine Regel, keine Ermessensfrage.
    """
    from_info = email_data.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    subject = email_data.get("subject", "")
    mmt = email_data.get("meetingMessageType") or ""
    message_id = email_data["id"]

    triage_record = EmailTriage(
        message_id=message_id,
        subject=subject,
        from_address=from_addr,
        from_name=from_info.get("name"),
        received_at=_parse_received_at(email_data.get("receivedDateTime")),
        inference_class=email_data.get("inferenceClassification", ""),
        triage_class="fyi",
        reply_expected=False,
        confidence=1.0,
        suggested_action={
            "label": "Kalender",
            "triage_class": "fyi",
            "deterministic_override": "meeting_response",
            "meeting_message_type": mmt,
            "rationale": (
                f"Meeting-Antwort ({mmt}) -- deterministisch als fyi eingeordnet "
                "(Kategorie Kalender + Verschiebung), kein Task, kein LLM."
            ),
        },
        status="acted",
    )
    db.add(triage_record)
    await db.flush()

    # Graph-Aktionen best-effort: Kategorie setzen, dann nach Kalender verschieben.
    # set_categories kippt isRead -> true; fuer reine Infos ist "gelesen + aus der
    # Inbox" gewuenscht (kein Unread-Clutter). Reihenfolge: Kategorie -> Move.
    try:
        await client.set_categories(message_id, ["Kalender"])
    except Exception:  # noqa: BLE001 - Finalisierung darf den Zyklus nie stoppen
        logger.warning("Meeting-Response: Kategorie setzen fehlgeschlagen (mid=%s)", message_id[:30])
    try:
        await client.move_to_folder(message_id, "Kalender")
    except Exception:  # noqa: BLE001
        logger.info("Meeting-Response: Move nach 'Kalender' nicht moeglich (Ordner fehlt?)")

    logger.info(
        "Meeting-Response deterministisch behandelt: %s von %s (%s) -> fyi+Kalender, kein Task",
        subject[:60], from_addr, mmt,
    )


async def _load_active_deterministic_rules(db: AsyncSession) -> list:
    """Lädt aktive deterministische Regeln, sortiert nach Priorität (klein zuerst)."""
    from app.models import LearnedRule

    result = await db.execute(
        select(LearnedRule)
        .where(
            LearnedRule.status == "active",
            LearnedRule.rule_type == "deterministic",
        )
        .order_by(LearnedRule.priority, LearnedRule.created_at)
    )
    return list(result.scalars().all())


async def apply_deterministic_rules(
    db: AsyncSession,
    client: GraphClient,
    email_data: dict,
    rules: list | None = None,
) -> bool:
    """Wendet die erste passende deterministische Regel auf eine E-Mail an.

    Generalisierung der Meeting-Override: prüft die ``match_conditions`` der aktiven
    deterministischen Regeln gegen die E-Mail und führt bei erstem Treffer die
    Aktion aus (EmailTriage-Record + Kategorie + Move, optional Task) -- ohne
    AgentJob/LLM. Gibt ``True`` zurück, wenn eine Regel gegriffen hat. ``rules`` kann
    vorab geladen übergeben werden, um pro Zyklus nur einmal zu laden.
    """
    from app.services.rules import evaluate_conditions

    if rules is None:
        rules = await _load_active_deterministic_rules(db)
    if not rules:
        return False
    for rule in rules:
        conditions = rule.match_conditions if isinstance(rule.match_conditions, list) else []
        if not evaluate_conditions(conditions, email_data):
            continue
        await _execute_deterministic_action(db, client, email_data, rule)
        return True
    return False


async def _execute_deterministic_action(
    db: AsyncSession, client: GraphClient, email_data: dict, rule
) -> None:
    """Führt die Aktion einer deterministischen Regel aus (fyi/task + Kategorie + Move)."""
    from app.models import LearnedRule

    action = rule.action if isinstance(rule.action, dict) else {}
    triage_class = action.get("triage_class") or "fyi"
    category = action.get("category")
    folder = action.get("folder")

    from_info = email_data.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    subject = email_data.get("subject", "")
    message_id = email_data["id"]

    triage_record = EmailTriage(
        message_id=message_id,
        subject=subject,
        from_address=from_addr,
        from_name=from_info.get("name"),
        received_at=_parse_received_at(email_data.get("receivedDateTime")),
        inference_class=email_data.get("inferenceClassification", ""),
        triage_class=triage_class,
        reply_expected=False,
        confidence=1.0,
        suggested_action={
            "label": category or triage_class,
            "triage_class": triage_class,
            "deterministic_override": str(rule.id),
            "rule_text": rule.rule_text,
            "rationale": (
                f"Deterministische Regel angewandt (kein LLM): {rule.rule_text}"
            ),
        },
        status="acted",
    )
    db.add(triage_record)
    await db.flush()

    # Task-Aktion (Fortgeschrittenen-Option): bestehende E-Mail-Task-Logik nutzen.
    if triage_class == "task":
        try:
            from app.services.hermes_worker import _create_email_task

            meta = {
                "email_message_id": message_id,
                "subject": subject,
                "from_address": from_addr,
                "from_name": from_info.get("name", ""),
                "conversation_id": email_data.get("conversationId", ""),
            }
            await _create_email_task(
                db,
                None,
                meta,
                task_title=subject or "Aufgabe aus E-Mail",
                task_description=f"Automatisch erstellt durch deterministische Regel: {rule.rule_text}",
                suggested_project=None,
                deadline=None,
                reply_expected=False,
            )
        except Exception:  # noqa: BLE001 - Task-Fehler darf den Zyklus nie stoppen
            logger.exception("Deterministische Task-Erstellung fehlgeschlagen (Regel %s)", rule.id)

    # Graph-Aktionen best-effort: erst Kategorie, dann Move (analog Meeting-Override).
    if category:
        try:
            await client.set_categories(message_id, [category])
        except Exception:  # noqa: BLE001
            logger.warning("Det. Regel: Kategorie '%s' setzen fehlgeschlagen (mid=%s)", category, message_id[:30])
    if folder:
        try:
            await client.move_to_folder(message_id, folder)
        except Exception:  # noqa: BLE001
            logger.info("Det. Regel: Move nach '%s' nicht möglich (Ordner fehlt?)", folder)

    # Anwendungszähler erhöhen (Anzeige/Vertrauen im Cockpit).
    await db.execute(
        update(LearnedRule)
        .where(LearnedRule.id == rule.id)
        .values(applied_count=LearnedRule.applied_count + 1)
    )

    logger.info(
        "Deterministische Regel angewandt: '%s' -> %s (Regel=%s) für '%s' von %s",
        rule.rule_text[:40], triage_class, rule.id, subject[:50], from_addr,
    )


def _determine_recipient_type(email_data: dict) -> str:
    """Bestimmt ob der Owner im TO, CC oder gar nicht als Empfänger steht."""
    to_addrs = {
        r.get("emailAddress", {}).get("address", "").lower()
        for r in email_data.get("toRecipients", [])
    }
    cc_addrs = {
        r.get("emailAddress", {}).get("address", "").lower()
        for r in email_data.get("ccRecipients", [])
    }
    if OWNER_EMAIL_ADDRESSES & to_addrs:
        return "to"
    if OWNER_EMAIL_ADDRESSES & cc_addrs:
        return "cc"
    return "unknown"


async def _create_triage_job(db: AsyncSession, email_data: dict) -> None:
    """Erstellt einen EmailTriage-Record und einen AgentJob für eine neue E-Mail.

    Keine Vorab-Klassifikation -- der Hermes-Agent uebernimmt alles via LLM.
    """
    from app.services.llm_defaults import get_default_local_model

    from_info = email_data.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    subject = email_data.get("subject", "")
    inference = email_data.get("inferenceClassification", "")
    recipient_type = _determine_recipient_type(email_data)

    triage_record = EmailTriage(
        message_id=email_data["id"],
        subject=subject,
        from_address=from_addr,
        from_name=from_info.get("name"),
        received_at=_parse_received_at(email_data.get("receivedDateTime")),
        inference_class=inference,
        triage_class=None,
        confidence=None,
        suggested_action=None,
        status="pending",
    )
    db.add(triage_record)

    local_model = await get_default_local_model(db)

    agent_job = AgentJob(
        task_id=None,
        job_type="email_triage",
        status="queued",
        llm_model=local_model,
        metadata_json={
            "email_message_id": email_data["id"],
            "subject": subject,
            "from_address": from_addr,
            "from_name": from_info.get("name", ""),
            "inference_classification": inference,
            "body_preview": email_data.get("bodyPreview", "")[:500],
            "categories": email_data.get("categories", []),
            "conversation_id": email_data.get("conversationId", ""),
            "recipient_type": recipient_type,
        },
    )
    db.add(agent_job)
    await db.flush()

    triage_record.agent_job_id = agent_job.id


async def _triage_cycle() -> int:
    """Ein Triage-Zyklus: Neue E-Mails erkennen, AgentJobs für den Hermes-Worker erstellen."""
    client = _get_graph_client()
    if client is None:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=COLD_START_CUTOFF_HOURS)
    processed = 0
    try:
        async with async_session() as db:
            known_ids = await _get_known_message_ids(db)

            new_emails = await _fetch_new_inbox_emails(client, known_ids, cutoff)

            # Deterministische Regeln einmal pro Zyklus laden (klein, kein Per-Mail-Query).
            det_rules = await _load_active_deterministic_rules(db)

            for email_data in new_emails:
                # Deterministische Override-Schicht VOR dem LLM: erst reine Meeting-
                # Antworten (built-in), dann gepflegte deterministische Regeln. Beides
                # ohne AgentJob/LLM (verhindert z. B. "Terminzusage -> Aufgabe").
                if is_meeting_response(email_data):
                    await _handle_meeting_response(db, client, email_data)
                elif await apply_deterministic_rules(db, client, email_data, det_rules):
                    pass
                else:
                    await _create_triage_job(db, email_data)
                processed += 1

            await db.commit()

    except PermissionError as e:
        logger.warning("Graph API Permission-Fehler: %s", e)
    except Exception:
        logger.exception("Triage-Zyklus Fehler")
    finally:
        if client:
            await client.close()

    return processed


async def run_triage_now(top: int = 50) -> int:
    """Manueller Triage-Trigger (für API-Endpoint, optional)."""
    client = _get_graph_client()
    if client is None:
        return 0

    processed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COLD_START_CUTOFF_HOURS)
    try:
        async with async_session() as db:
            known_ids = await _get_known_message_ids(db)

            new_emails = await _fetch_new_inbox_emails(client, known_ids, cutoff)

            det_rules = await _load_active_deterministic_rules(db)

            for email_data in new_emails:
                if is_meeting_response(email_data):
                    await _handle_meeting_response(db, client, email_data)
                elif await apply_deterministic_rules(db, client, email_data, det_rules):
                    pass
                else:
                    await _create_triage_job(db, email_data)
                processed += 1

            await db.commit()
    except Exception:
        logger.exception("Manueller Triage-Lauf Fehler")
    finally:
        if client:
            await client.close()

    return processed


RECONCILE_LOOKBACK_DAYS = 7


async def _reconcile_sent_drafts(limit: int = 25) -> int:
    """Sent-Items-Reconciliation: erkennt in Outlook versendete/editierte Entwuerfe.

    Wichtigstes implizites Lernsignal OHNE Verhaltensaenderung des Beraters:
    Wird ein Agent-Entwurf direkt in Outlook (statt im Cockpit) versendet, bleibt
    der ``email_triage``-Job sonst ewig in ``awaiting_approval`` und kein Stil-Edit
    wird gelernt. Diese Funktion gleicht den Entwurf-Snapshot
    (``original_draft_html`` + ``draft_conversation_id``) gegen die tatsaechlich
    gesendete Fassung in derselben Konversation (Ordner ``sentitems``) ab und
    schreibt ein ``draft_edit``/``approved_clean``-Signal (``source='outlook'``).

    Matching: ``conversationId`` + Empfaenger + ``sentDateTime`` nach Job-Erstellung.
    Best-effort -- darf den Poll-Loop nie scheitern lassen.
    """
    from app.services.learning import (
        bump_sender_correction,
        compute_draft_diff,
        mark_episode_corrected,
        record_feedback,
    )

    client = _get_graph_client()
    if client is None:
        return 0

    reconciled = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECONCILE_LOOKBACK_DAYS)
    try:
        async with async_session() as db:
            rows = await db.execute(
                select(AgentJob)
                .where(
                    AgentJob.job_type == "email_triage",
                    AgentJob.status == "awaiting_approval",
                    AgentJob.created_at >= cutoff,
                )
                .order_by(AgentJob.created_at.desc())
                .limit(limit)
            )
            jobs = list(rows.scalars().all())
            for job in jobs:
                meta = dict(job.metadata_json or {})
                if meta.get("feedback_captured"):
                    continue
                original_html = meta.get("original_draft_html")
                conv_id = meta.get("draft_conversation_id")
                to_list = meta.get("draft_to") or []
                recipient = to_list[0] if to_list else None
                # Ohne Snapshot + conversationId + Empfaenger kein sicheres Matching.
                if not (original_html and conv_id and recipient):
                    continue

                try:
                    sent = await client.search_my_replies_to(recipient, top=5)
                except Exception:
                    logger.warning("Reconciliation: sentitems-Abfrage fehlgeschlagen (%s)", recipient)
                    continue

                match = None
                for m in sent:
                    if m.get("conversationId") != conv_id:
                        continue
                    sent_dt = m.get("sentDateTime")
                    try:
                        sent_at = isoparse(sent_dt) if sent_dt else None
                    except Exception:
                        sent_at = None
                    if sent_at and job.created_at and sent_at <= job.created_at:
                        continue
                    match = m
                    break
                if match is None:
                    continue

                body = match.get("body", {}) or {}
                sent_html = (
                    body.get("content")
                    if body.get("contentType") == "html"
                    else match.get("bodyPreview")
                )
                diff_text, is_clean = compute_draft_diff(original_html, sent_html)
                await record_feedback(
                    db,
                    feedback_type="approved_clean" if is_clean else "draft_edit",
                    agent_job_id=job.id,
                    sender_email=recipient,
                    source="outlook",
                    original={"body_html": original_html},
                    corrected={"body_html": sent_html},
                    diff_text=diff_text or None,
                )
                if not is_clean:
                    await mark_episode_corrected(db, agent_job_id=job.id)
                    await bump_sender_correction(db, email=recipient, diff_text=diff_text)

                meta["feedback_captured"] = True
                job.metadata_json = meta
                job.status = "completed"
                job.completed_at = datetime.now(timezone.utc)
                job.output = (job.output or "") + (
                    "\n\n--- In Outlook versendet erkannt; Lernsignal erfasst. ---"
                )
                # Die zugehoerige Triage nicht in 'processing' haengen lassen.
                await db.execute(
                    update(EmailTriage)
                    .where(EmailTriage.agent_job_id == job.id)
                    .values(status="acted")
                )
                reconciled += 1

            await db.commit()
    except Exception:
        logger.exception("Sent-Items-Reconciliation fehlgeschlagen")
    finally:
        if client:
            await client.close()

    return reconciled


async def _reconcile_stuck_processing() -> int:
    """Repariert ``email_triage``-Records, die in ``processing`` haengen geblieben sind.

    Wird ein auto_reply-Entwurf ueber das Cockpit freigegeben oder vom
    Draft-Cleanup abgeschlossen, geht der Agent-Job auf ``completed``, der
    Triage-Record blieb bisher aber auf ``processing`` (sichtbar als Dauer-
    "in Bearbeitung"). Diese Wartung setzt solche Records auf ``acted``.
    """
    try:
        async with async_session() as db:
            result = await db.execute(
                update(EmailTriage)
                .where(
                    EmailTriage.status == "processing",
                    EmailTriage.agent_job_id.in_(
                        select(AgentJob.id).where(AgentJob.status == "completed")
                    ),
                )
                .values(status="acted")
                .returning(EmailTriage.id)
            )
            fixed = result.scalars().all()
            await db.commit()
        return len(fixed)
    except Exception:
        logger.exception("Stuck-Processing-Reconciliation fehlgeschlagen")
        return 0


async def triage_loop() -> None:
    """Automatische Endlosschleife: Prueft alle 2 Minuten auf neue E-Mails.

    Prüft vor jedem Zyklus:
    - Stufe 1: TP_INTEGRATIONS_ACTIVE (Env)
    - Stufe 2: triage_enabled (Owner-Settings in DB)
    """
    settings = get_settings()
    interval = settings.triage_interval_seconds
    logger.info(
        "Triage-Service gestartet -- automatischer Poll alle %d Sekunden",
        interval,
    )
    await asyncio.sleep(5)
    while True:
        try:
            if not settings.integrations_active:
                await asyncio.sleep(interval)
                continue
            if not await _is_triage_enabled_in_db():
                await asyncio.sleep(interval)
                continue
            count = await _triage_cycle()
            if count:
                logger.info("Triage: %d neue E-Mail(s) → AgentJobs für Hermes-Worker erstellt", count)
            # Sent-Items-Reconciliation: in Outlook versendete Entwuerfe als Lernsignal erfassen.
            reconciled = await _reconcile_sent_drafts()
            if reconciled:
                logger.info("Reconciliation: %d in Outlook versendete Entwurf/Entwuerfe als Lernsignal erfasst", reconciled)
            stuck_fixed = await _reconcile_stuck_processing()
            if stuck_fixed:
                logger.info("Reconciliation: %d haengende 'processing'-Triage(s) auf 'acted' gesetzt", stuck_fixed)
        except Exception:
            logger.exception("Triage-Service: unerwarteter Fehler")
        await asyncio.sleep(interval)


MAX_CHAT_MESSAGES_PER_CYCLE = 30


async def _get_known_chat_message_ids(db: AsyncSession) -> set[str]:
    result = await db.execute(select(ChatTriage.message_id))
    return {row[0] for row in result.all()}


async def _create_chat_triage_job(
    db: AsyncSession, chat_id: str, msg: dict, chat_type: str | None = None,
) -> None:
    """Erstellt einen ChatTriage-Record und einen AgentJob für eine neue Chat-Nachricht."""
    from app.services.llm_defaults import get_default_local_model

    sender = (msg.get("from") or {}).get("user", {})
    from_name = sender.get("displayName", "")
    from_id = sender.get("id", "")
    body = (msg.get("body") or {}).get("content", "")[:500]

    triage_record = ChatTriage(
        chat_id=chat_id,
        message_id=msg["id"],
        from_name=from_name,
        from_id=from_id,
        body_preview=body,
        chat_type=chat_type,
        received_at=_parse_received_at(msg.get("createdDateTime")),
        triage_class=None,
        confidence=None,
        suggested_action=None,
        status="pending",
    )
    db.add(triage_record)

    local_model = await get_default_local_model(db)

    agent_job = AgentJob(
        task_id=None,
        job_type="chat_triage",
        status="queued",
        llm_model=local_model,
        metadata_json={
            "chat_id": chat_id,
            "chat_message_id": msg["id"],
            "from_name": from_name,
            "from_id": from_id,
            "body_preview": body,
            "chat_type": chat_type or "",
            "created_at": msg.get("createdDateTime", ""),
        },
    )
    db.add(agent_job)
    await db.flush()

    triage_record.agent_job_id = agent_job.id


async def _chat_triage_cycle() -> int:
    """Ein Chat-Triage-Zyklus: Neue Teams-Nachrichten erkennen, AgentJobs erstellen."""
    client = _get_graph_client()
    if client is None:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=COLD_START_CUTOFF_HOURS)
    processed = 0
    skipped_old = 0
    try:
        async with async_session() as db:
            known_ids = await _get_known_chat_message_ids(db)

            chats = await client.list_chats(top=20)
            for chat in chats:
                chat_id = chat.get("id")
                if not chat_id:
                    continue
                chat_type = chat.get("chatType")

                # Meeting-Chats (Teams-Besprechungs-Threads) liefern via Graph keine
                # regulaeren Nachrichten und verursachten pro Zyklus Dauer-Warnungen
                # (~2000 im Log). Sie sind fuer die Triage irrelevant -> ueberspringen.
                if chat_type == "meeting" or chat_id.startswith("19:meeting_"):
                    continue

                try:
                    msgs = await client.list_chat_messages(chat_id=chat_id, top=10)
                except Exception:
                    # Kein Dauer-Alarm mehr: nur auf DEBUG, da einzelne Chats
                    # (Berechtigung/Typ) systembedingt nicht ladbar sind.
                    logger.debug("Chat-Nachrichten für %s nicht ladbar", chat_id[:20])
                    continue

                for msg in msgs:
                    msg_id = msg.get("id")
                    msg_type = msg.get("messageType")
                    if not msg_id or msg_id in known_ids:
                        continue
                    if msg_type in ("systemEventMessage",):
                        continue
                    created = msg.get("createdDateTime")
                    if created:
                        try:
                            if isoparse(created) < cutoff:
                                skipped_old += 1
                                continue
                        except (ValueError, TypeError):
                            pass
                    await _create_chat_triage_job(db, chat_id, msg, chat_type)
                    processed += 1

            if skipped_old:
                logger.info(
                    "Chat-Triage: %d alte Nachrichten (>%dh) übersprungen",
                    skipped_old, COLD_START_CUTOFF_HOURS,
                )

            await db.commit()

    except PermissionError as e:
        logger.warning("Graph API Permission-Fehler (Chat): %s", e)
    except Exception:
        logger.exception("Chat-Triage-Zyklus Fehler")
    finally:
        if client:
            await client.close()

    return processed


async def chat_triage_loop() -> None:
    """Automatische Endlosschleife: Prüft alle 5 Minuten auf neue Chat-Nachrichten.

    Prüft vor jedem Zyklus:
    - Stufe 1: TP_INTEGRATIONS_ACTIVE (Env)
    - Stufe 2: triage_enabled (Owner-Settings in DB)
    """
    settings = get_settings()
    interval = settings.chat_triage_interval_seconds
    logger.info(
        "Chat-Triage-Service gestartet -- automatischer Poll alle %d Sekunden",
        interval,
    )
    await asyncio.sleep(15)
    while True:
        try:
            if not settings.integrations_active:
                await asyncio.sleep(interval)
                continue
            if not await _is_triage_enabled_in_db():
                await asyncio.sleep(interval)
                continue
            count = await _chat_triage_cycle()
            if count:
                logger.info("Chat-Triage: %d neue Nachricht(en) → AgentJobs erstellt", count)
        except Exception:
            logger.exception("Chat-Triage-Service: unerwarteter Fehler")
        await asyncio.sleep(interval)


_triage_task: asyncio.Task | None = None
_chat_triage_task: asyncio.Task | None = None


async def start_triage_service() -> None:
    global _triage_task, _chat_triage_task
    s = get_settings()
    if not s.integrations_active:
        logger.info(
            "Triage-Service deaktiviert (TP_INTEGRATIONS_ACTIVE=false, Umgebung: %s)",
            s.app_env,
        )
        return
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        logger.info("Triage-Service deaktiviert (Graph API nicht konfiguriert)")
        return
    _triage_task = asyncio.create_task(triage_loop())
    _chat_triage_task = asyncio.create_task(chat_triage_loop())
    logger.info(
        "Triage-Service: E-Mail (%ds) + Chat (%ds) Hintergrund-Tasks laufen [Umgebung: %s]",
        s.triage_interval_seconds,
        s.chat_triage_interval_seconds,
        s.app_env,
    )


async def stop_triage_service() -> None:
    global _triage_task, _chat_triage_task
    for task in (_triage_task, _chat_triage_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _triage_task = None
    _chat_triage_task = None
