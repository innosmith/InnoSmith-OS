"""Der Kreditoreneingang: was wartet, und was vorgeschlagen wird.

Geprueft wird die Rangfolge, nicht die Verdrahtung. Die drei Faelle, an denen
der Entwurf haette scheitern koennen, sind: ein Zahlungsabwickler auf dem Beleg,
ein Lieferant mit zwei moeglichen Konten, und der zweite Lauf ueber eine bereits
getroffene Entscheidung.
"""

import pytest
import pytest_asyncio
from app.config import get_settings
from app.services import kreditorenlieferanten as decl
from app.services import kreditoreneingang as eing
from app.services import kreditorenregister as reg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def db():
    """Eine Sitzung, die am Ende alles zurückrollt — wie im Registertest.

    Gegen echtes Postgres, weil die Prüfregeln auf ``sollkonto`` und
    ``sollkonto_herkunft`` nur dort gelten. Eine Attrappe bestätigte sie,
    ohne dass sie greifen.
    """
    s = get_settings()
    motor = create_async_engine(
        f"postgresql+asyncpg://{s.db_user}:{s.db_password}"
        f"@{s.db_host}:{s.db_port}/{s.db_name}"
    )
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


def _bestand() -> decl.Bestand:
    """Lieferanten, die je einen Fall abdecken -- dazu ein Absender, der
    mehrere meint (``google``)."""
    return decl.Bestand(
        lieferanten={
            "cursor": decl.Lieferant(
                schluessel="cursor",
                ordner="Cursor",
                sollkonto="6570",
                zahlweg=("karte",),
                steuer=(decl.Steuerzeile(None, "bezugssteuer"),),
                aktiv=True,
                bestaetigt=True,
                leistung="Usage",
            ),
            "hosttech": decl.Lieferant(
                schluessel="hosttech",
                ordner="Hosttech",
                sollkonto_kandidaten=("6512", "4200"),
                zahlweg=("rechnung",),
                steuer=(decl.Steuerzeile(None, "inland_mwst"),),
                aktiv=True,
                bestaetigt=False,
            ),
            "openai": decl.Lieferant(
                schluessel="openai",
                ordner="OpenAI",
                sollkonto="6570",
                zahlweg=("karte",),
                steuer=(decl.Steuerzeile(None, "inland_mwst"),),
                aktiv=True,
                bestaetigt=True,
                leistung="Monatsabo",
            ),
            "google": decl.Lieferant(
                schluessel="google",
                ordner="Google",
                aufteilen=("google_workspace", "youtube"),
                aktiv=True,
            ),
            "google_workspace": decl.Lieferant(
                schluessel="google_workspace",
                ordner="Google Workspace",
                sollkonto="6570",
                zahlweg=("karte",),
                steuer=(decl.Steuerzeile(None, "inland_mwst"),),
                aktiv=True,
                leistung="Abo",
            ),
            "youtube": decl.Lieferant(
                schluessel="youtube",
                ordner="YouTube",
                sollkonto="6570",
                zahlweg=("karte",),
                steuer=(decl.Steuerzeile(None, "inland_mwst"),),
                aktiv=True,
                leistung="Abo",
            ),
        }
    )


def _zeile(**felder):
    grund = felder.pop("grund", "ordner")
    basis = {
        "beleg_id": 4711,
        "sha256": "a" * 64,
        # Genau die Form, die der Abzug liefert: Ordner und Dateiname getrennt,
        # der Ordner relativ zur Archivwurzel. Bis zum 22.09.2026 stand hier ein
        # absoluter Vollpfad in 'beleg_datei' -- den gibt es dort nicht, und weil
        # die Fixture grosszuegiger war als die Schnittstelle, blieben die Tests
        # gruen, waehrend der Eingang sich nicht fuellen konnte.
        "beleg_ordner": "_OPEN/InnoSmith",
        "beleg_datei": "x.pdf",
        "lieferant": "Cursor",
        "lieferant_schluessel": "cursor",
        "lieferant_grund": grund,
        "datum": "2026-09-20",
        "mwst": None,
        "zahlungsart": "KREDITKARTE",
    }
    return {**basis, **felder}


class TestOrtstattStichtag:
    def test_was_im_eingang_liegt_wartet(self):
        assert eing._im_eingang("_OPEN/InnoSmith", "_OPEN/InnoSmith") is True
        assert eing._im_eingang("_OPEN/InnoSmith/autodownload", "_OPEN/InnoSmith") is True

    def test_was_im_archiv_liegt_ist_erledigt(self):
        """Der Eingang ist ein Ort. Ein Beleg im Archiv ist entschieden."""
        assert eing._im_eingang("Cursor/2026", "_OPEN/InnoSmith") is False

    def test_der_dateiname_allein_nennt_keinen_ort(self):
        """Der Fehler, der den Eingang leer hielt -- als Test festgehalten.

        Geprueft wurde ``beleg_datei``, und darin steht nur der Dateiname. Von 1197
        Belegen galt keiner als wartend, obwohl 17 im Eingang lagen. Wer hier
        wieder den Dateinamen einsetzt, bekommt genau diese leere Liste zurueck.
        """
        assert eing._im_eingang("Cursor Sammelbeleg September 2026.pdf", "_OPEN/InnoSmith") is False

    def test_ein_aehnlich_benannter_ordner_zaehlt_nicht(self):
        """Verglichen wird auf Gliedgrenzen, nicht als Teilzeichenkette."""
        assert eing._im_eingang("_OPEN/InnoSmithPrivat", "_OPEN/InnoSmith") is False

    def test_der_unterordner_nennt_die_quelle(self):
        """Ein zweiter Bezug ist beim Autodownload normal, bei Handablage nicht."""
        assert eing._quelle_von("_OPEN/InnoSmith/autodownload") == "autodownload"
        assert eing._quelle_von("_OPEN/InnoSmith") == "ablage_hand"


