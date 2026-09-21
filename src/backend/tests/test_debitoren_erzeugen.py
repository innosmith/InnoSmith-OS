"""Tests für das Erzeugen datierter Entwürfe aus Bexio-Aufträgen.

Der Schwerpunkt liegt auf dem Datum und auf dem, was bei einem Teilausfall
sichtbar bleiben muss. Eine erzeugte Rechnung, die im Protokoll fehlt, weil
das Umdatieren scheiterte, wäre der teuerste Fehler: der nächste Lauf legte
eine zweite an.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import debitoren_erzeugen as erz


class FakeBexio:
    """Bexio-Ersatz, der mitschreibt. Keine echte Schnittstelle im Test."""

    def __init__(self, *, erzeugt=None, erzeugen_kaputt=False, aendern_kaputt=False):
        self._erzeugt = erzeugt or {
            "id": 900, "document_nr": "RE-00700",
            "is_valid_from": "2026-10-01", "is_valid_to": "2026-10-30",
        }
        self._erzeugen_kaputt = erzeugen_kaputt
        self._aendern_kaputt = aendern_kaputt
        self.erzeugt_fuer: list[int] = []
        self.geaendert: list[tuple[int, dict]] = []

    async def create_invoice_from_order(self, order_id, positionen=None):
        if self._erzeugen_kaputt:
            raise RuntimeError("422 Unprocessable Entity")
        self.erzeugt_fuer.append(order_id)
        return dict(self._erzeugt)

    async def update_invoice(self, invoice_id, **felder):
        if self._aendern_kaputt:
            raise RuntimeError("500 vom Server")
        self.geaendert.append((invoice_id, felder))
        return {**self._erzeugt, **felder}


STICHTAG = date(2026, 9, 30)


@pytest.mark.asyncio
async def test_rechnung_wird_auf_den_monatsletzten_umdatiert():
    bexio = FakeBexio()
    protokoll = await erz.erzeugen(bexio, [(31, "AKV-Bot")], stichtag=STICHTAG)

    assert bexio.erzeugt_fuer == [31]
    assert protokoll.erzeugt == 1
    assert protokoll.eintraege[0].datum == "2026-09-30"
    assert protokoll.eintraege[0].gelungen


@pytest.mark.asyncio
async def test_die_zahlungsfrist_von_bexio_bleibt_erhalten():
    """29 Tage sind die Regel des Hauses — aber sie stehen nicht im Code.

    Gesetzt wird der Abstand, den Bexio beim Erzeugen selbst gewählt hat.
    Hier sind es bewusst 14 Tage: eine andere Zahlungsbedingung muss
    durchkommen, nicht auf 29 gebogen werden.
    """
    bexio = FakeBexio(erzeugt={
        "id": 900, "document_nr": "RE-00700",
        "is_valid_from": "2026-10-01", "is_valid_to": "2026-10-15",
    })
    await erz.erzeugen(bexio, [(31, "AKV-Bot")], stichtag=STICHTAG)

    _, felder = bexio.geaendert[0]
    assert felder["is_valid_from"] == "2026-09-30"
    assert felder["is_valid_to"] == "2026-10-14"   # weiterhin 14 Tage


@pytest.mark.asyncio
async def test_richtig_datiert_wird_nicht_geschrieben():
    """Ein Aufruf ohne Wirkung täuscht im Audit-Log eine Änderung vor."""
    bexio = FakeBexio(erzeugt={
        "id": 900, "document_nr": "RE-00700",
        "is_valid_from": "2026-09-30", "is_valid_to": "2026-10-29",
    })
    protokoll = await erz.erzeugen(bexio, [(31, "AKV-Bot")], stichtag=STICHTAG)

    assert bexio.geaendert == []
    assert protokoll.eintraege[0].gelungen


@pytest.mark.asyncio
async def test_unlesbare_frist_wird_nicht_erfunden():
    """Lieber ein Hindernis im Protokoll als ein geratenes Fälligkeitsdatum."""
    bexio = FakeBexio(erzeugt={
        "id": 900, "document_nr": "RE-00700",
        "is_valid_from": "2026-10-01", "is_valid_to": None,
    })
    protokoll = await erz.erzeugen(bexio, [(31, "AKV-Bot")], stichtag=STICHTAG)

    _, felder = bexio.geaendert[0]
    assert felder == {"is_valid_from": "2026-09-30"}
    assert "Zahlungsfrist" in protokoll.eintraege[0].hindernis
    assert not protokoll.eintraege[0].gelungen


@pytest.mark.asyncio
async def test_gescheiterte_erzeugung_bricht_den_lauf_nicht_ab():
    bexio = FakeBexio(erzeugen_kaputt=True)
    protokoll = await erz.erzeugen(
        bexio, [(31, "AKV-Bot"), (36, "COflow")], stichtag=STICHTAG
    )

    assert protokoll.erzeugt == 0
    assert protokoll.gescheitert == 2
    assert all("422" in e.hindernis for e in protokoll.eintraege)


@pytest.mark.asyncio
async def test_erzeugte_rechnung_bleibt_sichtbar_wenn_das_datum_scheitert():
    """Der teuerste denkbare Fehler: die Rechnung steht in Bexio, das Protokoll
    schweigt, und der nächste Lauf legt eine zweite an."""
    bexio = FakeBexio(aendern_kaputt=True)
    protokoll = await erz.erzeugen(bexio, [(31, "AKV-Bot")], stichtag=STICHTAG)

    eintrag = protokoll.eintraege[0]
    assert eintrag.rechnung_id == 900
    assert eintrag.nummer == "RE-00700"
    assert "erzeugt" in eintrag.hindernis
    assert not eintrag.gelungen


@pytest.mark.asyncio
async def test_jeder_auftrag_wird_einzeln_verarbeitet():
    bexio = FakeBexio()
    protokoll = await erz.erzeugen(
        bexio, [(31, "AKV-Bot"), (36, "COflow"), (37, "KI-Pilotprojekte")],
        stichtag=STICHTAG,
    )

    assert bexio.erzeugt_fuer == [31, 36, 37]
    assert [e.titel for e in protokoll.eintraege] == [
        "AKV-Bot", "COflow", "KI-Pilotprojekte"
    ]


@pytest.mark.asyncio
async def test_rechnung_ohne_kennung_gilt_nicht_als_erzeugt():
    bexio = FakeBexio(erzeugt={"document_nr": "RE-00700"})
    protokoll = await erz.erzeugen(bexio, [(31, "AKV-Bot")], stichtag=STICHTAG)

    assert protokoll.erzeugt == 0
    assert "keine Kennung" in protokoll.eintraege[0].hindernis
