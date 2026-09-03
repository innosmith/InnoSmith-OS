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
        if not bestaetigt:
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
                    "bestaetigt": bestaetigt,
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

    offene_fragen = inhalt.get("offen") or []
    if offene_fragen:
        befund["offene_fragen"] = [
            f"{f.get('system')} {f.get('kennung')} ({f.get('name')})"
            for f in offene_fragen
        ]

    return zeilen, befund
