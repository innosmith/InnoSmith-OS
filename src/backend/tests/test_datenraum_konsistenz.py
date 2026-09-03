"""Prüft, dass Prompt, Werkzeugbeschreibungen und Registrierung dasselbe sagen.

Agentenverhalten entsteht aus drei Quellen: dem Chat-Prompt, den Beschreibungen der
MCP-Werkzeuge und der Toolset-Registrierung. Widersprechen sie sich, entscheidet das
Modell -- und keiner der drei Orte ist für sich falsch, weshalb es kein Test bemerkt,
der nur einen davon liest.

Konkret abgesichert: Der Chat-Prompt wies bis zum 02.09.2026 für Buchhaltungsfragen
auf ``list_invoices`` und ``search_invoices``. Beide liefern nur eine Seite, und
``list_invoices`` nahm zusätzlich einen wirkungslosen Kundenfilter entgegen. Der
Agent folgte der Anweisung korrekt und bekam trotzdem eine falsche Zahl.
"""

import os
import re
import sys

import pytest

BACKEND = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(BACKEND, "..")
sys.path.insert(0, BACKEND)


def _werkzeugnamen(server_pfad: str) -> set[str]:
    """Toolnamen aus einer MCP-Server-Datei lesen, ohne sie zu importieren.

    Ein Import zöge die MCP-Bibliothek und die Zugangsdaten nach; für einen
    Konsistenztest genügt der Quelltext.
    """
    with open(server_pfad, encoding="utf-8") as f:
        return set(re.findall(r'name="([a-z_]+)"', f.read()))


class TestRegistrierung:
    def test_datenraum_ist_als_mcp_server_bekannt(self):
        from app.services.hermes_worker import _KNOWN_MCP_SERVERS

        assert "datenraum" in _KNOWN_MCP_SERVERS

    def test_datenraum_steht_in_der_hermes_config(self):
        from app.services.hermes_config import build_config_dict

        server = build_config_dict()["mcp_servers"]
        assert "datenraum" in server
        assert server["datenraum"]["args"][0].endswith("mcp-datenraum/server.py")

    def test_recherchelauf_kennt_datenraum_und_sandbox(self):
        """Der Sammel-Lauf soll Zahlen aus dem Datenraum holen, nicht aus dem Mailarchiv."""
        from app.services.hermes_worker import _GATHER_MCP_SERVERS_WIDE

        assert {"datenraum", "sandbox"} <= set(_GATHER_MCP_SERVERS_WIDE)


class TestWerkzeugnamen:
    """Was der Prompt nennt, muss es auch geben."""

    def test_datenraum_werkzeuge_existieren(self):
        namen = _werkzeugnamen(os.path.join(SRC, "mcp-datenraum", "server.py"))
        assert namen == {"datenraum_katalog", "datenraum_auffrischen"}

    def test_beschreibungen_nennen_nur_echte_bexio_werkzeuge(self):
        from app.routers.chat import MCP_SERVER_DESCRIPTIONS

        echte = _werkzeugnamen(os.path.join(SRC, "mcp-bexio", "server.py"))
        text = MCP_SERVER_DESCRIPTIONS["bexio"]["tools"]
        genannte = set(re.findall(r"\b([a-z_]+)\(", text))
        assert genannte <= echte, f"nicht vorhanden: {sorted(genannte - echte)}"

    def test_beschreibungen_nennen_nur_echte_datenraum_werkzeuge(self):
        from app.routers.chat import MCP_SERVER_DESCRIPTIONS

        echte = _werkzeugnamen(os.path.join(SRC, "mcp-datenraum", "server.py"))
        text = MCP_SERVER_DESCRIPTIONS["datenraum"]["tools"]
        genannte = {n for n in re.findall(r"\b(datenraum_[a-z_]+)\(", text)}
        assert genannte <= echte


