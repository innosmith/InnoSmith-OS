"""Tests für den Mailentwurf zur Rechnung.

Der wichtigste Test ist der, der **nichts** prüft, was man sieht: dass ein
Entwurf ohne Anhang wieder verschwindet. Eine Mail ohne Rechnung ist
gefährlicher als gar keine, weil sie im Postfach versandfertig aussieht.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import debitoren_mail as mail
from app.services.debitorenvertraege import Kunde, Vertragsdaten


def vertrag(**rest) -> Vertragsdaten:
    grund = dict(
        schluessel="subventix",
        bezeichnung="Subventix",
        kunde=Kunde(schluessel="mba", empfaenger="kreditoren@be.ch", ablage="MBA"),
        mwst=8.1,
    )
    return Vertragsdaten(**{**grund, **rest})


class FakeGraph:
    """Merkt sich, was gerufen wurde — und kann auf Wunsch scheitern."""

    def __init__(self, anhang_scheitert: bool = False):
        self.entwuerfe: list[dict] = []
        self.anhaenge: list[tuple] = []
        self.geloescht: list[str] = []
        self._anhang_scheitert = anhang_scheitert

    async def create_draft(self, **kwargs):
        self.entwuerfe.append(kwargs)
        return {"id": "AAMk-neu"}

    async def add_attachment(self, kennung, name, inhalt, mime="application/pdf"):
        if self._anhang_scheitert:
            raise RuntimeError("Graph mag nicht")
        self.anhaenge.append((kennung, name, inhalt))
        return "anhang-1"

    async def delete_message(self, kennung):
        self.geloescht.append(kennung)


# ── Text und Benennung ──────────────────────────────────────────────────────


def test_betreff_wie_seit_jahren():
    assert mail.betreff("Subventix", 2026, 8) == "InnoSmith Rechnung Subventix August 2026"


def test_dateiname_beginnt_mit_der_rechnungsnummer():
    """Danach sucht die Buchhaltung der Gegenseite."""
    name = mail.dateiname("Subventix", "RE-00706", 2026, 8)

    assert name == "RE-00706 InnoSmith Rechnung Subventix August 2026.pdf"


def test_schraegstrich_im_projektnamen_zerlegt_keinen_pfad():
    name = mail.dateiname("Analyse/Konzept", "RE-00706", 2026, 8)

    assert "/" not in name


def test_ich_form_und_wir_form_unterscheiden_sich_an_zwei_stellen():
    ich, wir = mail.text("ich"), mail.text("wir")

    assert "meine Leistungen" in ich and "stehe ich Ihnen" in ich
    assert "unsere Leistungen" in wir and "stehen wir Ihnen" in wir


def test_unbekannte_anrede_faellt_auf_die_ich_form_zurueck():
    """Vertretbar, weil der Mensch es im Entwurf sieht — anders als ein Betrag."""
    assert mail.text("blubb") == mail.text("ich")


def test_html_erhaelt_die_absaetze():
    html = mail.als_html("Guten Tag\n\nText")

    assert "Calibri" in html
    assert html.count("<br>") == 2


def test_html_maskiert_sonderzeichen():
    """Ein «&» im Firmennamen zerlegte sonst die Darstellung."""
    assert "&amp;" in mail.als_html("Meier & Co")


# ── Der Entwurf ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entwurf_traegt_empfaenger_betreff_und_anhang():
    g = FakeGraph()

    kennung = await mail.entwurf_anlegen(
        g, vertrag=vertrag(), nummer="RE-00706", jahr=2026, monat=8,
        dokument=b"%PDF-1.5 Rechnung",
    )

    assert kennung == "AAMk-neu"
    assert g.entwuerfe[0]["to_recipients"] == ["kreditoren@be.ch"]
    assert g.entwuerfe[0]["subject"] == "InnoSmith Rechnung Subventix August 2026"
    assert g.anhaenge[0][1].startswith("RE-00706")
    assert g.anhaenge[0][2] == b"%PDF-1.5 Rechnung"


@pytest.mark.asyncio
async def test_fehlender_empfaenger_erzeugt_gar_keinen_entwurf():
    """Ein Entwurf ohne Adresse sieht fertig aus und fällt erst beim Klick auf."""
    g = FakeGraph()

    with pytest.raises(ValueError, match="kein Empfänger"):
        await mail.entwurf_anlegen(
            g, vertrag=vertrag(kunde=Kunde(schluessel="xy")),
            nummer="RE-00706", jahr=2026, monat=8, dokument=b"%PDF",
        )

    assert g.entwuerfe == []


@pytest.mark.asyncio
async def test_gescheiterter_anhang_raeumt_den_entwurf_wieder_weg():
    """Der eigentliche Punkt dieses Moduls.

    Bliebe der Entwurf liegen, läge eine versandfertig aussehende Mail ohne
    Rechnung im Postfach — und im Monatslauf gingen zwanzig davon raus.
    """
    g = FakeGraph(anhang_scheitert=True)

    with pytest.raises(RuntimeError):
        await mail.entwurf_anlegen(
            g, vertrag=vertrag(), nummer="RE-00706", jahr=2026, monat=8,
            dokument=b"%PDF",
        )

    assert g.geloescht == ["AAMk-neu"]


@pytest.mark.asyncio
async def test_der_empfaenger_von_damals_gilt_fuer_einen_alten_monat():
    """`deinklima` wechselte Anfang 2026 von der Wyss Academy zum AUE.

    Ein Nachtrag für Dezember 2025 muss an die damalige Adresse gehen.
    """
    v = vertrag(
        schluessel="deinklima", bezeichnung="deinklima",
        kunde=Kunde(schluessel="aue", empfaenger="kreditoren@be.ch"),
        frueher=((date(2025, 12, 31),
                  Kunde(schluessel="wyss", empfaenger="finanzen@wyssacademy.org")),),
    )
    g = FakeGraph()

    await mail.entwurf_anlegen(
        g, vertrag=v, nummer="RE-00650", jahr=2025, monat=12, dokument=b"%PDF",
    )

    assert g.entwuerfe[0]["to_recipients"] == ["finanzen@wyssacademy.org"]


@pytest.mark.asyncio
async def test_es_wird_nie_versendet():
    """Externe Kommunikation ist L1 — ausnahmslos.

    Geprüft wird das Verhalten und nicht der Quelltext: der nachgebildete
    Graph bietet ein Senden an und zählt mit. Griffe das Modul danach, stünde
    hier eine Eins.
    """
    g = FakeGraph()
    gesendet: list[str] = []
    g.send_draft = lambda kennung: gesendet.append(kennung)  # type: ignore[attr-defined]

    await mail.entwurf_anlegen(
        g, vertrag=vertrag(), nummer="RE-00706", jahr=2026, monat=8, dokument=b"%PDF",
    )

    assert gesendet == []
