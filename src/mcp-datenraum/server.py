"""MCP-Server für den Datenraum: der Wegweiser zu den lokalen Tabellen.

Dieser Server liefert bewusst **keine** Daten. Er sagt dem Agenten nur, was im
Datenraum liegt, wie frisch es ist und dass er damit in der Sandbox rechnen soll --
und er stösst bei Bedarf einen Abgleich an.

Warum die Trennung wichtig ist: Sobald ein Werkzeug Zeilen zurückgibt, wandern sie
durch den Kontext des Modells. Genau das soll der Datenraum verhindern. Der Katalog
ist ein paar hundert Zeichen gross, die Tabellen dahinter sind es nicht.
"""

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.environ.get("TP_MCP_BASE_DIR", "/app"))

logger = logging.getLogger("mcp_datenraum")

TOOLS = [
    Tool(
        name="datenraum_katalog",
        description=(
            "Zeigt, welche Tabellen der Fachsysteme lokal bereitliegen: die "
            "Buchhaltung aus Bexio (Buchungsjournal, Debitoren, Kreditoren, "
            "Kontenplan, Geschäftsjahre), die Belegauswertung aus InvoiceInsight, "
            "Toggl (Zeiterfassung) und Pipedrive (CRM). Liefert Tabellennamen, "
            "Spalten samt Bedeutung, Zeilenzahl, Stand — und fertige Abfragen für "
            "die üblichen Fragen. Aber keine Zeilen.\n\n"
            "IMMER zuerst hier nachsehen, bevor ein Fachsystem einzeln abgefragt wird. "
            "Die Tabellen liegen in der Code-Sandbox unter /daten/<name>.parquet und "
            "werden dort mit duckdb oder pandas ausgewertet: das ist vollständig, "
            "schnell und braucht nur einen einzigen Werkzeugaufruf statt vieler.\n\n"
            "Für Ausgaben und Kosten ist 'bexio_journal' zuständig, NICHT "
            "'bexio_kreditoren' — letztere deckt nur rund ein Fünftel des Aufwands ab. "
            "Der Katalog erklärt das unter 'ausgaben_lesart'."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="datenraum_auffrischen",
        description=(
            "Holt eine Quelle neu vom Fachsystem und schreibt die Tabellen neu. "
            "Nur nötig, wenn die Frage taggenaue Aktualität verlangt und der Stand im "
            "Katalog zu alt ist — der Abgleich läuft ohnehin stündlich. Dauert einige "
            "Sekunden bis Minuten. Gibt nur den neuen Stand zurück, keine Zeilen.\n\n"
            "Beispiel: {\"quelle\": \"bexio\"}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "quelle": {
                    "type": "string",
                    "enum": ["bexio", "toggl", "pipedrive", "invoiceinsight", "alle"],
                    "description": "Welche Quelle aufgefrischt wird",
                },
            },
            "required": ["quelle"],
        },
    ),
]

server = Server("datenraum")