class TestQuellenUndWegweiserPassenZusammen:
    """Eine Quelle, die es gibt, muss auffrischbar sein und erklärt werden.

    Beim Anlegen der Kreditorenquelle stand sie zuerst in ``QUELLEN``, aber nicht im
    Enum von ``datenraum_auffrischen`` -- der Agent hätte sie sehen, aber nicht
    auffrischen können. Genau die Art Bruch, die kein Einzeltest bemerkt, weil beide
    Seiten für sich richtig sind.
    """

    @pytest.fixture(scope="class")
    def server_quelltext(self):
        with open(os.path.join(SRC, "mcp-datenraum", "server.py"), encoding="utf-8") as f:
            return f.read()

    def test_jede_quelle_ist_auffrischbar(self, server_quelltext):
        from app.services.datenraum import QUELLEN

        enum = set(re.search(r'"enum": \[([^\]]+)\]', server_quelltext).group(1).replace('"', "").split(", "))
        fehlend = set(QUELLEN) - enum
        assert not fehlend, f"Quellen ohne Eintrag im Auffrischen-Enum: {sorted(fehlend)}"

    def test_zeitspalten_gehoeren_zu_einer_tabelle_die_erzeugt_wird(self):
        """Eine Zeitspalten-Deklaration für eine Tabelle, die niemand schreibt, ist
        tot -- und verdeckt, dass die echte Tabelle keine hat."""
        import inspect

        from app.services import datenraum

        quelltext = inspect.getsource(datenraum)
        for tabelle in datenraum.ZEITSPALTEN:
            assert f'"{tabelle}"' in quelltext, f"ZEITSPALTEN nennt unbekannte Tabelle: {tabelle}"

    def test_die_ausgaben_lesart_trennt_alle_drei_bestaende(self, server_quelltext):
        """Drei Tabellen berühren Ausgaben, und nur eine beantwortet «wie viel».

        Der Text stand bis zum 03.09.2026 falsch da: er empfahl ``bexio_kreditoren``
        für Ausgabensummen. Über den Kreditorenweg liefen 2025 aber nur 88'177 von
        401'459 CHF. Der Fehler war nicht bemerkbar, weil beide Zahlen plausibel
        aussehen.
        """
        assert "KREDITOREN_LESART" in server_quelltext
        for pflicht in (
            "bexio_journal", "bexio_kreditoren", "invoiceinsight_rechnungen", "doppelt",
        ):
            assert pflicht in server_quelltext, f"Lesart nennt '{pflicht}' nicht"

    def test_die_lesart_empfiehlt_die_kreditoren_nicht_fuer_ausgaben(self, server_quelltext):
        """Der genaue Rückfall, der einmal drinstand -- als Test festgehalten."""
        lesart = server_quelltext[server_quelltext.index("KREDITOREN_LESART"):]
        lesart = lesart[: lesart.index("\nREZEPTE")]
        assert "UNGEEIGNET" in lesart
        assert "ist_aufwand" in lesart, "die Lesart muss den Filter nennen, nicht nur die Tabelle"

    def test_die_lesart_schreibt_keine_zahl_der_falschen_tabelle_zu(self, server_quelltext):
        """Die erste Korrektur enthielt den nächsten Fehler: sie schrieb die 88'177 CHF
        (Journal, Haben 2000) der Tabelle ``bexio_kreditoren`` zu, die auf 156'934 CHF
        summiert. Eine Behauptung, die ein Agent in zehn Sekunden widerlegt, kostet
        das Vertrauen in den ganzen Katalog -- sie ist schlimmer als keine.

        Der Test hält fest, dass beide Zahlen dastehen und der Kontofilter genannt
        ist, der sie ineinander überführt.
        """
        lesart = server_quelltext[server_quelltext.index("KREDITOREN_LESART"):]
        lesart = lesart[: lesart.index("\nREZEPTE")]
        assert "156'934" in lesart, "die tatsächliche Tabellensumme fehlt"
        assert "88'177" in lesart, "der Journalwert fehlt"
        assert "konto_nr" in lesart, "der Filter, der beide Zahlen versöhnt, fehlt"

    def test_kein_rezept_summiert_die_buchungswaehrung(self, server_quelltext):
        """``betrag`` steht in Buchungswährung, ``betrag_chf`` in Franken. 250 der
        5262 Journalbuchungen lauten auf Fremdwährung, und die falsche Summe kommt
        ohne Fehlermeldung -- bei Cursor 2026 sind es 16'164 statt 12'924 CHF.

        Der Fehler unterlief beim Nachmessen dem Agenten selbst, mit der Deklaration
        vor Augen. Ein Rezept ist die geprüfte Referenz; wenn dort ``sum(betrag)``
        steht, wandert der Fehler in jede daraus abgewandelte Abfrage.
        """
        rezepte = server_quelltext[server_quelltext.index("REZEPTE = {"):]
        rezepte = rezepte[: rezepte.index("\nVORLAGE")]
        # sum(betrag) trifft zu, sum(betrag_chf) nicht -- der Unterstrich trennt.
        verstoesse = re.findall(r"sum\(\s*betrag\s*\)", rezepte)
        assert not verstoesse, (
            f"{len(verstoesse)} Rezept(e) summieren die Buchungswährung statt betrag_chf"
        )

    def test_die_waehrungsfalle_nennt_die_gemessene_abweichung(self, server_quelltext):
        """Eine Warnung ohne Zahl wirkt nicht -- gemessen am eigenen Fehlgriff."""
        erklaerung = server_quelltext[server_quelltext.index('"bexio_journal.betrag"'):]
        erklaerung = erklaerung[:500]
        assert "16'164" in erklaerung and "12'924" in erklaerung, (
            "die Deklaration muss die gemessene Abweichung nennen, nicht nur die Eigenschaft"
        )

    def test_die_bezugsteuer_falle_ist_deklariert(self, server_quelltext):
        """Eine Auslandsleistung erzeugt zwei Aufwandsbuchungen auf demselben
        Sollkonto. Wer Rechnungen zählt, zählt doppelt -- Cursor 2026: 62 Buchungen,
        31 Rechnungen. Die Beträge bleiben richtig, nur die Anzahl nicht."""
        erklaerung = server_quelltext[server_quelltext.index('"bexio_journal.haben_konto_nr"'):]
        erklaerung = erklaerung[:600]
        assert "2203" in erklaerung
        assert "Bezugsteuer" in erklaerung

    def test_gefaehrliche_geldspalten_sind_erklaert(self, server_quelltext):
        """``jahreskosten_chf`` ist eine Hochrechnung. Wer sie für Ausgaben summiert,
        meldet ein Vielfaches -- und die Zahl sieht plausibel aus."""
        assert "invoiceinsight_rechnungen.jahreskosten_chf" in server_quelltext
        assert "HOCHRECHNUNG" in server_quelltext

    def test_fertige_abfragen_zeigen_auf_vorhandene_tabellen(self, server_quelltext):
        """Ein Rezept, das eine Tabelle nennt, die niemand schreibt, scheitert erst
        in der Sandbox -- und dort sieht es aus wie ein Fehler des Modells.

        Geprüft wird gegen den echten Katalog. Vorher stand hier ``ZEITSPALTEN`` als
        Ersatzverzeichnis, was es nicht ist: eine Tabelle ohne Datumsspalte fehlte
        dort und brauchte eine Ausnahme von Hand -- eine Liste, die bei jeder neuen
        Tabelle nachzupflegen gewesen wäre.
        """
        from app.services.datenraum import katalog_lesen

        vorhanden = set(katalog_lesen().get("tabellen", {}))
        if not vorhanden:
            pytest.skip("kein Datenraum vorhanden -- Tabellennamen nicht prüfbar")

        rezepte = server_quelltext[server_quelltext.index("REZEPTE = {"):]
        genannt = set(re.findall(r"/daten/(\w+)\.parquet", rezepte))
        assert genannt, "keine einzige fertige Abfrage vorhanden"
        assert genannt <= vorhanden, (
            f"unbekannte Tabellen in den Rezepten: {sorted(genannt - vorhanden)}"
        )


