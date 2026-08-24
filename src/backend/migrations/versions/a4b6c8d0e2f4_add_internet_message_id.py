"""add_internet_message_id

Revision ID: a4b6c8d0e2f4
Revises: f2c4a6e8b0d1
Create Date: 2026-08-24 11:40:00.000000

Die Graph-``id`` einer Nachricht ist ein Handle, keine Identitaet: sie aendert sich
bei **jedem** Ordnerwechsel. Bisher speicherte TaskPilot ausschliesslich dieses
Handle, und kein Codepfad schrieb nach einem Move die neue ID zurueck. Sobald ein
Task bestaetigt und die Quell-Mail archiviert wurde, zeigte ``tasks.email_message_id``
darum ins Leere -- samt dem Outlook-Deeplink im Task-Detail.

``internetMessageId`` (RFC 5322) bleibt ueber Moves hinweg gleich. Diese Migration
legt die Spalte an; das Nachladen fuer den Bestand erfolgt zur Laufzeit
(best-effort), weil es Graph-Zugriffe braucht, die in einer Migration nichts zu
suchen haben.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b6c8d0e2f4"
down_revision: Union[str, Sequence[str], None] = "f2c4a6e8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("internet_message_id", sa.Text(), nullable=True))
    op.add_column(
        "email_triage", sa.Column("internet_message_id", sa.Text(), nullable=True)
    )
    op.create_index(
        "idx_tasks_internet_message_id", "tasks", ["internet_message_id"]
    )
    op.create_index(
        "idx_email_triage_internet_message_id",
        "email_triage",
        ["internet_message_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_email_triage_internet_message_id", table_name="email_triage")
    op.drop_index("idx_tasks_internet_message_id", table_name="tasks")
    op.drop_column("email_triage", "internet_message_id")
    op.drop_column("tasks", "internet_message_id")