# Bedeutung der Geldspalten. Nur die Spaltennamen zu nennen genügt nicht: «netto»
# und «brutto» sind erst dann eindeutig, wenn dabeisteht, dass die Steuer im einen
# fehlt und im anderen steckt. Ohne diesen Satz wählt ein Modell die Spalte nach
# Gefühl -- und eine Umsatzzahl ohne Steuer sieht genauso plausibel aus wie eine mit.
SPALTEN_BEDEUTUNG = {
    # ── Bexio: das Buchungsjournal, das vollständige Ausgabenbild ──
    "bexio_journal.datum": "BUCHUNGSdatum, nicht Rechnungsdatum -- die beiden liegen regelmässig in verschiedenen Monaten und über den Jahreswechsel auch in verschiedenen Jahren",
    "bexio_journal.betrag": (
        "Betrag in BUCHUNGSwährung -- über mehrere Währungen NICHT summierbar. "
        "250 der 5262 Buchungen lauten auf Fremdwährung, und die Falle ist leise: "
        "die Summe kommt, sieht plausibel aus und ist zu hoch. Bei Cursor 2026 "
        "ergibt 'betrag' 16'164 statt 12'924 CHF, ein Viertel zu viel."
    ),
    "bexio_journal.betrag_chf": (
        "Betrag in CHF; das ist die Spalte für JEDE Summe. Wer nach Geld fragt, "
        "nimmt diese -- nie 'betrag'."
    ),
    "bexio_journal.ist_aufwand": "true, wenn das SOLLkonto ein Aufwandskonto ist (4000-8999). Für jede Ausgabenfrage: WHERE ist_aufwand",
    "bexio_journal.soll_konto": "Sollkonto als «Nummer Name», z.B. '6570 Software'. Bei Aufwand ist das die Kategorie -- danach gruppieren",
    "bexio_journal.soll_konto_nr": "nur die Kontonummer des Sollkontos, für Vergleiche und Bereiche",
    "bexio_journal.haben_konto": "Habenkonto als «Nummer Name». NIE zusätzlich zum Sollkonto summieren -- das zählt jeden Betrag doppelt",
    "bexio_journal.haben_konto_nr": (
        "Kontonummer des Habenkontos; sagt bei Aufwand, WIE bezahlt wurde. "
        "'2120' = der Inhaber hat vorgeschossen (Kreditkarte), '2000' = lief über eine "
        "Lieferantenrechnung, '1021'/'1020' = direkt ab Bankkonto. "
        "'2203' = Bezugsteuer, und das ist eine Falle beim ZÄHLEN: eine Leistung aus "
        "dem Ausland erzeugt ZWEI Aufwandsbuchungen auf demselben Sollkonto -- den "
        "Rechnungsbetrag und die Bezugsteuer darauf. Cursor 2026 steht deshalb mit 62 "
        "Buchungen da, hat aber 31 Rechnungen: 11'956 CHF plus 968 CHF Steuer, "
        "zusammen 12'924 CHF. Für Summen ist beides richtig (die Steuer ist echter "
        "Aufwand, dieses Konto zieht keine Vorsteuer ab), für Anzahlen nicht -- wer "
        "Rechnungen zählen will, schliesst haben_konto_nr = '2203' aus."
    ),
    "bexio_journal.herkunft": (
        "lieferantenrechnung | kundenrechnung | zahlung | manuell. "
        "'manuell' ist der Normalfall, nicht die Ausnahme: 2026 sind 245 von 299 "
        "Aufwandsbuchungen manuell erfasst."
    ),
    "bexio_journal.beschreibung": "Buchungstext als Freitext, z.B. 'Cursor, USA, Usage 2025.12'. Kein Lieferantenfeld -- eine Gruppierung darauf ist eine Gruppierung nach Schreibweise",
    # ── Bexio: Stammdaten ──
    "bexio_konten.konto": "Kontoname allein, ohne Nummer. Im Journal und in bexio_kreditoren heisst dieselbe Spalte «Nummer Name» -- zum Verbinden 'konto_nr' nehmen",
    "bexio_konten.klasse": "aktiven | passiven | ertrag | aufwand_material | personalaufwand | betriebsaufwand | nebenerfolg | ausserordentlich | abschluss -- aus der ersten Ziffer der Kontonummer",
    "bexio_konten.aktiv": "true, wenn das Konto bebuchbar ist",
    "bexio_konten.gesperrt": "true, wenn Bexio das Konto für neue Buchungen gesperrt hat",
    "bexio_geschaeftsjahre.jahr": "Kalenderjahr des Geschäftsjahrs",
    "bexio_geschaeftsjahre.von": "erster Tag des Geschäftsjahrs",
    "bexio_geschaeftsjahre.bis": "letzter Tag des Geschäftsjahrs",
    "bexio_geschaeftsjahre.ist_abgeschlossen": (
        "true = das Jahr ist buchhalterisch fertig und ändert sich nicht mehr. "
        "false = LAUFENDES Jahr, also ein Teiljahr. VOR jedem Jahresvergleich prüfen: "
        "ein offenes Jahr gegen ein abgeschlossenes zu stellen zeigt einen Einbruch, "
        "wo bloss noch Monate fehlen."
    ),
    "bexio_geschaeftsjahre.abgeschlossen_am": "Tag des Abschlusses; leer bei offenen Jahren",
    # ── Bexio: Debitoren, die Einnahmenseite ──
    "bexio_rechnungen.datum": "Rechnungsdatum",
    "bexio_rechnungen.faellig_am": "Zahlungsfrist der Rechnung",
    "bexio_rechnungen.geaendert_am": "letzte Änderung in Bexio -- kein fachliches Datum, nie für Auswertungen nehmen",
    "bexio_rechnungen.netto": "Rechnungsbetrag ohne Mehrwertsteuer -- die übliche Umsatzzahl",
    "bexio_rechnungen.mwst": "ausgewiesene Mehrwertsteuer",
    "bexio_rechnungen.brutto": "Rechnungsbetrag inklusive Mehrwertsteuer (netto + mwst)",
    "bexio_rechnungen.bezahlt": "davon eingegangen, brutto",
    "bexio_rechnungen.offen": "davon noch ausstehend, brutto",
    "bexio_rechnungen.ist_umsatz": "true bei gestellten Rechnungen (offen und bezahlt), false bei Entwürfen -- für Umsatzfragen immer filtern",
    "bexio_rechnungen.kunde": "Kundenname als Text; es gibt keine Spalte 'kundenname'. Für Gruppierungen ist 'kunden_id' die stabile Kennung",
    "bexio_rechnungen.status": "entwurf | offen | bezahlt",
    "bexio_kontakte.name": "Kundenname; 'kunden_id' verbindet mit bexio_rechnungen",
    "bexio_kontakte.ist_lead": "true bei Interessenten ohne Kundenstatus",
    "bexio_kontakte.geaendert_am": "letzte Änderung in Bexio -- kein fachliches Datum",
    # ── Bexio: Kreditoren, die Teilmenge mit Fälligkeit ──
    "bexio_kreditoren.datum": "RECHNUNGSdatum des Lieferanten, nicht das Buchungsdatum",
    "bexio_kreditoren.faellig_am": "Zahlungsfrist -- zusammen mit 'ist_offen' die Grundlage jeder Fälligkeitsfrage",
    "bexio_kreditoren.erfasst_am": "wann die Rechnung in Bexio angelegt wurde -- kein fachliches Datum",
    "bexio_kreditoren.betrag": "Rechnungsbetrag in Rechnungswährung -- über mehrere Währungen NICHT summierbar",
    "bexio_kreditoren.betrag_chf": "Rechnungsbetrag in CHF; das ist die Spalte für jede Summe",
    "bexio_kreditoren.kurs": "Umrechnungskurs bei Fremdwährung; leer bei CHF",
    "bexio_kreditoren.offen_betrag": "noch nicht bezahlter Teil, in Rechnungswährung",
    "bexio_kreditoren.positionen": "Anzahl Positionen der Rechnung; 'konto' nennt das Konto der grössten",
    "bexio_kreditoren.status": "entwurf | offen | bezahlt",
    "bexio_kreditoren.ist_offen": "true bei noch nicht bezahlten Rechnungen",
    "bexio_kreditoren.ueberfaellig": "true, wenn die Zahlungsfrist verstrichen und die Rechnung offen ist",
    "bexio_kreditoren.lieferant": "Lieferantenname als Text; für Gruppierungen ist 'lieferant_id' die stabile Kennung",
    "bexio_kreditoren.konto": "Aufwandskonto als «Nummer Name», z.B. '6512 Internet' -- die buchhalterische Kategorie",
    "bexio_kreditoren.mwst": (
        "Diese Spalte gibt es NICHT. Lieferantenrechnungen dieses Kontos werden ohne "
        "Vorsteuer verbucht -- es gibt genau einen Betrag. Wer nach Vorsteuer fragt, "
        "findet sie in invoiceinsight_rechnungen.mwst."
    ),
    # ── InvoiceInsight: die Belegebene ──
    "invoiceinsight_rechnungen.datum": "RECHNUNGSdatum laut Beleg. Nicht das Buchungsdatum aus bexio_journal -- daher rührt der häufigste Periodenunterschied zwischen den beiden",
    "invoiceinsight_rechnungen.faellig_am": "Zahlungsfrist laut Beleg",
    "invoiceinsight_rechnungen.erfasst_am": "wann der Beleg eingelesen wurde -- kein fachliches Datum",
    "invoiceinsight_rechnungen.jahr": "Jahr aus 'datum', als Zahl. Bequem, aber 'datum' ist ein echter Zeitstempel -- EXTRACT funktioniert auch",
    "invoiceinsight_rechnungen.monat": "Monat aus 'datum' (1-12)",
    "invoiceinsight_rechnungen.quartal": "Quartal aus 'datum' (1-4)",
    "invoiceinsight_rechnungen.betrag_chf": "Rechnungsbetrag in CHF; das ist die Spalte für jede Summe",
    "invoiceinsight_rechnungen.betrag": "Bruttobetrag in Rechnungswährung -- über mehrere Währungen NICHT summierbar",
    "invoiceinsight_rechnungen.betrag_netto": "Betrag ohne Mehrwertsteuer, in Rechnungswährung",
    "invoiceinsight_rechnungen.mwst": (
        "ausgewiesene Mehrwertsteuer. LEER heisst zweierlei und ist nicht "
        "unterscheidbar: entweder trug der Beleg keine (AHV, BVG, Steuern und "
        "Versicherungen sind befreit) oder sie war nicht lesbar. Bei "
        "Auslandsrechnungen ist leer der Normalfall -- dort entsteht die Schweizer "
        "Steuer erst in der Buchhaltung als Bezugsteuer (bexio_journal, Habenkonto 2203)."
    ),
    "invoiceinsight_rechnungen.mwst_satz": "Steuersatz in Prozent, sofern der Beleg einen auswies",
    "invoiceinsight_rechnungen.jahreskosten_chf": (
        "HOCHRECHNUNG, kein bezahlter Betrag: eine Monatsrechnung steht hier mit dem "
        "Zwölffachen. Beantwortet «was kostet uns das im Jahr», niemals «was haben wir "
        "ausgegeben». Für Ausgaben immer 'betrag_chf'."
    ),
    "invoiceinsight_rechnungen.betrag_unbekannt": "true, wenn kein Betrag gelesen werden konnte -- solche Zeilen verfälschen jede Summe nach unten",
    "invoiceinsight_rechnungen.lizenzmenge": "Anzahl Lizenzen laut Beleg, sofern genannt",
    "invoiceinsight_rechnungen.konfidenz": "Sicherheit der Extraktion (0-100); niedrige Werte sind Prüffälle, keine Fehler",
    "invoiceinsight_rechnungen.ist_ausland": "true bei Rechnungen aus dem Ausland -- dort fällt in der Buchhaltung Bezugsteuer an, auf dem Beleg steht keine",
    "invoiceinsight_rechnungen.erneuerung_am": "nächste Verlängerung eines Abos -- die Spalte für «was läuft demnächst aus»",
    "invoiceinsight_rechnungen.leistung_von": "Beginn der abgerechneten Leistungsperiode",
    "invoiceinsight_rechnungen.leistung_bis": "Ende der abgerechneten Leistungsperiode",
    "invoiceinsight_rechnungen.dokumenttyp": (
        "Nicht jede Zeile ist eine Rechnung: auch KONTOAUSZUG, MAHNUNG, GUTSCHRIFT und "
        "VORAUSRECHNUNG kommen vor. Für Ausgabensummen auf 'RECHNUNG' filtern."
    ),
    "invoiceinsight_rechnungen.zahlungsart": (
        "KREDITKARTE oder RECHNUNG. Entscheidet, auf welchem Weg der Beleg in die "
        "Buchhaltung gelangt: auf Rechnung als Zeile in bexio_kreditoren, mit Karte "
        "dagegen als Journalbuchung gegen Konto 2120. Verbucht wird BEIDES."
    ),
    "invoiceinsight_rechnungen.kategorie": "SOFTWARE, AI_LLM, TELEKOM, DOMAIN, HARDWARE, PERSONAL, ... -- der Detailgrad, den Bexio nicht führt",
    "invoiceinsight_rechnungen.lieferant": "vereinheitlichter Name; 'lieferant_original' trägt die Schreibweise der Rechnung",
    "invoiceinsight_rechnungen.qr_betrag": (
        "Betrag aus dem QR-Code des Einzahlungsscheins -- maschinell gelesen, nicht "
        "geschätzt, und deshalb die Gegenprobe zur Extraktion: bei 210 von 212 Belegen "
        "mit QR-Code stimmt er auf den Rappen mit 'betrag' überein. Für Summen bleibt "
        "trotzdem 'betrag_chf' zuständig, weil nur ein Fünftel der Belege einen QR-Code "
        "trägt. 0.00 ist KEIN Fehler: der Einzahlungsschein liess den Betrag offen."
    ),
    "invoiceinsight_rechnungen.qr_kreditor": "Zahlungsempfänger laut QR-Code -- die zuverlässigste Schreibweise des Lieferanten",
    "invoiceinsight_rechnungen.beleg_datei": "Dateiname des PDF, damit eine Zahl auf ihren Beleg zeigt",
    # ── Toggl ──
    "toggl_zeiteintraege.datum": "Tag der Zeitbuchung",
    "toggl_zeiteintraege.beginn": "Startzeitpunkt der Buchung",
    "toggl_zeiteintraege.stunden": "Dauer in Stunden als Dezimalzahl, eine Zeile je Buchung",
    "toggl_zeiteintraege.stundensatz": "hinterlegter Satz des Projekts; leer bei nicht verrechenbaren Projekten",
    "toggl_zeiteintraege.kunde": "Kunde des Projekts; leer, wenn das Projekt keinem Kunden zugeordnet ist",
    "toggl_zeiteintraege.projekt": "Projektname; 'projekt_id' ist die stabile Kennung",
    "toggl_zeiteintraege.betrag": "verrechenbarer Betrag dieser Buchung (Stundensatz mal Zeit)",
    "toggl_zeiteintraege.verrechenbar": "true, wenn die Zeit fakturierbar erfasst wurde -- sagt nichts darüber, ob sie fakturiert wurde",
    "toggl_projekte.aktiv": "true bei laufenden Projekten",
    "toggl_projekte.verrechenbar": "true, wenn das Projekt grundsätzlich fakturierbar ist",
    "toggl_projekte.kunde": "Kunde des Projekts; 'kunden_id' ist die stabile Kennung",
    "toggl_projekte.projekt": "Projektname; 'projekt_id' ist die stabile Kennung",
    # ── Pipedrive ──
    "pipedrive_deals.status": "open | won | lost",
    "pipedrive_deals.wert": (
        "Auftragswert im CRM -- **nicht** Umsatz. Was tatsächlich fakturiert wurde, "
        "steht in bexio_rechnungen. Beide Zahlen weichen regelmässig voneinander ab."
    ),
    "pipedrive_deals.wahrscheinlichkeit": "Abschlusswahrscheinlichkeit in Prozent, gepflegt im CRM",
    "pipedrive_deals.organisation": (
        "Freitext aus dem CRM. Derselbe Kunde kann unter mehreren Schreibweisen "
        "geführt sein; eine Summe je Kunde ist deshalb eine Summe je Schreibweise. "
        "'organisation_id' ist die stabile Kennung."
    ),
    "pipedrive_deals.person": "Ansprechperson; 'person_id' ist die stabile Kennung",
    "pipedrive_deals.phase": "Name der Trichterphase (nicht die Kennung)",
    "pipedrive_deals.erstellt_am": "Anlage des Deals",
    "pipedrive_deals.abgeschlossen_am": "gesetzt bei gewonnenen UND verlorenen Deals",
    "pipedrive_deals.gewonnen_am": "gesetzt nur bei status='won' -- für Jahresauswertungen gewonnener Deals genauer als 'abgeschlossen_am'",
    "pipedrive_deals.verloren_am": "gesetzt nur bei status='lost'",
    "pipedrive_deals.erwarteter_abschluss": "geplantes Abschlussdatum offener Deals",
    "pipedrive_deals.archiviert": "true bei archivierten Deals -- für laufende Auswertungen ausschliessen",
    "pipedrive_personen.organisation": "Organisation der Person; ein Deal darf NICHT über die Organisation mit Personen verbunden werden, das vervielfacht jeden Deal",
}

