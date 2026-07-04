"""add semantic_documents (semantischer Such-Index E-Mails + Dokumente)

Revision ID: f9a1b2c3d4e5
Revises: a2b3c4d5e6f7
Create Date: 2026-07-04 14:20:00.000000

User-facing semantischer Such-Index ueber E-Mails und Dokumente (pgvector).
Eigenes, staerkeres Embedding-Modell (Qwen3-Embedding-4B, native 2560d) als der
Agent-Index. Speichertyp ``halfvec(2560)`` (fp16): bei 2560d ist float32-HNSW
technisch nicht moeglich (pgvector-Cap 2000d; halfvec hebt auf 4000d), der
Recall-Verlust liegt nachweislich < 0.5 %. ``content_tsv`` (generiert) traegt den
lexikalen Hybrid-Anteil. Benoetigt pgvector >= 0.7 (halfvec/HNSW halfvec_ops).
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f9a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_documents (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_type        TEXT NOT NULL
                               CHECK (source_type IN ('email', 'onedrive', 'upload', 'transcript')),
            source_id          TEXT NOT NULL,
            chunk_index        INT NOT NULL DEFAULT 0,
            title              TEXT,
            content_text       TEXT NOT NULL,
            url                TEXT,
            mime               TEXT,
            metadata           JSONB DEFAULT '{}'::jsonb,
            embedding          halfvec(2560),
            content_tsv        tsvector GENERATED ALWAYS AS (
                                   to_tsvector('german',
                                       coalesce(title, '') || ' ' || coalesce(content_text, ''))
                               ) STORED,
            source_modified_at TIMESTAMPTZ,
            indexed_at         TIMESTAMPTZ DEFAULT now(),
            UNIQUE (source_type, source_id, chunk_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_semantic_documents_source "
        "ON semantic_documents(source_type, source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_semantic_documents_modified "
        "ON semantic_documents(source_modified_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_semantic_documents_embedding "
        "ON semantic_documents USING hnsw (embedding halfvec_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_semantic_documents_tsv "
        "ON semantic_documents USING gin (content_tsv)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS semantic_documents")
