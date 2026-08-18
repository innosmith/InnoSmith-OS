"""repair_completed_recurrence_templates

Revision ID: f2c4a6e8b0d1
Revises: e3f5a7b9d1c4
Create Date: 2026-08-18 14:20:00.000000

Reparatur-Migration ohne Schema-Änderung.

Abgehakte Vorlagen (`is_completed = true`) verschwanden aus dem Projekt-Board,
während der Scheduler weiterhin Instanzen erzeugte — die Serie war damit aktiv,
aber über die UI nicht mehr erreichbar. Fälligkeitsdaten auf Vorlagen sind
ebenfalls bedeutungslos (der Cron-Ausdruck steuert die Termine) und führen zu
falschen «überfällig»-Markierungen.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f2c4a6e8b0d1"
down_revision: Union[str, Sequence[str], None] = "e3f5a7b9d1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE tasks
        SET is_completed = false,
            due_date = NULL,
            updated_at = now()
        WHERE recurrence_rule IS NOT NULL
          AND template_id IS NULL
          AND (is_completed = true OR due_date IS NOT NULL)
        """
    )


def downgrade() -> None:
    # Der ursprüngliche Zustand war ein Datenfehler — keine Wiederherstellung.
    pass