class TestVorschlag:
    def test_der_klare_fall_ist_entscheidbar(self):
        v = eing.vorschlagen(_zeile(), _bestand())

        assert v.sollkonto == "6570"
        assert v.sollkonto_herkunft == "vorschlag"
        assert v.steuerbehandlung == "bezugssteuer"
        assert v.zahlweg == "karte"
        assert v.entscheidbar
        assert v.abweichungen == []
        assert (v.anzeigename, v.leistung) == ("Cursor", "Usage")

    def test_ohne_leistung_wird_sie_am_beleg_verlangt(self):
        """Hosttech nennt auf jeder Rechnung ihre Domain."""
        v = eing.vorschlagen(
            _zeile(lieferant_schluessel="hosttech", lieferant="Hosttech"), _bestand()
        )
        assert v.leistung is None
        assert any("hängt die Leistung an der Rechnung" in a for a in v.abweichungen)

    def test_zwei_moegliche_konten_ergeben_keinen_vorschlag(self):
        """Bei Hosttech ist dieselbe Domain einmal eigener Aufwand (6512) und
        einmal weiterverrechnete Leistung (4200). Ein Wert waere dort nicht
        unsicher, sondern falsch benannt -- und die Freigabe muss warten."""
        v = eing.vorschlagen(
            _zeile(lieferant_schluessel="hosttech", lieferant="Hosttech",
                   zahlungsart="RECHNUNG"),
            _bestand(),
        )

        assert v.sollkonto is None
        assert v.entscheidbar is False
        assert v.sollkonto_kandidaten == ("6512", "4200")
        assert any("6512 oder 4200" in a for a in v.abweichungen)

    def test_ein_zahlungsabwickler_beantwortet_nichts(self):
        """Revolut steht auf der Zahlungsbestaetigung und ist nie der Kreditor.

        Florian Salman UG hat ein Revolut-Konto angegeben -- der Lieferant ist
        Florian Salman UG. Ohne diese Frage waere der Abwickler zum Kreditor
        geworden, und das faellt an keiner Zahl auf.
        """
        v = eing.vorschlagen(
            _zeile(lieferant_schluessel=None, lieferant="Revolut",
                   grund="nur_zahlungsanbieter"),
            _bestand(),
        )

        assert v.sollkonto is None
        assert any("Zahlungsabwickler" in a for a in v.abweichungen)

    def test_erstmals_ausgewiesene_mwst_wird_gefragt_nicht_uebernommen(self):
        """Die teure Abweichung: waere sie durchgerutscht, stuende die Steuer
        zweimal -- einmal als Vorsteuer, einmal als Bezugssteuer gegen 2203."""
        v = eing.vorschlagen(_zeile(mwst=8.10), _bestand())

        assert v.steuerbehandlung == "bezugssteuer"
        assert any("Schweizer MWST" in a for a in v.abweichungen)

    def test_gelesene_null_ist_keine_abweichung(self):
        """``mwst = 0`` ist eine gelesene Null und bestaetigt die Erwartung."""
        v = eing.vorschlagen(_zeile(mwst=0), _bestand())

        assert v.abweichungen == []

    def test_ein_unbestaetigter_eintrag_wird_benannt(self):
        v = eing.vorschlagen(
            _zeile(lieferant_schluessel="hosttech", zahlungsart="RECHNUNG"),
            _bestand(),
        )
        assert any("von niemandem geprüft" in a for a in v.abweichungen)

    def test_ohne_deklaration_kein_geratenes_konto(self):
        v = eing.vorschlagen(_zeile(lieferant_schluessel="unbekannter"), _bestand())

        assert v.sollkonto is None
        assert any("in der Deklaration" in a for a in v.abweichungen)


def _google(**felder):
    return _zeile(lieferant="Google", lieferant_schluessel="google", **felder)


class TestSammelabsender:
    """Auf allen Google-Rechnungen steht «Google». Im Archiv entscheidet der
    Ordner, im Eingang nur der Absender -- dort muss ein Mensch den Dienst wählen."""

    def test_der_absender_allein_ergibt_keinen_vorschlag(self):
        v = eing.vorschlagen(_google(), _bestand())

        assert v.sollkonto is None and not v.entscheidbar
        assert v.lieferant_kandidaten == ("google_workspace", "youtube")
        assert any("Welcher ist es?" in a for a in v.abweichungen)

    def test_die_wahl_am_beleg_gilt_hinter_dem_absender(self):
        v = eing.vorschlagen(_google(), _bestand(), gewaehlt="google_workspace")

        assert (v.lieferant_schluessel, v.sollkonto, v.leistung) == ("google_workspace", "6570", "Abo")
        # Umwählen bleibt möglich, bis freigegeben ist.
        assert v.lieferant_kandidaten == ("google_workspace", "youtube")

    def test_die_wahl_schlaegt_den_ordner_nicht(self):
        v = eing.vorschlagen(_zeile(lieferant_schluessel="openai"), _bestand(), gewaehlt="google_workspace")

        assert v.lieferant_schluessel == "openai" and not v.lieferant_kandidaten

    def test_ohne_modulschluessel_gilt_die_wahl(self):
        v = eing.vorschlagen(
            _zeile(lieferant_schluessel=None, lieferant_grund="unbekannt"), _bestand(), gewaehlt="openai"
        )

        assert (v.lieferant_schluessel, v.sollkonto) == ("openai", "6570")

    def test_die_normpruefung_haelt_den_absender_an(self):
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from app.services import kreditorenbuchung as kb

        beleg = SimpleNamespace(freigegeben_am=datetime.now(UTC), gebucht_am=None)
        p = kb.pruefen(beleg, _google(), _bestand().lieferanten["google"], None, kurs=None)

        assert not p.buchbar
        assert any("mehrere Dienste" in v for v in p.verstoesse)

    def test_ein_verteiler_ohne_ziel_ist_ein_mangel(self, tmp_path):
        datei = tmp_path / "l.yaml"
        datei.write_text(
            "version: 1\nlieferanten:\n"
            "  - schluessel: google\n    ordner: Google\n    aufteilen: [google_cloud]\n",
            encoding="utf-8",
        )

        assert any("google_cloud" in m for m in decl.laden(datei).maengel)

    def test_ein_verteiler_fehlt_nicht_als_unvollstaendig(self):
        assert "google" not in {l.schluessel for l in _bestand().unvollstaendig()}


