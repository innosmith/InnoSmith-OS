"""Deutung der Bexio-Rechnungspositionen.

Die Erkennungsmerkmale sind aus ``D2_updateDraftRechnungen`` übernommen; geprüft
wird hier vor allem das, was die Vorlage **nicht** tat — laut scheitern, wenn die
Textsuche danebengreift.
"""

from app.services.debitoren_positionen import STUNDENEINHEIT, deuten


def stunden(text: str, menge: str, pos: int = 1, **rest) -> dict:
    return {
        "id": pos, "pos": pos, "positionsart": "custom",
        "unit_id": STUNDENEINHEIT, "amount": menge, "text": text, **rest,
    }


def textzeile(text: str, pos: int = 2) -> dict:
    return {"id": 100 + pos, "pos": pos, "positionsart": "text", "text": text}


UEBERTRAGSSATZ = (
    "Per 31.08.2026 sind 3.5h verrechnet aber noch nicht geleistet, "
    "welche dem nächsten Monat angerechnet werden."
)


def pauschale(text: str, pos: int = 1, preis: str = "3400.00") -> dict:
    """Die Fixposition, wie Bexio sie tatsächlich führt: eine Einheit zum
    Monatspreis. Die Stunden nennt allein der Text."""
    return {
        "id": pos, "pos": pos, "positionsart": "custom",
        "unit_id": 1, "amount": "1.00", "unit_price": preis, "text": text,
    }


class TestEchteAugustrechnungen:
    """Die Fälle, an denen die erste Fassung scheiterte — Wortlaut aus Bexio.

    Sie verlangte für die Fixstunden die Stundeneinheit. Die Pauschale steht
    aber als ``1 x 3400.00`` in der Rechnung, und damit trug im Rücklauf jede
    der sechs Pauschalrechnungen eine Warnung statt eines Urteils.
    """

    def test_pauschale_mit_zusatzstunden(self):
        """RE-00695 ImpulsKöniz: 7h Pauschale, 0.25h darüber hinaus."""
        deutung = deuten([
            pauschale(
                "Decidim Sparring Partner im Monatsabo mit: - 7h fix inklusive "
                "(nicht übertragbar) - Monatlich kündbar",
                pos=1, preis="1400.00",
            ),
            stunden("Variable Zusatzstunden", "0.25", pos=2),
        ])
        assert deutung.fix_stunden == 7.0
        assert deutung.zusatz_stunden == 0.25
        assert deutung.verrechnete_stunden == 7.25, (
            "ohne die Stunden der Pauschale meldete die Prüfung «0.25h verrechnet» "
            "gegen 7.25h geleistet — richtig gezählt und trotzdem falsch"
        )
        assert deutung.eindeutig, deutung.auffaelligkeiten

    def test_pauschale_mit_halber_stunde(self):
        """RE-00696 Cheetah: 14.5h. Der Altbestand las hier 5h, weil sein
        Muster ``(\\d+)h`` keine Dezimalstelle kannte."""
        deutung = deuten([
            pauschale(
                "Unterstützung mit einer monatlichen Kapazität von: - 14.5h fix "
                "inklusive (übertragbar in Folgemonat)",
                preis="3198.70",
            ),
            textzeile(
                "Per 31.08.2026 sind 7h verrechnet aber noch nicht geleistet, "
                "welche dem nächsten Monat angerechnet werden.", pos=2,
            ),
        ])
        assert deutung.fix_stunden == 14.5
        assert deutung.verrechnete_stunden == 14.5
        assert deutung.uebertrag_angabe == 7.0
        assert deutung.eindeutig, deutung.auffaelligkeiten

    def test_html_entitaeten_im_positionstext(self):
        """Bexio liefert «Unterst&uuml;tzung». Ohne Auflösen stand das so in
        jeder Meldung, und ein Muster auf «für» träfe nie ein «f&uuml;r»."""
        deutung = deuten([
            pauschale("Unterst&uuml;tzung mit einer Kapazit&auml;t von 14h fix inklusive"),
        ])
        assert deutung.fix_stunden == 14.0
        assert deutung.eindeutig, deutung.auffaelligkeiten

    def test_fremdposition_neben_der_pauschale_wird_genannt(self):
        """RE-00698 Sympholio: die Hostingkosten sind keine Stunden — die
        Pauschale selbst darf aber nicht als übergangen gemeldet werden."""
        deutung = deuten([
            pauschale("Unterstützung mit 14h fix inklusive (übertragbar)"),
            {"id": 2, "pos": 2, "positionsart": "custom", "unit_id": 1,
             "amount": "1.00", "text": "Monatlich Hostingkosten Sympholio Cockpit"},
        ])
        assert deutung.fix_stunden == 14.0
        assert deutung.verrechnete_stunden == 14.0
        uebergangen = [a for a in deutung.auffaelligkeiten if "ohne Stundeneinheit" in a]
        assert len(uebergangen) == 1
        assert "hostingkosten" in uebergangen[0].lower()
        assert "kapazität" not in uebergangen[0].lower()

    def test_rein_variable_rechnung_ist_kein_sonderfall(self):
        """RE-00704 COflow: eine Stundenposition, die nach dem Projekt heisst.

        Die erste Fassung meldete hier «weder Fix- noch Zusatzposition» — ein
        Fehlalarm aus nichts, denn ohne Pauschale *ist* diese Position die
        Abrechnung.
        """
        deutung = deuten([
            stunden("COflow – Effektiver Aufwand in der Entwicklung", "19.50"),
        ])
        assert deutung.verrechnete_stunden == 19.5
        assert deutung.fix_stunden is None
        assert deutung.eindeutig, deutung.auffaelligkeiten


