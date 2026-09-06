"""Die Finanzansicht rechnet aus dem Datenraum -- und zwar mit den richtigen Spalten.

Zwei Sorten Prüfung, aus zwei verschiedenen Gründen:

**Der Spaltenvertrag** wird gegen den **echten Katalog** gehalten. Eine Fixture
könnte das nicht: die Frage ist ja gerade, ob der Konnektor die Spalte noch so
schreibt, wie die Ansicht sie liest. Benennt jemand ``betrag_chf`` um, zeigte das
Dashboard eine Null -- diese Prüfung bricht vorher.

**Die drei Korrekturen** werden gegen erfundene Zeilen geprüft, weil nur so
feststeht, dass die Regel greift und nicht bloss der heutige Bestand zufällig
passt. Sie sind der Kern der Umstellung vom 06.09.2026:

1. Aufwand rechnet mit ``betrag_chf``, nicht mit der Buchungswährung
2. Umsatz zählt nur ``ist_umsatz`` -- Entwürfe nicht
3. Offene Forderungen zählen Entwürfe ebenfalls nicht
"""

import ast
import os
import sys

import pytest

BACKEND = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(BACKEND, "..")
sys.path.insert(0, BACKEND)


# ── Spaltenvertrag gegen den echten Katalog ──────────────


class TestSpaltenvertrag:
    """Was die Ansicht liest, muss der Konnektor schreiben."""

    @pytest.fixture(scope="class")
    def katalog(self):
        from app.services.datenraum import katalog_lesen

        tabellen = katalog_lesen().get("tabellen", {})
        if not tabellen:
            pytest.skip("kein Datenraum vorhanden -- Spaltenbild nicht prüfbar")
        return tabellen

    def test_jede_gelesene_spalte_existiert(self, katalog):
        from app.services.datenraum_lesen import SPALTENVERTRAG

        fehlend: list[str] = []
        for tabelle, spalten in SPALTENVERTRAG.items():
            vorhanden = (katalog.get(tabelle) or {}).get("spalten")
            if vorhanden is None:
                fehlend.append(f"{tabelle} (Tabelle fehlt ganz)")
                continue
            fehlend += [f"{tabelle}.{s}" for s in spalten if s not in vorhanden]

        assert not fehlend, (
            "Die Finanzansicht liest Spalten, die es nicht gibt -- sie würde 0 CHF "
            f"zeigen statt zu scheitern: {sorted(fehlend)}"
        )

    def test_keine_gelesene_spalte_ist_durchgehend_leer(self, katalog):
        """Eine Spalte, die es gibt und die nichts trägt, ist die teuerste Fehlerart.

        Der Wächter im Schreibpfad meldet sie im Katalog; hier wird daraus ein
        Testversagen. Ohne das könnte die Ansicht auf ``offen_betrag`` rechnen --
        bei allen 435 Kreditorenzeilen 0.00, auch bei den offenen.
        """
        from app.services.datenraum_lesen import SPALTENVERTRAG

        betroffen: list[str] = []
        for tabelle, spalten in SPALTENVERTRAG.items():
            leer = set((katalog.get(tabelle) or {}).get("leere_spalten") or [])
            betroffen += [f"{tabelle}.{s}" for s in sorted(leer & set(spalten))]

        assert not betroffen, f"gelesene, aber leere Spalten: {betroffen}"

    def test_bankkonten_stehen_als_eigene_tabelle_bereit(self, katalog):
        """Welche Konten Bankkonten sind, wird gefragt und nicht aus der Nummer geraten.

        ``1090 Transferkonto`` und ``1099 Unklare Beträge`` liegen im selben
        Hunderterblock wie die echten Bankkonten. Ein Saldo, der sie mitzählt, ist
        falsch und sieht richtig aus.
        """
        assert "bexio_bankkonten" in katalog, (
            "die Tabelle fehlt -- der Banksaldo müsste über einen Nummernbereich raten"
        )
        assert (katalog["bexio_bankkonten"].get("zeilen") or 0) > 0

    def test_aufwand_ist_auf_beiden_seiten_erkennbar(self, katalog):
        """Ohne ``ist_aufwand_haben`` gibt es keine Netto-Summe, nur eine zu hohe.

        Gefunden am 06.09.2026 bei der Kreuzprobe: die Finanzansicht rechnete Soll
        minus Haben und kam für 2025 auf 335'982 CHF, der Katalog empfahl dem Agenten
        ``WHERE ist_aufwand`` und damit 401'459 CHF. Zwei Zahlen auf dieselbe Frage im
        selben System -- und die höhere zählt 43'335 CHF Umbuchungen zwischen zwei
        Aufwandskonten doppelt und lässt 22'142 CHF Rückerstattungen weg.

        Die Spalte hält die Habenseite als Entscheidung in den Daten fest, statt sie
        in jeder Abfrage neu aus einer Kontonummer abzuleiten.
        """
        spalten = (katalog.get("bexio_journal") or {}).get("spalten") or {}
        assert "ist_aufwand_haben" in spalten, (
            "die Habenseite ist nicht als Aufwand erkennbar -- jede Ausgabensumme "
            "aus dem Journal wäre brutto und damit zu hoch"
        )