class TestSammelmonat:
    """Die Warteliste zählt je Kalendermonat, nach dem Rechnungsdatum."""

    def test_gezaehlt_wird_je_monat_und_summiert_nur_was_gelesen_ist(self):
        from datetime import date
        from types import SimpleNamespace

        from app.routers.kreditoreneingang import _je_monat

        def e(datum, betrag):
            return SimpleNamespace(lieferant_schluessel="cursor"), {
                "datum": datum, "betrag": betrag, "waehrung": "USD",
            }

        august, september = _je_monat(
            [e("2026-08-31", 20.0), e("2026-08-01", 5.5), e("2026-09-01", None)],
            _bestand(),
            heute=date(2026, 9, 25),
        )

        assert (august.monat, august.anzahl, august.betrag, august.abgeschlossen) == (8, 2, 25.5, True)
        assert (september.monat, september.anzahl, september.betrag, september.abgeschlossen) == (
            9, 1, None, False,
        )
        assert september.bezeichnung == "September 2026"


@pytest.mark.asyncio
class TestAbzugOhneOrdner:
    """Eine fehlende Spalte bricht mit Meldung ab -- nicht mit leerer Liste.

    Der Unterschied ist der ganze Punkt: der Eingang war einen Tag lang nicht
    leer, sondern unerreichbar, und beides sieht am Bildschirm gleich aus.
    """

    async def test_ohne_beleg_ordner_wird_abgebrochen(self, monkeypatch):
        alt = {k: v for k, v in _zeile().items() if k != "beleg_ordner"}

        async def _abzug(*_args, **_kwargs):
            return [alt], {}

        monkeypatch.setattr(eing, "rechnungen_holen", _abzug)

        with pytest.raises(RuntimeError, match="beleg_ordner"):
            await eing.abgleichen(
                None, basis_url="http://x", token="t", eingang="_OPEN/InnoSmith"
            )


@pytest.mark.asyncio
@pytest.mark.db
class TestDoppelImAbgleich:
    async def test_eine_kopie_wird_gezaehlt_und_nicht_aufgenommen(
        self, db: AsyncSession, monkeypatch
    ):
        zeilen = [
            _zeile(beleg_id=1, sha256="d" * 64, beleg_datei="OpenAI.pdf",
                   lieferant="OpenAI", lieferant_schluessel="openai",
                   rechnungsnummer="2DD42E43-0001"),
            _zeile(beleg_id=2, sha256="e" * 64, beleg_datei="OpenAI (1).pdf",
                   lieferant="OpenAI", lieferant_schluessel="openai",
                   rechnungsnummer="2DD42E43-0001"),
        ]

        async def _abzug(*_args, **_kwargs):
            return zeilen, {}

        monkeypatch.setattr(eing, "rechnungen_holen", _abzug)
        befund = await eing.abgleichen(
            db, basis_url="http://x", token="t", eingang="_OPEN/InnoSmith",
            bestand=_bestand(),
        )

        assert befund.neu == 1
        assert befund.dubletten == 1
        assert any("OpenAI (1).pdf" in h and "2DD42E43-0001" in h for h in befund.hinweise)
        offen = await reg.offene(db)
        original = next(b for b in offen if b.datei_hash == "d" * 64)
        # Das Original zeigt weiter auf seine eigene Modulzeile, nicht auf die Kopie.
        assert original.modul_dokument_id == 1


def _abzug_aus(zeilen, befund=None):
    async def _abzug(*_args, **_kwargs):
        return zeilen, befund or {}
    return _abzug


