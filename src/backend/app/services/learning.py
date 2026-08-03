"""Self-Learning-Service: Korrektursignale erfassen + episodisches Gedaechtnis.

Dies ist das Fundament der Lern-Schicht (Saeule 1 + 2):
- ``record_feedback`` schreibt Korrektursignale (Draft-Edits, Reklassifikation,
  Daumen, Chat-Teach) in ``agent_feedback`` -- die Quelle der Wahrheit fuer
  Lern-KPIs und gelernte Regeln.
- ``record_episode`` legt einen abgeschlossenen Entscheid samt lokalem Embedding
  in ``agent_episodes`` ab (Recall-Basis).
- Hilfsfunktionen (HTML->Text, Diff, "saubere" Freigabe-Erkennung) sind rein und
  damit gut testbar.

Alle DB-schreibenden Funktionen sind **best-effort**: Sie duerfen die
Job-Verarbeitung bzw. den E-Mail-Versand niemals scheitern lassen.
"""

from __future__ import annotations

import difflib
import logging
import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentFeedback, AgentJob
from app.core.principal import system_principal_id
# Reine Textanalyse liegt in ``text_style`` (ohne DB-/Framework-Abhaengigkeiten),
# damit auch das Offline-Eval dieselben Regeln nutzen kann. Re-Export, damit
# bestehende Importe aus diesem Modul weiterhin funktionieren.
from app.services.text_style import (  # noqa: F401
    _ANY_GREETING,
    _CLOSING,
    _FORMAL_GREETING,
    _INFORMAL_GREETING,
    _QUOTE_MARKERS,
    extract_salutation_signature,
    has_content_between_greeting_and_closing,
    html_to_text,
    strip_quoted_history,
)
from ai9.embeddings import embed_text, to_pgvector

logger = logging.getLogger("taskpilot.learning")


async def _resolve_principal(db: AsyncSession, user_id: uuid.UUID | str | None):
    """Liefert den handelnden Principal fuer eine Schreiboperation.

    Wird kein ``user_id`` uebergeben (die meisten Background-Aufrufer), faellt die
    Funktion auf den System-Principal (heute == Owner) zurueck. So ist jede
    gelernte Zeile einem Principal zugeordnet und fuer diesen sichtbar.
    """
    if user_id is not None:
        return uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    return await system_principal_id(db)

def _normalize(text_body: str) -> str:
    """Whitespace-normalisierte Form fuer den Gleichheits-Vergleich."""
    return re.sub(r"\s+", " ", text_body or "").strip().lower()


async def update_learned_tone(
    db: AsyncSession,
    *,
    email: str | None,
    tone: dict | None,
    user_id: uuid.UUID | str | None = None,
    commit: bool = False,
) -> None:
    """Merged Anrede/Register/Schlussformel in ``sender_profiles.learned_tone``.

    JSONB-Merge (``||``) -- vorhandene Schluessel werden durch neuere Werte ersetzt,
    andere bleiben erhalten. Legt das Profil bei Bedarf an. Best-effort.
    """
    if not email or not tone:
        return
    try:
        import json as _json

        principal = await _resolve_principal(db, user_id)
        await db.execute(
            text(
                """
                INSERT INTO sender_profiles (user_id, email, learned_tone, language)
                VALUES (:uid, :email, CAST(:tone AS jsonb), 'de')
                ON CONFLICT (user_id, email) DO UPDATE SET
                    learned_tone = COALESCE(sender_profiles.learned_tone, '{}'::jsonb)
                                   || CAST(:tone AS jsonb),
                    updated_at = now()
                """
            ),
            {"uid": principal, "email": email.lower(), "tone": _json.dumps(tone)},
        )
        if commit:
            await db.commit()
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("update_learned_tone fehlgeschlagen (%s)", email)


def compute_draft_diff(original_html: str | None, sent_html: str | None) -> tuple[str, bool]:
    """Vergleicht Agent-Entwurf und gesendete Fassung (nur neuer Text, ohne Zitat).

    Returns ``(diff_text, is_clean)`` -- ``is_clean`` True, wenn inhaltlich
    unveraendert (Freigabe ohne Edit).
    """
    orig = strip_quoted_history(html_to_text(original_html))
    sent = strip_quoted_history(html_to_text(sent_html))

    is_clean = _normalize(orig) == _normalize(sent)
    if is_clean:
        return "", True

    diff_lines = difflib.unified_diff(
        orig.splitlines(),
        sent.splitlines(),
        fromfile="agent_entwurf",
        tofile="versendet",
        lineterm="",
        n=2,
    )
    diff_text = "\n".join(diff_lines)[:8000]
    return diff_text, False


