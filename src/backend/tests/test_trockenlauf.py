"""Die Schreibsperre — was sie anhält, was sie durchlässt, was sie abweist."""

from __future__ import annotations

from datetime import date

import pytest

from app.services import trockenlauf as t
from app.services.trockenlauf import (
    BexioSperre,
    GraphSperre,
    TogglSperre,
    UnbekannterAufruf,
)


class EchtBexio:
    def __init__(self):
        self.geschrieben: list[str] = []

    async def get_invoice(self, invoice_id):
        return {"id": invoice_id, "document_nr": "RE-00700"}

    async def search_invoices(self, *a, **k):
        return [{"id": 1}]

    async def create_invoice_from_order(self, order_id):
        self.geschrieben.append(f"create({order_id})")
        return {"id": 99, "document_nr": "RE-99999"}

    async def update_invoice(self, invoice_id, **felder):
        self.geschrieben.append(f"update({invoice_id})")
        return {"id": invoice_id, **felder}

    async def issue_invoice(self, invoice_id):
        self.geschrieben.append(f"issue({invoice_id})")
        return True

    async def update_invoice_position(self, *a, **k):
        self.geschrieben.append("position")
        return {}

    async def delete_invoice_position(self, *a, **k):
        self.geschrieben.append("entfernen")

    async def neuer_schreibweg(self, *a, **k):  # absichtlich nicht eingeordnet
        self.geschrieben.append("neu")


class EchtGraph:
    def __init__(self, vorhanden: dict[str, int] | None = None):
        self.dateien = dict(vorhanden or {})
        self.getan: list[str] = []

    async def create_draft(self, **kw):
        self.getan.append("draft")
        return {"id": "AAA"}

    async def add_attachment(self, *a, **k):
        self.getan.append("anhang")
        return {"id": "BBB"}

    async def send_draft(self, message_id):
        self.getan.append("senden")

    async def ensure_drive_folder(self, pfad):
        self.getan.append(f"ordner:{pfad}")
        return {"id": "CCC"}

    async def upload_drive_file(self, pfad, inhalt, **kw):
        self.getan.append(f"upload:{pfad}")
        self.dateien[pfad] = len(inhalt)
        return {"id": "DDD"}

    async def drive_item_by_path(self, pfad):
        pfad = pfad.strip("/")
        if pfad in self.dateien:
            return {"name": pfad.rsplit("/", 1)[-1], "file": {}, "size": self.dateien[pfad]}
        return None

    async def list_drive_items(self, pfad, top=20):
        pfad = pfad.strip("/")
        return [
            {"name": f.rsplit("/", 1)[-1], "file": {}}
            for f in self.dateien if f.rsplit("/", 1)[0] == pfad
        ]


# ── Die Grundunterscheidung ────────────────────────────────────────────


@pytest.mark.asyncio
class TestLesenUndSchreiben:
    async def test_lesen_geht_an_das_echte_system(self):
        """Der Trockenlauf arbeitet mit echten Daten, nicht mit erfundenen."""
        echt = EchtBexio()
        sperre = BexioSperre(echt)
        assert (await sperre.get_invoice(7))["document_nr"] == "RE-00700"

    async def test_suchen_ist_lesen_obwohl_es_post_ist(self):
        """Bexios Suche ist ein POST — «POST heisst schreiben» wäre falsch."""
        sperre = BexioSperre(EchtBexio())
        assert await sperre.search_invoices() == [{"id": 1}]

    async def test_schreiben_erreicht_das_echte_system_nicht(self):
        echt = EchtBexio()
        sperre = BexioSperre(echt)
        await sperre.issue_invoice(12)
        await sperre.update_invoice_position(1, 2, "default", {})
        await sperre.delete_invoice_position(1, 2, "default")
        assert echt.geschrieben == []

    async def test_schreibversuche_stehen_im_protokoll(self):
        sperre = BexioSperre(EchtBexio())
        await sperre.issue_invoice(12)
        assert sperre.protokoll() == ["issue_invoice(12)"]

    async def test_unbekanntes_wird_abgewiesen(self):
        """Der Kern: was nicht eingeordnet ist, läuft nicht — auch nicht lesend.

        Eine Sperre, die Unbekanntes durchreicht, lässt genau die Methode
        durch, die nach ihrer Entstehung dazukam.
        """
        sperre = BexioSperre(EchtBexio())
        with pytest.raises(UnbekannterAufruf, match="neuer_schreibweg"):
            await sperre.neuer_schreibweg()

    async def test_pdf_rumpf_steht_nicht_im_protokoll(self):
        sperre = GraphSperre(EchtGraph())
        await sperre.add_attachment("AAA", "Rechnung.pdf", b"%PDF" + b"x" * 5000)
        assert "<5004 Bytes>" in sperre.protokoll()[0]
        assert "xxxx" not in sperre.protokoll()[0]

    async def test_senden_ist_gesperrt_obwohl_der_lauf_es_nie_ruft(self):
        """Die eiserne Regel braucht keine zweite Gelegenheit, gebrochen zu werden."""
        echt = EchtGraph()
        await GraphSperre(echt).send_draft("AAA")
        assert echt.getan == []