# Spaltentypen, bei denen ein Missverständnis still bleibt.
#
# Eine Zahl summiert man falsch, ein Datum vergleicht man falsch, eine Wahrheit
# filtert man falsch -- und in allen drei Fällen kommt ein plausibles Ergebnis
# heraus statt einer Fehlermeldung. Bei Text passiert das nicht: wer den falschen
# Namen liest, sieht es.
ERKLAERUNGSPFLICHTIGE_TYPEN = ("double", "float", "int", "date", "timestamp", "bool")


def _erklaerungspflichtig(spalten: dict[str, str]) -> set[str]:
    """Welche Spalten einer Tabelle eine Erklärung brauchen.

    Kennungen (``*_id``) sind ausgenommen: sie sind Identität, nicht Messung, und
    eine Erklärung zu ``deal_id`` wäre Rauschen im Katalog. Ihr Textzwilling
    dagegen ist pflichtig -- die Wahl zwischen ``lieferant`` und ``lieferant_id``
    ist eine Entscheidung, und die falsche gruppiert nach Schreibweise.
    """
    kennungen = {n for n in spalten if n.endswith("_id")}
    pflicht = set()
    for name, typ in spalten.items():
        if name in kennungen:
            continue
        if any(t in str(typ).lower() for t in ERKLAERUNGSPFLICHTIGE_TYPEN):
            pflicht.add(name)
        elif f"{name}_id" in kennungen:
            pflicht.add(name)
    return pflicht


