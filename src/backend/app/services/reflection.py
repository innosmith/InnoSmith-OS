"""Reflexions-Job (Saeule 5): konsolidiert Korrektursignale zu Regel-Vorschlaegen.

Laeuft **lokal** und rein deterministisch (keine LLM-Abhaengigkeit): Der Job
gruppiert die in ``agent_feedback`` erfassten Korrekturen und schlaegt bei
wiederkehrenden Mustern eine ``learned_rule`` (Status ``proposed``) vor. Die
Freigabe erfolgt strikt ueber HITL (Intelligence-Tab) -- erst eine ``active``
Regel beeinflusst den Triage-Prompt.

Erkannte Muster:
- **Triage-Reklassifikation**: derselbe Absender wird wiederholt von Klasse A
  nach B umklassifiziert -> Regel, kuenftig direkt als B zu triagieren.
- **Verworfene Vorschlaege pro Absender**: Agent-Vorschlaege desselben Absenders
  werden wiederholt verworfen/abgelehnt (``task_deleted``/``rejected``) -> Regel,
  solche Mails zurueckhaltender (eher ``fyi``, kein Task) zu behandeln. Dies ist
  im Realbetrieb das haeufigste Korrektursignal und wurde zuvor ignoriert.
- **Draft-Edits pro Absender**: Antworten an denselben Kontakt werden wiederholt
  stilistisch angepasst -> Regel, den Stil-Anker konsequenter zu uebernehmen.

Idempotenz: Ein Vorschlag wird nur erzeugt, wenn noch keine Regel mit gleicher
**semantischer Signatur** (``evidence['key']``, z. B. ``triage:absender:A->B``)
existiert -- ueber ALLE Status, auch ``rejected``. Da der Schluessel den
veraenderlichen Zaehler NICHT enthaelt, wird eine bereits verworfene Regel auch
bei steigendem Zaehler nicht erneut vorgeschlagen. Fuer Altdaten ohne Schluessel
gilt weiterhin der exakte ``rule_text`` als Fallback-Signatur.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import AgentFeedback, LearnedRule

logger = logging.getLogger("taskpilot.reflection")


async def _existing_rule_signatures(db: AsyncSession) -> set[str]:
    """Signaturen bereits vorhandener Regeln (ueber ALLE Status).

    Bevorzugt der semantische ``evidence['key']`` (stabil gegen Zaehler-
    Aenderungen); fuer Altdaten ohne Key faellt es auf den exakten ``rule_text``
    zurueck. So wird eine bereits verworfene Regel nicht erneut vorgeschlagen,
    auch wenn der Beleg-Zaehler inzwischen gestiegen ist.
    """
    rows = await db.execute(select(LearnedRule.rule_text, LearnedRule.evidence))
    signatures: set[str] = set()
    for rule_text, evidence in rows.all():
        key = evidence.get("key") if isinstance(evidence, dict) else None
        if key:
            signatures.add(key)
        if rule_text:
            signatures.add(rule_text)
    return signatures


def _build_proposals(
    feedback: list[AgentFeedback], min_occurrences: int
) -> list[tuple[str, str, dict, str]]:
    """Leitet aus Korrektursignalen Regel-Vorschlaege ab.

    Returns Liste von ``(scope, rule_text, evidence, autonomy_hint)``. Rein und
    damit unabhaengig testbar.
    """
    proposals: list[tuple[str, str, dict, str]] = []

    # 1) Triage-Reklassifikation: (Absender, alt->neu)
    reclass: Counter[tuple[str, str | None, str]] = Counter()
    for fb in feedback:
        if fb.feedback_type != "triage_reclass" or not fb.sender_email:
            continue
        old = (fb.original or {}).get("triage_class")
        new = (fb.corrected or {}).get("triage_class")
        if not new or old == new:
            continue
        reclass[(fb.sender_email.lower(), old, new)] += 1
    for (sender, old, new), count in reclass.items():
        if count < min_occurrences:
            continue
        suffix = f" (statt '{old}')" if old else ""
        rule_text = (
            f"E-Mails von {sender} als '{new}' triagieren{suffix}. "
            f"Belegt durch {count} manuelle Korrekturen."
        )
        proposals.append(
            (
                "triage",
                rule_text,
                {
                    "sender": sender,
                    "from_class": old,
                    "to_class": new,
                    "count": count,
                    "key": f"triage:{sender}:{old or '*'}->{new}",
                },
                "L1",
            )
        )

    # 2) Verworfene Task-Vorschlaege / abgelehnte Entwuerfe pro Absender.
    # Semantik: Der Agent hat wiederholt etwas vorgeschlagen, das der Berater
    # weggeworfen hat -> kuenftig zurueckhaltender sein. Bleibt eine LLM-Leitregel
    # (kein deterministisches fyi), weil ein reiner Absender-Filter zu grob waere
    # (z. B. Self-Mails: nur bestimmte Betreffe sind Laerm).
    discard: Counter[str] = Counter()
    for fb in feedback:
        if fb.feedback_type in ("task_deleted", "rejected") and fb.sender_email:
            discard[fb.sender_email.lower()] += 1
    for sender, count in discard.items():
        if count < min_occurrences:
            continue
        rule_text = (
            f"E-Mails von {sender} fuehrten wiederholt zu verworfenen Agent-Vorschlaegen "
            f"({count}x abgelehnt/geloescht). Solche Mails zurueckhaltend behandeln: im "
            f"Zweifel als 'fyi' einordnen und KEINEN Task erstellen, ausser es ist klar "
            f"eine konkrete Handlung von Anthony noetig."
        )
        proposals.append(
            (
                "triage",
                rule_text,
                {
                    "sender": sender,
                    "signal": "discarded_suggestions",
                    "count": count,
                    "key": f"triage:{sender}:discard",
                },
                "L1",
            )
        )

    # 3) Draft-Edits pro Absender
    edits: Counter[str] = Counter()
    for fb in feedback:
        if fb.feedback_type == "draft_edit" and fb.sender_email:
            edits[fb.sender_email.lower()] += 1
    for sender, count in edits.items():
        if count < min_occurrences:
            continue
        rule_text = (
            f"Antworten an {sender} konsequenter am bisherigen Schreibstil "
            f"ausrichten: vor dem Entwurf search_my_replies('{sender}') als "
            f"verbindlichen Stil-Anker nutzen. {count} manuelle Stil-Korrekturen "
            f"erfasst."
        )
        proposals.append(
            (
                "draft",
                rule_text,
                {"sender": sender, "count": count, "key": f"draft:{sender}:style"},
                "L1",
            )
        )

    return proposals


async def run_reflection(
    db: AsyncSession,
    *,
    lookback_days: int = 30,
    min_occurrences: int | None = None,
) -> int:
    """Analysiert das Feedback-Fenster und legt neue Regel-Vorschlaege an.

    Returns die Zahl der neu erzeugten Vorschlaege. Best-effort -- Fehler werden
    geloggt, nicht propagiert.
    """
    if min_occurrences is None:
        min_occurrences = get_settings().agent_reflection_min_occurrences
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = await db.execute(
            select(AgentFeedback).where(AgentFeedback.created_at >= cutoff)
        )
        feedback = list(rows.scalars().all())
        proposals = _build_proposals(feedback, min_occurrences)
        if not proposals:
            return 0

        existing = await _existing_rule_signatures(db)
        created = 0
        for scope, rule_text, evidence, hint in proposals:
            key = evidence.get("key")
            if (key and key in existing) or rule_text in existing:
                continue
            db.add(
                LearnedRule(
                    scope=scope,
                    rule_text=rule_text,
                    evidence=evidence,
                    status="proposed",
                    autonomy_hint=hint,
                )
            )
            if key:
                existing.add(key)
            existing.add(rule_text)
            created += 1
        if created:
            await db.commit()
            logger.info("Reflexion: %d neue Regel-Vorschlaege erstellt", created)
        return created
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("run_reflection fehlgeschlagen")
        return 0


async def reflection_loop() -> None:
    """Endlosschleife: fuehrt den Reflexions-Job periodisch aus (lokal)."""
    interval = max(3600, get_settings().agent_reflection_interval_seconds)
    logger.info("Reflexions-Scheduler gestartet (Intervall: %ds)", interval)
    # Kurz nach dem Start einmal laufen, damit fruehe Signale schnell sichtbar werden.
    await asyncio.sleep(300)
    while True:
        try:
            async with async_session() as db:
                await run_reflection(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Reflexions-Scheduler: unerwarteter Fehler")
        await asyncio.sleep(interval)


_reflection_task: asyncio.Task | None = None


async def start_reflection_scheduler() -> None:
    global _reflection_task
    if not get_settings().agent_reflection_enabled:
        logger.info("Reflexions-Scheduler deaktiviert (agent_reflection_enabled=false)")
        return
    if _reflection_task is None or _reflection_task.done():
        _reflection_task = asyncio.create_task(reflection_loop())


async def stop_reflection_scheduler() -> None:
    global _reflection_task
    if _reflection_task and not _reflection_task.done():
        _reflection_task.cancel()
        try:
            await _reflection_task
        except asyncio.CancelledError:
            pass
    _reflection_task = None