# ── Die drei Korrekturen, gegen erfundene Zeilen ──────────


@pytest.fixture
def dl_mit_zeilen(monkeypatch):
    """Die Leseschicht mit erfundenen Tabellen versorgen.

    Umgangen werden die Datei und der Katalog, nicht die Logik: geprüft wird genau
    das, was zwischen Parquet-Zeile und Kennzahl passiert.
    """
    from app.services import datenraum_lesen as dl

    def bestuecken(tabellen: dict[str, list[dict]]):
        monkeypatch.setattr(dl, "_zeilen", lambda name: tabellen.get(name, []))
        monkeypatch.setattr(dl, "katalog_lesen", lambda: {"tabellen": {}})
        return dl

    return bestuecken


class TestFremdwaehrung:
    """Der Aufwand rechnet in Franken, nicht in Buchungswährung.

    250 der 5262 Buchungen lauten auf Fremdwährung. Der Fehler kam ohne
    Fehlermeldung: bei Cursor 2026 ergab die Buchungswährung 16'164 statt 12'924 CHF.
    """

    def test_journal_liefert_den_frankenwert(self, dl_mit_zeilen):
        from datetime import date

        dl = dl_mit_zeilen({
            "bexio_journal": [{
                "datum": date(2026, 3, 15),
                "betrag": 1000.0,          # USD
                "betrag_chf": 800.0,       # CHF -- das ist die richtige Zahl
                "soll_konto_nr": "6570",
                "haben_konto_nr": "2120",
            }],
        })
        zeilen = dl.journal("2026-01-01", "2026-12-31")
        assert len(zeilen) == 1
        assert zeilen[0]["betrag_chf"] == 800.0
        assert zeilen[0]["soll_nr"] == 6570
        assert zeilen[0]["monat"] == "2026-03"

    def test_die_aufwandssumme_nimmt_den_frankenwert(self, dl_mit_zeilen):
        """Die Prüfung eine Ebene höher: die Aggregation der Ansicht selbst."""
        from datetime import date

        dl_mit_zeilen({
            "bexio_journal": [{
                "datum": date(2026, 3, 15),
                "betrag": 1000.0,
                "betrag_chf": 800.0,
                "soll_konto_nr": "6570",
                "haben_konto_nr": "2120",
            }],
        })
        from app.routers.finance import _compute_expenses_by_month

        assert _compute_expenses_by_month("2026-01-01", "2026-12-31") == {"2026-03": 800.0}