# ── Das Umdatieren läuft echt durch ────────────────────────────────────


@pytest.mark.asyncio
class TestErzeugenImTrockenlauf:
    """Der wertvollste Teil: das Datum wird gerechnet, nicht übersprungen.

    Ein falsches Rechnungsdatum verschiebt einen ganzen Monatsumsatz, ohne
    dass irgendwo ein Fehler erschiene. Genau diese Rechnung muss der
    Trockenlauf durchlaufen.
    """

    async def test_datum_wird_auf_den_stichtag_gerechnet(self):
        from app.services.debitoren_erzeugen import erzeugen

        echt = EchtBexio()
        protokoll = await erzeugen(
            BexioSperre(echt), [(4711, "AUE")], stichtag=date(2026, 8, 31)
        )
        eintrag = protokoll.eintraege[0]
        assert eintrag.datum == "2026-08-31"
        assert echt.geschrieben == []

    async def test_zahlungsfrist_bleibt_erhalten(self):
        """Verschoben werden beide Daten um denselben Betrag."""
        from app.services.debitoren_erzeugen import erzeugen

        protokoll = await erzeugen(
            BexioSperre(EchtBexio()), [(4711, "AUE")], stichtag=date(2026, 8, 31)
        )
        eintrag = protokoll.eintraege[0]
        abstand = date.fromisoformat(eintrag.faellig) - date.fromisoformat(eintrag.datum)
        assert abstand == t._FRIST_PLATZHALTER

    async def test_nummer_ist_als_platzhalter_erkennbar(self):
        """Eine Rechnungsnummer entsteht erst beim Anlegen. Nichts vortäuschen."""
        from app.services.debitoren_erzeugen import erzeugen

        protokoll = await erzeugen(
            BexioSperre(EchtBexio()), [(4711, "AUE")], stichtag=date(2026, 8, 31)
        )
        assert protokoll.eintraege[0].nummer == t.PLATZHALTER


# ── Der Schatten ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSchatten:
    """Nachzählen und Sperrklinke hängen an unterdrückten Schreibaufrufen."""

    async def test_vorgetaeuschte_datei_ist_danach_auffindbar(self):
        sperre = GraphSperre(EchtGraph())
        await sperre.upload_drive_file("A/B/x.pdf", b"12345")
        gefunden = await sperre.drive_item_by_path("A/B/x.pdf")
        assert gefunden["size"] == 5

    async def test_echte_dateien_bleiben_sichtbar(self):
        sperre = GraphSperre(EchtGraph({"A/alt.pdf": 9}))
        assert (await sperre.drive_item_by_path("A/alt.pdf"))["size"] == 9

    async def test_ordnerkette_erscheint_vollstaendig(self):
        sperre = GraphSperre(EchtGraph())
        await sperre.ensure_drive_folder("A/B/C")
        for pfad in ("A", "A/B", "A/B/C"):
            eintrag = await sperre.drive_item_by_path(pfad)
            assert "folder" in eintrag, pfad

    async def test_nachzaehlen_meldet_keinen_phantomfehler(self):
        """Ohne Schatten meldete jede Ablage «nach dem Hochladen nicht auffindbar»."""
        from app.services import debitoren_ablage as ab

        sperre = GraphSperre(EchtGraph())
        eintrag = await ab._eine_datei(sperre, "A/B", "x.pdf", b"12345")
        assert eintrag.gelungen and not eintrag.hindernis

    async def test_bereits_abgelegtes_wird_auch_trocken_erkannt(self):
        from app.services import debitoren_ablage as ab

        sperre = GraphSperre(EchtGraph({"A/B/x.pdf": 5}))
        eintrag = await ab._eine_datei(sperre, "A/B", "x.pdf", b"12345")
        assert eintrag.lag_schon
        assert sperre.protokoll() == []

    async def test_sperrklinke_sieht_den_eigenen_lauf(self):
        """Zwei Rechnungen derselben Kundschaft dürfen nicht auseinanderfallen."""
        sperre = GraphSperre(EchtGraph())
        await sperre.ensure_drive_folder("F/MBA/2026/Cheetah")
        inhalt = await sperre.list_drive_items("F/MBA/2026")
        assert any("folder" in e for e in inhalt)

    async def test_leserfehler_bleibt_ein_fehler(self):
        """Der Schatten darf einen echten Ausfall nicht in Leere verwandeln."""

        class Blind(EchtGraph):
            async def list_drive_items(self, pfad, top=20):
                raise TimeoutError("OneDrive antwortet nicht")

        sperre = GraphSperre(Blind())
        with pytest.raises(TimeoutError):
            await sperre.list_drive_items("F/MBA/2026")


