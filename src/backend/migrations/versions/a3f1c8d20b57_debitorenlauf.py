"""Debitoren-Monatslauf: Entscheidungen und vollzogene Schritte festhalten

Revision ID: a3f1c8d20b57
Revises: c1a5e37b90d4
Create Date: 2026-09-21

Der Lauf hält **keine** Zahlen und keinen Prüfzustand. Ob eine Rechnung
stimmt, wird bei jedem Aufruf neu aus Bexio und Toggl gelesen. Gespeichert
wird nur, was sonst nirgends eine Spur hinterlässt: die Entscheidungen des
Menschen und die Schritte, die sich nicht wiederholen dürfen.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3f1c8d20b57"
down_revision = "c1a5e37b90d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "debitorenlaeufe",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("jahr", sa.Integer(), nullable=False),
        sa.Column("monat", sa.Integer(), nullable=False),
        sa.Column("stichtag", sa.Date(), nullable=False),
        sa.Column("abgeschlossen_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notiz", sa.Text(), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint("monat BETWEEN 1 AND 12", name="ck_debitorenlauf_monat"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jahr", "monat", name="uq_debitorenlauf_periode"),
    )

    op.create_table(
        "debitorenlauf_rechnungen",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("lauf_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rechnung_id", sa.Integer(), nullable=False),
        sa.Column("nummer", sa.Text(), nullable=True),
        sa.Column("zurueckgestellt", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("grund", sa.Text(), nullable=True),
        sa.Column("dokumente_erzeugt_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("mailentwurf_id", sa.Text(), nullable=True),
        sa.Column("versendet_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("abgelegt_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["lauf_id"], ["debitorenlaeufe.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lauf_id", "rechnung_id", name="uq_debitorenlauf_rechnung"),
    )
    op.create_index(
        "idx_debitorenlauf_rechnungen_lauf", "debitorenlauf_rechnungen", ["lauf_id"]
    )

    for tabelle in ("debitorenlaeufe", "debitorenlauf_rechnungen"):
        op.execute(
            f"CREATE TRIGGER {tabelle}_updated_at BEFORE UPDATE ON {tabelle} "
            "FOR EACH ROW EXECUTE FUNCTION update_updated_at()"
        )


def downgrade() -> None:
    op.drop_index("idx_debitorenlauf_rechnungen_lauf", table_name="debitorenlauf_rechnungen")
    op.drop_table("debitorenlauf_rechnungen")
    op.drop_table("debitorenlaeufe")
