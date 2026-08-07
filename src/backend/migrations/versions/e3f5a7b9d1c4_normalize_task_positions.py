"""normalize_task_positions

Revision ID: e3f5a7b9d1c4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-07 09:40:00.000000

Reine Datenbereinigung: board_position und pipeline_position werden pro Spalte
lückenlos mit 1..N neu durchnummeriert.

Hintergrund: Drag & Drop hat bisher nur den gezogenen Task aktualisiert -- und
zwar mit einem 0-basierten Array-Index, während alle anderen Schreibpfade
(create_task, recurring, pipeline_promoter) Float-Werte ab 1.0 vergeben. So
entstanden doppelte Positionswerte, bei denen PostgreSQL keine definierte
Reihenfolge garantiert; die manuelle Arbeitsreihenfolge sprang beim Neuladen.
Ohne diesen Schritt bestünden die Kollisionen so lange, bis in jede Spalte
einmal etwas gezogen wird.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e3f5a7b9d1c4"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Bestehende Reihenfolge bleibt erhalten (ORDER BY position), nur die Werte
    # werden vereindeutigt. created_at entscheidet dort, wo Positionen kollidieren.
    op.execute(
        """
        UPDATE tasks t
        SET board_position = r.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY board_column_id
                ORDER BY board_position, created_at
            ) AS rn
            FROM tasks
        ) r
        WHERE t.id = r.id
          AND t.board_position <> r.rn
        """
    )
    op.execute(
        """
        UPDATE tasks t
        SET pipeline_position = r.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY pipeline_column_id
                ORDER BY pipeline_position NULLS LAST, created_at
            ) AS rn
            FROM tasks
            WHERE pipeline_column_id IS NOT NULL
        ) r
        WHERE t.id = r.id
          AND (t.pipeline_position IS NULL OR t.pipeline_position <> r.rn)
        """
    )


def downgrade() -> None:
    # Die alten (teils kollidierenden) Positionswerte sind nicht rekonstruierbar
    # und für die Anzeige auch bedeutungslos -- bewusst kein Rollback.
    pass