# ── Toggl ──────────────────────────────────────────────────────────────


class EchtToggl:
    def __init__(self):
        self.geschrieben: list[tuple[int, int]] = []

    async def list_tags(self, workspace=None):
        return [{"id": 1, "name": "Kunde verrechnet"}]

    async def search_all_time_entries(self, *a, **k):
        return [{"project_id": 5, "time_entries": [{"id": 7, "seconds": 3600}]}]

    async def add_time_entry_tag(self, eintrag_id, tag_id, workspace=None):
        self.geschrieben.append((eintrag_id, tag_id))
        return {"id": eintrag_id, "tags": ["Kunde verrechnet"]}


TAGS = [{"id": 1, "name": "Kunde verrechnet"}, {"id": 4, "name": "keine Verrechnung"}]


@pytest.mark.asyncio
class TestTogglSperre:
    async def test_lesen_geht_durch(self):
        echt = EchtToggl()
        sperre = TogglSperre(echt, tags=TAGS)
        assert await sperre.list_tags(42) == [{"id": 1, "name": "Kunde verrechnet"}]

    async def test_der_tag_wird_nicht_geschrieben(self):
        echt = EchtToggl()
        sperre = TogglSperre(echt, tags=TAGS)
        await sperre.add_time_entry_tag(7, 1, 42)

        assert echt.geschrieben == []
        assert sperre.protokoll() == ["add_time_entry_tag(7, 1, 42)"]

    async def test_die_antwort_traegt_den_tagnamen(self):
        """Sonst meldete die Nachprüfung im Trockenlauf einen Fehler, den es
        nicht gibt — derselbe Phantomfehler wie beim Nachzählen im Archiv."""
        sperre = TogglSperre(EchtToggl(), tags=TAGS)
        antwort = await sperre.add_time_entry_tag(7, 4, 42)

        assert antwort["tags"] == ["keine Verrechnung"]

    async def test_ohne_tagtabelle_steht_der_platzhalter(self):
        sperre = TogglSperre(EchtToggl())
        antwort = await sperre.add_time_entry_tag(7, 1, 42)

        assert antwort["tags"] == [t.PLATZHALTER]

    async def test_die_antwort_taeuscht_keine_pruefung_vor(self):
        """Weder Dauer noch Beschreibung: der Trockenlauf kann nicht wissen, ob
        der ``PUT`` mehr verändert. Eine erfundene Angabe wäre Zuversicht ohne
        Grundlage."""
        sperre = TogglSperre(EchtToggl(), tags=TAGS)
        antwort = await sperre.add_time_entry_tag(7, 1, 42)

        assert "description" not in antwort
        assert "duration" not in antwort

    async def test_unbekanntes_wird_abgewiesen(self):
        sperre = TogglSperre(EchtToggl(), tags=TAGS)
        with pytest.raises(UnbekannterAufruf):
            await sperre.delete_time_entry(7)

    async def test_der_trockenlauf_meldet_gelungen(self):
        """Der Plan läuft echt durch, nur der Schreibaufruf wird angehalten."""
        from app.services import debitoren_verrechnungsart as va
        from tests.test_debitoren_verrechnungsart import bestand_bauen, buchung

        plan = va.planen([buchung(7, "Cheetah")], bestand_bauen(), vertraege={"cheetah"})
        echt = EchtToggl()
        ergebnisse = await va.setzen(TogglSperre(echt, tags=TAGS), 42, plan, tags=TAGS)

        assert echt.geschrieben == []
        assert [e.gelungen for e in ergebnisse] == [True]


# ── Die Einstellung ────────────────────────────────────────────────────


class TestVoreinstellung:
    def test_ohne_einstellung_wird_gezeigt_nicht_geschrieben(self):
        assert t.voreinstellung(None) is True
        assert t.voreinstellung({}) is True

    def test_abschaltbar(self):
        assert t.voreinstellung({t.EINSTELLUNG: False}) is False

    def test_die_sperre_haengt_nicht_an_der_einstellung(self):
        """Die Sicherung liegt im Vorgabewert des Endpunkts, nicht im Browser."""
        import inspect

        from app.routers import debtors

        quelle = inspect.getsource(debtors)
        assert "echt: bool = False" in quelle
