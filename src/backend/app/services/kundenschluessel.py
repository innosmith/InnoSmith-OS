"""Der Kundenschluessel: welche Kennungen dieselbe Kundschaft meinen.

Bexio, Toggl und Pipedrive fuehren dieselbe Kundschaft unter drei Namen in drei
Kennungsraeumen, die sich nicht ueberschneiden. Am 03.09.2026 gemessen:

* Ueber den Namen finden **8 von 16** Toggl-Kunden ihr Gegenstueck in Bexio. Es
  gelingt bei kurzen Firmennamen (``T+R``, ``GSW``) und scheitert bei Kuerzeln --
  also ausgerechnet bei den groessten Kunden.
* «AGG» ergibt in ``bexio_rechnungen`` **null** Treffer. Der Kunde heisst dort
  ``Bau- und Verkehrsdirektion des Kantons Bern (BVD) Amt fuer Grundstuecke und
  Gebaeude`` und hat 227'789 CHF auf 49 Rechnungen. Dasselbe bei MBA
  (676'880 CHF), BFH, AUE und WA-AUE.
* Schlimmer als die Null ist der Beinahe-Treffer: Bexio fuehrt einen zweiten
  Kontakt ``Amt fuer Grundstuecke und Gebaeude (AGG`` mit null Rechnungen. Wer
  Kontakte nach dem Kuerzel durchsucht und sauber ueber ``kunden_id`` verknuepft,
  bekommt 0 CHF -- und der Join sieht fehlerfrei aus.

Deshalb ist die Zuordnung ein **Stammdatum** (``docs/kundenschluessel.yaml``) und
keine Laufzeitheuristik. Kein Aehnlichkeitsmass, keine Stammformen, keine
Straflisten: entweder eine Kennung steht in der Datei, oder sie gilt als nicht
zugeordnet und wird als solche gemeldet.

Der Preis dieser Loesung ist Pflege, und der Preis schlechter Pflege waere ein
neues stilles Versagen -- eine nicht zugeordnete Kundschaft faellt aus jedem Join
heraus, ohne dass jemand es merkt. Dagegen zwei Waechter:

1. **Jede Kennung wird gegen den Datenraum geprueft.** Was dort nicht existiert,
   wird verworfen und gemeldet, statt eine tote Verknuepfung zu erzeugen.
2. **Was fehlt, wird gezaehlt und benannt.** ``nicht_zugeordnet`` nennt je System
   die Kundschaften ohne Schluessel -- dieselbe Bauart wie der Waechter gegen
   durchgehend leere Spalten.

## Wer die Datei pflegt -- und wer nicht

Der Mensch nicht. Eine YAML-Datei ist der Pruefpfad, nicht die Bedienoberflaeche:
gemessen kamen seit 2023 fuenf, ein, ein und fuenf neue Rechnungskunden pro Jahr
dazu, und nur ein Bruchteil davon heisst in zwei Systemen verschieden. Fuer zwei
bis vier Aenderungen im Jahr jemanden YAML editieren zu lassen, waere die falsche
Zumutung -- und eine Pflege, die als laestig empfunden wird, verfaellt.

Darum drei Wege in die Datei, und nur der letzte kostet Aufmerksamkeit:

1. ``vorschlagen()`` laeuft nach jedem Abgleich. Fuer jede nicht zugeordnete
   Kundschaft schlaegt das lokale Modell einen Gegenpart vor; jede vorgeschlagene
   Kennung wird gegen den Bestand geprueft, und was nicht existiert, faellt weg.
   Angenommene Vorschlaege stehen unter ``vorgeschlagen`` und gelten als
   unbestaetigt.
2. ``zuordnen()`` schreibt eine menschliche Entscheidung fest -- aufgerufen vom
   Werkzeug ``kundschaft_zuordnen``, damit die Antwort im Gespraech faellt und
   nicht im Editor.
3. ``frage_notieren()`` haelt fest, was das Modell nicht entscheiden konnte. Nur
   das erreicht den Menschen, und zwar als Frage, nicht als Datei.

Die Grenze zwischen 1 und 2 ist die tragende: **eine Maschine darf hinzufuegen,
aber nie aendern oder entfernen.** Eine neue Kennung war noch nie Gegenstand einer
menschlichen Entscheidung -- sie zu ergaenzen ueberschreibt nichts. Eine
bestehende zu korrigieren hiesse, ein Urteil zu ueberstimmen, und das darf kein
Modell. Deshalb ist ``bestaetigt`` am Eintrag *und* ``vorgeschlagen`` je Kennung
noetig: ohne das zweite Feld wuerde eine maschinelle Ergaenzung einen bestaetigten
Eintrag entweder still mitbestaetigen oder ihn faelschlich entwerten.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Die Systeme, die einen eigenen Kennungsraum haben, und wo ihre Kennungen im
# Datenraum stehen. Deklariert statt erschlossen: welche Spalte eine Kundschaft
# bezeichnet, weiss der Konnektor, nicht der Spaltenname.
SYSTEME: dict[str, tuple[str, str, str]] = {
    # System -> (Tabelle, Kennungsspalte, Namensspalte)
    "bexio": ("bexio_kontakte", "kunden_id", "name"),
    "toggl": ("toggl_zeiteintraege", "kunden_id", "kunde"),
    "pipedrive": ("pipedrive_deals", "organisation_id", "organisation"),
}

# Wer einen Schluessel braucht -- und wer nicht.
#
# Ohne diese Einschraenkung meldete der Waechter 146 nicht zugeordnete
# Bexio-Kontakte, ueberwiegend Lieferanten und Privatpersonen, die richtigerweise
# nie einen Schluessel bekommen. Eine Warnliste, die zu neunzig Prozent aus
# Rauschen besteht, wird nicht gelesen -- und wirkt damit wie gar keine.
#
# Gemeldet wird deshalb nur, wer wirtschaftlich zaehlt: in Bexio, wer eine
# gestellte Rechnung hat; in Toggl jede Kundschaft mit erfasster Zeit; in
# Pipedrive, wer einen Auftrag gewonnen hat. Ein Interessent ohne Abschluss
# braucht keine Verbindung zur Buchhaltung, weil es dort nichts zu verbinden gibt.
RELEVANZ: dict[str, tuple[str, str, tuple[str, object] | None]] = {
    # System -> (Tabelle, Kennungsspalte, Bedingung als (Spalte, Wert) oder None)
    "bexio": ("bexio_rechnungen", "kunden_id", ("ist_umsatz", True)),
    "toggl": ("toggl_zeiteintraege", "kunden_id", None),
    "pipedrive": ("pipedrive_deals", "organisation_id", ("status", "won")),
}

# Der Kopf der Datei. Er steht hier und nicht dort, weil die Datei maschinell neu
# geschrieben wird und ein Kommentar das nicht ueberlebt -- die Begruendung, warum
# es diese Datei ueberhaupt gibt, waere nach der ersten Ergaenzung weg.
KOPF = """\
# Kundenschlüssel — welche Kennungen dieselbe Kundschaft meinen
#
# Bexio, Toggl und Pipedrive führen dieselbe Kundschaft unter drei Namen und drei
# Kennungsräumen, die sich nirgends überschneiden. In Bexio heisst das Amt für
# Grundstücke und Gebäude «Bau- und Verkehrsdirektion des Kantons Bern (BVD) Amt
# für Grundstücke und Gebäude», in Toggl «AGG», in Pipedrive «Amt für Grundstücke
# und Gebäude AGG». Wer wörtlich nach «AGG» sucht, findet in Bexio nichts — und
# bekommt keine Fehlermeldung, sondern 0 CHF.
#
# Diese Datei hält die Zuordnung als Stammdatum fest, statt sie zur Laufzeit zu
# raten. Kein Ähnlichkeitsmass, keine Stammformen, keine Straflisten: entweder
# eine Kennung steht hier, oder sie gilt als nicht zugeordnet und wird als solche
# gemeldet.
#
# ## Von Hand zu pflegen ist sie nicht
#
# Sie wird geschrieben, nicht editiert. Nach jedem Abgleich schlägt das lokale
# Modell für nicht zugeordnete Kundschaften einen Gegenpart vor; jede Kennung wird
# gegen den Datenraum geprüft. Was das Modell nicht entscheiden kann, steht unter
# `offen` — und nur das erreicht einen Menschen, als Frage im Gespräch. Die
# Antwort schreibt das Werkzeug `kundschaft_zuordnen` hierher zurück.
#
# ## Regeln
#
# * Ein Eintrag entsteht nur, wenn dieselbe Kundschaft in **mindestens zwei**
#   Systemen vorkommt. Wer nur in Bexio steht, braucht keinen Schlüssel — dort
#   genügt der eigene Name.
# * Je System eine **Liste** von Kennungen, nie eine einzelne. Dieselbe Kundschaft
#   hat regelmässig mehrere Datensätze: Gemeinde Köniz führt in Bexio zwei
#   Direktionen, T+R steht in Pipedrive dreimal.
# * `bestaetigt: true` heisst «von einem Menschen geprüft». `vorgeschlagen` nennt
#   die einzelnen Kennungen, die eine Maschine ergänzt hat und die noch niemand
#   geprüft hat — auch an einem sonst bestätigten Eintrag.
# * Eine Maschine darf **hinzufügen, nie ändern oder entfernen**. Eine neue
#   Kennung war noch nie Gegenstand einer menschlichen Entscheidung; eine
#   bestehende zu korrigieren hiesse, ein Urteil zu überstimmen.
# * Unsicherheit ist ein zulässiges Ergebnis. Was sich nicht eindeutig zuordnen
#   lässt, steht unter `offen` und wird nicht geraten.
# * Jede Kennung wird beim Laden gegen den Datenraum geprüft. Eine Kennung, die es
#   dort nicht gibt, wird verworfen und gemeldet.
"""


def _datei_finden() -> Path:
    """Wo die gepflegte Datei liegt -- im Baum wie im Container.

    Zwei Ablagen, weil das Modul in beiden laeuft: im Arbeitsbaum unter
    ``<wurzel>/docs``, im Backend-Image unter ``/app/docs``. Gesucht wird deshalb
    die Verzeichniskette aufwaerts und nicht eine feste Tiefe: ``parents[4]`` traf
    im Container nicht bloss daneben, es warf einen ``IndexError`` -- von
    ``/app/app/services`` aus gibt es keine fuenfte Ebene.

    Weil eine fehlende Datei nur eine leere Zuordnung ergibt, waere jeder Fehlgriff
    hier ein stiller Ausfall: alle Kundenfragen ohne Treffer, ohne Fehlermeldung.
    """
    hier = Path(__file__).resolve()
    kandidaten = [
        eltern / "docs" / "kundenschluessel.yaml" for eltern in hier.parents
    ]
    for pfad in kandidaten:
        if pfad.exists():
            return pfad
    # Nichts gefunden: den Ort nennen, der im Arbeitsbaum gemeint waere -- damit
    # die Warnung einen Pfad zeigt, den jemand anlegen kann.
    return kandidaten[min(4, len(kandidaten) - 1)]


DATEI = _datei_finden()


def _datei_lesen(pfad: Path | None = None) -> dict:
    """Die gepflegte Datei laden. Fehlt sie, gilt eine leere Zuordnung."""
    import yaml

    ziel = pfad or _datei_finden()
    try:
        inhalt = yaml.safe_load(ziel.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        # Warnung, nicht Hinweis: ohne Datei beantwortet der Datenraum jede Frage
        # nach einer Kundschaft mit einer leeren Menge.
        logger.warning(
            "Kundenschluessel: %s fehlt -- Kundenfragen finden nichts mehr", ziel
        )
        return {}
    except yaml.YAMLError as exc:
        logger.warning("Kundenschluessel: %s ist nicht lesbar: %s", ziel, exc)
        return {}
    if not isinstance(inhalt, dict):
        logger.warning("Kundenschluessel: %s hat nicht die erwartete Form", ziel)
        return {}
    return inhalt


def _vorhandene_kennungen(system: str, tabellen: dict[str, list[dict]]) -> set:
    """Welche Kennungen dieses Systems im aktuellen Bestand tatsaechlich vorkommen."""
    tabellenname, kennungsspalte, _ = SYSTEME[system]
    zeilen = tabellen.get(tabellenname) or []
    return {z.get(kennungsspalte) for z in zeilen if z.get(kennungsspalte) is not None}


def _relevante_kennungen(system: str, tabellen: dict[str, list[dict]]) -> set:
    """Wer einen Schluessel braucht -- siehe ``RELEVANZ``.

    Fehlt die Tabelle (etwa weil eine Quelle noch nie abgeglichen wurde), gilt
    niemand als relevant: dann fehlt nichts, es ist bloss noch nichts da.
    """
    tabellenname, kennungsspalte, bedingung = RELEVANZ[system]
    zeilen = tabellen.get(tabellenname) or []
    gefunden = set()
    for zeile in zeilen:
        kennung = zeile.get(kennungsspalte)
        if kennung is None:
            continue
        if bedingung is not None:
            spalte, wert = bedingung
            if zeile.get(spalte) != wert:
                continue
        gefunden.add(kennung)
    return gefunden


def _namen(system: str, tabellen: dict[str, list[dict]]) -> dict[Any, str]:
    """Kennung -> Name, wie das jeweilige System die Kundschaft nennt."""
    tabellenname, kennungsspalte, namensspalte = SYSTEME[system]
    gefunden: dict[Any, str] = {}
    for zeile in tabellen.get(tabellenname) or []:
        kennung = zeile.get(kennungsspalte)
        if kennung is not None and kennung not in gefunden:
            gefunden[kennung] = zeile.get(namensspalte) or ""
    return gefunden


def aufbauen(
    tabellen: dict[str, list[dict]], pfad: Path | None = None
) -> tuple[list[dict], dict]:
    """Die Zuordnung in Tabellenzeilen ueberfuehren und ihre Luecken melden.

    Die Zeilenform ist bewusst lang statt breit -- eine Zeile je Kennung. Damit ist
    der Join in DuckDB eine gewoehnliche Verknuepfung und braucht kein Entpacken
    einer Liste::

        SELECT k.name, round(sum(r.netto)) AS umsatz
        FROM '/daten/bexio_rechnungen.parquet' r
        JOIN '/daten/kundenschluessel.parquet' k
          ON k.system = 'bexio' AND k.fremd_id = r.kunden_id
        WHERE r.ist_umsatz
        GROUP BY 1

    Zurueck kommen die Zeilen und ein Befund: verworfene Kennungen, unbestaetigte
    Eintraege, offene Fragen und je System die nicht zugeordneten Kundschaften.
    """
    inhalt = _datei_lesen(pfad)
    kundschaften = inhalt.get("kundschaften") or []

    vorhanden = {s: _vorhandene_kennungen(s, tabellen) for s in SYSTEME}
    namen = {s: _namen(s, tabellen) for s in SYSTEME}

    zeilen: list[dict] = []
    verworfen: list[str] = []
    unbestaetigt: list[str] = []
    zugeordnet: dict[str, set] = {s: set() for s in SYSTEME}

    for eintrag in kundschaften:
        schluessel = eintrag.get("schluessel")
        anzeigename = eintrag.get("name")
        if not schluessel or not anzeigename:
            verworfen.append(f"Eintrag ohne Schluessel oder Name: {eintrag!r:.80}")
            continue
        bestaetigt = bool(eintrag.get("bestaetigt"))
        # Maschinell ergaenzte Kennungen an einem sonst bestaetigten Eintrag. Ohne
        # diese Liste haette eine Ergaenzung nur zwei haessliche Moeglichkeiten:
        # still als bestaetigt gelten (ein Urteil, das niemand gefaellt hat) oder
        # den ganzen Eintrag entwerten (ein Urteil, das jemand gefaellt hat, wird
        # zurueckgenommen). Beides waere falsch.
        offene_kennungen = {str(k) for k in (eintrag.get("vorgeschlagen") or [])}
        if not bestaetigt or offene_kennungen:
            unbestaetigt.append(schluessel)

        for system in SYSTEME:
            for kennung in eintrag.get(system) or []:
                if kennung not in vorhanden[system]:
                    # Eine tote Verknuepfung ist schlimmer als eine fehlende: sie
                    # sieht aus wie eine Zuordnung und traegt keine.
                    verworfen.append(f"{schluessel}: {system} {kennung} gibt es nicht")
                    continue
                zugeordnet[system].add(kennung)
                zeilen.append({
                    "schluessel": schluessel,
                    "name": anzeigename,
                    "system": system,
                    "fremd_id": kennung,
                    "fremd_name": namen[system].get(kennung, ""),
                    "bestaetigt": bestaetigt and f"{system}:{kennung}" not in offene_kennungen,
                })

    befund: dict = {
        "eintraege": len(kundschaften),
        "zeilen": len(zeilen),
        "unbestaetigt": len(unbestaetigt),
    }
    if verworfen:
        befund["verworfen"] = verworfen
        logger.warning(
            "Kundenschluessel: %d Kennung(en) verworfen, weil im Bestand nicht "
            "vorhanden: %s", len(verworfen), "; ".join(verworfen[:5]),
        )

    # Was fehlt, wird benannt -- sonst ist die Luecke still. Gemeldet wird nur, wer
    # laut RELEVANZ wirtschaftlich zaehlt.
    fehlend: dict[str, list[str]] = {}
    for system in SYSTEME:
        offen = sorted(
            f"{kennung} ({namen[system].get(kennung) or 'ohne Namen'})"
            for kennung in _relevante_kennungen(system, tabellen) - zugeordnet[system]
        )
        if offen:
            fehlend[system] = offen
    if fehlend:
        befund["nicht_zugeordnet"] = {s: len(v) for s, v in fehlend.items()}
        befund["ohne_schluessel"] = fehlend

    # Mit dem Wortlaut, nicht bloss als Etikett: der Agent soll die Frage stellen
    # koennen, wenn sie zur Sprache kommt. Stuende hier nur «pipedrive 652 (Kanton
    # Bern)», bliebe die Frage im Katalog liegen und erreichte nie jemanden -- und
    # eine Frage, die niemand hoert, ist so gut wie keine.
    offene_fragen = inhalt.get("offen") or []
    if offene_fragen:
        befund["offene_fragen"] = [
            {
                "system": f.get("system"),
                "kennung": f.get("kennung"),
                "name": f.get("name"),
                "frage": f.get("frage"),
            }
            for f in offene_fragen
        ]
        befund["so_wird_geantwortet"] = (
            "Kommt eine dieser Kundschaften zur Sprache, die Frage stellen und die "
            "Antwort mit dem Werkzeug 'kundschaft_zuordnen' eintragen. Nicht selbst "
            "entscheiden -- eine falsche Zuordnung bleibt unbemerkt."
        )

    return zeilen, befund


# ── Schreiben ────────────────────────────────────────────────────────

def _als_text(wert: str) -> str:
    """Ein Feldwert als YAML-Skalar -- in Anfuehrungszeichen nur, wo noetig."""
    heikel = any(z in wert for z in ":#\n'\"") or wert.strip() != wert or not wert
    return f"'{wert.replace(chr(39), chr(39) * 2)}'" if heikel else wert


def _ausgeben(inhalt: dict) -> str:
    """Die Datei aus der geladenen Struktur neu schreiben.

    Ein eigener Ausgeber statt ``yaml.safe_dump``, aus zwei Gruenden. Erstens
    haelt der Kopfkommentar die Begruendung fest, warum es diese Datei gibt --
    ``safe_dump`` wirft Kommentare weg, und die Begruendung waere nach der ersten
    maschinellen Ergaenzung verloren. Zweitens schreibt ``safe_dump``
    Kennungslisten als Bloecke; damit wuerde jede kleine Aenderung die ganze Datei
    umformen und der Diff waere nicht mehr zu lesen -- ausgerechnet bei der Datei,
    deren einziger Zweck die Nachvollziehbarkeit ist.
    """
    zeilen = [KOPF.strip(), "", f"version: {inhalt.get('version', 1)}",
              f"stand: {inhalt.get('stand')}", "", "kundschaften:"]

    for eintrag in inhalt.get("kundschaften") or []:
        zeilen.append(f"  - schluessel: {eintrag['schluessel']}")
        zeilen.append(f"    name: {_als_text(str(eintrag.get('name') or ''))}")
        for system in SYSTEME:
            kennungen = eintrag.get(system)
            if kennungen:
                zeilen.append(f"    {system}: [{', '.join(str(k) for k in kennungen)}]")
        zeilen.append(f"    bestaetigt: {str(bool(eintrag.get('bestaetigt'))).lower()}")
        vorgeschlagen = eintrag.get("vorgeschlagen") or []
        if vorgeschlagen:
            zeilen.append(f"    vorgeschlagen: [{', '.join(_als_text(str(v)) for v in vorgeschlagen)}]")
        hinweis = (eintrag.get("hinweis") or "").strip()
        if hinweis:
            zeilen.append("    hinweis: >-")
            zeilen.extend(_umbrechen(hinweis, "      "))
        zeilen.append("")

    allein = inhalt.get("ohne_gegenstueck") or []
    if allein:
        zeilen.append("# Kommt nur in einem System vor und braucht deshalb keinen Schluessel.")
        zeilen.append("# Festgehalten, damit der Abgleich das nicht stuendlich neu beurteilt.")
        zeilen.append("ohne_gegenstueck:")
        zeilen.extend(f"  - {_als_text(str(e))}" for e in allein)
        zeilen.append("")

    offen = inhalt.get("offen") or []
    zeilen.append("# Was sich nicht eindeutig zuordnen laesst. Wird nicht geraten --")
    zeilen.append("# hier steht die Frage, bis ein Mensch sie beantwortet.")
    zeilen.append("offen:" if offen else "offen: []")
    for frage in offen:
        zeilen.append(f"  - system: {frage.get('system')}")
        zeilen.append(f"    kennung: {frage.get('kennung')}")
        zeilen.append(f"    name: {_als_text(str(frage.get('name') or ''))}")
        zeilen.append("    frage: >-")
        zeilen.extend(_umbrechen(str(frage.get("frage") or "").strip(), "      "))
        zeilen.append("")

    return "\n".join(zeilen).rstrip() + "\n"


def _umbrechen(text: str, einzug: str, breite: int = 80) -> list[str]:
    """Fliesstext auf Zeilenbreite umbrechen, damit der Diff zeilenweise bleibt."""
    import textwrap

    return [einzug + z for z in textwrap.wrap(" ".join(text.split()), breite - len(einzug))] or [einzug + "-"]


def _schreiben(inhalt: dict, pfad: Path | None = None) -> None:
    """Die Datei ersetzen -- atomar, damit ein Leser nie eine halbe Datei sieht."""
    import os
    from datetime import date

    ziel = pfad or _datei_finden()
    inhalt["stand"] = date.today().isoformat()
    vorlaeufig = ziel.with_suffix(".yaml.neu")
    vorlaeufig.write_text(_ausgeben(inhalt), encoding="utf-8")
    os.replace(vorlaeufig, ziel)


def spalten_bedarf() -> dict[str, set[str]]:
    """Welche Spalten je Tabelle noetig sind, um die Zuordnung zu pruefen."""
    gebraucht: dict[str, set[str]] = {}
    for tabelle, kennung, anzeige in SYSTEME.values():
        gebraucht.setdefault(tabelle, set()).update((kennung, anzeige))
    for tabelle, kennung, bedingung in RELEVANZ.values():
        spalten = gebraucht.setdefault(tabelle, set())
        spalten.add(kennung)
        if bedingung is not None:
            spalten.add(bedingung[0])
    return gebraucht


def tabellen_laden() -> dict[str, list[dict]]:
    """Den aktuellen Bestand aus dem Datenraum lesen.

    Spaeter Import, weil ``datenraum`` dieses Modul benutzt -- auf Modulebene
    entstuende ein Ringschluss.
    """
    from app.services.datenraum import _zeilen_lesen

    return {
        name: _zeilen_lesen(name, tuple(sorted(spalten)))
        for name, spalten in spalten_bedarf().items()
    }


def zuordnen(
    schluessel: str,
    system: str,
    kennung: Any,
    *,
    name: str | None = None,
    hinweis: str | None = None,
    tabellen: dict[str, list[dict]] | None = None,
    pfad: Path | None = None,
) -> dict:
    """Eine menschliche Entscheidung festschreiben.

    Aufgerufen vom Werkzeug ``kundschaft_zuordnen``: die Antwort faellt im
    Gespraech («WA-AUE gehoert zur Wyss Academy»), nicht im Editor. Was hier
    ankommt, gilt als geprueft -- ``bestaetigt: true``, und die Kennung faellt aus
    ``vorgeschlagen`` heraus.

    Geprueft wird trotzdem, und zwar dasselbe wie beim Aufbau: existiert die
    Kennung im Bestand? Ein Vertipper darf keine tote Verknuepfung erzeugen, die
    hinterher wie eine Zuordnung aussieht.
    """
    if system not in SYSTEME:
        return {"ok": False, "grund": f"Unbekanntes System '{system}'. Erlaubt: {', '.join(SYSTEME)}"}

    tabellen = tabellen if tabellen is not None else tabellen_laden()
    vorhanden = _vorhandene_kennungen(system, tabellen)
    passend = [k for k in vorhanden if str(k) == str(kennung)]
    if not passend:
        return {"ok": False, "grund": (
            f"{system} {kennung} kommt im Datenraum nicht vor. Nicht eingetragen -- "
            "eine Kennung, die es nicht gibt, ergaebe eine Verknuepfung ueber null Zeilen."
        )}
    kennung = passend[0]

    inhalt = _datei_lesen(pfad)
    inhalt.setdefault("kundschaften", [])
    for anderer in inhalt["kundschaften"]:
        if anderer.get("schluessel") != schluessel and kennung in (anderer.get(system) or []):
            return {"ok": False, "grund": (
                f"{system} {kennung} gehoert bereits zu '{anderer.get('schluessel')}'. "
                "Eine Kennung kann nur einer Kundschaft gehoeren -- sonst zaehlt jede "
                "Summe sie doppelt."
            )}

    eintrag = next((e for e in inhalt["kundschaften"] if e.get("schluessel") == schluessel), None)
    if eintrag is None:
        namen = _namen(system, tabellen)
        eintrag = {"schluessel": schluessel, "name": name or namen.get(kennung) or schluessel}
        inhalt["kundschaften"].append(eintrag)
        inhalt["kundschaften"].sort(key=lambda e: e.get("schluessel") or "")
    elif name:
        eintrag["name"] = name

    eintrag.setdefault(system, [])
    if kennung not in eintrag[system]:
        eintrag[system].append(kennung)
    eintrag["bestaetigt"] = True
    eintrag["vorgeschlagen"] = [
        v for v in (eintrag.get("vorgeschlagen") or []) if v != f"{system}:{kennung}"
    ]
    if hinweis:
        eintrag["hinweis"] = hinweis

    # Die Frage ist beantwortet und verschwindet aus der Liste.
    inhalt["offen"] = [
        f for f in (inhalt.get("offen") or [])
        if not (f.get("system") == system and str(f.get("kennung")) == str(kennung))
    ]

    _schreiben(inhalt, pfad)
    systeme = [s for s in SYSTEME if eintrag.get(s)]
    return {
        "ok": True,
        "schluessel": schluessel,
        "name": eintrag["name"],
        "systeme": {s: eintrag[s] for s in systeme},
        "hinweis": (
            "Eingetragen und bestaetigt. Die Tabelle 'kundenschluessel' wird beim "
            "naechsten Abgleich neu geschrieben."
            if len(systeme) > 1 else
            "Eingetragen. Solange nur ein System zugeordnet ist, verbindet der "
            "Schluessel noch nichts -- er wirkt erst mit einer zweiten Kennung."
        ),
    }


def frage_notieren(system: str, kennung: Any, name: str, frage: str, pfad: Path | None = None) -> bool:
    """Eine offene Frage festhalten. Doppelte werden nicht erneut gestellt."""
    inhalt = _datei_lesen(pfad)
    offen = inhalt.setdefault("offen", [])
    if any(f.get("system") == system and str(f.get("kennung")) == str(kennung) for f in offen):
        return False
    offen.append({"system": system, "kennung": kennung, "name": name, "frage": frage})
    _schreiben(inhalt, pfad)
    return True


# ── Vorschlagen ──────────────────────────────────────────────────────

AUFTRAG = """Du ordnest Kundschaften zwischen drei Geschäftssystemen einander zu.

