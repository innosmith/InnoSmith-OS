"""add_recurrence_last_spawn

Revision ID: d8e9f0a1b2c3
Revises: c7e1a2b3d4f5
Create Date: 2026-08-02 15:30:00.000000

Neue Spalte tasks.recurrence_last_spawn: persistiert die zuletzt gespawnte
Cron-Okkurrenz auf der Vorlage. Verhindert Respawn nach Instanz-Löschung.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7e1a2b3d4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("recurrence_last_spawn", sa.Date(), nullable=True),
    )
    # Backfill: bestehende Vorlagen erhalten max(due_date) ihrer Instanzen,
    # damit der Scheduler keine bereits erledigten Okkurrenzen nachholt.
    op.execute(
        """
        UPDATE tasks t
        SET recurrence_last_spawn = (
            SELECT max(i.due_date)
            FROM tasks i
            WHERE i.template_id = t.id
        )
        WHERE t.recurrence_rule IS NOT NULL
          AND t.template_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("tasks", "recurrence_last_spawn")