# Trigger-Phrasen, mit denen der Berater im Chat etwas dauerhaft lehren will.
# Der Text NACH der Phrase wird als zu merkende Lektion extrahiert.
_TEACH_PATTERNS = [
    r"\bmerk(?:e)?\s+dir(?:\s+bitte)?\b[:,]?\s*",
    # Blosser Imperativ "merke:" / "merk:" mit Doppelpunkt (haeufige Kurzform).
    # Bewusst nur ":" (nicht ","), damit "ich merke, dass ..." kein Fehltreffer ist.
    r"\bmerk(?:e)?\s*:\s*",
    r"\bnotier(?:e)?\s+dir(?:\s+bitte)?\b[:,]?\s*",
    r"\bpräg(?:e)?\s+dir\b.*?\bein\b[:,]?\s*",
    r"\bvergiss\s+nicht[:,]?\s*",
    r"\bbehalte\b.*?\bim\s+hinterkopf[:,]?\s*",
    r"\b(?:für\s+die\s+zukunft|ab\s+jetzt|künftig|in\s+zukunft)\b[:,]?\s*",
    r"\blern(?:e)?(?:\s+bitte)?[:,]?\s+",
]


def extract_teach_intent(message: str) -> str | None:
    """Erkennt eine "merk dir ..."-Lehr-Absicht und extrahiert die Lektion.

    Returns den zu merkenden Text (ohne Trigger-Phrase) oder ``None``. Rein und
    damit unabhaengig testbar.
    """
    if not message or not message.strip():
        return None
    lowered = message.lower()
    for pat in _TEACH_PATTERNS:
        m = re.search(pat, lowered)
        if not m:
            continue
        lesson = message[m.end():].strip(" \t\n.:,-")
        # Sehr kurze Reste sind kein sinnvoller Lern-Eintrag.
        if len(lesson) >= 4:
            return lesson[:2000]
        # Trigger erkannt, aber kein verwertbarer Inhalt -> ganze Nachricht.
        return message.strip()[:2000]
    return None


async def record_chat_teach(
    db: AsyncSession,
    *,
    content: str,
    conversation_id: str | None = None,
    user_id: uuid.UUID | str | None = None,
    commit: bool = False,
) -> bool:
    """Erfasst eine Chat-Lehr-Absicht: ``chat_teach``-Feedback + Regel-Vorschlag.

    Der Regel-Vorschlag bleibt ``proposed`` (HITL) -- erst nach Freigabe im
    Intelligence-Tab beeinflusst er den Agenten. Best-effort.
    """
    try:
        from app.models import LearnedRule

        principal = await _resolve_principal(db, user_id)
        await record_feedback(
            db,
            feedback_type="chat_teach",
            source="chat",
            reason=content[:500],
            original={"conversation_id": conversation_id} if conversation_id else None,
            user_id=principal,
        )
        # Idempotenz ueber ALLE Status (auch ``rejected``): eine einmal verworfene
        # Chat-Lektion wird nicht erneut vorgeschlagen (analog Reflexion). Pro Principal.
        existing = await db.execute(
            select(LearnedRule.id).where(
                LearnedRule.rule_text == content[:2000],
                LearnedRule.user_id == principal,
            )
        )
        if existing.first() is None:
            db.add(
                LearnedRule(
                    user_id=principal,
                    scope="general",
                    rule_text=content[:2000],
                    status="proposed",
                    evidence={"source": "chat_teach", "conversation_id": conversation_id},
                    autonomy_hint="L1",
                )
            )
        await db.flush()
        if commit:
            await db.commit()
        logger.info("Chat-Teach erfasst (conv=%s): %.80s", conversation_id, content)
        return True
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("record_chat_teach fehlgeschlagen")
        return False


