"""Style-Store: lokaler Few-Shot-Speicher gesendeter Mails fuer Draft-Anker.

Ein periodischer Sync indexiert Anthonys gesendete Antworten (Ordner ``sentitems``)
mit lokalem Embedding (pgvector). Pro Entwurf liefert :func:`find_style_anchors` die
stilistisch/thematisch passendsten eigenen Antworten -- auch fuer neue Kontakte ohne
History. Bleibt vollstaendig on-prem (lokale Ollama-Embeddings).

Zusaetzlich lernt der Sync pro Kontakt die tatsaechlich verwendete Anrede/Register/
Schlussformel (``sender_profiles.learned_tone``) aus der jeweils neuesten Antwort.

Best-effort: alle Funktionen fangen Fehler ab und duerfen den Scheduler bzw. die
Draft-Erstellung niemals scheitern lassen.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.principal import system_principal_id
from app.database import async_session
from ai9.embeddings import embed_text, to_pgvector
from app.services.learning import (
    extract_salutation_signature,
    html_to_text,
    strip_quoted_history,
    update_learned_tone,
)

logger = logging.getLogger("taskpilot.style_store")

# Mindestlaenge des bereinigten Bodys, damit ein Beispiel als Stil-Anker taugt.
_MIN_BODY_CHARS = 40
# Betreff-Praefixe, die auf Weiterleitungen hindeuten (kein eigener Schreibstil).
_FORWARD_RE = re.compile(r"^\s*(fwd?|wg)\s*:", re.I)


async def _build_graph_client():
    """Baut einen GraphClient aus den Settings (oder None). Lokal, ohne Worker-Import."""
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


def _primary_recipient(msg: dict) -> str | None:
    """Erste To-Adresse einer gesendeten Nachricht (kleingeschrieben)."""
    for r in msg.get("toRecipients", []) or []:
        addr = (r.get("emailAddress", {}) or {}).get("address")
        if addr:
            return addr.lower()
    return None


def _clean_sent_body(msg: dict) -> str:
    """Bereinigt den Body einer gesendeten Mail (HTML->Text, ohne zitierten Verlauf)."""
    body = msg.get("body", {}) or {}
    raw = body.get("content") or msg.get("bodyPreview") or ""
    txt = html_to_text(raw) if raw else ""
    return (strip_quoted_history(txt) or txt).strip()


def _detect_language(text_body: str) -> str:
    """Sehr einfache DE/EN-Heuristik (nur Grobzuordnung, nicht kritisch)."""
    lowered = text_body.lower()
    de = len(re.findall(r"\b(und|der|die|das|ich|nicht|mit|für|grüsse|liebe[rs]?|danke)\b", lowered))
    en = len(re.findall(r"\b(the|and|you|regards|thanks|please|kind|best)\b", lowered))
    return "en" if en > de else "de"


def _is_noise(subject: str, body_text: str, recipient: str | None) -> bool:
    """Filtert ungeeignete Beispiele (Weiterleitungen, zu kurz, ohne Empfaenger)."""
    if not recipient:
        return True
    if _FORWARD_RE.match(subject or ""):
        return True
    if len(body_text) < _MIN_BODY_CHARS:
        return True
    return False


def _parse_graph_dt(value: str | None) -> datetime | None:
    """Graph-ISO-Zeitstempel (z. B. ``2026-07-11T16:26:28Z``) -> ``datetime``.

    asyncpg leitet den Parametertyp in ``CAST(:sent_at AS timestamptz)`` als
    ``timestamptz`` ab und erwartet daher ein ``datetime``-Objekt statt eines Strings.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def _existing_graph_ids(db: AsyncSession, user_id) -> set[str]:
    rows = await db.execute(
        text("SELECT graph_id FROM sent_mail_examples WHERE user_id = :uid"),
        {"uid": user_id},
    )
    return {r[0] for r in rows}