@pytest.mark.asyncio
@pytest.mark.db
class TestNichtLesbar:
    """Am 25.09.2026 meldete der Eingang «12 nicht gelesen», und keiner davon
    lag im Eingang: zehn Checklisten im Archiv, zwei Dateien, die es nicht mehr gab."""

    EINZELN = [
        {"beleg_id": 1, "status": "NICHT_EXTRAHIERBAR", "beleg_ordner": "_OPEN/InnoSmith",
         "beleg_datei": "Scan.pdf", "datei_vorhanden": True},
        {"beleg_id": 2, "status": "NICHT_EXTRAHIERBAR", "beleg_ordner": "Swiss Life/2025",
         "beleg_datei": "Checkliste.pdf", "datei_vorhanden": True},
        {"beleg_id": 3, "status": "NICHT_EXTRAHIERBAR", "beleg_ordner": "_OPEN/InnoSmith",
         "beleg_datei": "Fort.pdf", "datei_vorhanden": False},
    ]

    async def test_nur_vorhandene_im_eingang_werden_genannt(self, db: AsyncSession, monkeypatch):
        monkeypatch.setattr(eing, "rechnungen_holen", _abzug_aus(
            [], {"nicht_auswertbare_belege": {"NICHT_EXTRAHIERBAR": 3},
                 "nicht_auswertbar_einzeln": self.EINZELN}))
        befund = await eing.abgleichen(
            db, basis_url="http://x", token="t", eingang="_OPEN/InnoSmith", bestand=_bestand(),
        )

        assert befund.nicht_lesbar == ["Scan.pdf"]
        assert befund.nicht_auswertbar == {}

    async def test_ohne_einzelliste_bleibt_die_zaehlung(self, db: AsyncSession, monkeypatch):
        """Eine ältere Schnittstelle: lieber zu viel melden als still nichts."""
        monkeypatch.setattr(eing, "rechnungen_holen", _abzug_aus(
            [], {"nicht_auswertbare_belege": {"NICHT_EXTRAHIERBAR": 3}}))
        befund = await eing.abgleichen(
            db, basis_url="http://x", token="t", eingang="_OPEN/InnoSmith", bestand=_bestand(),
        )

        assert befund.nicht_lesbar == []
        assert befund.nicht_auswertbar == {"NICHT_EXTRAHIERBAR": 3}


async def _abgleichen(db, monkeypatch, zeilen):
    monkeypatch.setattr(eing, "rechnungen_holen", _abzug_aus(zeilen))
    return await eing.abgleichen(
        db, basis_url="http://x", token="t", eingang="_OPEN/InnoSmith", bestand=_bestand(),
    )


@pytest.mark.asyncio
@pytest.mark.db
class TestWasNichtMehrWartet:
    """Der Anlass: die Warteliste zeigte Rechnungen, die in der Cloud längst
    gelöscht, archiviert oder von Hand abgelegt waren."""

    async def test_eine_archivierte_rechnung_im_eingang_ist_ein_zweiter_bezug(
        self, db: AsyncSession, monkeypatch
    ):
        """OpenAI 2DD42E43-0001 lag dreimal im Autodownload und einmal unter
        OpenAI/2024 -- als andere Datei, also mit anderem Hash."""
        befund = await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=11, sha256="1" * 64, beleg_ordner="OpenAI/2024",
                   beleg_datei="OpenAI 12.03.2024 KK.pdf", lieferant_schluessel="openai",
                   rechnungsnummer="2DD42E43-0001"),
            _zeile(beleg_id=12, sha256="2" * 64, beleg_ordner="_OPEN/InnoSmith/autodownload",
                   beleg_datei="Invoice-2DD42E43-0001.pdf", lieferant_schluessel="openai",
                   rechnungsnummer="2DD42E43-0001"),
        ])

        assert befund.neu == 0
        assert len(befund.schon_im_archiv) == 1
        assert "OpenAI/2024/OpenAI 12.03.2024 KK.pdf" in befund.schon_im_archiv[0]
        assert await reg.nach_hash(db, "2" * 64) is None

    async def test_der_absender_findet_die_rechnung_unter_dem_dienst(
        self, db: AsyncSession, monkeypatch
    ):
        """Workspace 30.11.2025: im Autodownload «google», im Archiv
        «google_workspace». Über den Schlüssel allein fand der Wächter sie nicht."""
        befund = await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=13, sha256="b" * 64, beleg_ordner="Google Workspace/2025",
                   beleg_datei="Google Workspace Abo 30.11.2025 KK.pdf",
                   lieferant_schluessel="google_workspace", rechnungsnummer="5425526049",
                   datum="2025-11-30"),
            _google(beleg_id=14, sha256="c" * 64, beleg_ordner="_OPEN/InnoSmith/autodownload",
                    beleg_datei="5425526049.pdf", rechnungsnummer="5425526049", datum="2025-11-30"),
        ])

        assert befund.neu == 0 and len(befund.schon_im_archiv) == 1

    async def test_gleiche_nummer_an_anderem_tag_ist_eine_andere_rechnung(
        self, db: AsyncSession, monkeypatch
    ):
        befund = await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=15, sha256="d" * 64, beleg_ordner="Hosttech/2026",
                   beleg_datei="Hosttech 01.01.2026.pdf", lieferant_schluessel="hosttech",
                   rechnungsnummer="1001", datum="2026-01-01"),
            _zeile(beleg_id=16, sha256="e" * 64, lieferant_schluessel="openai",
                   rechnungsnummer="1001", datum="2026-09-20"),
        ])

        assert befund.neu == 1 and not befund.schon_im_archiv

    async def test_wer_schon_wartete_verlaesst_die_liste_mit_begruendung(
        self, db: AsyncSession, monkeypatch
    ):
        await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=21, sha256="3" * 64, beleg_datei="Arzt.pdf",
                   lieferant_schluessel=None, lieferant_grund="unbekannt"),
        ])
        assert (await reg.nach_hash(db, "3" * 64)).zurueckgestellt is False

        befund = await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=21, sha256="3" * 64, beleg_datei="Arzt.pdf",
                   lieferant_schluessel=None, lieferant_grund="unbekannt",
                   datei_vorhanden=False),
        ])

        beleg = await reg.nach_hash(db, "3" * 64)
        assert befund.fort == 1 and befund.gesehen == 0
        assert beleg.zurueckgestellt and "nicht mehr vorhanden" in beleg.grund

    async def test_fort_wird_nur_im_lauf_des_verschwindens_gemeldet(
        self, db: AsyncSession, monkeypatch
    ):
        """Das Modul führt eine gelöschte Datei weiter. Ohne diese Grenze stand
        «16 Beleg(e) … nicht mehr vorhanden» bei jedem Abgleich da."""
        await _abgleichen(db, monkeypatch, [_zeile(beleg_id=22, sha256="7" * 64)])
        fort = [_zeile(beleg_id=22, sha256="7" * 64, datei_vorhanden=False)]

        erster = await _abgleichen(db, monkeypatch, fort)
        zweiter = await _abgleichen(db, monkeypatch, fort)

        assert erster.fort == 1 and zweiter.fort == 0

    async def test_fort_ohne_registerzeile_ist_keine_meldung(
        self, db: AsyncSession, monkeypatch
    ):
        befund = await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=23, sha256="8" * 64, datei_vorhanden=False),
        ])
        assert befund.fort == 0 and await reg.nach_hash(db, "8" * 64) is None

    async def test_von_hand_abgelegt_heisst_nicht_mehr_offen(self, db: AsyncSession, monkeypatch):
        await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=31, sha256="4" * 64, beleg_datei="YouTube.pdf",
                   rechnungsnummer="YT-0908"),
        ])

        befund = await _abgleichen(db, monkeypatch, [
            _zeile(beleg_id=31, sha256="4" * 64, beleg_ordner="YouTube/2026",
                   beleg_datei="YouTube Abo 08.09.2026 KK.pdf", rechnungsnummer="YT-0908"),
        ])

        beleg = await reg.nach_hash(db, "4" * 64)
        assert befund.von_hand_abgelegt == 1
        assert beleg.zurueckgestellt and "YouTube/2026" in beleg.grund

    async def test_eine_freigabe_hebt_kein_abgleich_auf(self, db: AsyncSession, monkeypatch):
        await _abgleichen(db, monkeypatch, [_zeile(beleg_id=41, sha256="5" * 64)])
        beleg = await reg.nach_hash(db, "5" * 64)
        await reg.freigeben(db, beleg, durch=None)

        await _abgleichen(db, monkeypatch, [_zeile(beleg_id=41, sha256="5" * 64, datei_vorhanden=False)])

        assert beleg.zurueckgestellt is False

    async def test_eine_alte_schnittstelle_ohne_die_spalte_gilt_als_vorhanden(
        self, db: AsyncSession, monkeypatch
    ):
        befund = await _abgleichen(db, monkeypatch, [_zeile(beleg_id=51, sha256="6" * 64)])
        assert befund.neu == 1 and befund.fort == 0