# Wie die drei Kreditorentabellen zueinander stehen. Steht bewusst getrennt von
# SPALTEN_BEDEUTUNG, weil es keine Spalte erklärt, sondern eine Mengenbeziehung --
# und weil ein Vergleich der blossen Zeilenzahlen die falsche Antwort ergäbe.
#
# Der Text stand bis zum 03.09.2026 falsch hier: er empfahl 'bexio_kreditoren' für
# Ausgabensummen und behauptete dabei, diese Tabelle summiere 2025 auf 88'177 CHF.
# Beides war falsch. Die Tabelle summiert auf 156'934 CHF, und die 88'177 stammen
# aus dem Journal (Aufwand mit Haben 2000). Ein Agent hätte die Aussage in zehn
# Sekunden widerlegt -- und danach dem ganzen Katalog nicht mehr geglaubt.
KREDITOREN_LESART = (
    "Drei Tabellen berühren Ausgaben, und jede beantwortet eine ANDERE Frage:\n\n"
    "1. «Was hat uns X gekostet?» -> 'bexio_journal' mit WHERE ist_aufwand, "
    "gruppiert nach 'soll_konto'. NUR hier ist das Bild vollständig: 2025 sind das "
    "401'459 CHF, wovon bloss 88'177 (22 Prozent) über den Kreditorenweg liefen.\n"
    "2. «Was ist offen, wann fällig?» -> 'bexio_kreditoren'. Für Ausgabensummen "
    "UNGEEIGNET, aus zwei verschiedenen Gründen: die Tabelle enthält NUR erfasste "
    "Lieferantenrechnungen (der grössere Teil des Aufwands wird manuell gebucht), "
    "und sie enthält ZUGLEICH Rechnungen, die gar kein Aufwand sind.\n"
    "3. «Wofür genau, welches Produkt, welche Mehrwertsteuer, wann läuft das Abo "
    "aus?» -> 'invoiceinsight_rechnungen'.\n\n"
    "Die naive Summe über 'bexio_kreditoren' ergibt 2025 156'934 CHF und ist in "
    "BEIDE Richtungen falsch. Zu hoch, weil 68'796 CHF davon auf Bilanzkonten "
    "kontiert sind und keinen Aufwand darstellen: Rückzahlungen an den Inhaber "
    "(2120), MWST-Abrechnung (2201), beschlossene Ausschüttung (2261). Zu tief, "
    "weil alles fehlt, was nicht als Lieferantenrechnung erfasst wurde. Wer nach "
    "Aufwandskonten filtert -- erste Ziffer von 'konto_nr' zwischen 4 und 8 --, "
    "erhält 88'138 CHF und trifft damit den Journalwert auf 39 CHF genau; 2023, "
    "2024 und 2026 stimmen auf den Franken. Die Tabellen widersprechen sich also "
    "nicht, die naive Summe stellt bloss die falsche Frage.\n\n"
    "Warum Journal und InvoiceInsight auseinanderlaufen -- drei Gründe, keiner "
    "davon ein Fehler:\n"
    "- ZAHLWEG: mit Karte bezahlte Abos stehen nicht in 'bexio_kreditoren', sondern "
    "im Journal als 'Soll Aufwandskonto / Haben 2120 Kontokorrent Gesellschafter'. "
    "Cursor steht 2026 mit 12'924 CHF im Journal und mit null in 'bexio_kreditoren'. "
    "Was der Inhaber insgesamt vorgeschossen hat, beantwortet 'haben_konto_nr' = "
    "'2120': 2025 waren das 24'255 CHF.\n"
    "- PERIODE: 'invoiceinsight_rechnungen.datum' ist das Rechnungsdatum, "
    "'bexio_journal.datum' das Buchungsdatum. Über den Jahreswechsel landet derselbe "
    "Beleg dadurch in zwei verschiedenen Jahren.\n"
    "- SAMMELBUCHUNG: mehrere Belege werden zu einer Buchung zusammengefasst. Cursor "
    "2026: 129 Einzelrechnungen gegen 31 Buchungsvorgänge.\n\n"
    "Bei Lieferanten auf Rechnung beschreiben die Systeme DIESELBEN Belege: von 16 "
    "T+R-Rechnungen in Bexio stehen 15 in InvoiceInsight mit identischem Datum und "
    "identischem Betrag. Trotzdem NIE über zwei Tabellen hinweg summieren -- das "
    "zählt doppelt. Immer eine Tabelle wählen und dazusagen, welche."
)