class TestVollstaendigeRechnung:
    def test_alle_drei_teile_werden_erkannt(self):
        deutung = deuten([
            stunden("20h/Monat fix inklusive Bereitschaft", "20.00", pos=1),
            textzeile(UEBERTRAGSSATZ, pos=2),
            stunden("Variable Zusatzstunden", "2.50", pos=3),
        ])
        assert deutung.fix_stunden == 20.0
        assert deutung.zusatz_stunden == 2.5
        assert deutung.uebertrag_angabe == 3.5
        assert deutung.verrechnete_stunden == 22.5
        assert deutung.eindeutig, deutung.auffaelligkeiten

    def test_stundensumme_wird_gelesen_nicht_rekonstruiert(self):
        """Der Kern des Umbaus.

        ``admin_core`` rechnete ``fix + zusatz`` aus denselben zwei Zahlen, gegen
        die es anschliessend verglich — die Differenz war damit immer null. Hier
        ist die Summe die der tatsächlichen Positionsmengen, und sie kann von
        ``fix + zusatz`` abweichen.
        """
        deutung = deuten([
            stunden("20h/Monat fix", "20.00", pos=1),
            stunden("Variable Zusatzstunden", "2.50", pos=2),
            stunden("Nachbearbeitung Workshop", "4.00", pos=3),
        ])
        assert deutung.verrechnete_stunden == 26.5
        assert deutung.fix_stunden + deutung.zusatz_stunden == 22.5


class TestHtmlUndSchreibweisen:
    def test_html_auszeichnung_stoert_den_anfangsvergleich_nicht(self):
        """Ohne Entfernen begänne der Text mit ``<p>`` statt mit ``Variable``."""
        deutung = deuten([
            stunden("<p><strong>Variable Zusatzstunden</strong> September</p>", "2.00")
        ])
        assert deutung.zusatz_stunden == 2.0

    def test_halbe_fixstunden_sind_erkennbar(self):
        """``(\\d+)h`` in der Vorlage liess nur ganze Zahlen zu — und lieferte für
        7.5h keinen Fehler, sondern 0.0."""
        deutung = deuten([stunden("7.5h/Monat fix", "7.50")])
        assert deutung.fix_stunden_laut_text == 7.5
        assert deutung.fix_stunden == 7.5

    def test_schreibweise_ohne_monatsangabe(self):
        deutung = deuten([stunden("19h fix inklusive Support", "19.00")])
        assert deutung.fix_stunden == 19.0


