"""Die Ablage ins Kundenarchiv — Zielordner, Namen und die drei Regeln.

Gemessen am 21.09.2026 gegen das echte Archiv: elf von 25 aktiven Verträgen
hätten mit dem alten Rückfall auf den technischen Schlüssel einen **neuen**
Ordner angelegt. Der erste Test hier hält genau das fest.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import debitoren_ablage as ab
from app.services.debitorenvertraege import Kunde, Vertragsdaten, Bestand


def _kunde(schluessel: str, ablage: str | None = None) -> Kunde:
    return Kunde(schluessel=schluessel, empfaenger="x@y.ch", ablage=ablage)


def _vertrag(schluessel: str, bezeichnung: str, kunde: Kunde) -> Vertragsdaten:
    return Vertragsdaten(
        schluessel=schluessel, bezeichnung=bezeichnung, kunde=kunde, mwst=8.1
    )


def _bestand(*vertraege: Vertragsdaten) -> Bestand:
    return Bestand(
        vertraege={v.schluessel: v for v in vertraege},
        kunden={v.kunde.schluessel: v.kunde for v in vertraege},
        vorgaben={},
        offen=[],
    )


# ── Der Ordner wird nicht erfunden ───────────────────────


class TestOhneAlias:
    def test_kein_rueckfall_auf_den_schluessel(self):
        """Der technische Schlüssel darf nie zum Ordnernamen werden.

        Sonst entstünde «bfh» neben «Berner Fachhochschule», und eine Ablage,
        die einen Ordner anlegt, sieht aus wie eine, die abgelegt hat.
        """
        assert _kunde("bfh").ordner is None

    def test_alias_gilt(self):
        assert _kunde("bfh", "Berner Fachhochschule").ordner == "Berner Fachhochschule"

    def test_zielordner_sperrt_ohne_alias(self):
        v = _vertrag("cas", "CAS IBD HS26", _kunde("bfh"))
        assert ab.zielordner(_bestand(v), v, jahr=2026) is None

    def test_fehlender_alias_steht_im_befund(self):
        """Der Mangel wird gemeldet, nicht verschwiegen."""
        from app.services.debitorenvertraege import laden

        _bestand_echt, befund = laden()
        maengel = befund.get("bemaengelt") or []
        # Die sechs Aliase sind gepflegt -- also darf kein aktiver Vertrag
        # mehr wegen fehlendem Ordner gesperrt sein.
        gesperrt = [m for m in maengel if "Ablageordner" in m]
        assert gesperrt == [], f"Ohne Ordner: {gesperrt}"


class TestAliaseGepflegt:
    """Die sechs am 21.09.2026 nachgetragenen Aliase, gegen die Datei."""

    @pytest.mark.parametrize("schluessel,erwartet", [
        ("bfh", "Berner Fachhochschule"),
        ("gsw", "GSW Treuhand AG"),
        ("stadt-bern", "Stadt Bern"),
        ("matrics", "MATRICS Consult Ltd"),
        ("overpar", "Overpar Solutions AG"),
        ("gemeinde-koeniz", "Gemeinde Köniz"),
    ])
    def test_alias_steht_in_der_datei(self, schluessel, erwartet):
        from app.services.debitorenvertraege import laden

        bestand, _ = laden()
        kunde = bestand.kunden.get(schluessel)
        assert kunde is not None, f"Kundschaft {schluessel} fehlt"
        assert kunde.ordner == erwartet

    def test_jeder_aktive_vertrag_hat_einen_ordner(self):
        from app.services.debitorenvertraege import laden

        bestand, _ = laden()
        ohne = sorted({
            v.kunde_am(date(2026, 12, 31)).schluessel
            for v in bestand.aktive()
            if v.kunde_am(date(2026, 12, 31)).ordner is None
        })
        assert ohne == [], f"Kundschaft ohne Ablageordner: {ohne}"


# ── Wohin genau ──────────────────────────────────────────


class TestZielordner:
    def test_ein_vertrag_ohne_projektebene(self):
        """Ein einziger Vertrag braucht keinen Unterordner."""
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))
        assert ab.zielordner(_bestand(v), v, jahr=2026) == (
            "Finanzen/Debitoren/AUE/2026"
        )

    def test_mehrere_vertraege_mit_projektebene(self):
        k = _kunde("mba", "MBA")
        a = _vertrag("cheetah", "Cheetah", k)
        b = _vertrag("coflow", "COflow", k)
        assert ab.zielordner(_bestand(a, b), a, jahr=2026) == (
            "Finanzen/Debitoren/MBA/2026/Cheetah"
        )

    def test_ruhender_geschwistervertrag_erzwingt_keine_ebene(self):
        """Gezählt werden die aktiven Verträge, nicht alle.

        MBA trägt ruhende Einträge aus früheren Jahren. Zählten sie mit,
        entstünde für den letzten verbliebenen Vertrag eine Projektebene, die
        es vorher nicht gab.
        """
        k = _kunde("mba", "MBA")
        aktiv = _vertrag("cheetah", "Cheetah", k)
        ruhend = Vertragsdaten(
            schluessel="alt", bezeichnung="Altes", kunde=k, mwst=8.1, ruhend=True
        )
        assert ab.zielordner(_bestand(aktiv, ruhend), aktiv, jahr=2026) == (
            "Finanzen/Debitoren/MBA/2026"
        )

    def test_jahr_kommt_aus_der_periode(self):
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))
        assert "/2025" in (ab.zielordner(_bestand(v), v, jahr=2025) or "")

    def test_projektebene_laesst_sich_erzwingen(self):
        """Die Sperrklinke gibt die Ebene von aussen vor."""
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))
        assert ab.zielordner(_bestand(v), v, jahr=2026, projektebene=True) == (
            "Finanzen/Debitoren/AUE/2026/deinklima"
        )

    def test_bfh_traegt_den_durchgang_im_ordnernamen(self):
        """«CAS IBD HS26» — der Durchgang ist Teil der Vertragsbezeichnung."""
        k = _kunde("bfh", "Berner Fachhochschule")
        a = _vertrag("hs26", "CAS IBD HS26", k)
        b = _vertrag("fs26", "CAS TCM FS26", k)
        assert ab.zielordner(_bestand(a, b), a, jahr=2026) == (
            "Finanzen/Debitoren/Berner Fachhochschule/2026/CAS IBD HS26"
        )
        assert ab.zielordner(_bestand(a, b), b, jahr=2026) == (
            "Finanzen/Debitoren/Berner Fachhochschule/2026/CAS TCM FS26"
        )

    def test_gegenpartei_des_jahres_nicht_die_heutige(self):
        """``deinklima`` lief bis Ende 2025 über die Wyss Academy.

        Eine Nachfakturierung für 2025 gehört dorthin und nicht ins AUE-Archiv.
        """
        heute = _kunde("aue", "AUE")
        damals = _kunde("wyss-academy", "Wyss Academy")
        v = Vertragsdaten(
            schluessel="dek", bezeichnung="deinklima", kunde=heute, mwst=8.1,
            frueher=((date(2025, 12, 31), damals),),
        )
        assert ab.zielordner(_bestand(v), v, jahr=2025) == (
            "Finanzen/Debitoren/Wyss Academy/2025"
        )
        assert ab.zielordner(_bestand(v), v, jahr=2026) == (
            "Finanzen/Debitoren/AUE/2026"
        )


class TestNamen:
    def test_rechnungsname_wie_im_archiv(self):
        """Gemessen: «RE-00703 InnoSmith Rechnung Aug 2026.pdf»."""
        assert ab.rechnungsname("RE-00703", 2026, 8) == (
            "RE-00703 InnoSmith Rechnung Aug 2026.pdf"
        )

    def test_rechnungsname_ohne_projekt(self):
        """Der Projektname steht nicht drin — die Nummer unterscheidet."""
        name = ab.rechnungsname("RE-00611", 2026, 1)
        assert "Cheetah" not in name
        assert name == "RE-00611 InnoSmith Rechnung Jan 2026.pdf"

    def test_rapportname_wie_im_archiv(self):
        assert ab.rapportname("OnboardingQS", 2026, 8) == (
            "Leistungsrapport Aug 2026 OnboardingQS.pdf"
        )

    def test_maerz_wird_maer_geschrieben(self):
        """«Mär», nicht «Mrz» — so steht es in sechs Jahren Archiv."""
        assert ab.MONAT_KURZ[3] == "Mär"
        assert "Mär 2026" in ab.rapportname("deinklima", 2026, 3)

    def test_alle_zwoelf_monate(self):
        assert sorted(ab.MONAT_KURZ) == list(range(1, 13))

    def test_verbotene_zeichen_werden_ersetzt_nicht_entfernt(self):
        """«KI/ML» soll «KI ML» werden und nicht «KIML»."""
        assert ab.sauber("KI/ML") == "KI ML"
        assert ab.sauber("Projekt: Neu") == "Projekt Neu"
        assert ab.sauber("  doppelt  Leer ") == "doppelt Leer"

    def test_punkt_am_ende_faellt_weg(self):
        """OneDrive lehnt Namen ab, die auf einen Punkt enden."""
        assert ab.sauber("Firma AG.") == "Firma AG"


# ── Der Graph-Doppelgänger ───────────────────────────────


class FakeGraph:
    """Ein OneDrive im Gedächtnis. Zählt Uploads und verweigert Ersetzen."""

    def __init__(self, vorhanden: dict[str, int] | None = None,
                 ordner: set[str] | None = None):
        self.dateien: dict[str, int] = dict(vorhanden or {})
        self.ordner: set[str] = set(ordner or ())
        self.uploads: list[str] = []
        self.verschluckt = 0
        self.abfragen: list[str] = []

    async def list_drive_items(self, pfad, top=20):
        pfad = pfad.strip("/")
        self.abfragen.append(pfad)
        eintraege: list[dict] = []
        for d in self.ordner:
            if d.rsplit("/", 1)[0] == pfad:
                eintraege.append({"name": d.rsplit("/", 1)[-1], "folder": {}})
        for f in self.dateien:
            if f.rsplit("/", 1)[0] == pfad:
                eintraege.append({"name": f.rsplit("/", 1)[-1], "file": {}})
        return eintraege

    async def drive_item_by_path(self, pfad: str) -> dict | None:
        pfad = pfad.strip("/")
        if pfad in self.dateien:
            return {"id": f"id-{pfad}", "name": pfad.rsplit("/", 1)[-1],
                    "size": self.dateien[pfad], "file": {}}
        if pfad in self.ordner:
            return {"id": f"dir-{pfad}", "name": pfad.rsplit("/", 1)[-1],
                    "folder": {}}
        return None

    async def ensure_drive_folder(self, pfad: str) -> dict:
        teile = [t for t in pfad.strip("/").split("/") if t]
        bisher = ""
        for t in teile:
            bisher = f"{bisher}/{t}" if bisher else t
            self.ordner.add(bisher)
        return {"id": f"dir-{pfad}"}

    async def upload_drive_file(self, pfad, inhalt, *, ueberschreiben=False):
        pfad = pfad.strip("/")
        if pfad in self.dateien and not ueberschreiben:
            raise RuntimeError("nameAlreadyExists")
        self.uploads.append(pfad)
        if self.verschluckt > 0:
            # Antwortet mit Erfolg und legt nichts ab -- der Fehler, den man
            # erst im naechsten Steuerjahr bemerkt.
            self.verschluckt -= 1
            return {}
        self.dateien[pfad] = len(inhalt)
        return {"id": f"id-{pfad}", "size": len(inhalt)}


class FakeBeilage:
    def __init__(self, rechnung_id, nummer, schluessel, bezeichnung,
                 dokument=b"D" * 100, rapport=None):
        self.rechnung_id = rechnung_id
        self.nummer = nummer
        self.vertrag_schluessel = schluessel
        self.bezeichnung = bezeichnung
        self.dokument = dokument
        self.rapport = rapport


# ── Die drei Regeln ──────────────────────────────────────


@pytest.mark.asyncio
class TestAblegen:
    async def test_beide_dateien_landen_im_archiv(self):
        """Verschmolzenes Dokument plus Rapport einzeln — wie heute im Archiv."""
        v = _vertrag("onb", "OnboardingQS", _kunde("sb", "Swiss Bankers"))
        g = FakeGraph()
        ergebnis = await ab.ablegen(
            g,
            [FakeBeilage(1, "RE-00703", "onb", "OnboardingQS",
                         dokument=b"D" * 380000, rapport=b"R" * 74000)],
            _bestand(v), jahr=2026, monat=8,
        )
        assert ergebnis[0].gelungen
        assert sorted(g.dateien) == [
            "Finanzen/Debitoren/Swiss Bankers/2026/"
            "Leistungsrapport Aug 2026 OnboardingQS.pdf",
            "Finanzen/Debitoren/Swiss Bankers/2026/"
            "RE-00703 InnoSmith Rechnung Aug 2026.pdf",
        ]

    async def test_ohne_rapport_nur_eine_datei(self):
        """BFH hat keine Rapportpflicht — dann liegt dort nur die Rechnung."""
        v = _vertrag("cas", "CAS IBD HS26", _kunde("bfh", "Berner Fachhochschule"))
        g = FakeGraph()
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(2, "RE-00706", "cas", "CAS IBD HS26")],
            _bestand(v), jahr=2026, monat=9,
        )
        assert ergebnis[0].gelungen
        assert len(g.dateien) == 1
        assert list(g.dateien)[0].endswith("RE-00706 InnoSmith Rechnung Sep 2026.pdf")

    async def test_nie_ueberschreiben(self):
        """Eine vorhandene Datei wird gelesen, nicht ersetzt — und das ist ok."""
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))
        pfad = ("Finanzen/Debitoren/AUE/2026/"
                "RE-00700 InnoSmith Rechnung Aug 2026.pdf")
        g = FakeGraph({pfad: 371624})
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(3, "RE-00700", "dek", "deinklima")],
            _bestand(v), jahr=2026, monat=8,
        )
        assert g.uploads == [], "Es wurde trotz vorhandener Datei geschrieben"
        assert ergebnis[0].gelungen
        assert ergebnis[0].dateien[0].lag_schon
        assert g.dateien[pfad] == 371624, "Die Grösse hat sich verändert"

    async def test_nachzaehlen_faengt_den_stillen_fehlschlag(self):
        """Ein Upload, der mit Erfolg antwortet und nichts ablegt, gilt nicht."""
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))
        g = FakeGraph()
        g.verschluckt = 1
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(4, "RE-00700", "dek", "deinklima")],
            _bestand(v), jahr=2026, monat=8,
        )
        assert not ergebnis[0].gelungen
        assert "nicht auffindbar" in ergebnis[0].dateien[0].hindernis

    async def test_ohne_alias_wird_nichts_geschrieben(self):
        """Der wichtigste Fall: kein Ordner heisst keine Ablage."""
        v = _vertrag("cas", "CAS IBD HS26", _kunde("bfh"))
        g = FakeGraph()
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(5, "RE-00706", "cas", "CAS IBD HS26")],
            _bestand(v), jahr=2026, monat=9,
        )
        assert not ergebnis[0].gelungen
        assert "kein Ablageordner" in ergebnis[0].hindernis
        assert g.dateien == {} and g.ordner == set()

    async def test_ein_fehlschlag_kostet_die_anderen_nicht(self):
        """Fehler sind örtlich begrenzt."""
        ka = _kunde("aue", "AUE")
        kb = _kunde("bfh")  # ohne Alias -- scheitert bewusst
        a = _vertrag("dek", "deinklima", ka)
        b = _vertrag("cas", "CAS", kb)
        g = FakeGraph()
        ergebnis = await ab.ablegen(
            g,
            [FakeBeilage(6, "RE-00706", "cas", "CAS"),
             FakeBeilage(7, "RE-00700", "dek", "deinklima")],
            _bestand(a, b), jahr=2026, monat=8,
        )
        assert len(ergebnis) == 2
        assert not ergebnis[0].gelungen
        assert ergebnis[1].gelungen
        assert len(g.dateien) == 1

    async def test_unbekannter_vertrag_wird_gemeldet(self):
        g = FakeGraph()
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(8, "RE-00999", "gibtsnicht", "?")],
            _bestand(), jahr=2026, monat=8,
        )
        assert not ergebnis[0].gelungen
        assert "nicht mehr auffindbar" in ergebnis[0].hindernis
        assert g.dateien == {}

    async def test_ordnerkette_entsteht(self):
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))
        g = FakeGraph()
        await ab.ablegen(
            g, [FakeBeilage(9, "RE-00700", "dek", "deinklima")],
            _bestand(v), jahr=2026, monat=8,
        )
        assert "Finanzen" in g.ordner
        assert "Finanzen/Debitoren" in g.ordner
        assert "Finanzen/Debitoren/AUE" in g.ordner
        assert "Finanzen/Debitoren/AUE/2026" in g.ordner

    async def test_graph_ausfall_bricht_nicht_durch(self):
        """Ein Zeitausfall bei einer Rechnung darf die anderen nicht kosten."""
        v = _vertrag("dek", "deinklima", _kunde("aue", "AUE"))

        class Kaputt(FakeGraph):
            async def ensure_drive_folder(self, pfad):
                raise TimeoutError("OneDrive antwortet nicht")

        ergebnis = await ab.ablegen(
            Kaputt(), [FakeBeilage(10, "RE-00700", "dek", "deinklima")],
            _bestand(v), jahr=2026, monat=8,
        )
        assert not ergebnis[0].gelungen
        assert "TimeoutError" in ergebnis[0].hindernis


# ── Die Sperrklinke: einmal Projektordner, immer Projektordner ──


@pytest.mark.asyncio
class TestSperrklinke:
    """Fällt ein Kunde auf einen Vertrag zurück, bleibt die Projektebene.

    Sonst lägen bei MBA die Rechnungen eines Jahres teils in Unterordnern,
    teils daneben — je nachdem, wie viele Verträge zu jenem Zeitpunkt liefen.
    """

    def _einer(self):
        return _vertrag("cheetah", "Cheetah", _kunde("mba", "MBA"))

    async def test_mehrere_vertraege_fragen_das_archiv_nicht(self):
        """Bei zwei Verträgen ist die Ebene ohne Abfrage entschieden."""
        k = _kunde("mba", "MBA")
        a, b = _vertrag("c", "Cheetah", k), _vertrag("f", "COflow", k)
        g = FakeGraph()
        assert await ab.projektebene_gilt(g, _bestand(a, b), a, jahr=2026)
        assert g.abfragen == []

    async def test_bestehender_projektordner_haelt_die_ebene(self):
        v = self._einer()
        g = FakeGraph(ordner={"Finanzen/Debitoren/MBA/2026/Cheetah"})
        assert await ab.projektebene_gilt(g, _bestand(v), v, jahr=2026)

    async def test_fremder_projektordner_haelt_die_ebene_auch(self):
        """Die Klinke gehört der Kundschaft, nicht dem einzelnen Projekt."""
        v = self._einer()
        g = FakeGraph(ordner={"Finanzen/Debitoren/MBA/2026/Subventix"})
        assert await ab.projektebene_gilt(g, _bestand(v), v, jahr=2026)

    async def test_flaches_jahr_bleibt_flach(self):
        v = self._einer()
        g = FakeGraph({"Finanzen/Debitoren/MBA/2026/RE-1.pdf": 10})
        assert not await ab.projektebene_gilt(g, _bestand(v), v, jahr=2026)

    async def test_vorjahr_entscheidet_im_leeren_januar(self):
        """Im neuen Jahr ist der Ordner leer — ohne Rückblick begänne es flach."""
        v = self._einer()
        g = FakeGraph(ordner={"Finanzen/Debitoren/MBA/2026/Cheetah"})
        assert await ab.projektebene_gilt(g, _bestand(v), v, jahr=2027)

    async def test_flaches_jahr_schlaegt_das_vorjahr(self):
        """Ist das laufende Jahr da und flach, ist die Frage beantwortet.

        Der Blick ins Vorjahr wäre dann irreführend: die Entscheidung für
        dieses Jahr ist schon gefallen und sichtbar.
        """
        v = self._einer()
        g = FakeGraph(
            {"Finanzen/Debitoren/MBA/2026/RE-1.pdf": 10},
            ordner={"Finanzen/Debitoren/MBA/2025/Cheetah"},
        )
        assert not await ab.projektebene_gilt(g, _bestand(v), v, jahr=2026)

    async def test_gar_kein_archiv_bleibt_flach(self):
        v = self._einer()
        assert not await ab.projektebene_gilt(FakeGraph(), _bestand(v), v, jahr=2026)

    async def test_antwort_wird_je_kundschaft_einmal_geholt(self):
        """Zwanzig Rechnungen sollen nicht zwanzig Abfragen erzeugen."""
        v = self._einer()
        g = FakeGraph()
        gedaechtnis: dict[str, bool] = {}
        for _ in range(5):
            await ab.projektebene_gilt(
                g, _bestand(v), v, jahr=2026, gedaechtnis=gedaechtnis
            )
        assert len(g.abfragen) == 2, g.abfragen  # 2026 und 2025, einmal

    async def test_ablegen_nutzt_die_klinke(self):
        """Ein einziger Vertrag, aber bestehender Projektordner → Unterordner."""
        v = self._einer()
        g = FakeGraph(ordner={"Finanzen/Debitoren/MBA/2026/Cheetah"})
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(11, "RE-00696", "cheetah", "Cheetah")],
            _bestand(v), jahr=2026, monat=8,
        )
        assert ergebnis[0].ordner == "Finanzen/Debitoren/MBA/2026/Cheetah"
        assert ergebnis[0].gelungen

    async def test_unlesbares_archiv_legt_nicht_flach_ab(self):
        """Raten in Richtung «flach» streut neben bestehende Projektordner.

        Und dort fällt es nicht auf. Lieber diese Rechnung liegen lassen.
        """
        v = self._einer()

        class Blind(FakeGraph):
            async def list_drive_items(self, pfad, top=20):
                raise TimeoutError("OneDrive antwortet nicht")

        g = Blind()
        ergebnis = await ab.ablegen(
            g, [FakeBeilage(12, "RE-00696", "cheetah", "Cheetah")],
            _bestand(v), jahr=2026, monat=8,
        )
        assert not ergebnis[0].gelungen
        assert "Projektebene unklar" in ergebnis[0].hindernis
        assert g.uploads == []