Dieselbe Organisation heisst in Bexio (Buchhaltung), Toggl (Zeiterfassung) und
Pipedrive (CRM) oft verschieden: die Buchhaltung führt den vollen amtlichen Namen,
die Zeiterfassung ein Kürzel, das CRM etwas dazwischen. Beispiel: «Bau- und
Verkehrsdirektion des Kantons Bern (BVD) Amt für Grundstücke und Gebäude» in
Bexio ist dasselbe wie «AGG» in Toggl.

Vor dir steht eine Kundschaft ohne Zuordnung. Entscheide, ob sie zu einer
bereits bekannten Kundschaft gehört, oder zu einem der aufgeführten Kandidaten
aus den anderen Systemen.

Antworte AUSSCHLIESSLICH als JSON:
  {"schluessel": "<bekannter Schlüssel>", "sicher": true}
  {"kandidaten": [{"system": "bexio", "kennung": 123}], "schluessel": "<neuer kurzer Schlüssel, klein, ohne Umlaute>", "name": "<Anzeigename>", "sicher": true}
  {"sicher": false, "frage": "<eine konkrete Frage an den Menschen>"}

Regeln:
- Rate NICHT. Bist du nicht sicher, antworte mit sicher=false und einer Frage.
  Eine falsche Zuordnung ist schlimmer als gar keine, weil sie unbemerkt bleibt.