def erklaerte_spalten(server_quelltext: str) -> set[str]:
    """Die Schlüssel aus ``SPALTEN_BEDEUTUNG``, aus dem Quelltext gelesen."""
    abschnitt = server_quelltext[
        server_quelltext.index("SPALTEN_BEDEUTUNG = {"):
        server_quelltext.index("KREDITOREN_LESART = (")
    ]
    return set(re.findall(r'"(\w+\.\w+)":', abschnitt))


class TestErklaerungspflicht:
    """Jede Zahl-, Datums- und Wahrheitsspalte muss erklärt sein.

    Nicht als einmalige Fleissarbeit, sondern als Invariante: am 03.09.2026 waren
    8 von 33 InvoiceInsight-Spalten erklärt. Eine Nachpflege hält nicht, eine
    Prüfung schon -- eine neue Spalte bricht diesen Test, bis jemand entschieden
    hat, was sie bedeutet.

    Geprüft wird gegen den tatsächlichen Katalog, weil nur er die wahren Spalten
    kennt. Ohne Datenraum ist die Frage nicht beantwortbar, und ein Test, der dann
    stillschweigend durchgeht, wäre schlimmer als keiner.
    """

    @pytest.fixture(scope="class")
    def server_quelltext(self):
        with open(os.path.join(SRC, "mcp-datenraum", "server.py"), encoding="utf-8") as f:
            return f.read()

    @pytest.fixture(scope="class")
    def katalog(self):
        from app.services.datenraum import katalog_lesen

        tabellen = katalog_lesen().get("tabellen", {})
        if not tabellen:
            pytest.skip("kein Datenraum vorhanden -- Spaltenbild nicht prüfbar")
        return tabellen

    def test_jede_stille_spalte_ist_erklaert(self, katalog, server_quelltext):
        erklaert = erklaerte_spalten(server_quelltext)
        fehlend: list[str] = []
        for name, eintrag in katalog.items():
            spalten = eintrag.get("spalten") or {}
            for spalte in _erklaerungspflichtig(spalten):
                if f"{name}.{spalte}" not in erklaert:
                    fehlend.append(f"{name}.{spalte}")
        assert not fehlend, (
            "Spalten ohne Erklärung im Katalog (Zahl, Datum oder Wahrheit -- genau die, "
            f"die man still falsch benutzt): {sorted(fehlend)}"
        )

    def test_keine_erklaerung_zeigt_ins_leere(self, katalog, server_quelltext):
        """Eine Erklärung zu einer Spalte, die es nicht gibt, lässt ein Modell nach
        ihr suchen. ``bexio_kreditoren.mwst`` ist die bewusste Ausnahme: sie erklärt,
        dass es die Spalte NICHT gibt."""
        gewollte_ausnahmen = {"bexio_kreditoren.mwst"}
        echte = {
            f"{name}.{spalte}"
            for name, eintrag in katalog.items()
            for spalte in (eintrag.get("spalten") or {})
        }
        bekannte_tabellen = set(katalog)
        verwaist = [
            schluessel
            for schluessel in erklaerte_spalten(server_quelltext)
            if schluessel.split(".", 1)[0] in bekannte_tabellen
            and schluessel not in echte
            and schluessel not in gewollte_ausnahmen
        ]
        assert not verwaist, f"Erklärungen ohne Spalte: {sorted(verwaist)}"