async def sync_style_store(top: int | None = None) -> int:
    """Indexiert die letzten gesendeten Mails in den Style-Store. Returns Neu-Anzahl.

    Idempotent ueber ``graph_id`` (bereits indexierte werden uebersprungen, bevor
    das teure Embedding erzeugt wird). Aktualisiert nebenbei ``learned_tone`` pro
    Kontakt aus der jeweils neuesten Antwort. Best-effort.
    """
    cfg = get_settings()
    if not cfg.style_store_enabled:
        return 0
    client = await _build_graph_client()
    if client is None:
        logger.info("Style-Store-Sync uebersprungen: Graph nicht konfiguriert")
        return 0

    limit = top or cfg.style_store_sync_top
    inserted = 0
    tone_seen: set[str] = set()
    try:
        messages = await client.list_sent_messages(top=limit)
        async with async_session() as db:
            principal = await system_principal_id(db)
            known = await _existing_graph_ids(db, principal)
            for msg in messages:
                gid = msg.get("id")
                if not gid:
                    continue
                recipient = _primary_recipient(msg)
                body_text = _clean_sent_body(msg)
                subject = msg.get("subject") or ""

                # learned_tone aus der NEUESTEN Antwort je Kontakt (Liste ist
                # neueste-zuerst) -- unabhaengig davon, ob das Beispiel neu ist.
                if recipient and recipient not in tone_seen:
                    tone = extract_salutation_signature(body_text)
                    if tone:
                        await update_learned_tone(db, email=recipient, tone=tone, user_id=principal)
                    tone_seen.add(recipient)

                if gid in known or _is_noise(subject, body_text, recipient):
                    continue
                if len(body_text) > 2000:
                    body_text = body_text[:2000].rstrip() + " […]"

                vec = await embed_text(body_text)
                emb_literal = to_pgvector(vec) if vec else None
                await db.execute(
                    text(
                        """
                        INSERT INTO sent_mail_examples
                            (user_id, graph_id, recipient, subject, body_text, sent_at, language, embedding)
                        VALUES
                            (:uid, :gid, :rec, :subj, :body,
                             CAST(:sent_at AS timestamptz), :lang, CAST(:emb AS vector))
                        ON CONFLICT (user_id, graph_id) DO NOTHING
                        """
                    ),
                    {
                        "uid": principal,
                        "gid": gid,
                        "rec": recipient,
                        "subj": subject[:500],
                        "body": body_text,
                        "sent_at": _parse_graph_dt(msg.get("sentDateTime")),
                        "lang": _detect_language(body_text),
                        "emb": emb_literal,
                    },
                )
                inserted += 1
            await db.commit()
        logger.info("Style-Store-Sync: %d neue Beispiele (von %d geladen)", inserted, len(messages))
        return inserted
    except Exception:  # noqa: BLE001 - best-effort, darf Scheduler nie stoppen
        logger.exception("Style-Store-Sync fehlgeschlagen")
        return inserted
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def find_style_anchors(
    db: AsyncSession,
    *,
    query_text: str,
    recipient: str | None = None,
    k: int = 3,
    user_id=None,
) -> list[dict]:
    """Hybrid-Retrieval der besten Stil-Anker fuer einen neuen Entwurf.

    Zuerst (bis zu 1) die neueste eigene Antwort an DENSELBEN Empfaenger, dann die
    semantisch aehnlichsten ueber ALLE Kontakte (pgvector Cosine). Dedupliziert und
    auf ``k`` begrenzt. Best-effort: leere Liste bei Fehler/ohne Embedding. Wird ein
    ``user_id`` uebergeben, bleiben die Anker auf diesen Principal beschraenkt.
    """
    if k <= 0:
        return []
    scope = "AND user_id = :uid" if user_id is not None else ""
    results: list[dict] = []
    seen: set[str] = set()
    try:
        # 1) Kontakt-spezifisch (staerkstes Signal), neueste zuerst.
        if recipient:
            rows = await db.execute(
                text(
                    f"""
                    SELECT graph_id, recipient, subject, body_text
                    FROM sent_mail_examples
                    WHERE recipient = :rec AND length(body_text) >= :minlen {scope}
                    ORDER BY sent_at DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"rec": recipient.lower(), "minlen": _MIN_BODY_CHARS, "uid": user_id},
            )
            for r in rows.mappings():
                gid = r["graph_id"]
                if gid not in seen:
                    seen.add(gid)
                    results.append(dict(r))

        # 2) Semantisch aehnlichste ueber alle Kontakte.
        vec = await embed_text(query_text, is_query=True)
        if vec:
            remaining = max(0, k - len(results)) + 2  # Puffer fuer Dedup
            rows = await db.execute(
                text(
                    f"""
                    SELECT graph_id, recipient, subject, body_text,
                           1 - (embedding <=> CAST(:emb AS vector)) AS similarity
                    FROM sent_mail_examples
                    WHERE embedding IS NOT NULL AND length(body_text) >= :minlen {scope}
                    ORDER BY embedding <=> CAST(:emb AS vector)
                    LIMIT :lim
                    """
                ),
                {"emb": to_pgvector(vec), "minlen": _MIN_BODY_CHARS, "lim": remaining, "uid": user_id},
            )
            for r in rows.mappings():
                gid = r["graph_id"]
                if gid in seen:
                    continue
                seen.add(gid)
                results.append(dict(r))
                if len(results) >= k:
                    break
        return results[:k]
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("find_style_anchors fehlgeschlagen")
        return results[:k]