class _Nutzer:
    id = None
    email = "test@example.ch"
    settings: dict = {}


def _pruefung(buchbar: bool):
    from app.services import kreditorenbuchung as kb

    plan = object() if buchbar else None
    return kb.Pruefung(plan=plan, verstoesse=[] if buchbar else ["Konto 6570 ist gesperrt."], hinweise=[])


@pytest.mark.asyncio
@pytest.mark.db
class TestFreigabeInEinemSchritt:
    """Freigeben bucht und legt ab. Hält die Norm an, bleibt der Beleg unberührt;
    scheitert erst die Buchung, steht die Freigabe und der Beleg wartet aufs Nachholen."""

    @pytest_asyncio.fixture
    async def db(self):
        """Wie oben, aber auf Savepoints: der Router rollt selbst zurück und
        schreibt selbst fest, und beides darf die äussere Transaktion nicht
        berühren -- sonst verschwänden die Testdaten mit dem ersten Verstoss."""
        s = get_settings()
        motor = create_async_engine(
            f"postgresql+asyncpg://{s.db_user}:{s.db_password}"
            f"@{s.db_host}:{s.db_port}/{s.db_name}"
        )
        try:
            async with motor.connect() as verbindung:
                transaktion = await verbindung.begin()
                sitzung = async_sessionmaker(
                    bind=verbindung, expire_on_commit=False, join_transaction_mode="create_savepoint"
                )()
                try:
                    yield sitzung
                finally:
                    await sitzung.close()
                    await transaktion.rollback()
        finally:
            await motor.dispose()

    @pytest.fixture(autouse=True)
    def _ohne_fremdsysteme(self, monkeypatch):
        from app.routers import kreditoreneingang as router

        bestaetigt: list[str] = []
        monkeypatch.setattr(router.decl, "laden", _bestand)
        monkeypatch.setattr(
            router.decl, "bestaetigen", lambda s, **_kw: bestaetigt.append(s) or _bestand().lieferanten[s]
        )
        self.router, self.bestaetigt = router, bestaetigt

    async def _beleg(self, db, hash_):
        auf = await reg.aufnehmen(db, datei_hash=hash_, dateiname="cursor.pdf", quelle="autodownload")
        auf.beleg.lieferant_schluessel = "hosttech"
        auf.beleg.sollkonto = "6512"
        auf.beleg.sollkonto_herkunft = "vorschlag"
        auf.beleg.leistung = "Domain beispiel.ch"
        await db.commit()
        return auf.beleg

    async def test_ein_verstoss_laesst_den_beleg_unfreigegeben(self, db, monkeypatch):
        from fastapi import HTTPException

        beleg = await self._beleg(db, "7" * 64)

        async def _pruefen(_user, _beleg):
            return _pruefung(False), None, None

        monkeypatch.setattr(self.router, "_pruefen", _pruefen)
        with pytest.raises(HTTPException) as fehler:
            await self.router.freigeben(beleg.id, self.router.Freigabe(), user=_Nutzer(), db=db)

        assert fehler.value.status_code == 409 and "gesperrt" in fehler.value.detail
        await db.refresh(beleg)
        assert beleg.freigegeben_am is None

    async def test_eine_gescheiterte_buchung_laesst_die_freigabe_stehen(self, db, monkeypatch):
        beleg = await self._beleg(db, "8" * 64)

        async def _pruefen(_user, _beleg):
            return _pruefung(True), None, None

        async def _scheitern(*_args):
            raise self.router._NichtGebucht(409, "«x.pdf» liegt in OneDrive nicht mehr im Eingang. Nicht gebucht.")

        monkeypatch.setattr(self.router, "_pruefen", _pruefen)
        monkeypatch.setattr(self.router, "_buchen_und_ablegen", _scheitern)
        bericht = await self.router.freigeben(beleg.id, self.router.Freigabe(), user=_Nutzer(), db=db)

        assert bericht.freigegeben and not bericht.gebucht
        assert "nicht mehr im Eingang" in bericht.meldungen[0]
        assert beleg.id in {b.id for b in await reg.zu_buchen(db)}

    async def test_die_erste_freigabe_bestaetigt_den_lieferanten(self, db, monkeypatch):
        beleg = await self._beleg(db, "9" * 64)

        async def _pruefen(_user, _beleg):
            return _pruefung(True), None, None

        async def _buchen(*_args):
            return self.router.Buchungsbericht(gebucht=True, abgelegt="Hosttech/2026/x.pdf")

        monkeypatch.setattr(self.router, "_pruefen", _pruefen)
        monkeypatch.setattr(self.router, "_buchen_und_ablegen", _buchen)
        bericht = await self.router.freigeben(beleg.id, self.router.Freigabe(), user=_Nutzer(), db=db)

        assert bericht.freigegeben and bericht.gebucht
        assert self.bestaetigt == ["hosttech"]

    async def test_die_vorschau_schreibt_nichts(self, db, monkeypatch):
        beleg = await self._beleg(db, "0" * 64)
        gesehen = {}

        async def _pruefen(_user, b):
            gesehen.update(konto=b.sollkonto, frei=b.freigegeben_am is not None)
            return _pruefung(True), None, None

        monkeypatch.setattr(self.router, "_pruefen", _pruefen)
        monkeypatch.setattr(self.router, "_pruefung_aus", lambda p, _l: p)
        await self.router.buchung_pruefen(beleg.id, sollkonto="4200", leistung=None, user=_Nutzer(), db=db)

        assert gesehen == {"konto": "4200", "frei": True}
        await db.refresh(beleg)
        assert (beleg.sollkonto, beleg.freigegeben_am) == ("6512", None)

    async def _google_beleg(self, db, hash_):
        auf = await reg.aufnehmen(
            db, datei_hash=hash_, dateiname="5670492345.pdf", quelle="autodownload",
            lieferant_schluessel="google",
        )
        auf.beleg.modul_dokument_id = 1233
        await db.commit()
        return auf.beleg

    async def test_die_wahl_des_dienstes_baut_den_vorschlag_neu(self, db, monkeypatch):
        beleg = await self._google_beleg(db, "f" * 64)

        async def _modulzeile(_user, _beleg):
            return _google(beleg_id=1233, sha256="f" * 64)

        monkeypatch.setattr(self.router, "_modulzeile", _modulzeile)
        zeile = await self.router.lieferant_waehlen(
            beleg.id, self.router.LieferantWahl(schluessel="google_workspace"), user=_Nutzer(), db=db
        )

        assert (zeile.lieferant_schluessel, zeile.sollkonto, zeile.leistung) == ("google_workspace", "6570", "Abo")
        assert [k.anzeigename for k in zeile.lieferant_kandidaten] == ["Google Workspace", "YouTube"]
        assert zeile.buchungstext and zeile.buchungstext.startswith("Google Workspace,")

    async def test_der_verteiler_selbst_ist_nicht_waehlbar(self, db, monkeypatch):
        from fastapi import HTTPException

        beleg = await self._google_beleg(db, "g" * 64)
        with pytest.raises(HTTPException) as fehler:
            await self.router.lieferant_waehlen(
                beleg.id, self.router.LieferantWahl(schluessel="google"), user=_Nutzer(), db=db
            )
        assert fehler.value.status_code == 409

    async def test_vor_der_wahl_gibt_es_keine_freigabe(self, db, monkeypatch):
        from fastapi import HTTPException

        beleg = await self._google_beleg(db, "h" * 64)
        with pytest.raises(HTTPException) as fehler:
            await self.router.freigeben(
                beleg.id, self.router.Freigabe(sollkonto="6570", leistung="Abo"), user=_Nutzer(), db=db
            )

        assert fehler.value.status_code == 409 and "mehrere Dienste" in fehler.value.detail
        await db.refresh(beleg)
        assert beleg.freigegeben_am is None and beleg.sollkonto is None