class TestWaechterGegenStilleSpalten:
    """Eine Spalte, die es gibt und die nichts trägt, ist die teuerste Fehlerart.

    ``bexio_kreditoren.offen_betrag`` ist der Anlass: Bexio füllt ``pending_amount``
    nicht, die Spalte steht bei allen 435 Zeilen auf 0.00 -- auch bei den drei
    offenen Rechnungen über 4'496 CHF. Der Wächter fing bis zum 03.09.2026 nur
    ``NULL`` und ging an dieser Spalte vorbei.
    """

    def test_eine_durchgehende_null_gilt_als_leer(self):
        import pyarrow as pa
        from app.services.datenraum import durchgehend_leere_spalten

        tabelle = pa.Table.from_pylist([
            {"offen_betrag": 0.0, "betrag_chf": 1200.0},
            {"offen_betrag": 0.0, "betrag_chf": 3295.5},
        ])
        assert durchgehend_leere_spalten(tabelle) == ["offen_betrag"]

    def test_ein_einziger_wert_rettet_die_spalte(self):
        """Sonst verschwände eine Spalte aus dem Katalog, sobald ein Bestand gerade
        ausgeglichen ist -- etwa alle Rechnungen bezahlt."""
        import pyarrow as pa
        from app.services.datenraum import durchgehend_leere_spalten

        tabelle = pa.Table.from_pylist([
            {"offen_betrag": 0.0}, {"offen_betrag": 0.0}, {"offen_betrag": 4495.5},
        ])
        assert durchgehend_leere_spalten(tabelle) == []

    def test_eine_wahrheitsspalte_darf_durchgehend_falsch_sein(self):
        """«überall false» ist eine Aussage, keine Lücke: dass gerade nichts
        überfällig ist, will man wissen und nicht als Defekt gemeldet bekommen."""
        import pyarrow as pa
        from app.services.datenraum import durchgehend_leere_spalten

        tabelle = pa.Table.from_pylist([
            {"ueberfaellig": False}, {"ueberfaellig": False},
        ])
        assert durchgehend_leere_spalten(tabelle) == []

    def test_negative_werte_zaehlen_nicht_als_leer(self):
        """Eine Summe aus +100 und -100 ist 0, die Spalte trägt aber sehr wohl."""
        import pyarrow as pa
        from app.services.datenraum import durchgehend_leere_spalten

        tabelle = pa.Table.from_pylist([{"saldo": 100.0}, {"saldo": -100.0}])
        assert durchgehend_leere_spalten(tabelle) == []

    def test_offen_betrag_ist_als_unbrauchbar_deklariert(self):
        """Der Wächter meldet die Spalte, aber erst beim nächsten Abgleich. Die
        Deklaration muss sie unabhängig davon als unbrauchbar ausweisen und den
        richtigen Weg nennen."""
        with open(os.path.join(SRC, "mcp-datenraum", "server.py"), encoding="utf-8") as f:
            quelle = f.read()
        erklaerung = re.search(
            r'"bexio_kreditoren\.offen_betrag":\s*\((.*?)\n    \),', quelle, re.S
        )
        assert erklaerung, "offen_betrag ist nicht erklärt"
        text = erklaerung.group(1)
        assert "UNBRAUCHBAR" in text
        assert "ist_offen" in text, "die Deklaration nennt den richtigen Weg nicht"


