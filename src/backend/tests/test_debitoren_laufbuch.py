"""Tests für das Laufbuch des Rechnungslaufs.

Geprüft wird vor allem die eine Eigenschaft, die das Laufbuch überhaupt
rechtfertigt: **ein vollzogener Schritt wird nicht zweimal vollzogen**. Ein
zweiter Mailentwurf im Postfach oder eine zweite Kopie im Kundenarchiv fällt
sonst erst auf, wenn jemand sie von Hand findet.

Die Tests laufen gegen die echte Datenbank (``@pytest.mark.db``) und räumen
hinter sich auf. Als Periode dient 1999 — sie kollidiert mit keinem Lauf.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.database import async_session
from app.models import Debitorenlauf
from app.services import debitoren_laufbuch as lb

pytestmark = [pytest.mark.asyncio, pytest.mark.db]

JAHR, MONAT = 1999, 7
STICHTAG = date(1999, 7, 31)


@pytest_asyncio.fixture
async def db():
    async with async_session() as sitzung:
        await sitzung.execute(
            delete(Debitorenlauf).where(Debitorenlauf.jahr == JAHR)
        )
        await sitzung.commit()
        yield sitzung
        await sitzung.rollback()
        await sitzung.execute(
            delete(Debitorenlauf).where(Debitorenlauf.jahr == JAHR)
        )
        await sitzung.commit()


async def test_zweimal_oeffnen_ergibt_einen_lauf(db):
    erst = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)
    await db.commit()
    nochmal = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    assert erst.id == nochmal.id


async def test_ohne_vermerk_gibt_es_keine_zeile(db):
    """Der Nullzustand braucht keinen Eintrag.

    Vorsorglich je Rechnung eine Zeile anzulegen wäre eine Behauptung über
    einen Bestand, der live aus Bexio kommt und sich noch ändert.
    """
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)
    await db.commit()

    assert lauf.rechnungen == []
    assert lb.als_karte(lauf) == {}


async def test_ein_vollzogener_schritt_wird_nicht_zweimal_vollzogen(db):
    """Die tragende Eigenschaft des Laufbuchs."""
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    _, erstmals = await lb.schritt_vermerken(
        db, lauf, rechnung_id=707, schritt="mailentwurf_id",
        wert="AAMk-1", nummer="RE-00707",
    )
    zeile, nochmal = await lb.schritt_vermerken(
        db, lauf, rechnung_id=707, schritt="mailentwurf_id", wert="AAMk-2",
    )
    await db.commit()

    assert erstmals is True
    assert nochmal is False
    assert zeile.mailentwurf_id == "AAMk-1"   # der zweite Wert kam nicht durch


async def test_erneut_ueberschreibt_nur_auf_ansage(db):
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    await lb.schritt_vermerken(
        db, lauf, rechnung_id=707, schritt="mailentwurf_id", wert="AAMk-1"
    )
    zeile, neu = await lb.schritt_vermerken(
        db, lauf, rechnung_id=707, schritt="mailentwurf_id",
        wert="AAMk-2", erneut=True,
    )
    await db.commit()

    assert neu is True
    assert zeile.mailentwurf_id == "AAMk-2"


async def test_zeitschritte_tragen_einen_zeitpunkt(db):
    """``NULL`` heisst «noch nicht», ein Zeitstempel beantwortet auch «wann»."""
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    zeile, _ = await lb.schritt_vermerken(
        db, lauf, rechnung_id=707, schritt="versendet_am"
    )
    await db.commit()

    assert zeile.versendet_am is not None
    assert zeile.abgelegt_am is None
    assert zeile.dokumente_erzeugt_am is None


async def test_unbekannter_schritt_ist_ein_fehler(db):
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    with pytest.raises(ValueError):
        await lb.schritt_vermerken(db, lauf, rechnung_id=707, schritt="geprueft")


async def test_zuruecklegen_verlangt_eine_begruendung(db):
    """Ohne Grund sieht eine bewusste Ausnahme aus wie ein Versehen."""
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    with pytest.raises(ValueError):
        await lb.zuruecklegen(db, lauf, rechnung_id=707, grund="   ")


async def test_zurueckholen_laesst_den_grund_stehen(db):
    """Er gehört zur Geschichte der Rechnung."""
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    await lb.zuruecklegen(
        db, lauf, rechnung_id=707, grund="Kunde prüft die Stunden noch"
    )
    zeile = await lb.zurueckholen(db, lauf, rechnung_id=707)
    await db.commit()

    assert zeile is not None
    assert zeile.zurueckgestellt is False
    assert zeile.grund == "Kunde prüft die Stunden noch"


async def test_zurueckholen_einer_unbekannten_rechnung_legt_nichts_an(db):
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    assert await lb.zurueckholen(db, lauf, rechnung_id=999) is None
    assert lauf.rechnungen == []


async def test_karte_schluesselt_nach_bexio_kennung(db):
    """Die Ansicht legt die Vermerke über den live gelesenen Bestand."""
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)

    await lb.schritt_vermerken(db, lauf, rechnung_id=707, schritt="versendet_am")
    await lb.zuruecklegen(db, lauf, rechnung_id=708, grund="wartet auf Beleg")
    await db.commit()

    karte = lb.als_karte(lauf)
    assert set(karte) == {707, 708}
    assert karte[707]["versendet_am"] is not None
    assert karte[708]["zurueckgestellt"] is True
    assert karte[708]["versendet_am"] is None


async def test_laufbuch_ueberlebt_den_seitenwechsel(db):
    """Der eigentliche Zweck: neu geladen, und die Entscheidung steht noch da."""
    lauf = await lb.lauf_oeffnen(db, jahr=JAHR, monat=MONAT, stichtag=STICHTAG)
    await lb.zuruecklegen(db, lauf, rechnung_id=707, grund="Rückfrage offen")
    await db.commit()
    db.expunge_all()

    frisch = await lb.lauf_holen(db, jahr=JAHR, monat=MONAT)
    assert frisch is not None
    assert lb.als_karte(frisch)[707]["grund"] == "Rückfrage offen"


async def test_ohne_lauf_ist_die_karte_leer(db):
    """Vor dem ersten Vermerk gibt es keinen Lauf — und das ist kein Fehler."""
    assert await lb.lauf_holen(db, jahr=JAHR, monat=1) is None
    assert lb.als_karte(None) == {}