@pytest.mark.asyncio
@pytest.mark.db
class TestZweiterLauf:
    async def test_eine_entscheidung_ueberlebt_den_naechsten_abgleich(
        self, db: AsyncSession
    ):
        """Ohne diese Sperre machte der naechste Lauf jede Korrektur
        rueckgaengig -- ohne Meldung, also unbemerkt."""
        auf = await reg.aufnehmen(
            db, datei_hash="b" * 64, dateiname="hosttech.pdf", quelle="ablage_hand")
        auf.beleg.sollkonto = "4200"
        auf.beleg.sollkonto_herkunft = "entscheid"

        eing._vorschlag_anlegen(auf.beleg, eing.Vorschlag(sollkonto="6512", zahlweg="karte"))

        assert auf.beleg.sollkonto == "4200"
        assert auf.beleg.sollkonto_herkunft == "entscheid"
        # Auch die Nebenangaben bleiben: wer das Konto entschieden hat, hat den
        # Beleg angesehen.
        assert auf.beleg.zahlweg is None

    async def test_ein_offener_vorschlag_wird_nachgefuehrt(self, db: AsyncSession):
        """Andernfalls blieb ein Beleg auf einer Erwartung stehen, die die
        Deklaration inzwischen berichtigt hat."""
        auf = await reg.aufnehmen(
            db, datei_hash="c" * 64, dateiname="cursor.pdf", quelle="autodownload")
        auf.beleg.sollkonto = "6500"
        auf.beleg.sollkonto_herkunft = "vorschlag"

        eing._vorschlag_anlegen(auf.beleg, eing.Vorschlag(sollkonto="6570"))

        assert auf.beleg.sollkonto == "6570"