# Fertige Abfragen für die Fragen, die tatsächlich gestellt werden.
#
# Der Grund ist gemessen: das lokale Modell erfand Spaltennamen, liess die
# Anführungszeichen um Pfade weg und griff bei Ausgabenfragen zur falschen Tabelle.
# Eine lauffähige Abfrage abzuwandeln gelingt zuverlässig, eine aus einer
# Beschreibung zu bauen nicht.
REZEPTE = {
    "was hat uns etwas gekostet (Aufwand je Kategorie und Jahr)": (
        "SELECT EXTRACT(YEAR FROM datum) AS jahr, soll_konto,\n"
        "       count(*) AS buchungen, round(sum(betrag_chf),2) AS chf\n"
        "FROM '/daten/bexio_journal.parquet'\n"
        "WHERE ist_aufwand\n"
        "GROUP BY 1, 2 ORDER BY jahr DESC, chf DESC"
    ),
    "was hat der Inhaber privat vorgeschossen (Kreditkarte)": (
        "SELECT EXTRACT(YEAR FROM datum) AS jahr, soll_konto,\n"
        "       round(sum(betrag_chf),2) AS chf\n"
        "FROM '/daten/bexio_journal.parquet'\n"
        "WHERE ist_aufwand AND haben_konto_nr = '2120'\n"
        "GROUP BY 1, 2 ORDER BY jahr DESC, chf DESC"
    ),
    "ist der Jahresvergleich zulässig (offenes gegen abgeschlossenes Jahr)": (
        "SELECT jahr, von, bis, ist_abgeschlossen\n"
        "FROM '/daten/bexio_geschaeftsjahre.parquet' ORDER BY jahr DESC"
    ),
    "wofür geben wir Geld aus (Produkt, Kategorie, Abo-Erneuerung)": (
        "SELECT kategorie, lieferant, count(*) AS belege,\n"
        "       round(sum(betrag_chf),2) AS chf, max(erneuerung_am) AS naechste_erneuerung\n"
        "FROM '/daten/invoiceinsight_rechnungen.parquet'\n"
        "WHERE dokumenttyp = 'RECHNUNG' AND EXTRACT(YEAR FROM datum) = 2026\n"
        "GROUP BY 1, 2 ORDER BY chf DESC LIMIT 25"
    ),
    "was ist offen und überfällig": (
        "SELECT lieferant, nummer, datum, faellig_am, round(betrag_chf,2) AS chf\n"
        "FROM '/daten/bexio_kreditoren.parquet'\n"
        "WHERE ist_offen ORDER BY faellig_am"
    ),
    "was lief über Lieferantenrechnungen (und ist wirklich Aufwand)": (
        "-- Ohne den Kontofilter zählt die Summe Rückzahlungen an den Inhaber,\n"
        "-- die MWST-Abrechnung und Ausschüttungen mit: 2025 sind das 68'796 CHF,\n"
        "-- die kein Aufwand sind. Mit Filter trifft die Summe den Journalwert.\n"
        "SELECT EXTRACT(YEAR FROM datum) AS jahr, konto,\n"
        "       count(*) AS rechnungen, round(sum(betrag_chf),2) AS chf\n"
        "FROM '/daten/bexio_kreditoren.parquet'\n"
        "WHERE substr(konto_nr, 1, 1) BETWEEN '4' AND '8'\n"
        "GROUP BY 1, 2 ORDER BY jahr DESC, chf DESC"
    ),
    "wie viele Rechnungen von einem Lieferanten (ohne Bezugsteuer doppelt zu zählen)": (
        "SELECT EXTRACT(YEAR FROM datum) AS jahr,\n"
        "       count(*) AS rechnungen, round(sum(betrag_chf),2) AS chf\n"
        "FROM '/daten/bexio_journal.parquet'\n"
        "WHERE ist_aufwand AND haben_konto_nr <> '2203'\n"
        "  AND lower(beschreibung) LIKE '%cursor%'\n"
        "GROUP BY 1 ORDER BY jahr DESC"
    ),
    "Umsatz je Kunde": (
        "SELECT kunde, count(*) AS rechnungen, round(sum(netto),2) AS netto,\n"
        "       round(sum(offen),2) AS offen\n"
        "FROM '/daten/bexio_rechnungen.parquet'\n"
        "WHERE ist_umsatz GROUP BY 1 ORDER BY netto DESC LIMIT 20"
    ),
}