class TestEntwuerfe:
    """Eine Entwurfsrechnung wurde nie gestellt -- sie ist weder Umsatz noch Forderung.

    Der Bestand vom 06.09.2026 enthielt einen Entwurf über 8'000 CHF. Er zählte im
    Umsatz 2026 mit **und** in den offenen Debitoren, die damit 53'593 statt 45'593
    CHF meldeten. Der daraus abgeleitete DSO war entsprechend zu hoch.
    """

    @staticmethod
    def _bestand():
        from datetime import date

        return {
            "bexio_rechnungen": [
                {
                    "datum": date(2026, 5, 1), "brutto": 10_000.0, "offen": 10_000.0,
                    "ist_umsatz": True, "status": "offen",
                    "kunde": "Echtkunde", "kunden_id": 1,
                },
                {
                    "datum": date(2026, 5, 2), "brutto": 8_000.0, "offen": 8_000.0,
                    "ist_umsatz": False, "status": "entwurf",
                    "kunde": "Nochnichtkunde", "kunden_id": 2,
                },
                {
                    "datum": date(2026, 4, 1), "brutto": 5_000.0, "offen": 0.0,
                    "ist_umsatz": True, "status": "bezahlt",
                    "kunde": "Echtkunde", "kunden_id": 1,
                },
            ],
        }

    def test_umsatz_zaehlt_den_entwurf_nicht(self, dl_mit_zeilen):
        dl = dl_mit_zeilen(self._bestand())
        assert dl.umsatz_je_monat() == {"2026-05": 10_000.0, "2026-04": 5_000.0}

    def test_offene_forderungen_zaehlen_den_entwurf_nicht(self, dl_mit_zeilen):
        dl = dl_mit_zeilen(self._bestand())
        assert dl.offene_debitoren() == (10_000.0, 1)

    def test_die_debitorensicht_zaehlt_den_entwurf_nicht(self, dl_mit_zeilen):
        """Dieselbe Regel in der zweiten Ansicht -- vorher eine eigene Kopie."""
        dl_mit_zeilen(self._bestand())
        from app.routers.debtors import _debtors_berechnen

        class _Nutzer:
            settings: dict = {}

        antwort = _debtors_berechnen(_Nutzer())
        assert antwort.total_open == 10_000.0
        assert {d.contact_name for d in antwort.debtors} == {"Echtkunde"}


class TestGeschaeftsjahr:
    """Der Banksaldo beginnt am offenen Geschäftsjahr, nicht an einem festen Datum.

    Die Live-Fassung hatte ``2025-01-01`` verdrahtet -- ab 2027 hätte sie den Saldo
    aus dem falschen Jahr aufsummiert, ohne zu klagen.
    """

    def test_beginn_kommt_aus_dem_offenen_jahr(self, dl_mit_zeilen):
        from datetime import date

        dl = dl_mit_zeilen({
            "bexio_geschaeftsjahre": [
                {"jahr": 2025, "von": date(2025, 1, 1), "bis": date(2025, 12, 31),
                 "ist_abgeschlossen": True},
                {"jahr": 2026, "von": date(2026, 1, 1), "bis": date(2026, 12, 31),
                 "ist_abgeschlossen": False},
            ],
        })
        assert dl.geschaeftsjahr_beginn() == "2026-01-01"

    def test_ohne_offenes_jahr_gilt_das_kalenderjahr(self, dl_mit_zeilen):
        from datetime import date

        dl = dl_mit_zeilen({
            "bexio_geschaeftsjahre": [
                {"jahr": 2025, "von": date(2025, 1, 1), "bis": date(2025, 12, 31),
                 "ist_abgeschlossen": True},
            ],
        })
        assert dl.geschaeftsjahr_beginn() == f"{date.today().year}-01-01"


class TestKeinStillerNullwert:
    """Fehlt eine Tabelle, wird das gemeldet -- nicht mit 0 CHF beantwortet."""

    def test_fehlende_tabelle_wirft(self, monkeypatch, tmp_path):
        from app.services import datenraum_lesen as dl

        monkeypatch.setattr(dl, "datenraum_pfad", lambda: tmp_path)
        dl._vorrat.clear()
        with pytest.raises(dl.DatenraumUnbrauchbar, match="fehlt im Datenraum"):
            dl.kontonamen()

    def test_leer_gemeldete_spalte_wirft(self, monkeypatch):
        from app.services import datenraum_lesen as dl

        monkeypatch.setattr(dl, "_zeilen", lambda name: [])
        monkeypatch.setattr(dl, "katalog_lesen", lambda: {
            "tabellen": {"bexio_journal": {"leere_spalten": ["betrag_chf"]}},
        })
        with pytest.raises(dl.DatenraumUnbrauchbar, match="betrag_chf"):
            dl.journal("2026-01-01", "2026-12-31")


