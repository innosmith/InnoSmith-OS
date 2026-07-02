"""followup_suggestions: 'skipped' als gültigen Status zulassen

Revision ID: f1a2b3c4d5e6
Revises: e2a4c6b8d0f2
Create Date: 2026-07-02 22:45:00.000000

Der LLM-Gate der Follow-up-Erkennung merkt bewusst übersprungene Konversationen
(keine offene Frage/Bitte) dauerhaft als 'skipped', damit sie nicht erneut
bewertet werden. Der bisherige CHECK-Constraint erlaubte nur
('suggested', 'answered').
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e2a4c6b8d0f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE followup_suggestions DROP CONSTRAINT IF EXISTS followup_suggestions_status_check")
    op.execute(
        "ALTER TABLE followup_suggestions ADD CONSTRAINT followup_suggestions_status_check "
        "CHECK (status IN ('suggested', 'answered', 'skipped'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE followup_suggestions DROP CONSTRAINT IF EXISTS followup_suggestions_status_check")
    op.execute(
        "ALTER TABLE followup_suggestions ADD CONSTRAINT followup_suggestions_status_check "
        "CHECK (status IN ('suggested', 'answered'))"
    )