# Eine lauffähige Vorlage statt einer Beschreibung. Der Lauf vom 02.09.2026 scheiterte
# sechsmal an erfundenen Spaltennamen und einem unzitierten Pfad -- beides Fehler, die
# beim Abwandeln einer funktionierenden Abfrage nicht entstehen.
VORLAGE = (
    "Mit execute_code auswerten -- eine einzige Ausführung genügt für die ganze Frage:\n"
    "import duckdb\n"
    "print(duckdb.sql(\"\"\"\n"
    "  SELECT kunde, count(*) AS rechnungen, round(sum(netto),2) AS netto,\n"
    "         round(sum(brutto),2) AS brutto, round(sum(offen),2) AS offen,\n"
    "         round(100.0*sum(netto)/sum(sum(netto)) OVER (), 1) AS anteil_prozent\n"
    "  FROM '/daten/bexio_rechnungen.parquet'\n"
    "  WHERE ist_umsatz\n"
    "  GROUP BY kunde ORDER BY netto DESC LIMIT 20\n"
    "\"\"\"))\n"
    "Die Pfade müssen in einfachen Anführungszeichen stehen. Spaltennamen genau aus "
    "'spalten' übernehmen, nicht erfinden.\n\n"
    "Bei Fragen nach «wie viel pro Kunde» oder «wie viele je Gruppe»: die Gruppierung "
    "gehört in die Abfrage (GROUP BY), nicht in den Kopf. Nie Einzelzeilen ausgeben und "
    "danach selbst zusammenzählen -- jede Zahl der Antwort muss so in der Ausgabe des "
    "Laufs stehen. Wo sie das nicht tut, ist sie geraten.\n"
    "**Das gilt auch für den Fliesstext**, nicht nur für die Tabellen: Anteile in "
    "Prozent, Zwischensummen, Durchschnitte, «ein Drittel», «über dem Schnitt» und jeder "
    "Vergleich zweier Zeilen sind Rechnungen. Sie gehören in dieselbe Abfrage (etwa "
    "`round(100.0*count(*)/sum(count(*)) OVER (), 1) AS anteil`) und werden von dort "
    "abgeschrieben. Wer eine korrekte Tabelle ausgibt und darunter im Kommentar rechnet, "
    "erzeugt einen Widerspruch im eigenen Text -- und der Kommentar wird eher gelesen "
    "als die Tabelle. Beim Abschreiben Zeile für Zeile prüfen, dass Beschriftung und "
    "Wert zusammengehören.\n"
    "Vor dem Verknüpfen zweier Tabellen prüfen, worüber verknüpft wird: eine Verbindung "
    "über die Organisation statt über die Person vervielfacht jeden Deal um die Anzahl "
    "ihrer Kontaktpersonen -- die Summe steigt, und nichts weist darauf hin.\n\n"
    "Datumsspalten sind echte DATE- bzw. TIMESTAMP-Werte. EXTRACT, Vergleiche und "
    "Sortierung funktionieren direkt; eine Umwandlung von Hand ist weder nötig noch "
    "richtig.\n"
    "Die Ausgabe knapp halten: aggregierte Tabellen und LIMIT statt jeder Einzelzeile. "
    "Die Ausgabe eines Laufs wird bei 20'000 Zeichen abgeschnitten, und lange Listen "
    "verdrängen den Platz, den die Antwort selbst noch braucht.\n\n"
    "Sobald die **gestellte** Frage beantwortet ist, die Antwort schreiben. Zusätzliche "
    "Auswertungen, nach denen niemand gefragt hat, kosten den Platz und die Zeit, die "
    "der Antwort fehlen.\n"
    "Scheitert eine Abfrage dreimal, nicht ein viertes Mal versuchen: mit dem antworten, "
    "was schon vorliegt, und den fehlenden Teil benennen. Ein genannter blinder Fleck "
    "ist brauchbar, ein abgebrochener Lauf ist es nicht. Meldet DuckDB «Referenced "
    "column X not found», steht der richtige Name in derselben Meldung unter "
    "«Candidate bindings» -- ihn von dort übernehmen, nicht neu raten."
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


def _antwort(nutzlast: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(nutzlast, ensure_ascii=False, indent=2))]