async def record_feedback(
    db: AsyncSession,
    *,
    feedback_type: str,
    agent_job_id: uuid.UUID | str | None = None,
    sender_email: str | None = None,
    source: str = "cockpit",
    original: dict | None = None,
    corrected: dict | None = None,
    diff_text: str | None = None,
    reason: str | None = None,
    user_id: uuid.UUID | str | None = None,
    commit: bool = False,
) -> AgentFeedback | None:
    """Schreibt ein Korrektursignal. Best-effort -- darf nie den Aufrufer stoppen."""
    try:
        if isinstance(agent_job_id, str):
            agent_job_id = uuid.UUID(agent_job_id)
        principal = await _resolve_principal(db, user_id)
        fb = AgentFeedback(
            user_id=principal,
            agent_job_id=agent_job_id,
            sender_email=(sender_email or None),
            source=source,
            feedback_type=feedback_type,
            original=original or {},
            corrected=corrected or {},
            diff_text=diff_text,
            reason=reason,
        )
        db.add(fb)
        await db.flush()
        if commit:
            await db.commit()
        logger.info(
            "Feedback erfasst: type=%s source=%s sender=%s job=%s",
            feedback_type, source, sender_email, agent_job_id,
        )
        return fb
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("record_feedback fehlgeschlagen (type=%s)", feedback_type)
        return None


async def capture_draft_feedback(
    db: AsyncSession,
    *,
    draft_id: str,
    sent_html: str | None,
    recipient: str | None = None,
    source: str = "cockpit",
) -> bool:
    """Erfasst das Lernsignal beim tatsaechlichen Versand eines Agent-Entwurfs.

    Findet den ``email_triage``-Job mit dem urspruenglichen Entwurf (Snapshot
    ``original_draft_html``), vergleicht ihn mit der versendeten Fassung und
    schreibt ``draft_edit`` bzw. ``approved_clean``. Idempotent ueber das
    Metadata-Flag ``feedback_captured``. Best-effort.

    Returns True, wenn ein Signal geschrieben wurde.
    """
    try:
        result = await db.execute(
            select(AgentJob)
            .where(
                AgentJob.metadata_json["draft_id"].astext == draft_id,
                AgentJob.job_type == "email_triage",
            )
            .order_by(AgentJob.created_at.desc())
            .limit(1)
        )
        src_job = result.scalar_one_or_none()
        if src_job is None:
            return False
        meta = dict(src_job.metadata_json or {})
        if meta.get("feedback_captured"):
            return False
        original_html = meta.get("original_draft_html")
        if not original_html:
            return False

        diff_text, is_clean = compute_draft_diff(original_html, sent_html)
        recipient = recipient or (meta.get("draft_to") or [None])[0] or meta.get("from_address")

        await record_feedback(
            db,
            feedback_type="approved_clean" if is_clean else "draft_edit",
            agent_job_id=src_job.id,
            sender_email=recipient,
            source=source,
            original={"body_html": original_html},
            corrected={"body_html": sent_html},
            diff_text=diff_text or None,
        )
        if not is_clean:
            await mark_episode_corrected(db, agent_job_id=src_job.id)
            await bump_sender_correction(db, email=recipient, diff_text=diff_text)
            # Die versendete Fassung zeigt Anthonys tatsaechlich bevorzugte Anrede/
            # Schlussformel fuer diesen Kontakt -> als learned_tone festhalten.
            tone = extract_salutation_signature(sent_html or "")
            await update_learned_tone(db, email=recipient, tone=tone)

        meta["feedback_captured"] = True
        src_job.metadata_json = meta
        await db.flush()
        return True
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("capture_draft_feedback fehlgeschlagen (draft_id=%s)", str(draft_id)[:40])
        return False


