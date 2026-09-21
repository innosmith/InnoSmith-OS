"""Prueft das Register -- vor allem die Regel, an der das Modul heute scheitert.

Der tragende Test ist ``test_derselbe_inhalt_an_neuem_ort_bleibt_eine_zeile``:
er stellt genau den Vorgang nach, der im InvoiceInsight-Modul heute vier
doppelte Rechnungen erzeugt hat -- Datei wird verschoben, Pfad aendert sich,
und weil der Pfad die Identitaet war, entstand eine zweite Zeile.

Die Tests laufen gegen eine **echte** Postgres-Datenbank, weil genau das
geprueft wird, was nur dort gilt: die Eindeutigkeit auf dem Hash und das
Verhalten der Nebenbedingungen. Eine Attrappe wuerde beides bestaetigen, ohne
dass es stimmt.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from app.config import get_settings
from app.models.models import Kreditorenbeleg
from app.services import kreditorenregister as reg
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


def _url() -> str:
    s = get_settings()
    return (
        f"postgresql+asyncpg://{s.db_user}:{s.db_password}"
        f"@{s.db_host}:{s.db_port}/{s.db_name}"
    )


@pytest_asyncio.fixture
async def db():
    """Eine Sitzung, die am Ende alles zurückrollt — die Dev-DB bleibt sauber."""
    motor = create_async_engine(_url())
    try:
        async with motor.connect() as verbindung:
            transaktion = await verbindung.begin()
            sitzung = async_sessionmaker(bind=verbindung, expire_on_commit=False)()
            try:
                yield sitzung
            finally:
                await sitzung.close()
                await transaktion.rollback()
    finally:
        await motor.dispose()


def _hash(text_: str) -> str:
    return reg.hash_von(text_.encode())


class TestAufnahme:
    async def test_ein_neuer_beleg_wird_aufgenommen(self, db: AsyncSession):
        ergebnis = await reg.aufnehmen(
            db,
            datei_hash=_hash("cursor-september"),
            dateiname="Cursor Usage 20.09.2026 KK.pdf",
            quelle="autodownload",
            graph_pfad="Finanzen/Kreditoren/_OPEN/InnoSmith/autodownload/Cursor.pdf",
        )
        assert ergebnis.neu is True
        assert ergebnis.beleg.freigegeben_am is None
        assert ergebnis.beleg.belegart == "rechnung"

    async def test_derselbe_inhalt_an_neuem_ort_bleibt_eine_zeile(self, db: AsyncSession):
        """Der Vorgang, der im Modul heute vier Doppelzählungen erzeugt hat.

        Datei liegt im Eingang, wird verschoben, taucht unter neuem Pfad
        wieder auf. Eine Zeile, Ort nachgeführt — keine zweite Rechnung.
        """
        h = _hash("google-workspace-januar")
        erst = await reg.aufnehmen(
            db, datei_hash=h, dateiname="Google Workspace Abo 31.01.2026 KK.pdf",
            quelle="ablage_hand", graph_item_id="handle-alt",
            graph_pfad="Finanzen/Kreditoren/_OPEN/InnoSmith/Google.pdf",
        )
        zweit = await reg.aufnehmen(
            db, datei_hash=h, dateiname="Google Cloud EMEA Limited 31.01.2026 KK.pdf",
            quelle="ablage_hand", graph_item_id="handle-neu",
            graph_pfad="Finanzen/Kreditoren/Google/2026/Google.pdf",
        )

        assert zweit.neu is False
        assert zweit.doppelt is True
        assert zweit.beleg.id == erst.beleg.id
        assert zweit.beleg.graph_pfad.endswith("Google/2026/Google.pdf")
        assert zweit.beleg.graph_item_id == "handle-neu"

        alle = (
            await db.execute(select(Kreditorenbeleg).where(Kreditorenbeleg.datei_hash == h))
        ).scalars().all()
        assert len(alle) == 1, "der Hash hat die zweite Zeile nicht verhindert"

    async def test_ein_abgelegter_beleg_wird_nicht_in_den_eingang_zurueckgezogen(
        self, db: AsyncSession
    ):
        """Ein zweiter Download ist keine Bewegung.

        Sonst zeigte das Register auf den Eingang, wo die Datei bald nicht
        mehr liegt — und die Ablage liefe ein zweites Mal.
        """
        h = _hash("cursor-bereits-archiviert")
        auf = await reg.aufnehmen(
            db, datei_hash=h, dateiname="Cursor.pdf", quelle="autodownload",
            graph_pfad="…/_OPEN/InnoSmith/autodownload/Cursor.pdf",
        )
        await reg.freigeben(db, auf.beleg, durch=None)
        await reg.ablage_vermerken(
            db, auf.beleg, archiv_pfad="Finanzen/Kreditoren/Cursor/2026/Cursor.pdf",
            graph_item_id="handle-archiv",
        )

        erneut = await reg.aufnehmen(
            db, datei_hash=h, dateiname="Cursor.pdf", quelle="autodownload",
            graph_pfad="…/_OPEN/InnoSmith/autodownload/Cursor.pdf",
        )
        assert erneut.neu is False
        assert erneut.beleg.archiv_pfad.endswith("Cursor/2026/Cursor.pdf")
        assert erneut.beleg.graph_pfad.endswith("Cursor/2026/Cursor.pdf")
        assert "Archiv" in erneut.vermerk

    async def test_die_datenbank_selbst_verhindert_die_zweite_zeile(self, db: AsyncSession):
        """Nicht nur der Dienst — auch ohne ihn darf kein Hash doppelt liegen."""
        h = _hash("direkt-eingefuegt")
        db.add(Kreditorenbeleg(datei_hash=h, dateiname="a.pdf", quelle="upload"))
        await db.flush()
        db.add(Kreditorenbeleg(datei_hash=h, dateiname="b.pdf", quelle="upload"))
        with pytest.raises(IntegrityError):
            await db.flush()

    async def test_eine_unbekannte_quelle_wird_abgewiesen(self, db: AsyncSession):
        with pytest.raises(ValueError, match="Quelle"):
            await reg.aufnehmen(
                db, datei_hash=_hash("x"), dateiname="x.pdf", quelle="irgendwoher"
            )


class TestWarteliste:
    async def test_zeigt_nur_was_offen_und_nicht_zurueckgestellt_ist(self, db: AsyncSession):
        offen = await reg.aufnehmen(
            db, datei_hash=_hash("offen"), dateiname="offen.pdf", quelle="upload")
        zurueck = await reg.aufnehmen(
            db, datei_hash=_hash("zurueck"), dateiname="zurueck.pdf", quelle="upload")
        fertig = await reg.aufnehmen(
            db, datei_hash=_hash("fertig"), dateiname="fertig.pdf", quelle="upload")
        await reg.zuruecklegen(db, zurueck.beleg, "warte auf Gutschrift")
        await reg.freigeben(db, fertig.beleg, durch=None)
        await db.flush()

        ids = {b.id for b in await reg.offene(db)}
        assert offen.beleg.id in ids
        assert zurueck.beleg.id not in ids
        assert fertig.beleg.id not in ids

        mit = {b.id for b in await reg.offene(db, mit_zurueckgestellten=True)}
        assert zurueck.beleg.id in mit

    async def test_zuruecklegen_ohne_grund_ist_nicht_erlaubt(self, db: AsyncSession):
        auf = await reg.aufnehmen(
            db, datei_hash=_hash("ohnegrund"), dateiname="x.pdf", quelle="upload")
        with pytest.raises(ValueError, match="Grund"):
            await reg.zuruecklegen(db, auf.beleg, "   ")


class TestFreigabeUndAblage:
    async def test_zweimal_freigeben_ist_gesperrt(self, db: AsyncSession):
        """Zweimal freigegeben hiesse zweimal gebucht, und das hiesse zweimal bezahlt."""
        auf = await reg.aufnehmen(
            db, datei_hash=_hash("zweimal"), dateiname="x.pdf", quelle="upload")
        await reg.freigeben(db, auf.beleg, durch=None)
        with pytest.raises(ValueError, match="zweite Buchung"):
            await reg.freigeben(db, auf.beleg, durch=None)

    async def test_zurueckgestelltes_wird_nicht_freigegeben(self, db: AsyncSession):
        auf = await reg.aufnehmen(
            db, datei_hash=_hash("gesperrt"), dateiname="x.pdf", quelle="upload")
        await reg.zuruecklegen(db, auf.beleg, "Betrag unklar")
        with pytest.raises(ValueError, match="zurückgestellt"):
            await reg.freigeben(db, auf.beleg, durch=None)

    async def test_ohne_freigabe_wird_nichts_abgelegt(self, db: AsyncSession):
        auf = await reg.aufnehmen(
            db, datei_hash=_hash("unfrei"), dateiname="x.pdf", quelle="upload")
        with pytest.raises(ValueError, match="nicht freigegeben"):
            await reg.ablage_vermerken(
                db, auf.beleg, archiv_pfad="irgendwo", graph_item_id=None)

    async def test_die_ablage_fuehrt_das_handle_nach(self, db: AsyncSession):
        """Nach dem Verschieben zeigt das alte Handle auf nichts."""
        auf = await reg.aufnehmen(
            db, datei_hash=_hash("handle"), dateiname="x.pdf", quelle="upload",
            graph_item_id="vorher")
        await reg.freigeben(db, auf.beleg, durch=None)
        await reg.ablage_vermerken(
            db, auf.beleg, archiv_pfad="Finanzen/Kreditoren/X/2026/x.pdf",
            graph_item_id="nachher")

        assert auf.beleg.graph_item_id == "nachher"
        assert auf.beleg.graph_pfad == auf.beleg.archiv_pfad
        assert auf.beleg.abgelegt_am is not None