- Ein Kürzel, das nur zufällig in einem längeren Namen vorkommt, ist kein Treffer.
- Verwende nur Kennungen aus den vorgelegten Listen. Erfinde keine.
- Gehört die Kundschaft offensichtlich zu niemandem sonst (sie kommt nur in einem
  System vor), antworte {"sicher": true, "allein": true}."""


async def _fragen(auftrag: str, frage: str) -> dict:
    """Das lokale Modell einmal fragen. Direkter Ollama-Aufruf, kein Agent."""
    import json

    import httpx

    from app.config import get_settings

    cfg = get_settings()
    try:
        async with httpx.AsyncClient(timeout=120) as klient:
            antwort = await klient.post(
                f"{cfg.ollama_base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": "Bearer ollama"},
                json={
                    "model": cfg.triage_model.removeprefix("ollama/"),
                    "messages": [
                        {"role": "system", "content": auftrag},
                        {"role": "user", "content": frage},
                    ],
                    "temperature": 0,
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
            )
            antwort.raise_for_status()
            inhalt = (((antwort.json().get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        return json.loads(inhalt)
    except Exception as exc:  # noqa: BLE001 -- ein Vorschlag darf den Abgleich nie reissen
        logger.warning("Kundenschluessel: Vorschlag nicht erhalten: %s", exc)
        return {}


def _luecken(tabellen: dict[str, list[dict]], inhalt: dict) -> dict[str, list[tuple[Any, str]]]:
    """Wer relevant ist, aber weder zugeordnet noch schon einmal beurteilt.

    «Schon beurteilt» umfasst beides: eine gestellte Frage und ein bereits
    festgestelltes «kommt nur in einem System vor». Ohne das zweite prüfte der
    Abgleich dieselben acht Kundschaften stündlich neu -- acht Modellaufrufe pro
    Stunde für ein Ergebnis, das feststeht.
    """
    schon_gefragt = {
        (f.get("system"), str(f.get("kennung"))) for f in (inhalt.get("offen") or [])
    }
    schon_gefragt |= {
        (str(e).split(":", 1)[0], str(e).split(":", 1)[1])
        for e in (inhalt.get("ohne_gegenstueck") or []) if ":" in str(e)
    }
    zugeordnet = {s: set() for s in SYSTEME}
    for eintrag in inhalt.get("kundschaften") or []:
        for system in SYSTEME:
            zugeordnet[system].update(eintrag.get(system) or [])

    gefunden: dict[str, list[tuple[Any, str]]] = {}
    for system in SYSTEME:
        namen = _namen(system, tabellen)
        offen = [
            (k, namen.get(k) or "")
            for k in _relevante_kennungen(system, tabellen) - zugeordnet[system]
            if (system, str(k)) not in schon_gefragt
        ]
        if offen:
            gefunden[system] = sorted(offen, key=lambda p: str(p[1]))
    return gefunden


async def vorschlagen(
    tabellen: dict[str, list[dict]] | None = None,
    pfad: Path | None = None,
    grenze: int = 12,
) -> dict:
    """Fuer nicht zugeordnete Kundschaften einen Gegenpart vorschlagen.

    Laeuft nach dem Abgleich und ist der Grund, warum niemand die Datei von Hand
    pflegen muss. Der Ablauf ist bewusst der aus der Architekturregel «einmalig
    erzeugen, Ausgabe versionieren»: das Modell schlaegt vor, **jede** Kennung
    wird gegen den Bestand geprueft, das Ergebnis landet als versionierter Text
    mit Pruefkennzeichen, und Unsicherheit ist ein zulaessiges Ergebnis.

    Was das Modell nicht entscheiden kann, wird zur Frage -- und nur die erreicht
    den Menschen. Bestaetigte Kennungen werden nie angeruehrt.
    """
    tabellen = tabellen if tabellen is not None else tabellen_laden()
    inhalt = _datei_lesen(pfad)
    luecken = _luecken(tabellen, inhalt)
    if not luecken:
        return {"geprueft": 0, "ergaenzt": 0, "gefragt": 0}

    bekannte = {
        e["schluessel"]: e.get("name") or e["schluessel"]
        for e in (inhalt.get("kundschaften") or []) if e.get("schluessel")
    }
    ergaenzt: list[str] = []
    gefragt: list[str] = []
    geprueft = 0

    for system, offene in luecken.items():
        for kennung, name in offene[:grenze]:
            geprueft += 1
            andere = "\n\n".join(
                f"Kandidaten aus {s} (Kennung — Name):\n" + "\n".join(
                    f"  {k} — {n}" for k, n in sorted(_namen(s, tabellen).items(), key=lambda p: str(p[1]))
                    if k in _relevante_kennungen(s, tabellen)
                )
                for s in SYSTEME if s != system
            )
            bekannt = "\n".join(f"  {s} — {n}" for s, n in sorted(bekannte.items()))
            vorschlag = await _fragen(AUFTRAG, (
                f"Nicht zugeordnet: System «{system}», Kennung {kennung}, Name «{name}».\n\n"
                f"Bereits bekannte Kundschaften (Schlüssel — Name):\n{bekannt}\n\n{andere}"
            ))

            if not vorschlag:
                continue
            if vorschlag.get("allein"):
                # Festhalten statt nur überspringen, sonst wird dieselbe Kundschaft
                # bei jedem Abgleich erneut beurteilt.
                inhalt.setdefault("ohne_gegenstueck", [])
                marke = f"{system}:{kennung}"
                if marke not in inhalt["ohne_gegenstueck"]:
                    inhalt["ohne_gegenstueck"] = sorted({*inhalt["ohne_gegenstueck"], marke})
                    _schreiben(inhalt, pfad)
                continue
            if not vorschlag.get("sicher"):
                frage = (vorschlag.get("frage") or "").strip()
                if frage and frage_notieren(system, kennung, name, frage, pfad):
                    gefragt.append(f"{system} {kennung} ({name})")
                    inhalt = _datei_lesen(pfad)
                continue

            # Angenommen wird nur, was sich nachweisen laesst. Jede Kennung des
            # Vorschlags -- auch die eigene -- geht durch dieselbe Pruefung wie eine
            # menschliche Eingabe; der einzige Unterschied ist das Pruefkennzeichen.
            schluessel = (vorschlag.get("schluessel") or "").strip().lower()
            if not schluessel:
                continue
            paare = [(system, kennung)] + [
                (p.get("system"), p.get("kennung"))
                for p in (vorschlag.get("kandidaten") or [])
                if p.get("system") in SYSTEME
            ]
            # Vor der ersten Schreibung merken: ``zuordnen`` setzt bestaetigt=true,
            # danach waere der Vorzustand nicht mehr feststellbar. Ein bestaetigter
            # Eintrag, der nur ergaenzt wird, bleibt bestaetigt -- die Ergaenzung
            # allein steht unter ``vorgeschlagen``.
            vorher = next(
                (e for e in (inhalt.get("kundschaften") or []) if e.get("schluessel") == schluessel),
                None,
            )
            war_bestaetigt = bool(vorher and vorher.get("bestaetigt"))

            angenommen = []
            for ziel_system, ziel_kennung in paare:
                ergebnis = zuordnen(
                    schluessel, ziel_system, ziel_kennung,
                    name=vorschlag.get("name") or name, tabellen=tabellen, pfad=pfad,
                )
                if ergebnis.get("ok"):
                    angenommen.append(f"{ziel_system}:{ergebnis['systeme'][ziel_system][-1]}")
                else:
                    logger.info("Kundenschluessel: Vorschlag verworfen -- %s", ergebnis.get("grund"))

            inhalt = _datei_lesen(pfad)
            if angenommen:
                # Der Eintrag ist gerade auf bestaetigt=true gelaufen, weil
                # ``zuordnen`` fuer menschliche Entscheidungen gebaut ist. Hier war
                # es aber eine Maschine -- also zurueck auf unbestaetigt.
                eintrag = next(e for e in inhalt["kundschaften"] if e["schluessel"] == schluessel)
                eintrag["vorgeschlagen"] = sorted(set(eintrag.get("vorgeschlagen") or []) | set(angenommen))
                eintrag["bestaetigt"] = war_bestaetigt
                _schreiben(inhalt, pfad)
                bekannte[schluessel] = eintrag.get("name") or schluessel
                ergaenzt.append(f"{schluessel}: {', '.join(angenommen)}")

    befund = {"geprueft": geprueft, "ergaenzt": len(ergaenzt), "gefragt": len(gefragt)}
    if ergaenzt:
        befund["neue_zuordnungen"] = ergaenzt
    if gefragt:
        befund["neue_fragen"] = gefragt
    logger.info("Kundenschluessel: %s", befund)
    return befund