def _bedeutungen_der_vorhandenen(katalog: dict) -> dict:
    """Nur die Spaltenerklärungen zu Tabellen, die es gerade gibt.

    Fällt eine Quelle aus, steht ihre Tabelle nicht im Katalog. Ihre Spalten
    trotzdem zu erklären, verleitet zu einer Abfrage auf eine Datei, die nicht da
    ist -- und der Fehler kommt dann aus DuckDB statt aus dem Katalog.

    Erklärungen ohne Tabellenpräfix (falls je welche dazukommen) bleiben immer
    stehen; sie gehören keiner Tabelle.
    """
    vorhanden = set(katalog.get("tabellen", {}))
    return {
        schluessel: text
        for schluessel, text in SPALTEN_BEDEUTUNG.items()
        if "." not in schluessel or schluessel.split(".", 1)[0] in vorhanden
    }


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    from app.services.datenraum import QUELLEN, abgleichen, katalog_lesen

    if name == "datenraum_katalog":
        katalog = katalog_lesen()
        if not katalog.get("tabellen"):
            return _antwort({
                "leer": True,
                "hinweis": (
                    "Der Datenraum ist noch leer. Mit datenraum_auffrischen "
                    "({'quelle': 'alle'}) befüllen oder abwarten -- der Abgleich läuft "
                    "stündlich."
                ),
            })
        return _antwort({
            "stand": katalog.get("stand"),
            "tabellen": {
                tab: {
                    # Fertig zitierter Pfad statt eines nackten Dateinamens: ohne die
                    # einfachen Anführungszeichen antwortet DuckDB mit «syntax error at
                    # or near "/"», und genau das ist am 02.09.2026 passiert.
                    "in_sql": f"'/daten/{eintrag['datei']}'",
                    "zeilen": eintrag.get("zeilen"),
                    "spalten": list((eintrag.get("spalten") or {}).keys()),
                    "stand": eintrag.get("stand"),
                    # Eine Spalte, die über den ganzen Bestand leer ist, trägt keine
                    # Antwort. Sie zu verschweigen hiesse, den Agenten eine Null
                    # ausrechnen zu lassen, die wie ein Ergebnis aussieht.
                    **({"unbrauchbar_weil_durchgehend_leer": eintrag["leere_spalten"]}
                       if eintrag.get("leere_spalten") else {}),
                }
                for tab, eintrag in katalog.get("tabellen", {}).items()
            },
            # Nur die Spalten erklären, die auch dastehen. Eine Erklärung zu einer
            # fehlenden Tabelle kostet Platz im Kontext und stiftet Verwirrung:
            # der Agent sucht dann eine Spalte, die es gerade nicht gibt.
            "spalten_bedeutung": _bedeutungen_der_vorhandenen(katalog),
            # Nur mitgeben, wenn die Tabellen tatsächlich dastehen -- sonst erklärt
            # der Katalog eine Beziehung zwischen einer Tabelle und nichts.
            **({"ausgaben_lesart": KREDITOREN_LESART}
               if {"bexio_journal", "bexio_kreditoren", "invoiceinsight_rechnungen"}
               <= set(katalog.get("tabellen", {}))
               else {}),
            "quellen": {
                q: {
                    "stand": e.get("stand"),
                    "letzter_fehler": e.get("letzter_fehler"),
                    "hinweise": e.get("hinweise"),
                }
                for q, e in katalog.get("quellen", {}).items()
            },
            "fertige_abfragen": REZEPTE,
            "so_gehts_weiter": VORLAGE,
        })

    if name == "datenraum_auffrischen":
        quelle = (arguments or {}).get("quelle", "").strip()
        if not quelle:
            return _antwort({"fehler": "Parameter 'quelle' fehlt", "erlaubt": [*QUELLEN, "alle"]})
        if quelle != "alle" and quelle not in QUELLEN:
            return _antwort({"fehler": f"Unbekannte Quelle '{quelle}'", "erlaubt": [*QUELLEN, "alle"]})

        logger.info("Datenraum wird aufgefrischt: %s", quelle)
        katalog = await abgleichen(None if quelle == "alle" else [quelle])
        betroffen = list(QUELLEN) if quelle == "alle" else [quelle]
        return _antwort({
            "stand": katalog.get("stand"),
            "quellen": {
                q: {
                    "stand": katalog.get("quellen", {}).get(q, {}).get("stand"),
                    "letzter_fehler": katalog.get("quellen", {}).get(q, {}).get("letzter_fehler"),
                    "tabellen": katalog.get("quellen", {}).get(q, {}).get("tabellen"),
                }
                for q in betroffen
            },
        })

    return _antwort({"fehler": f"Unbekanntes Werkzeug: {name}"})


async def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
