"""Ergänzt ``docs/kreditorenlieferanten.yaml`` um Felder, die der Erzeuger nicht kennt.

Nach jedem Neulauf von ``kreditoren_stammdaten_vorschlagen.py`` erneut
ausführen: jener bewahrt nur bestätigte Einträge und verwirft den Rest.

``schreibweisen``: die Anfänge, mit denen eine Buchung dieses Lieferanten im
Journal steht. Sie standen bis zum 25.09.2026 nur in ``ZUORDNUNG`` des
Erzeugers; die Normprüfung braucht sie aber zur Laufzeit, um «schon gebucht in
dieser Periode» zu erkennen. Übernommen wird wörtlich, ohne Zutat.

``leistung``, siehe unten.

Schicht 2 nach ``.cursor/rules/llm-oder-code.mdc``. Die Leistung steht heute
schon in den Archivnamen, nach der Konvention ``{Ordner} [{Leistung}]
{TT.MM.JJJJ}[ KK].pdf``. Sie abzuschneiden ist Code; zu entscheiden, ob das
Abgeschnittene eine Leistung ist, ist es nicht. Am 25.09.2026 gemessen ergab
eine Mehrheitsregel bei Quimbaya «AG», bei DPD «Schweiz AG» und bei der
Steuerverwaltung «Steuerverwaltung» -- Namensteile, keine Leistungen. Die
Zuordnung unten ist deshalb einmal gesichtet und hier eingefroren; das Skript
misst nur den Beleg dafür und schreibt ihn als Kommentar daneben.

Nicht eingetragen, mit Absicht -- dort hängt die Leistung an der Rechnung:

* Domains (Hosttech, Metanet, Hostpoint, Cloudflare, Namecheap, Hostinger),
* Einzelkäufe (Apple, HBADA, Gasser Bertschy, Handelsregisteramt),
* Lieferanten mit mehreren Leistungen (Microsoft: Copilot monatlich, M365
  jährlich; Mobiliar: je Versicherung; Napkin, Asga, VZ),
* Dateinamen ohne Leistung (Ausgleichskasse, ESTV, Protekta, Kanzlei Berger).

Eingetragen wird nur, wo das Feld fehlt. Aufruf::

    .venv/bin/python scripts/kreditoren_deklaration_ergaenzen.py [--schreiben]
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import duckdb

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "src/backend"))

from app.services import kreditorenlieferanten as decl  # noqa: E402
from app.services.kreditorennorm import leistung_normieren  # noqa: E402

DATENRAUM = Path.home() / ".local/share/taskpilot/datenraum"
SEIT = "2024-01-01"

LEISTUNG: dict[str, str] = {
    "1password": "Jahresabo",
    "aioseo": "Jahresabo",
    "anthropic": "Claude Pro Monatsabo",
    "bazg": "E-Vignette",
    "bexio": "Jahresabo",
    "calendly_llc": "Jahresabo",
    "cursor": "Usage",
    "digital_impact_network": "DIN Mitgliedschaft",
    "dropbox": "Jahresabo",
    "elementor": "Jahresabo",
    "euromaster": "Radwechsel",
    "figma": "Monatsabo",
    "freepik": "Jahresabo",
    "getabstract": "Jahresabo",
    "linkedin": "Premium Monatsabo",
    "meisterlabs": "MeisterTask Jahresabo",
    "miro": "Jahresabo",
    "neulandpro": "Monatsabo",
    "openai": "Monatsabo",
    "perplexity": "Jahresabo",
    "pipedrive": "Jahresabo",
    "rapidapi": "Monatsabo",
    "render": "Hosting",
    "salt": "Monatsabo",
    "solar_communications": "Server Hosting Jahresabo",
    "spusu": "Monatsabo",
    "tesla": "Connectivity",
    "toggl": "Track Jahresabo",
    "uizard": "Monatsabo",
    "wispr_flow": "Jahresabo",
    "wordfence": "Jahresabo",
    "zoom": "Jahresabo",
}

# Wo das Archiv keinen Beleg trägt, sondern ein Mensch entschieden hat.
ENTSCHIEDEN: dict[str, str] = {
    "rapidapi": "entschieden 25.09.2026 — im Archiv nur 2 von 8 Namen mit Leistung",
}

# Der Erzeuger bewahrt nur bestätigte Einträge. Ohne diese Zeile nähme ein
# Neulauf Cursor die Sammelbuchung weg, und die Rechnungen würden einzeln buchbar.
BUCHUNG: dict[str, str] = {"cursor": "sammelbeleg"}

_DATUM = re.compile(r"\s*\d{1,2}\.\d{2}\.\d{4}.*$")


def _leistung_aus(ordner: str, datei: str) -> str | None:
    stamm = re.sub(r"\.pdf$", "", datei, flags=re.IGNORECASE)
    if not _DATUM.search(stamm):
        return None
    stamm = _DATUM.sub("", stamm).strip()
    if stamm.lower().startswith(ordner.lower()):
        stamm = stamm[len(ordner):].strip()
    return leistung_normieren(stamm)


def belege_messen(bestand: decl.Bestand) -> dict[str, Counter]:
    pfad = DATENRAUM / "invoiceinsight_rechnungen.parquet"
    zeilen = duckdb.sql(
        f"select lieferant_schluessel, beleg_datei from '{pfad}' "
        f"where lieferant_schluessel is not null and beleg_ordner not like '_OPEN%' "
        f"and datum >= '{SEIT}'"
    ).fetchall()
    je: dict[str, Counter] = {}
    for schluessel, datei in zeilen:
        lieferant = bestand.lieferanten.get(schluessel)
        if lieferant:
            je.setdefault(schluessel, Counter())[_leistung_aus(lieferant.ordner, datei)] += 1
    return je


def schreibweisen_aus_erzeuger(bestand: decl.Bestand) -> dict[str, list[str]]:
    """``ZUORDNUNG`` des Erzeugers, von Ordner auf Schlüssel umgestellt."""
    sys.path.insert(0, str(WURZEL / "scripts"))
    from kreditoren_stammdaten_vorschlagen import ZUORDNUNG

    werte: dict[str, list[str]] = {}
    for ordner, schreibweisen in ZUORDNUNG.items():
        lieferant = bestand.nach_ordner(ordner)
        if lieferant is None:
            sys.exit(f"Ordner {ordner!r} aus ZUORDNUNG hat keinen Lieferanten")
        if schreibweisen:
            werte[lieferant.schluessel] = list(schreibweisen)
    return werte


def main() -> None:
    schreiben = "--schreiben" in sys.argv
    bestand = decl.laden()
    unbekannt = sorted(set(LEISTUNG) - set(bestand.lieferanten))
    if unbekannt:
        sys.exit(f"Nicht in der Deklaration: {unbekannt}")

    schreibweisen = schreibweisen_aus_erzeuger(bestand)
    print(f"schreibweisen für {len(schreibweisen)} Lieferanten aus ZUORDNUNG")
    if schreiben:
        eingetragen, uebergangen = decl.ergaenzen(
            schreibweisen, feld="schreibweisen",
            kommentare={s: "aus ZUORDNUNG des Erzeugers" for s in schreibweisen},
        )
        print(f"  {len(eingetragen)} eingetragen, {len(uebergangen)} standen schon")

    je = belege_messen(bestand)
    kommentare: dict[str, str] = {}
    for schluessel, leistung in LEISTUNG.items():
        zaehler = je.get(schluessel, Counter())
        gesamt = sum(zaehler.values())
        treffer = sum(n for form, n in zaehler.items() if form and leistung in form)
        kommentare[schluessel] = ENTSCHIEDEN.get(
            schluessel, f"{treffer} von {gesamt} Archivnamen seit {SEIT[:4]}"
        )
        print(f"{schluessel:24} {leistung:26} {kommentare[schluessel]}")

    if not schreiben:
        print("\nProbelauf — mit --schreiben wird eingetragen.")
        return
    eingetragen, uebergangen = decl.ergaenzen(LEISTUNG, feld="leistung", kommentare=kommentare)
    print(f"\n{len(eingetragen)} eingetragen, {len(uebergangen)} standen schon: {uebergangen}")
    eingetragen, _ = decl.ergaenzen(
        BUCHUNG, feld="buchung",
        kommentare={s: "entschieden: Monatsbeleg wird gebucht, die Rechnungen nur abgelegt" for s in BUCHUNG},
    )
    print(f"buchung: {len(eingetragen)} eingetragen")


if __name__ == "__main__":
    main()
