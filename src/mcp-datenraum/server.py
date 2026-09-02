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
            "Zeigt, welche Tabellen der Fachsysteme lokal bereitliegen: Bexio "
            "(Rechnungen, Kontakte), Toggl (Zeiteinträge, Projekte), Pipedrive "
            "(Deals, Personen, Organisationen). Liefert Tabellennamen, Spalten, "
            "Zeilenzahl und Stand der Daten — aber keine Zeilen.\n\n"
            "IMMER zuerst hier nachsehen, bevor ein Fachsystem einzeln abgefragt wird. "
            "Die Tabellen liegen in der Code-Sandbox unter /daten/<name>.parquet und "
            "werden dort mit duckdb oder pandas ausgewertet: das ist vollständig, "
            "schnell und braucht nur einen einzigen Werkzeugaufruf statt vieler."
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
                    "enum": ["bexio", "toggl", "pipedrive", "alle"],
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
    "bexio_rechnungen.netto": "Rechnungsbetrag ohne Mehrwertsteuer -- die übliche Umsatzzahl",
    "bexio_rechnungen.mwst": "ausgewiesene Mehrwertsteuer",
    "bexio_rechnungen.brutto": "Rechnungsbetrag inklusive Mehrwertsteuer (netto + mwst)",
    "bexio_rechnungen.bezahlt": "davon eingegangen, brutto",
    "bexio_rechnungen.offen": "davon noch ausstehend, brutto",
    "bexio_rechnungen.ist_umsatz": "true bei gestellten Rechnungen (offen und bezahlt), false bei Entwürfen -- für Umsatzfragen immer filtern",
    "bexio_rechnungen.kunde": "Kundenname als Text; es gibt keine Spalte 'kundenname'",
    "bexio_rechnungen.status": "entwurf | offen | bezahlt",
    "toggl_zeiteintraege.stunden": "Dauer in Stunden als Dezimalzahl, eine Zeile je Buchung",
    "toggl_zeiteintraege.kunde": "Kunde des Projekts; leer, wenn das Projekt keinem Kunden zugeordnet ist",
    "toggl_zeiteintraege.betrag": "verrechenbarer Betrag dieser Buchung (Stundensatz mal Zeit)",
    "toggl_zeiteintraege.verrechenbar": "true, wenn die Zeit fakturierbar erfasst wurde -- sagt nichts darüber, ob sie fakturiert wurde",
    "pipedrive_deals.status": "open | won | lost",
    "pipedrive_deals.wert": (
        "Auftragswert im CRM -- **nicht** Umsatz. Was tatsächlich fakturiert wurde, "
        "steht in bexio_rechnungen. Beide Zahlen weichen regelmässig voneinander ab."
    ),
    "pipedrive_deals.organisation": (
        "Freitext aus dem CRM. Derselbe Kunde kann unter mehreren Schreibweisen "
        "geführt sein; eine Summe je Kunde ist deshalb eine Summe je Schreibweise."
    ),
    "pipedrive_deals.phase": "Name der Trichterphase (nicht die Kennung)",
    "pipedrive_deals.gewonnen_am": "gesetzt nur bei status='won' -- für Jahresauswertungen gewonnener Deals genauer als 'abgeschlossen_am'",
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
            "spalten_bedeutung": SPALTEN_BEDEUTUNG,
            "quellen": {
                q: {
                    "stand": e.get("stand"),
                    "letzter_fehler": e.get("letzter_fehler"),
                    "hinweise": e.get("hinweise"),
                }
                for q, e in katalog.get("quellen", {}).items()
            },
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
