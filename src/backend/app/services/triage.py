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
from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import AgentJob, ChatTriage, EmailTriage, Task, User
from app.core.principal import get_owner, get_principal_settings, system_principal_id
from app.services.email_identity import backfill_identities, sync_message_id
from app.services.email_projection import reconcile_tasks_folder

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
# Wie viele Alt-Tasks pro Zyklus ihre internetMessageId nachgeholt bekommen. Klein
# gehalten, weil jede Zeile eine Graph-Anfrage kostet und der Bestand endlich ist.
BACKFILL_BATCH = 25


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
    """Alle bereits gesichteten Mails -- als Handles UND als Identitaeten.

    Ein Move aendert das Graph-Handle. Ein Set aus reinen Handles kann eine Mail
    darum nach dem Verschieben nicht wiedererkennen und liesse sie ein zweites Mal
    durch die Triage laufen (samt zweitem ``email_triage``-Record, den die
    UNIQUE-Bedingung nicht verhindert, weil sie am Handle haengt). Die
    ``internetMessageId`` steht im selben Set, weil der Aufrufer beide Werte
    dagegen prueft -- Handle und Identitaet kollidieren nie, sie sehen zu
    unterschiedlich aus.
    """
    result = await db.execute(
        select(EmailTriage.message_id, EmailTriage.internet_message_id).where(
            EmailTriage.user_id == await system_principal_id(db)
        )
    )
    known: set[str] = set()
    for handle, identity in result.all():
        if handle:
            known.add(handle)
        if identity:
            known.add(identity)
    return known


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
            # Gegen Handle UND Identitaet pruefen: nach einem Move traegt dieselbe
            # Mail ein neues Handle, die Identitaet dagegen nicht.
            if mid in known_ids or (msg.get("internetMessageId") or "") in known_ids:
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
        user_id=await system_principal_id(db),
        message_id=message_id,
        internet_message_id=email_data.get("internetMessageId"),
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
        moved = await client.move_to_folder(message_id, "Kalender")
        await sync_message_id(
            db,
            internet_message_id=triage_record.internet_message_id,
            new_message_id=(moved or {}).get("id"),
        )
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
            LearnedRule.user_id == await system_principal_id(db),
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
        user_id=await system_principal_id(db),
        message_id=message_id,
        internet_message_id=email_data.get("internetMessageId"),
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
                "internet_message_id": email_data.get("internetMessageId", ""),
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
            moved = await client.move_to_folder(message_id, folder)
            await sync_message_id(
                db,
                internet_message_id=triage_record.internet_message_id,
                new_message_id=(moved or {}).get("id"),
            )
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


