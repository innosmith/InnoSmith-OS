"""Kreditorenbeleg: die Rechnungsnummer als zweiter Wächter gegen Doppel

Revision ID: e8c2d4f61a37
Revises: b4e7c1a9d3f2
Create Date: 2026-09-25

Der Hash erkennt dieselbe Datei, aber nicht dieselbe Rechnung. Gemessen am
25.09.2026 im Archiv: für den 02.09. liegen neun Cursor-Dateien, aber nur vier
Rechnungen (04CDDAC1-0169 bis -0172) -- und zwei davon je zweimal mit
**verschiedenem** Hash. Ein Anbieter, der dasselbe PDF bei jedem Abruf neu
erzeugt, liefert andere Bytes für dieselbe Rechnung. Im Eingang lagen dazu drei
Kopien von OpenAI 2DD42E43-0001 mit drei Hashes.

Die Nummer gilt nur zusammen mit dem Lieferanten: zwei Lieferanten dürfen
dieselbe Nummer vergeben. Fehlt eines von beiden, greift allein der Hash --
geraten wird nicht.
"""

import sqlalchemy as sa
from alembic import op

revision = "e8c2d4f61a37"
down_revision = "b4e7c1a9d3f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kreditorenbelege", sa.Column("rechnungsnummer", sa.Text(), nullable=True)
    )
    # In der Datenbank und nicht nur im Dienst, weil die Regel für jeden
    # Schreibweg gelten muss -- auch für die spätere Zuordnung von Hand.
    op.create_index(
        "uq_kreditorenbeleg_rechnung",
        "kreditorenbelege",
        ["lieferant_schluessel", "rechnungsnummer"],
        unique=True,
        postgresql_where=sa.text(
            "lieferant_schluessel IS NOT NULL AND rechnungsnummer IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_kreditorenbeleg_rechnung", table_name="kreditorenbelege")
    op.drop_column("kreditorenbelege", "rechnungsnummer")
