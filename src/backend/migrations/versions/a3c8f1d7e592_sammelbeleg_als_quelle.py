"""Kreditorenbeleg: der Sammelbeleg als eigene Quelle

Revision ID: a3c8f1d7e592
Revises: f5b1c7e2a904
Create Date: 2026-09-25

Den Cursor-Monatssammelbeleg erzeugt TaskPilot seit dem 25.09.2026 selbst,
statt ihn wie K4 als Datei in den Eingang zu legen. Er kommt durch keine der
vier Türen herein und braucht deshalb eine fünfte: ``erzeugt``.
"""

from alembic import op

revision = "a3c8f1d7e592"
down_revision = "f5b1c7e2a904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_kreditorenbeleg_quelle", "kreditorenbelege", type_="check")
    op.create_check_constraint(
        "ck_kreditorenbeleg_quelle",
        "kreditorenbelege",
        "quelle IN ('autodownload', 'ablage_hand', 'postfach', 'upload', 'erzeugt')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_kreditorenbeleg_quelle", "kreditorenbelege", type_="check")
    op.create_check_constraint(
        "ck_kreditorenbeleg_quelle",
        "kreditorenbelege",
        "quelle IN ('autodownload', 'ablage_hand', 'postfach', 'upload')",
    )
