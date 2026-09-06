"""Aenderungsmass an der Rueckmeldung

``agent_feedback`` unterschied bisher nur ``approved_clean`` von ``draft_edit``.
Nachgerechnet an allen 29 damals erfassten Bearbeitungen ist das eine Zahl zu
wenig: vier lagen unter 0.15 -- ein Wort, eine Anrede -- und vierzehn ab 0.80,
also praktisch neu geschrieben. Unter einem gemeinsamen Etikett gelesen sah das
aus wie durchgaengige Politur.

``change_ratio`` haelt die Zahl fest. Die Deutung bleibt draussen: gespeichert
wird gemessen, nicht eingeteilt.

Revision ID: c1a5e37b90d4
Revises: b7d2e4f16a03
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa

revision = "c1a5e37b90d4"
down_revision = "b7d2e4f16a03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_feedback", sa.Column("change_ratio", sa.REAL(), nullable=True))
    op.create_check_constraint(
        "agent_feedback_change_ratio_check",
        "agent_feedback",
        "change_ratio IS NULL OR change_ratio BETWEEN 0 AND 1",
    )


def downgrade() -> None:
    op.drop_constraint("agent_feedback_change_ratio_check", "agent_feedback", type_="check")
    op.drop_column("agent_feedback", "change_ratio")