class TestKundenschluessel:
    """Die einzige Brücke zwischen drei Kennungsräumen, die sich nicht überschneiden.

    Ohne sie ergibt «Umsatz mit AGG» null Franken statt 227'789 -- ohne
    Fehlermeldung, weil das Kürzel in Bexio schlicht nicht vorkommt.
    """

    @pytest.fixture(scope="class")
    def datei(self):
        import yaml
        from app.services.kundenschluessel import DATEI

        if not DATEI.exists():
            pytest.skip("docs/kundenschluessel.yaml fehlt")
        with open(DATEI, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_die_datei_liegt_auch_im_container(self):
        """Im Image steht das Modul unter ``/app/app/services`` -- ein fester
        ``parents[4]`` zeigte dort auf ``/docs`` und damit ins Leere. Weil eine
        fehlende Datei bloss eine leere Zuordnung ergibt, wäre daraus ein stiller
        Ausfall geworden: alle Kundenfragen ohne Treffer, ohne Fehlermeldung."""
        with open(os.path.join(SRC, "..", "docker", "Dockerfile.backend"),
                  encoding="utf-8") as f:
            dockerfile = f.read()
        assert "docs/kundenschluessel.yaml" in dockerfile, (
            "die Datei wird nicht ins Backend-Image kopiert"
        )

        from app.services.kundenschluessel import _datei_finden

        gesucht = _datei_finden().parts
        assert gesucht[-2:] == ("docs", "kundenschluessel.yaml")

    def test_jeder_schluessel_kommt_nur_einmal_vor(self, datei):
        schluessel = [k["schluessel"] for k in datei["kundschaften"]]
        doppelte = {s for s in schluessel if schluessel.count(s) > 1}
        assert not doppelte, f"doppelte Schlüssel: {sorted(doppelte)}"

    def test_keine_kennung_gehoert_zwei_kundschaften(self, datei):
        """Sonst zählt dieselbe Rechnung bei zwei Kundschaften -- und die Summe
        über alle übersteigt den tatsächlichen Umsatz."""
        from app.services.kundenschluessel import SYSTEME

        for system in SYSTEME:
            gesehen: dict[int, str] = {}
            for eintrag in datei["kundschaften"]:
                for kennung in eintrag.get(system) or []:
                    assert kennung not in gesehen, (
                        f"{system} {kennung} steht bei '{gesehen.get(kennung)}' "
                        f"und bei '{eintrag['schluessel']}'"
                    )
                    gesehen[kennung] = eintrag["schluessel"]

    def test_ein_eintrag_verbindet_mindestens_zwei_systeme(self, datei):
        """Wer nur in einem System vorkommt, braucht keinen Schlüssel -- dort genügt
        der eigene Name. Ein einsamer Eintrag wäre Pflegeaufwand ohne Nutzen."""
        from app.services.kundenschluessel import SYSTEME

        einsam = [
            k["schluessel"] for k in datei["kundschaften"]
            if sum(1 for s in SYSTEME if k.get(s)) < 2
        ]
        assert not einsam, f"Einträge mit nur einem System: {einsam}"

    def test_eine_erfundene_kennung_wird_verworfen_und_gemeldet(self, tmp_path):
        """Eine tote Verknüpfung ist schlimmer als eine fehlende: sie sieht aus wie
        eine Zuordnung und trägt keine."""
        from app.services.kundenschluessel import aufbauen

        datei = tmp_path / "k.yaml"
        datei.write_text(
            "kundschaften:\n"
            "  - schluessel: agg\n"
            "    name: Amt für Grundstücke\n"
            "    bexio: [114, 999999]\n"
            "    toggl: [57641058]\n",
            encoding="utf-8",
        )
        zeilen, befund = aufbauen({
            "bexio_kontakte": [{"kunden_id": 114, "name": "BVD AGG"}],
            "toggl_zeiteintraege": [{"kunden_id": 57641058, "kunde": "AGG"}],
        }, pfad=datei)

        assert {z["fremd_id"] for z in zeilen} == {114, 57641058}
        assert any("999999" in v for v in befund["verworfen"])

    def test_wer_keinen_schluessel_hat_wird_benannt(self, tmp_path):
        """Eine nicht zugeordnete Kundschaft fällt aus jedem Join heraus. Still ist
        das nicht hinnehmbar -- gemeldet schon."""
        from app.services.kundenschluessel import aufbauen

        datei = tmp_path / "k.yaml"
        datei.write_text("kundschaften: []\n", encoding="utf-8")
        _, befund = aufbauen({
            "bexio_kontakte": [{"kunden_id": 114, "name": "BVD AGG"}],
            "bexio_rechnungen": [{"kunden_id": 114, "ist_umsatz": True}],
        }, pfad=datei)

        assert befund["nicht_zugeordnet"]["bexio"] == 1
        assert "114 (BVD AGG)" in befund["ohne_schluessel"]["bexio"]

    def test_ein_interessent_ohne_abschluss_wird_nicht_gemeldet(self, tmp_path):
        """Sonst bestünde die Warnliste zu neunzig Prozent aus Rauschen -- und würde
        damit gar nicht gelesen."""
        from app.services.kundenschluessel import aufbauen

        datei = tmp_path / "k.yaml"
        datei.write_text("kundschaften: []\n", encoding="utf-8")
        _, befund = aufbauen({
            "pipedrive_deals": [
                {"organisation_id": 1, "organisation": "Interessent", "status": "lost"},
                {"organisation_id": 2, "organisation": "Kundschaft", "status": "won"},
            ],
        }, pfad=datei)

        assert befund["ohne_schluessel"]["pipedrive"] == ["2 (Kundschaft)"]


class TestRezepteLaufen:
    """Ein Rezept ist eine Behauptung über die Daten, kein Kommentar.

    Am 03.09.2026 fand ein Rezept den Fehler, den die Deklaration danebenlegte: die
    Summe über ``betrag`` statt ``betrag_chf``. Rezepte sind deshalb die geprüfte
    Referenz und nicht bloss Hilfe für schwache Modelle -- was hier nicht läuft,
    läuft beim Agenten auch nicht.
    """

    @pytest.fixture(scope="class")
    def rezepte(self):
        import importlib.util

        pfad = os.path.join(SRC, "mcp-datenraum", "server.py")
        spec = importlib.util.spec_from_file_location("datenraum_server", pfad)
        modul = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modul)
        return modul.REZEPTE

    @pytest.fixture(scope="class")
    def verbindung(self):
        duckdb = pytest.importorskip("duckdb")
        from app.services.datenraum import datenraum_pfad, katalog_lesen

        if not katalog_lesen().get("tabellen"):
            pytest.skip("kein Datenraum vorhanden -- Rezepte nicht ausführbar")
        return duckdb.connect(), datenraum_pfad()

    def test_jedes_rezept_laeuft_gegen_die_echten_daten(self, rezepte, verbindung):
        c, verzeichnis = verbindung
        gescheitert = []
        for name, sql in rezepte.items():
            try:
                c.sql(sql.replace("/daten/", f"{verzeichnis}/")).fetchall()
            except Exception as exc:  # noqa: BLE001
                gescheitert.append(f"{name}: {type(exc).__name__}: {exc}")
        assert not gescheitert, "Rezepte scheitern:\n" + "\n".join(gescheitert)

    def test_der_kuerzel_join_findet_den_gemessenen_umsatz(self, rezepte, verbindung):
        """Die Zahl, an der die Demo scheiterte: 'AGG' ergibt in bexio_rechnungen
        keinen Treffer, über den Schlüssel aber 227'789 CHF auf 49 Rechnungen."""
        c, verzeichnis = verbindung
        sql = rezepte["Umsatz einer bestimmten Kundschaft (Kürzel wie AGG, MBA, BFH)"]
        zeilen = c.sql(sql.replace("/daten/", f"{verzeichnis}/")).fetchall()

        assert len(zeilen) == 1, "der Schlüssel löst 'agg' nicht eindeutig auf"
        _, rechnungen, netto, _ = zeilen[0]
        assert rechnungen == 49
        assert round(netto) == 227789

        direkt = c.sql(
            f"SELECT count(*) FROM '{verzeichnis}/bexio_rechnungen.parquet' "
            "WHERE kunde ILIKE '%AGG%'"
        ).fetchone()[0]
        assert direkt == 0, (
            "Die Namenssuche findet neuerdings etwas -- dann ist die Begründung des "
            "Schlüssels zu prüfen, nicht der Test anzupassen."
        )

    def test_kein_rezept_filtert_auf_einen_kundennamen(self, rezepte):
        """Der Namensfilter ist die Fehlerquelle, die der Schlüssel ablöst. Ein
        Rezept, das ihn vorführt, lehrt genau das Falsche."""
        for name, sql in rezepte.items():
            for zeile in sql.splitlines():
                if zeile.lstrip().startswith("--"):
                    continue
                assert not re.search(r"(kunde|lieferant|organisation)\s+I?LIKE", zeile), (
                    f"Rezept '{name}' filtert auf einen Namen: {zeile.strip()}"
                )


