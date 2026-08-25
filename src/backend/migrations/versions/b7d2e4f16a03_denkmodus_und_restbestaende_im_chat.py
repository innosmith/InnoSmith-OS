"""Denkmodus, Gedankengang und Restbestaende im Chat

Drei Spalten, drei Gruende:

``llm_conversations.thinking_mode`` -- der Denkmodus war bisher keine Wahl,
sondern ein Zustand: immer an. Vorgabe ``lang`` haelt das bisherige Verhalten
fuer alle bestehenden Unterhaltungen.

``llm_messages.thinking`` -- der Gedankengang lebte nur im SSE-Strom. Die
Oberflaeche zeigte ihn und beim Neuladen war er fort.

``llm_messages.residuals`` -- erfundene Namen, die die Rueckbildung nach einem
Cloud-Lauf ueberlebt haben. Sie sehen echt aus und sind es nicht.

Revision ID: b7d2e4f16a03
Revises: a4b6c8d0e2f4
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7d2e4f16a03"
down_revision = "a4b6c8d0e2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_conversations",
        sa.Column(
            "thinking_mode",
            sa.Text(),
            nullable=False,
            server_default="lang",
        ),
    )
    op.create_check_constraint(
        "llm_conversations_thinking_mode_check",
        "llm_conversations",
        "thinking_mode IN ('aus', 'kurz', 'lang')",
    )
    op.add_column("llm_messages", sa.Column("thinking", sa.Text(), nullable=True))
    op.add_column(
        "llm_messages",
        sa.Column(
            "residuals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_messages", "residuals")
    op.drop_column("llm_messages", "thinking")
    op.drop_constraint(
        "llm_conversations_thinking_mode_check", "llm_conversations", type_="check"
    )
    op.drop_column("llm_conversations", "thinking_mode")