class TestStilleFehlerWerdenLaut:
    def test_nicht_erkannte_stundenposition_wird_gemeldet(self):
        """Genau der Fall, in dem D2 die Zusatzposition nicht fand.

        Greift ``startswith('variable zusatzstunden')`` daneben — etwa weil die
        Position «Zusätzliche Stunden» heisst —, blieb sie im Altbestand auf null
        stehen und die Stunden gingen nicht in Rechnung. Gemeldet wurde nichts.
        """
        deutung = deuten([
            stunden("20h/Monat fix", "20.00", pos=1),
            stunden("Zusätzliche Stunden September", "3.00", pos=2),
        ])
        assert deutung.zusatz_stunden is None
        assert any("nicht gegriffen" in a for a in deutung.auffaelligkeiten)

    def test_position_ohne_stundeneinheit_wird_nicht_stillschweigend_summiert(self):
        deutung = deuten([
            stunden("20h/Monat fix", "20.00", pos=1),
            {"id": 2, "pos": 2, "positionsart": "custom", "unit_id": 1,
             "amount": "1.00", "text": "Pauschale Projektleitung"},
        ])
        assert deutung.verrechnete_stunden == 20.0
        assert any("ohne Stundeneinheit" in a for a in deutung.auffaelligkeiten)

    def test_pauschale_ohne_stundenangabe_ist_keine_fixposition(self):
        """Ohne «Xh fix» im Text gibt es nichts zu deuten — die Position wird
        als übergangene genannt, nicht als Fixposition geraten."""
        deutung = deuten([
            {"id": 1, "pos": 1, "positionsart": "custom", "unit_id": 1,
             "amount": "1.00", "text": "Monatliche Fixkosten Betreuung"},
        ])
        assert deutung.fix_stunden is None
        assert deutung.verrechnete_stunden is None
        assert any("ohne Stundeneinheit" in a for a in deutung.auffaelligkeiten)

    def test_text_und_menge_widersprechen_sich(self):
        deutung = deuten([stunden("20h/Monat fix", "16.00")])
        assert any("der Text nennt 20h" in a for a in deutung.auffaelligkeiten)

    def test_zwei_zusatzpositionen_sind_nicht_entscheidbar(self):
        deutung = deuten([
            stunden("Variable Zusatzstunden August", "2.00", pos=1),
            stunden("Variable Zusatzstunden September", "3.00", pos=2),
        ])
        assert deutung.zusatz_stunden is None
        assert any("nicht entscheidbar" in a for a in deutung.auffaelligkeiten)

    def test_uebertragssatz_ohne_lesbare_zahl(self):
        deutung = deuten([
            stunden("20h/Monat fix", "20.00", pos=1),
            textzeile("Per 31.08.2026 sind einige Stunden verrechnet aber noch "
                      "nicht geleistet.", pos=2),
        ])
        assert deutung.uebertrag_angabe is None
        assert any("keine lesbare Stundenzahl" in a for a in deutung.auffaelligkeiten)

    def test_rechnung_ganz_ohne_stunden(self):
        deutung = deuten([
            {"id": 1, "pos": 1, "positionsart": "custom", "unit_id": 1,
             "amount": "1.00", "text": "CAS-Durchführung, Pauschale"},
        ])
        assert deutung.verrechnete_stunden is None
        assert any("nicht feststellbar" in a for a in deutung.auffaelligkeiten)

    def test_leere_rechnung_meldet_nichts_erfundenes(self):
        deutung = deuten([])
        assert deutung.verrechnete_stunden is None
        assert deutung.auffaelligkeiten == []


class TestAbgrenzung:
    def test_zusatzstunden_im_fliesstext_greifen_nicht(self):
        """Die Vorlage verlangt den Textanfang, um «Abrechnung von Zusatzstunden»
        nicht fälschlich zu treffen. Das bleibt so."""
        deutung = deuten([
            stunden("20h/Monat fix", "20.00", pos=1),
            stunden("Abrechnung von Zusatzstunden aus dem Vorquartal", "5.00", pos=2),
        ])
        assert deutung.zusatz_stunden is None

    def test_uebertragssatz_braucht_beide_merkmale(self):
        deutung = deuten([
            stunden("20h/Monat fix", "20.00", pos=1),
            textzeile("Per Saldo sind alle Leistungen erbracht.", pos=2),
        ])
        assert deutung.uebertrag_angabe is None