class _Drive:
    """OneDrive, so weit die Freigabe es fragt: was liegt wo, und wohin verschoben wurde."""

    def __init__(self, dateien: dict[str, int]):
        self.dateien = dict(dateien)
        self.hochgeladen: dict[str, bytes] = {}

    async def drive_item_by_path(self, pfad):
        groesse = self.dateien.get(pfad)
        return None if groesse is None else {"id": pfad, "size": groesse}

    async def ensure_drive_folder(self, pfad):
        return {"id": pfad}

    async def upload_drive_file(self, pfad, inhalt):
        self.hochgeladen[pfad] = inhalt
        self.dateien[pfad] = len(inhalt)
        return {"id": pfad}

    async def move_drive_item(self, quelle, ordner, name):
        ziel = f"{ordner}/{name}"
        self.dateien[ziel] = self.dateien.pop(quelle)
        return {"id": ziel}


class _Bexio:
    def __init__(self, scheitern=False):
        self.scheitern, self.buchungen = scheitern, []

    async def create_manual_entry(self, nutzlast):
        if self.scheitern:
            raise RuntimeError("Bexio antwortet 500")
        self.buchungen.append(nutzlast)
        return {"id": 9001, "entries": [{"id": 1}]}

    async def attach_manual_entry_file(self, *_args):
        return None

    async def get_journal(self, *_args):
        return [{"description": "Sammler, USA, Usage 2026.08"}] * 2


