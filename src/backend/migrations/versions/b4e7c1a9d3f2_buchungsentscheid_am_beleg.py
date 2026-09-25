"""Kreditorenbeleg: wohin gebucht wird, und wer das entschieden hat

Revision ID: b4e7c1a9d3f2
Revises: d5b9f2a41c68
Create Date: 2026-09-22

Das Register hielt bisher Identität, Ort und Zustand -- aber keinen Ort für die
Buchungsentscheidung. Das Modul hat kein Kontofeld, und die Deklaration in
``docs/kreditorenlieferanten.yaml`` gilt je Lieferant, nicht je Rechnung. Bei
15 der 149 Lieferanten geht das nicht auf: Hosttech verteilt sich auf 6512 und
4200, Digitec auf 6571 und 6500. Dort steht in der Deklaration bewusst kein
Konto, sondern eine Kandidatenliste -- und die Entscheidung braucht eine Zeile,
in der sie stehen kann.

``sollkonto_herkunft`` trennt den übernommenen Vorschlag von der getroffenen
Entscheidung. Ohne diese Spalte sähen beide gleich aus, und damit wäre weder
eine Abweichung als Frage erkennbar noch die Deklaration je fortschreibbar.
"""

import sqlalchemy as sa
from alembic import op

revision = "b4e7c1a9d3f2"
down_revision = "d5b9f2a41c68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kreditorenbelege", sa.Column("sollkonto", sa.Text(), nullable=True))
    op.add_column(
        "kreditorenbelege", sa.Column("sollkonto_herkunft", sa.Text(), nullable=True)
    )
    op.add_column(
        "kreditorenbelege", sa.Column("steuerbehandlung", sa.Text(), nullable=True)
    )
    op.add_column("kreditorenbelege", sa.Column("zahlweg", sa.Text(), nullable=True))

    # Dasselbe Vokabular wie die Deklaration. Eine Prüfregel statt einer
    # Konvention, weil ein Tippfehler im Wert sonst erst beim Buchen auffällt --
    # also dort, wo Geld fliesst.
    op.create_check_constraint(
        "ck_kreditorenbeleg_sollkonto_herkunft",
        "kreditorenbelege",
        "sollkonto_herkunft IS NULL OR sollkonto_herkunft IN ('vorschlag', 'entscheid')",
    )
    op.create_check_constraint(
        "ck_kreditorenbeleg_steuerbehandlung",
        "kreditorenbelege",
        "steuerbehandlung IS NULL OR steuerbehandlung IN "
        "('bezugssteuer', 'inland_mwst', 'ohne_mwst', 'unbekannt')",
    )
    op.create_check_constraint(
        "ck_kreditorenbeleg_zahlweg",
        "kreditorenbelege",
        "zahlweg IS NULL OR zahlweg IN ('karte', 'rechnung', 'bank_direkt')",
    )

    # Freigegeben ohne Konto wäre eine Freigabe, die beim Buchen scheitert --
    # der Beleg sähe erledigt aus und wäre es nicht. Die Prüfung sitzt in der
    # Datenbank, weil sie für jeden Schreibweg gelten muss, auch für den, der
    # später dazukommt.
    op.create_check_constraint(
        "ck_kreditorenbeleg_freigabe_braucht_konto",
        "kreditorenbelege",
        "freigegeben_am IS NULL OR sollkonto IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_kreditorenbeleg_freigabe_braucht_konto", "kreditorenbelege")
    op.drop_constraint("ck_kreditorenbeleg_zahlweg", "kreditorenbelege")
    op.drop_constraint("ck_kreditorenbeleg_steuerbehandlung", "kreditorenbelege")
    op.drop_constraint("ck_kreditorenbeleg_sollkonto_herkunft", "kreditorenbelege")
    op.drop_column("kreditorenbelege", "zahlweg")
    op.drop_column("kreditorenbelege", "steuerbehandlung")
    op.drop_column("kreditorenbelege", "sollkonto")
    op.drop_column("kreditorenbelege", "sollkonto_herkunft")