# ── Rückfallschutz: keine Live-Beschaffung in der Ansicht ──


class TestKeineLiveBeschaffung:
    """Die Umstellung darf nicht stückweise zurückwandern.

    Der Rückfall wäre unauffällig: eine einzelne wiedereingeführte Live-Abfrage
    liefert plausible Zahlen und bringt die zweite Definition zurück, die diese
    Umstellung gerade beseitigt hat.
    """

    @staticmethod
    def _quelltext(datei: str) -> str:
        with open(os.path.join(BACKEND, "app", "routers", datei), encoding="utf-8") as f:
            return f.read()

    def test_finance_ruft_live_nur_in_der_kreuzprobe(self):
        quelltext = self._quelltext("finance.py")
        kreuzprobe = quelltext[quelltext.index("async def validate_gegen_live"):]
        ohne_kreuzprobe = quelltext[:quelltext.index("async def validate_gegen_live")]

        for aufruf in ("search_invoices", "get_journal", "list_accounts",
                       "list_bank_accounts", "get_summary_by_project"):
            assert aufruf not in ohne_kreuzprobe, (
                f"'{aufruf}' steht ausserhalb der Kreuzprobe -- die Ansicht holt "
                "wieder live"
            )
        assert "get_journal" in kreuzprobe, "die Kreuzprobe braucht die Live-Seite"

    def test_debtors_hat_keine_eigenen_rechnungshelfer(self):
        """Geprüft wird der Syntaxbaum, nicht der Text.

        Die Namen stehen im Modulkopf von ``debtors.py`` -- dort erklären sie, warum
        sie entfernt wurden. Eine Textsuche würde diese Erklärung als Rückfall
        melden und den Test damit unbrauchbar machen.
        """
        baum = ast.parse(self._quelltext("debtors.py"))
        bezeichner = {
            k.name for k in ast.walk(baum)
            if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | {
            k.id for k in ast.walk(baum) if isinstance(k, ast.Name)
        }

        for kopie in ("_parse_invoice_total", "_invoice_is_open", "_open_amount",
                      "_fetch_invoices", "BexioClient", "TogglClient"):
            assert kopie not in bezeichner, (
                f"'{kopie}' ist wieder da -- zwei Ansichten mit zwei Definitionen "
                "derselben Kennzahl"
            )

    def test_die_ansicht_kann_die_buchungswaehrung_nicht_sehen(self):
        """Der Währungsfehler wird strukturell verhindert, nicht durch Aufmerksamkeit.

        Eine Textsuche nach ``betrag`` taugt hier nicht: die Zeiterfassung führt eine
        Spalte, die tatsächlich so heisst und richtig ist. Entscheidend ist, dass die
        **Journal**-Spalte ``betrag`` nie gelesen wird -- dann kann keine Aggregation
        sie summieren, egal wie sie geschrieben ist.
        """
        from app.services.datenraum_lesen import SPALTENVERTRAG

        journal = SPALTENVERTRAG["bexio_journal"]
        assert "betrag_chf" in journal
        assert "betrag" not in journal, (
            "die Buchungswährung steht im Spaltenvertrag -- damit ist der Fehler "
            "wieder erreichbar (2026 ein Viertel zu viel bei Fremdwährung)"
        )

    def test_jede_finanzantwort_traegt_ihren_stand(self):
        """Ein abgeglichener Bestand ohne sichtbares Alter wäre ein Rückschritt
        gegenüber der Live-Abfrage."""
        quelltext = self._quelltext("finance.py")
        assert quelltext.count("datenstand=Datenstand(") >= 2
        assert "Datenstand(" in self._quelltext("debtors.py")