async def _enrich_auto_submitted(client: GraphClient, emails: list[dict]) -> None:
    """Ergaenzt neue Mails um den RFC-3834-Header ``Auto-Submitted``.

    Das ist ein Fakt aus dem Umschlag, keine Textdeutung: der Absender-Server
    deklariert damit selbst, dass die Mail automatisch erzeugt wurde
    (``auto-generated``) bzw. eine automatische Antwort ist (``auto-replied``).

    Der Header wird dem LLM als Kontext mitgegeben, damit es nicht mehr auf fremde
    Abwesenheitsnotizen antwortet. Bewusst KEINE deterministische Regel daraus: eine
    Messung am Postfach zeigte, dass nur 2 von 22 Abwesenheitsnotizen
    ``auto-replied`` tragen -- die uebrigen 20 tragen ``auto-generated``, genau wie
    Ticketsysteme und eine Lieferantenrechnung. Der Header trennt Autoresponder also
    nicht sauber von handlungsrelevanter Maschinenpost; das ist eine
    Bedeutungsfrage und damit Sache des LLM.

    Ein Aufruf pro NEUER Mail (typisch 0-5 pro Zyklus), nicht pro Listenabruf.
    Best-effort: bei Fehlern fehlt das Feld und der Prompt bleibt unveraendert.
    """
    for msg in emails:
        mid = msg.get("id")
        if not mid:
            continue
        try:
            detail = await client.get_message_headers(mid)
        except Exception:  # noqa: BLE001 - Kontext ist Beigabe
            logger.debug("Auto-Submitted nicht lesbar (mid=%s)", str(mid)[:30])
            continue
        for header in detail:
            if (header.get("name") or "").lower() == "auto-submitted":
                value = (header.get("value") or "").strip()
                if value and value.lower() != "no":
                    msg["autoSubmitted"] = value
                break


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

    principal = await system_principal_id(db)
    triage_record = EmailTriage(
        user_id=principal,
        message_id=email_data["id"],
        internet_message_id=email_data.get("internetMessageId"),
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
        user_id=principal,
        task_id=None,
        job_type="email_triage",
        status="queued",
        llm_model=local_model,
        metadata_json={
            "email_message_id": email_data["id"],
            "internet_message_id": email_data.get("internetMessageId", ""),
            "subject": subject,
            "from_address": from_addr,
            "from_name": from_info.get("name", ""),
            "inference_classification": inference,
            "body_preview": email_data.get("bodyPreview", "")[:500],
            "categories": email_data.get("categories", []),
            "conversation_id": email_data.get("conversationId", ""),
            "recipient_type": recipient_type,
            "auto_submitted": email_data.get("autoSubmitted", ""),
            # Sperrt jede Outlook-Aenderung an dieser Mail (Archiv-Nachlauf).
            "readonly_mail": bool(email_data.get("readonly_mail")),
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
            await _enrich_auto_submitted(client, new_emails)

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

            # Altbestand in kleinen Portionen nachziehen. Haengt am Triage-Zyklus,
            # weil hier ohnehin ein Graph-Client offen ist und die Drosselung schon
            # beachtet wird -- ein eigener Scheduler waere ein zweiter Ort fuer
            # dieselbe Sache.
            await backfill_identities(db, client, limit=BACKFILL_BATCH)

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
            await _enrich_auto_submitted(client, new_emails)

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


ARCHIVE_RESCAN_SINCE_KEY = "archive_rescan_since"
ARCHIVE_PAGE_SIZE = 50
MAX_ARCHIVE_PAGES = 6


async def _archive_rescan_cycle() -> int:
    """Sucht im Archiv nach Mails, die die Triage nie gesehen hat -- rein lesend.

    Der eine Fall, den keine Projektion fangen kann: Anthony archiviert unterwegs auf
    dem Handy, bevor die Triage die Mail überhaupt gesichtet hat. Genau dafür ist
    dieser Nachlauf da.

    Er **fasst die Mail nicht an**. Was daraus entsteht, ist ein Task-Vorschlag mit
    ``needs_review`` in «Vorschläge prüfen». Erst wenn Anthony ihn bestätigt, wird die
    Mail bewegt -- und dann, weil er es entschieden hat. Das ist auch die richtige
    Fehlerart-Zuordnung: ein überflüssiger Vorschlag ist laut und in zwei Klicks
    verworfen, ein übersehener Task ist still. Nur die stille Variante ist gefährlich.

    Verwirft er den Vorschlag, verhindert der dann existierende Triage-Record, dass
    dieselbe Mail je wieder aufgegriffen wird.
    """
    client = _get_graph_client()
    if client is None:
        return 0

    processed = 0
    try:
        async with async_session() as db:
            cutoff = await _activation_cutoff(
                db, ARCHIVE_RESCAN_SINCE_KEY, "Archiv-Nachlauf"
            )
            if cutoff is None:
                await db.commit()
                return 0
            known_ids = await _get_known_message_ids(db)
            candidates = await _fetch_untriaged_archive_emails(client, known_ids, cutoff)
            for email_data in candidates:
                # Der Marker sperrt jede Outlook-Änderung an dieser Mail. Struktur
                # statt Anweisung: das Sperren sitzt im einzigen Schreibpfad
                # (_finalize_email_state), nicht in einer Bitte an den Agenten.
                email_data["readonly_mail"] = True
                await _create_triage_job(db, email_data)
                processed += 1
            await db.commit()
    except Exception:  # noqa: BLE001 - darf den Poll-Loop nie stoppen
        logger.exception("Archiv-Nachlauf fehlgeschlagen")
    finally:
        if client:
            await client.close()

    return processed


async def _fetch_untriaged_archive_emails(
    client: GraphClient, known_ids: set[str], cutoff: datetime
) -> list[dict]:
    """Blättert das Archiv nach ``receivedDateTime desc`` bis zum Stichtag durch.

    Gleiche Mechanik wie beim Posteingang, gleiche Deckel. Erkannt wird über die
    ``internetMessageId``: Das Graph-Handle einer archivierten Mail ist ein anderes
    als das, unter dem sie im Posteingang triagiert wurde -- ohne die Identität würde
    hier jede bereits gesichtete Mail als neu gelten und der Nachlauf das Postfach
    fluten. Das ist der Grund, warum Phase 1 vor Phase 3 kommt.
    """
    found: list[dict] = []
    for page in range(MAX_ARCHIVE_PAGES):
        data = await client.list_emails(
            folder="archive", top=ARCHIVE_PAGE_SIZE, skip=page * ARCHIVE_PAGE_SIZE
        )
        msgs = data.get("value", [])
        if not msgs:
            break
        reached_old = False
        for msg in msgs:
            received = msg.get("receivedDateTime")
            if received:
                try:
                    if isoparse(received) < cutoff:
                        reached_old = True
                        continue
                except (ValueError, TypeError):
                    pass
            mid = msg.get("id")
            identity = msg.get("internetMessageId") or ""
            if not mid or mid in known_ids or (identity and identity in known_ids):
                continue
            if not identity:
                # Ohne Identität liesse sich diese Mail beim naechsten Lauf nicht
                # wiedererkennen -- sie wuerde in jedem Zyklus erneut vorgeschlagen.
                continue
            found.append(msg)
        if reached_old or len(msgs) < ARCHIVE_PAGE_SIZE:
            break
        if len(found) >= MAX_NEW_EMAILS_PER_CYCLE:
            logger.info("Archiv-Nachlauf: Mengenlimit erreicht -- Rest im naechsten Lauf")
            break
    return found


FLAG_PICKUP_SINCE_KEY = "flag_pickup_since"
FLAG_PICKUP_LIMIT = 25


async def _flag_pickup_cycle() -> int:
    """Greift selbst gesetzte Outlook-Fahnen auf und macht daraus Aufgaben.

    Die Gegenrichtung zu Phase 2: nicht das System markiert, sondern der Mensch.
    Markieren heisst dann dasselbe wie dort -- *daraus wird eine Aufgabe*. Damit gibt
    es Quick-Capture vom Handy ohne PWA, und die Outlook-Suche nach markierten Mails
    wird zu einer verlässlichen Liste der offenen Arbeit mit Mail-Bezug.

    **Deterministisch, kein LLM.** Der Mensch hat entschieden; das ist keine
    Ermessensfrage. Der Task entsteht darum ohne Sichtungsmarke, und die Fahne
    **bleibt** gesetzt -- sie ist ab jetzt der Nachweis aus Phase 2 und wird beim
    Erledigen aufgelöst. Genau dadurch behält «Fahne gesetzt» eine einzige Bedeutung.

    **Kein Altbestand.** Aufgegriffen wird nur, was ab dem Aktivierungszeitpunkt
    markiert wurde, und Mails im Archiv bleiben aussen vor: dort ist eine Fahne kein
    Auftrag, sondern ein Überrest. Die alten Fahnen räumt Anthony selbst auf.
    """
    from app.services.email_projection import mark_open_work

    client = _get_graph_client()
    if client is None:
        return 0

    picked = 0
    try:
        async with async_session() as db:
            cutoff = await _activation_cutoff(db, FLAG_PICKUP_SINCE_KEY, "Fahnen-Aufgriff")
            if cutoff is None:
                await db.commit()
                return 0
            archive_id = await client.well_known_folder_id("archive")
            known_ids = await _get_known_message_ids(db)

            mails = await client.list_flagged_emails(top=FLAG_PICKUP_LIMIT, since_days=30)
            for mail in mails:
                identity = mail.get("internetMessageId")
                if not identity or identity in known_ids or mail.get("id") in known_ids:
                    continue
                if archive_id and mail.get("parentFolderId") == archive_id:
                    continue
                received = _parse_received_at(mail.get("receivedDateTime"))
                if received is None or received < cutoff:
                    continue

                task = await _create_task_from_flag(db, mail)
                if task is None:
                    continue
                picked += 1
                # Ordnung nachziehen: Die Fahne steht schon, es fehlt nur der Move.
                # Nur fuer den frisch angelegten Task -- hat die Duplikaterkennung
                # stattdessen einen bestehenden Task zurueckgegeben, gehoert dessen
                # Mail einer anderen Identitaet und ist schon am richtigen Ort.
                if task.internet_message_id == identity:
                    await mark_open_work(db, task)

            await db.commit()
    except Exception:  # noqa: BLE001 - darf den Poll-Loop nie stoppen
        logger.exception("Fahnen-Aufgriff fehlgeschlagen")
    finally:
        if client:
            await client.close()

    return picked


async def _create_task_from_flag(db: AsyncSession, mail: dict) -> Task | None:
    """Legt Triage-Record und Task zu einer markierten Mail an. Gibt den Task zurueck.

    Der Triage-Record ist nicht Beigabe, sondern die Sperre: ohne ihn kennt
    ``_get_known_message_ids`` die Mail nicht und der nächste Zyklus legt denselben
    Task erneut an.
    """
    from app.services.hermes_worker import _create_email_task

    from_info = (mail.get("from") or {}).get("emailAddress", {}) or {}
    from_addr = from_info.get("address", "")
    subject = mail.get("subject") or "(kein Betreff)"
    preview = (mail.get("bodyPreview") or "").strip()

    triage_record = EmailTriage(
        user_id=await system_principal_id(db),
        message_id=mail["id"],
        internet_message_id=mail.get("internetMessageId"),
        subject=subject,
        from_address=from_addr,
        from_name=from_info.get("name"),
        received_at=_parse_received_at(mail.get("receivedDateTime")),
        inference_class=mail.get("inferenceClassification", ""),
        triage_class="task",
        reply_expected=False,
        confidence=1.0,
        suggested_action={
            # Bewusst OHNE ``label``. Der Fahnen-Aufgriff weiss, dass Arbeit ansteht,
            # aber nicht, um was es thematisch geht -- er ruft kein Modell. Ein Wort
            # wie "Aufgabe" hineinzuschreiben wäre eine Behauptung über das Thema und
            # gleichzeitig kein gültiges Label: "Aufgabe" steht nicht in
            # ``TRIAGE_LABELS`` und existiert in Outlook nicht. Gemessen war es
            # dadurch die häufigste Kategorie der Statistik, ohne je eine zu sein.
            # Die Tatsache steht in ``deterministic_override``, wo sie hingehört; im
            # Cockpit bleibt die Label-Auswahl leer und der Mensch kann eine setzen.
            "triage_class": "task",
            "deterministic_override": "manual_flag",
            "rationale": (
                "Vom Menschen in Outlook markiert -- deterministisch als Aufgabe "
                "verbucht, kein LLM. Die Fahne bleibt als Nachweis gesetzt."
            ),
        },
        status="acted",
    )
    db.add(triage_record)
    await db.flush()

    meta = {
        "email_message_id": mail["id"],
        "internet_message_id": mail.get("internetMessageId", ""),
        "subject": subject,
        "from_address": from_addr,
        "from_name": from_info.get("name", ""),
        "conversation_id": mail.get("conversationId", ""),
        "body_preview": preview[:500],
    }
    task = await _create_email_task(
        db,
        None,
        meta,
        task_title=subject,
        task_description=(
            f"In Outlook markiert von {from_info.get('name') or from_addr}."
            + (f"\n\n> {preview[:400]}" if preview else "")
        ),
        suggested_project=None,
        deadline=None,
        reply_expected=False,
        needs_review=False,
    )
    if task is None:
        logger.warning("Fahnen-Aufgriff: kein Task angelegt (Projekt/Spalte fehlt)")
        return None
    logger.info("Fahnen-Aufgriff: Task '%s' aus markierter Mail angelegt", subject[:60])
    return task


async def _activation_cutoff(
    db: AsyncSession, key: str, label: str
) -> datetime | None:
    """Aktivierungszeitpunkt einer Nachlauf-Funktion; beim ersten Lauf festgeschrieben.

    Ohne Stichtag liefe die halbe Postfach-Historie in die Warteschlange. Der erste
    Lauf setzt darum «jetzt» und findet nichts -- absichtlich: aufgegriffen wird nur,
    was **nach** der Aktivierung entsteht.
    """
    principal = await system_principal_id(db)
    if principal is None:
        return None
    settings = await get_principal_settings(db, principal)
    raw = settings.get(key)
    if raw:
        try:
            return isoparse(str(raw))
        except (ValueError, TypeError):
            logger.warning("Stichtag '%s' unlesbar -- wird neu gesetzt", key)
    now = datetime.now(timezone.utc)
    await db.execute(
        update(User)
        .where(User.id == principal)
        .values(
            settings=func.coalesce(User.settings, cast({}, JSONB)).op("||")(
                cast({key: now.isoformat()}, JSONB)
            )
        )
        .execution_options(synchronize_session=False)
    )
    logger.info("%s aktiviert, Stichtag: %s", label, now.isoformat())
    return now


# Bei 120 s Takt entspricht das rund einer Viertelstunde.
SLOW_MAINTENANCE_EVERY = 8


async def _run_slow_maintenance() -> None:
    """Die drei Postfach-Nachläufe, die nicht in jeden Zyklus gehören.

    Abgleich, Archiv-Nachlauf und Fahnen-Aufgriff kosten je mehrere Graph-Anfragen und
    haben keine Eile. Der seltene Takt ist keine Sparmassnahme, sondern eine Lehre:
    am 18.08.2026 gingen 2858 Anfragen in 36 Sekunden ans Postfach, danach drosselte
    Graph. Jede dieser Funktionen ist zudem in sich best-effort -- ein Ausfall bleibt
    ein Logeintrag.
    """
    async with async_session() as db:
        repaired = await reconcile_tasks_folder(db)
        await db.commit()
    if repaired:
        logger.info("Abgleich: %d erledigte Task-Mail(s) nachträglich archiviert", repaired)

    proposed = await _archive_rescan_cycle()
    if proposed:
        logger.info("Archiv-Nachlauf: %d ungesichtete Mail(s) zur Sichtung vorgeschlagen", proposed)

    picked = await _flag_pickup_cycle()
    if picked:
        logger.info("Fahnen-Aufgriff: %d markierte Mail(s) zu Aufgaben gemacht", picked)


async def triage_loop() -> None:
    """Automatische Endlosschleife: Prueft alle 2 Minuten auf neue E-Mails.

    Prüft vor jedem Zyklus:
    - Stufe 1: TP_INTEGRATIONS_ACTIVE (Env)
    - Stufe 2: triage_enabled (Owner-Settings in DB)

    Die Postfach-Nachläufe laufen nur jeden ``SLOW_MAINTENANCE_EVERY``-ten Zyklus
    (Richtwert eine Viertelstunde), siehe :func:`_run_slow_maintenance`.
    """
    settings = get_settings()
    interval = settings.triage_interval_seconds
    logger.info(
        "Triage-Service gestartet -- automatischer Poll alle %d Sekunden",
        interval,
    )
    await asyncio.sleep(5)
    cycle = 0
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
            cycle += 1
            if cycle % SLOW_MAINTENANCE_EVERY == 0:
                await _run_slow_maintenance()
        except Exception:
            logger.exception("Triage-Service: unerwarteter Fehler")
        await asyncio.sleep(interval)


MAX_CHAT_MESSAGES_PER_CYCLE = 30


async def _get_known_chat_message_ids(db: AsyncSession) -> set[str]:
    result = await db.execute(
        select(ChatTriage.message_id).where(
            ChatTriage.user_id == await system_principal_id(db)
        )
    )
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

    principal = await system_principal_id(db)
    triage_record = ChatTriage(
        user_id=principal,
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
        user_id=principal,
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
