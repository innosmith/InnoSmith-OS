"""Query-Seite der semantischen Suche: Hybrid-Retrieval über ``semantic_documents``.

``hybrid_search`` verbindet zwei Ranglisten und fusioniert sie via Reciprocal Rank
Fusion (RRF):
- **Semantik**: lokales Query-Embedding (Qwen3-Embedding-4B) + pgvector-Cosine.
  Der getroffene Chunk IST die Vorschau-Passage.
- **Keyword**: PostgreSQL Volltext (``websearch_to_tsquery`` + ``ts_rank_cd``) mit
  ``ts_headline`` als hervorgehobenem Snippet -- vollständig lokal, kein Graph/Auth.

``mode``:
  - ``hybrid`` (Default): beide Ranglisten, RRF-fusioniert.
  - ``semantic``: nur Vektor.
  - ``exact``: nur Keyword.

Ergebnisse sind pro Quelle (``source_type``/``source_id``) dedupliziert -- der beste
Chunk repräsentiert das Dokument/die E-Mail. Ein optionaler Cross-Encoder-Reranker
(``search_reranker_enabled``) läuft nur auf dem tiefen/agentischen Pfad, nie im
interaktiven UI-Pfad. Best-effort: leere Liste statt Exceptions.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.embeddings import SEARCH_QUERY_INSTRUCT, embed_text, to_pgvector

logger = logging.getLogger("taskpilot.semantic_search")

# RRF-Konstante (Standardwert aus der Literatur). Dämpft den Einfluss sehr hoher
# Einzelränge und macht die Fusion robust gegen unterschiedliche Score-Skalen.
_RRF_K = 60
_SNIPPET_CHARS = 260


def _clip(s: str | None, n: int = _SNIPPET_CHARS) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n].rstrip() + " …"


async def _semantic_candidates(
    db: AsyncSession, query: str, sources: list[str] | None, limit: int
) -> list[dict]:
    cfg = get_settings()
    vec = await embed_text(
        query, is_query=True, model=cfg.search_embed_model,
        dim=cfg.search_embed_dim, instruct=SEARCH_QUERY_INSTRUCT,
    )
    if not vec:
        return []
    src_filter = "AND source_type = ANY(:sources)" if sources else ""
    rows = await db.execute(
        text(
            f"""
            SELECT source_type, source_id, title, url, mime, content_text, chunk_index,
                   1 - (embedding <=> CAST(:emb AS halfvec)) AS similarity
            FROM semantic_documents
            WHERE embedding IS NOT NULL {src_filter}
            ORDER BY embedding <=> CAST(:emb AS halfvec)
            LIMIT :lim
            """
        ),
        {"emb": to_pgvector(vec), "sources": sources, "lim": limit},
    )
    out: list[dict] = []
    for r in rows.mappings():
        out.append({
            "source_type": r["source_type"],
            "source_id": r["source_id"],
            "title": r["title"],
            "url": r["url"],
            "mime": r["mime"],
            "chunk_index": r["chunk_index"],
            "snippet": _clip(r["content_text"]),
            "similarity": float(r["similarity"]) if r["similarity"] is not None else None,
        })
    return out


async def _keyword_candidates(
    db: AsyncSession, query: str, sources: list[str] | None, limit: int
) -> list[dict]:
    src_filter = "AND source_type = ANY(:sources)" if sources else ""
    rows = await db.execute(
        text(
            f"""
            SELECT source_type, source_id, title, url, mime, chunk_index,
                   ts_rank_cd(content_tsv, q) AS rank,
                   ts_headline('german', content_text, q,
                       'MaxFragments=2,MinWords=5,MaxWords=22,StartSel=<b>,StopSel=</b>'
                   ) AS headline
            FROM semantic_documents,
                 websearch_to_tsquery('german', :q) q
            WHERE content_tsv @@ q {src_filter}
            ORDER BY rank DESC
            LIMIT :lim
            """
        ),
        {"q": query, "sources": sources, "lim": limit},
    )
    out: list[dict] = []
    for r in rows.mappings():
        headline = (r["headline"] or "").replace("<b>", "").replace("</b>", "")
        out.append({
            "source_type": r["source_type"],
            "source_id": r["source_id"],
            "title": r["title"],
            "url": r["url"],
            "mime": r["mime"],
            "chunk_index": r["chunk_index"],
            "snippet": _clip(headline),
            "rank": float(r["rank"]) if r["rank"] is not None else None,
        })
    return out


def _dedupe_by_source(items: list[dict]) -> list[dict]:
    """Behält pro (source_type, source_id) den erstplatzierten (besten) Treffer."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in items:
        key = (it["source_type"], it["source_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def rrf_fuse(semantic: list[dict], keyword: list[dict], k: int = _RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion zweier (bereits sortierter) Ranglisten.

    Score je Quelle = Σ 1/(k + rang) über beide Listen (rang 0-basiert). Rein und
    deterministisch -> gut testbar. Merged Metadaten bevorzugt vom semantischen
    Treffer (bessere Passagen-Preview), Keyword-Snippet als Fallback.
    """
    scores: dict[tuple[str, str], float] = {}
    merged: dict[tuple[str, str], dict] = {}
    for rank, it in enumerate(semantic):
        key = (it["source_type"], it["source_id"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        entry = merged.setdefault(key, dict(it))
        entry.setdefault("matched_keyword", False)
    for rank, it in enumerate(keyword):
        key = (it["source_type"], it["source_id"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in merged:
            merged[key] = dict(it)
        elif not merged[key].get("snippet"):
            merged[key]["snippet"] = it.get("snippet")
        # Keyword-Deckung ist das verlaessliche Relevanzsignal (Begriff steht wirklich
        # im Dokument) -- unabhaengig vom semantischen Rang markieren.
        merged[key]["matched_keyword"] = True
    ranked = sorted(merged.values(), key=lambda it: scores[(it["source_type"], it["source_id"])], reverse=True)
    for it in ranked:
        it["score"] = round(scores[(it["source_type"], it["source_id"])], 6)
    return ranked


async def _maybe_rerank(query: str, items: list[dict], enable: bool) -> list[dict]:
    """Optionaler Cross-Encoder-Rerank (Qwen3-Reranker). No-op, wenn deaktiviert.

    Bewusst konservativ: Ist der Reranker nicht verfügbar, wird die Fusions-
    Reihenfolge unverändert zurückgegeben (kein harter Fehler).
    """
    cfg = get_settings()
    if not enable or not cfg.search_reranker_enabled or not items:
        return items
    # Platzhalter: Reranker-Modell muss lokal vorliegen. Bis dahin no-op.
    logger.info("Reranker aktiviert, aber noch nicht implementiert -- Fusion unverändert")
    return items


async def hybrid_search(
    db: AsyncSession,
    query: str,
    *,
    sources: list[str] | None = None,
    k: int | None = None,
    mode: str = "hybrid",
    rerank: bool = False,
) -> list[dict]:
    """Führt die (Hybrid-)Suche aus und gibt bis zu ``k`` deduplizierte Treffer zurück."""
    cfg = get_settings()
    q = (query or "").strip()
    if not q:
        return []
    k = k or cfg.search_semantic_k
    cand = max(k, cfg.search_candidate_k)
    try:
        if mode == "semantic":
            results = _dedupe_by_source(await _semantic_candidates(db, q, sources, cand))
            for r in results:
                r["matched_keyword"] = False
        elif mode == "exact":
            results = _dedupe_by_source(await _keyword_candidates(db, q, sources, cand))
            for r in results:
                r["matched_keyword"] = True
        else:  # hybrid
            sem = _dedupe_by_source(await _semantic_candidates(db, q, sources, cand))
            kw = _dedupe_by_source(await _keyword_candidates(db, q, sources, cand))
            results = rrf_fuse(sem, kw)
        results = await _maybe_rerank(q, results, rerank)
        return results[:k]
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("hybrid_search fehlgeschlagen (mode=%s)", mode)
        return []
