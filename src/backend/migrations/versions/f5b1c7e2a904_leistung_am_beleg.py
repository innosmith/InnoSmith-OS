"""Kreditorenbeleg: die Leistung, wenn sie an der Rechnung hängt

Revision ID: f5b1c7e2a904
Revises: e8c2d4f61a37
Create Date: 2026-09-25

Buchungstext und Dateiname folgen seit dem 25.09.2026 einer Norm, und beide
tragen die Leistung. Meist steht sie am Lieferanten (``leistung`` in
``docs/kreditorenlieferanten.yaml``). Bei Hosttech und Metanet nicht: jede
Rechnung nennt ihre Domain. Dann wird sie am Beleg entschieden und steht hier.
"""

import sqlalchemy as sa
from alembic import op

revision = "f5b1c7e2a904"
down_revision = "e8c2d4f61a37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kreditorenbelege", sa.Column("leistung", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("kreditorenbelege", "leistung")