def _summarize_diff_for_style(diff_text: str | None) -> str | None:
    """Leitet eine knappe Stil-Notiz aus einem Draft-Diff ab.

    Nutzt die vom Berater hinzugefuegten/bevorzugten Zeilen (``+``) als Signal,
    wie die Antwort an diesen Kontakt klingen soll. Rein und damit testbar.
    """
    if not diff_text:
        return None
    added = [
        line[1:].strip()
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    added = [a for a in added if a]
    if not added:
        return None
    sample = added[0][:160]
    return f'Berater bevorzugte Formulierung: "{sample}"'


async def bump_sender_correction(
    db: AsyncSession,
    *,
    email: str | None,
    diff_text: str | None = None,
    user_id: uuid.UUID | str | None = None,
    commit: bool = False,
) -> None:
    """Schreibt das per-Absender-Lernsignal nach einer Draft-Korrektur fort.

    Erhoeht ``sender_profiles.correction_count`` und haengt -- sofern ableitbar --
    eine knappe Stil-Notiz an ``style_notes`` an (gedeckelt auf 2000 Zeichen).
    Legt das Profil bei Bedarf an. Best-effort -- darf den Aufrufer nie stoppen.
    """
    if not email:
        return
    try:
        note = _summarize_diff_for_style(diff_text)
        bullet = f"- {note}" if note else None
        principal = await _resolve_principal(db, user_id)
        await db.execute(
            text(
                """
                INSERT INTO sender_profiles (user_id, email, correction_count, style_notes, language)
                VALUES (:uid, :email, 1, :bullet, 'de')
                ON CONFLICT (user_id, email) DO UPDATE SET
                    correction_count = sender_profiles.correction_count + 1,
                    style_notes = CASE
                        WHEN :bullet IS NULL THEN sender_profiles.style_notes
                        ELSE left(
                            COALESCE(sender_profiles.style_notes || E'\\n', '') || :bullet,
                            2000
                        )
                    END,
                    updated_at = now()
                """
            ),
            {"uid": principal, "email": email.lower(), "bullet": bullet},
        )
        if commit:
            await db.commit()
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("bump_sender_correction fehlgeschlagen (%s)", email)


async def record_episode(
    db: AsyncSession,
    *,
    summary: str,
    job_type: str | None = None,
    agent_job_id: uuid.UUID | str | None = None,
    sender_email: str | None = None,
    decision: dict | None = None,
    was_corrected: bool = False,
    lesson: str | None = None,
    user_id: uuid.UUID | str | None = None,
    commit: bool = False,
) -> bool:
    """Legt eine Episode mit lokalem Embedding ab (rohes SQL wegen pgvector).

    Best-effort: ohne Embedding wird die Episode trotzdem (mit NULL-Vektor)
    geschrieben, damit die Historie vollstaendig bleibt.
    """
    try:
        import json as _json

        principal = await _resolve_principal(db, user_id)
        vec = await embed_text(summary)
        emb_literal = to_pgvector(vec) if vec else None
        await db.execute(
            text(
                """
                INSERT INTO agent_episodes
                    (user_id, agent_job_id, job_type, sender_email, summary, decision,
                     was_corrected, lesson, embedding)
                VALUES
                    (:uid, :job_id, :job_type, :sender, :summary, CAST(:decision AS jsonb),
                     :was_corrected, :lesson, CAST(:emb AS vector))
                """
            ),
            {
                "uid": principal,
                "job_id": str(agent_job_id) if agent_job_id else None,
                "job_type": job_type,
                "sender": sender_email,
                "summary": summary[:4000],
                "decision": _json.dumps(decision or {}),
                "was_corrected": was_corrected,
                "lesson": lesson,
                "emb": emb_literal,
            },
        )
        if commit:
            await db.commit()
        return True
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("record_episode fehlgeschlagen")
        return False


async def mark_episode_corrected(
    db: AsyncSession,
    *,
    agent_job_id: uuid.UUID | str,
    lesson: str | None = None,
    commit: bool = False,
) -> None:
    """Markiert die Episode eines Jobs als korrigiert und ergaenzt die Lektion."""
    try:
        await db.execute(
            text(
                """
                UPDATE agent_episodes
                SET was_corrected = true,
                    lesson = COALESCE(:lesson, lesson)
                WHERE agent_job_id = :job_id
                """
            ),
            {"job_id": str(agent_job_id), "lesson": lesson},
        )
        if commit:
            await db.commit()
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("mark_episode_corrected fehlgeschlagen")


async def recall_similar_episodes(
    db: AsyncSession,
    *,
    query: str,
    job_type: str | None = None,
    k: int = 4,
    corrected_only: bool = False,
    user_id: uuid.UUID | str | None = None,
) -> list[dict]:
    """Findet die ``k`` aehnlichsten frueheren Episoden via pgvector (Cosine).

    Best-effort: ohne Embedding oder bei Fehler -> leere Liste. Ohne expliziten
    ``user_id`` wird auf den System-Principal gescoped (Recall bleibt pro Principal).
    """
    try:
        principal = await _resolve_principal(db, user_id)
        vec = await embed_text(query, is_query=True)
        if not vec:
            return []
        conditions = ["embedding IS NOT NULL", "user_id = :uid"]
        params: dict = {"emb": to_pgvector(vec), "k": k, "uid": principal}
        if job_type:
            conditions.append("job_type = :job_type")
            params["job_type"] = job_type
        if corrected_only:
            conditions.append("was_corrected = true")
        where = " AND ".join(conditions)
        rows = await db.execute(
            text(
                f"""
                SELECT summary, decision, was_corrected, lesson, sender_email,
                       created_at,
                       1 - (embedding <=> CAST(:emb AS vector)) AS similarity
                FROM agent_episodes
                WHERE {where}
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT :k
                """
            ),
            params,
        )
        return [dict(r._mapping) for r in rows]
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("recall_similar_episodes fehlgeschlagen")
        return []