@pytest.mark.asyncio
@pytest.mark.db
class TestSammelbelegFreigabe:
    """Ein Klick bucht den Monat einmal, legt den Beleg und jede Rechnung ab --
    und ein zweiter findet nichts mehr, was wartet."""

    db = TestFreigabeInEinemSchritt.db

    @pytest.fixture(autouse=True)
    def _ohne_fremdsysteme(self, monkeypatch):
        from decimal import Decimal

        from app.routers import kreditoreneingang as router
        from app.services import kreditorenbuchung as kb

        bestand = _bestand()
        bestand.lieferanten["sammler"] = decl.Lieferant(
            schluessel="sammler", ordner="Sammler", sollkonto="6570", zahlweg=("karte",),
            steuer=(decl.Steuerzeile(None, "bezugssteuer"),), aktiv=True, bestaetigt=True,
            leistung="Usage", buchung="sammelbeleg",
        )
        self.zeilen: list[dict] = []
        self.drive = _Drive({})
        self.bexio = _Bexio()

        async def _modulbestand(_user):
            return "http://modul", "t", self.zeilen

        async def _bexio_lesen(_client, _tag):
            return kb.Bexiostand(
                konto_id={"6570": 1, "2120": 2, "2203": 3}, steuer_id={"BZB81": 9},
                waehrung_id={"CHF": 1, "USD": 5}, journal=[],
            )

        async def _kurs(_w, _tag):
            return Decimal("0.8175")

        async def _datei(_url, _token, kennung):
            return _pdf(f"Rechnung {kennung}"), "x.pdf", "application/pdf"

        monkeypatch.setattr(router.decl, "laden", lambda: bestand)
        monkeypatch.setattr(router, "_modulbestand", _modulbestand)
        monkeypatch.setattr(router, "_eingangsordner", lambda _u: "_OPEN/InnoSmith")
        monkeypatch.setattr(router, "bexio_zugang", lambda _u: self.bexio)
        monkeypatch.setattr(router.kb, "bexio_lesen", _bexio_lesen)
        monkeypatch.setattr(router.bazg_kurse, "monatsmittel", _kurs)
        monkeypatch.setattr(router, "belegdatei_holen", _datei)
        monkeypatch.setattr(router, "get_graph_client", lambda: self.drive)
        self.router = router

    async def _rechnung(self, db, kennung: int, nummer: str, tag: str, betrag: float):
        pfad = f"_OPEN/InnoSmith/Invoice-{nummer}.pdf"
        auf = await reg.aufnehmen(
            db, datei_hash=f"{kennung:064d}", dateiname=f"Invoice-{nummer}.pdf", quelle="autodownload",
            graph_pfad=pfad, lieferant_schluessel="sammler", rechnungsnummer=nummer,
        )
        auf.beleg.modul_dokument_id = kennung
        self.drive.dateien[f"Finanzen/Kreditoren/{pfad}"] = len(_pdf(f"Rechnung {kennung}"))
        self.zeilen.append({
            "beleg_id": kennung, "lieferant_schluessel": "sammler", "rechnungsnummer": nummer,
            "datum": tag, "betrag": betrag, "waehrung": "USD", "abrechnungszyklus": "USAGE_BASED",
            "beleg_ordner": "_OPEN/InnoSmith", "beleg_datei": f"Invoice-{nummer}.pdf",
            "lieferant_original": "Sammler, Inc.",
        })
        return auf.beleg

    async def test_ein_klick_bucht_den_monat_und_legt_alles_ab(self, db):
        from sqlalchemy import select

        from app.models.models import Kreditorenbeleg
        from fastapi import HTTPException

        erste = await self._rechnung(db, 880001, "S-0001", "2026-08-03", 100.03)
        zweite = await self._rechnung(db, 880002, "S-0002", "2026-08-03", 100.27)
        await db.commit()
        # Die nächste Rechnung ist vom September: der August ist vollständig.
        self.zeilen.append({
            "beleg_id": None, "lieferant_schluessel": "sammler", "rechnungsnummer": "S-0003",
            "datum": "2026-09-01", "beleg_ordner": "Sammler/2026", "beleg_datei": "x.pdf",
        })

        vorschau = await self.router.sammelbeleg_vorschau("sammler", 2026, 8, user=_Nutzer(), db=db)
        assert vorschau.vollstaendig and vorschau.buchbar, vorschau.vollstaendig_grund
        await db.rollback()

        bericht = await self.router.sammelbeleg_freigeben("sammler", 2026, 8, user=_Nutzer(), db=db)

        assert bericht.gebucht and bericht.meldungen == [], bericht.meldungen
        assert bericht.abgelegt == "Sammler/2026/Sammelbelege/Sammler Sammelbeleg August 2026 KK.pdf"
        [buchung] = self.bexio.buchungen
        assert buchung["date"] == "2026-08-31"
        assert buchung["entries"][0]["amount"] == 200.30

        sammel = (await db.execute(
            select(Kreditorenbeleg).where(Kreditorenbeleg.rechnungsnummer == "Sammelbeleg 2026.08",
                                          Kreditorenbeleg.lieferant_schluessel == "sammler")
        )).scalar_one()
        assert (sammel.quelle, sammel.belegart, sammel.bexio_referenz) == ("erzeugt", "sammelbeleg", "manual_entry:9001")
        for b in (erste, zweite):
            await db.refresh(b)
            assert b.sammelbeleg_id == sammel.id and b.abgelegt_am is not None and b.gebucht_am is None
        assert {erste.archiv_pfad, zweite.archiv_pfad} == {
            "Sammler/2026/Sammler Usage 03.08.2026 KK.pdf",
            "Sammler/2026/Sammler Usage 03.08.2026 KK (2).pdf",
        }

        with pytest.raises(HTTPException) as zweimal:
            await self.router.sammelbeleg_freigeben("sammler", 2026, 8, user=_Nutzer(), db=db)
        assert zweimal.value.status_code == 409 and "wartet keine Rechnung" in zweimal.value.detail
        assert len(self.bexio.buchungen) == 1

    async def test_eine_gescheiterte_buchung_schreibt_nichts(self, db):
        from fastapi import HTTPException

        from app.schemas.kreditoreneingang import SammelFreigabe

        beleg = await self._rechnung(db, 880003, "S-0003", "2026-08-05", 50.0)
        await db.commit()
        self.bexio.scheitern = True

        with pytest.raises(HTTPException) as fehler:
            await self.router.sammelbeleg_freigeben(
                "sammler", 2026, 8, SammelFreigabe(vollstaendig_bestaetigt=True), user=_Nutzer(), db=db
            )

        assert fehler.value.status_code == 502
        await db.refresh(beleg)
        assert beleg.sammelbeleg_id is None and beleg.freigegeben_am is None and beleg.abgelegt_am is None
        assert self.drive.hochgeladen == {}

    async def test_ohne_vollstaendigkeitsbeleg_bucht_erst_die_bestaetigung(self, db):
        from app.schemas.kreditoreneingang import SammelFreigabe
        from fastapi import HTTPException

        beleg = await self._rechnung(db, 880005, "S-0005", "2026-08-05", 50.0)
        await db.commit()

        vorschau = await self.router.sammelbeleg_vorschau("sammler", 2026, 8, user=_Nutzer(), db=db)
        assert vorschau.bereit and not vorschau.vollstaendig and not vorschau.buchbar
        await db.rollback()

        with pytest.raises(HTTPException) as fehler:
            await self.router.sammelbeleg_freigeben("sammler", 2026, 8, user=_Nutzer(), db=db)
        assert fehler.value.status_code == 409 and "Folgemonat" in fehler.value.detail
        assert self.bexio.buchungen == []

        bericht = await self.router.sammelbeleg_freigeben(
            "sammler", 2026, 8, SammelFreigabe(vollstaendig_bestaetigt=True), user=_Nutzer(), db=db
        )
        assert bericht.gebucht and len(self.bexio.buchungen) == 1
        await db.refresh(beleg)
        assert beleg.sammelbeleg_id is not None

    async def test_der_laufende_monat_wird_nicht_freigegeben(self, db):
        from datetime import date

        from fastapi import HTTPException

        heute = date.today()
        await self._rechnung(db, 880004, "S-0004", heute.isoformat(), 10.0)
        await db.commit()

        with pytest.raises(HTTPException) as fehler:
            await self.router.sammelbeleg_freigeben("sammler", heute.year, heute.month, user=_Nutzer(), db=db)
        assert fehler.value.status_code == 409 and "läuft noch" in fehler.value.detail


def _pdf(text: str) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())