class TestPromptLeitplanken:
    """Geprüft wird die Vorlage im Quelltext.

    ``_build_agent_prompt`` ist asynchron und lädt Regeln und Rückschau aus der
    Datenbank; für die Frage, was in der Anweisung steht, ist das unnötiger Aufbau.
    """

    @pytest.fixture(scope="class")
    def prompt(self):
        with open(os.path.join(BACKEND, "app", "routers", "chat.py"), encoding="utf-8") as f:
            quelle = f.read()
        anfang = quelle.index("Du bist InnoPilot")
        return quelle[anfang:anfang + 6000]

    def test_prompt_lenkt_zahlen_in_den_datenraum(self, prompt):
        assert "/daten/" in prompt
        assert "datenraum_katalog" in prompt

    def test_prompt_nennt_den_stand_als_pflicht(self, prompt):
        """Ohne Datumsangabe ist eine gecachte Zahl von einer frischen ununterscheidbar."""
        assert "Stand" in prompt

    def test_prompt_bietet_list_invoices_nicht_fuer_umsatz_an(self, prompt):
        """list_invoices liefert eine Seite und filtert nicht nach Kunde."""
        for zeile in prompt.splitlines():
            if "list_invoices" in zeile:
                assert "Umsatz" not in zeile


class TestSandboxWeissVomDatenraum:
    def test_execute_code_beschreibt_daten_verzeichnis(self):
        with open(os.path.join(SRC, "mcp-sandbox", "server.py"), encoding="utf-8") as f:
            quelle = f.read()
        assert "/daten/" in quelle
        assert "duckdb" in quelle

    def test_executor_haengt_datenraum_bei_jedem_lauf_ein(self):
        """Ohne Bedingung auf workspace_key -- der Schlüssel kommt vom Modell."""
        with open(os.path.join(SRC, "sandbox-executor", "executor.py"), encoding="utf-8") as f:
            quelle = f.read()
        assert '"-v", f"{DATENRAUM_DIR}:/daten:ro"' in quelle
        assert "CONV_TTL_SECONDS" in quelle, "persistente Scopes brauchen eine Frist"
