"""add index_status (Laufstatus des semantischen Indexers)

Revision ID: b3d5f7a9c1e2
Revises: f9a1b2c3d4e5
Create Date: 2026-07-06 11:05:00.000000

Ein-Zeilen-Tabelle (Singleton, id=1), in die der Indexer-Daemon seinen aktuellen
Laufstatus schreibt (state/phase/detail, Fortschrittszaehler, Ergebnis des letzten
Laufs, letzter Fehler, Heartbeat). Genau EIN Writer (der Daemon), das Backend liest
nur -- so wird der Verarbeitungsstatus im Suchindex-Tab transparent, ohne die
getrennten Prozesse zu koppeln.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b3d5f7a9c1e2'
down_revision: Union[str, Sequence[str], None] = 'f9a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS index_status (
            id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            state            TEXT NOT NULL DEFAULT 'idle'
                             CHECK (state IN ('idle', 'running')),
            phase            TEXT,
            detail           TEXT,
            run_started_at   TIMESTAMPTZ,
            run_finished_at  TIMESTAMPTZ,
            heartbeat_at     TIMESTAMPTZ,
            folders_total    INT NOT NULL DEFAULT 0,
            folders_done     INT NOT NULL DEFAULT 0,
            docs_total       INT NOT NULL DEFAULT 0,
            docs_done        INT NOT NULL DEFAULT 0,
            last_emails      INT NOT NULL DEFAULT 0,
            last_documents   INT NOT NULL DEFAULT 0,
            last_chunks      INT NOT NULL DEFAULT 0,
            last_error       TEXT,
            last_error_at    TIMESTAMPTZ,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Singleton-Zeile anlegen, damit das Backend immer eine Zeile lesen kann.
    op.execute("INSERT INTO index_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS index_status")
