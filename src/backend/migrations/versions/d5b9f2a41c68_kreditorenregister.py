"""Kreditorenregister: Identität, Ort und Entscheidungen eines eingehenden Belegs

Revision ID: d5b9f2a41c68
Revises: a3f1c8d20b57
Create Date: 2026-09-21

Das Register hält **keine** Beträge. Betrag, Währung, Positionen und
Leistungszeitraum leben im InvoiceInsight-Modul, die Buchung in Bexio; hier
steht, was sonst nirgends eine Spur hinterlässt.

Der Hash ist die Identität, der Pfad ein Handle. Am 21.09.2026 an der
Moduldatenbank gemessen: sie erkennt Dokumente am Pfad und lässt den Hash
ungenutzt — vier Rechnungen sind dadurch doppelt erfasst. Verschiebt TaskPilot
eine Datei ins Archiv, ändert sich der Pfad; wäre er die Identität, zählte ab
der Ablage jede Auswertung doppelt.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d5b9f2a41c68"
down_revision = "a3f1c8d20b57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kreditorenbelege",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("datei_hash", sa.Text(), nullable=False),
        sa.Column("dateiname", sa.Text(), nullable=False),
        sa.Column("graph_item_id", sa.Text(), nullable=True),
        sa.Column("graph_pfad", sa.Text(), nullable=True),
        sa.Column("archiv_pfad", sa.Text(), nullable=True),
        sa.Column("quelle", sa.Text(), nullable=False),
        sa.Column("belegart", sa.Text(), server_default="rechnung", nullable=False),
        sa.Column("eingang_am", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("lieferant_schluessel", sa.Text(), nullable=True),
        sa.Column("modul_dokument_id", sa.Integer(), nullable=True),
        sa.Column("zurueckgestellt", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("grund", sa.Text(), nullable=True),
        sa.Column("freigegeben_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("freigegeben_von", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("gebucht_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("bexio_referenz", sa.Text(), nullable=True),
        sa.Column("abgelegt_am", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sammelbeleg_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.CheckConstraint(
            "quelle IN ('autodownload', 'ablage_hand', 'postfach', 'upload')",
            name="ck_kreditorenbeleg_quelle",
        ),
        sa.CheckConstraint(
            "belegart IN ('rechnung', 'spese', 'sammelbeleg')",
            name="ck_kreditorenbeleg_belegart",
        ),
        sa.ForeignKeyConstraint(["freigegeben_von"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sammelbeleg_id"], ["kreditorenbelege.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datei_hash", name="uq_kreditorenbeleg_hash"),
    )

    # Die Warteliste ist der häufigste Zugriff: was offen ist, steht ohne Freigabe da.
    op.execute(
        "CREATE INDEX idx_kreditorenbelege_offen ON kreditorenbelege(eingang_am) "
        "WHERE freigegeben_am IS NULL"
    )
    op.create_index(
        "idx_kreditorenbelege_lieferant", "kreditorenbelege", ["lieferant_schluessel"]
    )
    op.execute(
        "CREATE INDEX idx_kreditorenbelege_sammelbeleg ON kreditorenbelege(sammelbeleg_id) "
        "WHERE sammelbeleg_id IS NOT NULL"
    )
    op.execute(
        "CREATE TRIGGER kreditorenbelege_updated_at BEFORE UPDATE ON kreditorenbelege "
        "FOR EACH ROW EXECUTE FUNCTION update_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS kreditorenbelege_updated_at ON kreditorenbelege")
    op.drop_table("kreditorenbelege")
