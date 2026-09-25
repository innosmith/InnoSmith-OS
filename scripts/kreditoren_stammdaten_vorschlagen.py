"""Erzeugt den Vorschlag fuer ``docs/kreditorenlieferanten.yaml``.

Einmaliges Hilfsmittel, Schicht 2 nach ``.cursor/rules/llm-oder-code.mdc``: die
Zuordnung von Archivordner zu Journalschreibweise ist eine **offene** Menge --
das Journal fuehrt ``Anthropic`` und ``Antrophic``, ``Euromaster`` und
``Eruomaster``, ``getabstract`` und ``get Abstract``. Keine endliche Regel
erzeugt das. Also wird sie einmal von Hand zugeordnet (``ZUORDNUNG`` unten),
maschinell gegen die Daten geprueft und als versionierte Datei eingefroren.

Was das Skript **misst** statt annimmt, je Lieferant:

* das Sollkonto samt Anzahl Buchungen als Beleg -- und ob die Historie
  einstimmig ist oder mehrere Konten zeigt,
* die Steuerbehandlung aus dem Gegenkonto 2203,
* den Zahlweg aus dem Habenkonto (2120 Karte, 2000 Lieferantenrechnung,
  1020/1021 direkt ab Bank),
* den Rhythmus aus der Zahl belegter Monate.

Was es **nicht** tut: einen Wert erfinden, wo die Daten schweigen. Dann bleibt
das Feld leer und der Eintrag unbestaetigt.

Zwei Waechter laufen mit:

1. Jede zugeordnete Schreibweise muss im Journal vorkommen. Eine tote Zuordnung
   sieht aus wie eine Zuordnung und traegt keine.
2. Jede Journalzeile, die keinem Lieferanten zufaellt, wird gezaehlt und die
   groessten benannt -- sonst waechst eine Luecke, die niemand sieht.

Aufruf::

    .venv/bin/python scripts/kreditoren_stammdaten_vorschlagen.py
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path

import duckdb

WURZEL = Path(__file__).resolve().parent.parent
DATENRAUM = Path.home() / ".local/share/taskpilot/datenraum"
ORDNERDATEI = WURZEL / "scripts/kreditoren_ordner.json"
ZIEL = WURZEL / "docs/kreditorenlieferanten.yaml"

# Habenkonten, die einen bezahlten Aufwand bezeichnen. Alles andere -- 1300 und
# 2300 (Rechnungsabgrenzung), 1100, 2200er (Privatanteile) -- ist Arbeit der
# Treuhaenderin am Jahresabschluss und keine Kreditorenrechnung.
ZAHLWEGE = {
    "2120": "karte",
    "2203": "karte",  # Bezugssteuer-Gegenzeile, gehoert zur Kartenbuchung
    "2000": "rechnung",
    "1020": "bank_direkt",
    "1021": "bank_direkt",
}

# Bexio stellt jeder Buchung aus einer Lieferantenrechnung diesen Text voran.
# Ohne ihn abzustreifen verfehlt der Abgleich den ganzen Rechnungsweg -- und
# meldet Mobiliar, VZ und die Kanzlei als unbekannt, obwohl sie gebucht sind.
PRAEFIX = "(Lieferantenrechnung erstellt)"

# Sollkonten, auf denen nie ein Kreditorenbeleg landet. Deklariert statt
# erschlossen, mit Grund -- sonst besteht die Luecken-Meldung zu neunzig Prozent
# aus Lohnbuchungen und wird nicht gelesen.
KEIN_BELEG: dict[str, str] = {
    "5000": "Loehne — entsteht in der Lohnbuchhaltung, keine Rechnung",
    "5001": "Kinderzulagen — Teil der Lohnabrechnung",
    "5008": "uebrige Lohnbestandteile",
    "5832": "Pauschalspesen — Teil der Lohnabrechnung",
    "5891": "Privatanteile Personalaufwand — Abschlussbuchung",
    "6900": "Bankspesen — die Bank belastet direkt, kein Lieferantenordner",
    "6945": "Kursdifferenzen und Rundungen — keine Rechnung",
}

# Wie weit zurueck eine fehlende Zuordnung noch eine Handlung bedeutet. Aeltere
# Luecken sind Geschichte: der Lieferant ist still, der Ordner bleibt bestehen,
# und eine Deklaration dafuer zu verlangen kostet Aufmerksamkeit ohne Ertrag.
LUECKE_AB = date(date.today().year - 2, 1, 1)

# Die Datenbank des InvoiceInsight-Moduls. Sie haelt zu 1'133 Belegen, was auf
# der Rechnung steht -- und beantwortet die Steuerfrage damit an der Quelle,
# statt sie aus dem Fehlen einer Buchung zu erschliessen.
MODULDB = (
    Path.home() / "dev/github/invoiceInsightDev/InnoSmithInvoices/data/invoices.db"
)

# Welches Feld die Steuerfrage entscheiden darf -- und welches nicht.
#
# ``vat_amount`` ist belegbar richtig: die Bezugssteuer-Lieferanten stehen
# ausnahmslos auf 0.00 (Cursor 0 von 68, Render 0 von 9, Toggl 0 von 8), und
# Microsoft zeigt 7.7 **und** 8.1, also den Satzwechsel per 01.01.2024. Ein
# gerechneter Vorgabewert koennte das nicht.
#
# ``supplier_uid`` ist es nicht: das Modell erfindet plausible Nummern, wenn es
# keine lesen kann -- ``CHE-123.456.789`` bei der Steuerverwaltung,
# ``CHE-100.000.000`` bei der Mobiliar, und ``CHE-294.603.891`` bei sechs
# verschiedenen Lieferanten zugleich. Ein erfundener Wert sieht aus wie ein
# gelesener; deshalb traegt er hier keine Entscheidung. Fuer die Steuerfrage
# braucht es ihn ohnehin nicht -- ob MWST ausgewiesen ist, steht im Betrag.

# Ordnername -> Schreibweisen im Journal, mit denen eine Buchung beginnt.
# Von Hand zugeordnet, maschinell geprueft. Wer hier fehlt, hat im Journal seit
# 2019 keine eigene Buchung -- der Ordner bleibt trotzdem bestehen.
ZUORDNUNG: dict[str, list[str]] = {
    "1Password": ["1Password", "1 Password"],
    "AIOSEO": ["AIOSEO"],
    "Anthropic": ["Anthropic", "Antrophic"],
    "Apple": ["Apple"],
    "Asga": ["Asga"],
    "Ausgleichskasse": ["AHV akonto", "AHV Akonto", "Ausgleichskasse"],
    "Bexio": ["Bexio"],
    "Calendly LLC": ["Calendly"],
    "Cloudflare": ["Cloudflare"],
    "Cursor": ["Cursor"],
    "Decidim": ["Decidim"],
    "Digital Impact Network": ["Mitgliedschaft"],
    "Digitec": ["Digitec"],
    "DropBox": ["Dropbox", "DropBox"],
    "Elementor": ["Elementor"],
    "EuroMaster": ["Euromaster", "Eruomaster"],
    "Figma": ["Figma"],
    "Florian Salman UG": ["Florian Salman", "Florian SalmanUG"],
    "Freepik": ["Freepik", "freepikcompany"],
    "Galaxus": ["Galaxus"],
    "GoMo Salt": ["GoMo"],
    "Gasser Bertschy Elektro": ["Installation"],
    # «Google» allein ist ein Absender, kein Lieferant -- der Eintrag verteilt
    # nur (``aufteilen``). Die Dienste beginnen im Journal verschieden, und der
    # längste Anfang gewinnt: «Google, Workspace» vor «Google».
    "Google": [],
    "Google Cloud": ["Google Cloud", "Google, Cloud"],
    "Google Gemini": ["Google, Gemini"],
    "Google One": ["Google One", "Google AI"],
    "Google Workspace": ["Google Workspace", "Google, Workspace"],
    "Handelsregisteramt Kt. Bern": ["Handelsregister"],
    "Hostinger": ["Hostinger"],
    "Hostpoint": ["Hostpoint"],
    "Hosttech": ["hosttech", "Hosttec"],
    "Kanzlei Berger": ["Beratung"],
    "MeisterLabs": ["Meister Task", "MeisterTask"],
    "Metanet": ["Metanet", "Domain"],
    "Microsoft": ["Microsoft"],
    "Miro": ["Miro"],
    "Mobiliar": ["Fahrzeugversicherung", "Betriebsversicherung", "Unfallversicherung"],
    "Namecheap": ["Namecheap"],
    "Napkin": ["Napkin"],
    "Neulandpro": ["Neulandpro"],
    "OpenAI": ["OpenAI", "Open AI", "OpanAI", "ChatGPT"],
    "Phantombuster": ["PhantomBuster", "Phantom Buster", "Phantombuster"],
    "Orellfüssli": ["Orell", "Orellf"],
    "Perplexity": ["Perplexity"],
    "Pipedrive": ["Pipedrive"],
    "Post": ["Post"],
    "Protekta": ["Protekta"],
    "Quimbaya": ["Quimbaya"],
    "RapidAPI": ["Rapid API"],
    "Render": ["Render"],
    # Rüedu: im Journal unter keiner Schreibweise des Namens zu finden —
    # die sechs Lieferantenrechnungen tragen einen anderen Buchungstext.

    "Salt": ["Salt"],
    "Solar Communications": ["Solar Communications"],
    "Spusu": ["Spusu"],
    "Steuerverwaltung Kt. Bern": ["Kantons", "Direkte Bundessteuer"],
    "Strassenverkehrsamt": ["E Bike Vignette"],
    "T-R": ["Treuhand"],
    "Tesla": ["Tesla", "Tesal"],
    "Toggl": ["Toggl"],
    "Uizard": ["Uizard"],
    "VZ": ["VZ"],
    "VeloGfeller": ["veloGfeller"],
    "Wispr Flow": ["Wispr"],
    "Wordfence": ["Wordfence", "Worlfence"],
    "YouTube": ["YouTube", "Google YouTube", "Google, YouTube"],
    "Zapier": ["Zapier"],
    "Zoom": ["Zoom"],
    "getAbstract": ["getabstract", "get Abstract", "getAbstract"],
    "Spesen": [],  # Sammelordner fuer Quittungen, kein einzelner Lieferant
}

# Fragen, die aus dem Abgleich entstanden sind und die keine Maschine
# entscheiden darf. Sie stehen im Wortlaut in der Datei, weil ein Etikett wie
# «unklar» niemanden erreicht.
OFFEN: list[str] = [
    ">-\n      Rüedu hat sechs Lieferantenrechnungen in Bexio, aber unter keiner "
    "Schreibweise des Namens eine Journalbuchung. Unter welchem Text laufen sie?",
    ">-\n      Der Ordner «Spesen» sammelt Quittungen (Business-Lunch, "
    "gelegentlich Büromaterial) statt eines einzelnen Lieferanten. Er trägt "
    "deshalb keine Kontoerwartung; die Belegart entscheidet im Register.",
]

# Was Anthony am 21.09.2026 als Regel genannt hat. Steht hier, damit der
# Vorschlag sie mitschreibt statt sie aus der Historie zu erraten.
REGELN: dict[str, str] = {
    "Render": "in der Regel 4200 -- wird weiterverrechnet, nicht mehr selbst genutzt",
    "Digitec": "in der Regel 6571 Hardware fuer den eigenen Bedarf",
    "Hosttech": "Domains: 6512 fuer eigene, 4200 wenn fuer Kundschaft gekauft "
                "-- je Rechnung zu entscheiden",
    "Miro": "in der Regel 6570 Software; einmalig an Kundschaft weiterverrechnet",
    "Metanet": "wie Hosttech: 6512 eigen, 4200 weiterverrechnet",
}


def schluessel_aus(name: str) -> str:
    """ASCII-Kennung aus dem Ordnernamen. Bezeichner bleiben ohne Umlaute."""
    ersetzt = (
        name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    )
    roh = unicodedata.normalize("NFKD", ersetzt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", roh.lower()).strip("_")


def zeilen_holen(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Alle Aufwandbuchungen mit einem Zahlweg, der einen Beleg bedeutet."""
    platzhalter = ",".join(f"'{k}'" for k in ZAHLWEGE)
    return con.execute(
        f"""select beschreibung, soll_konto_nr, haben_konto_nr, datum
            from '{DATENRAUM}/bexio_journal.parquet'
            where ist_aufwand and haben_konto_nr in ({platzhalter})
            order by datum"""
    ).fetchall()


def rechnungen_lesen() -> list[tuple[str, date, bool | None, bool, str]]:
    """Was auf den Rechnungen steht: ``(Lieferant, Datum, MWST?, Ausland?, wofuer)``.

    Das dritte Feld hat **drei** Zustaende, und darin liegt der Unterschied
    zwischen einer Messung und einer Vermutung:

    - ``True``  -- Betrag gelesen und groesser null: MWST ausgewiesen
    - ``False`` -- Betrag gelesen und null: keine MWST ausgewiesen
    - ``None``  -- kein Betrag gelesen: die Rechnung sagt nichts

    Wer ``NULL`` und ``0`` zusammenwirft, verwandelt Schweigen in eine
    Aussage. Google Cloud hat drei Belege ohne gelesenen Betrag; daraus
    «keine MWST» zu folgern, erzeugte prompt eine Frage, die es nicht gibt.

    Die UID bleibt aussen vor -- das Modell erfindet sie, wenn es keine lesen
    kann, und fuer die Steuerfrage braucht es sie nicht.
    """
    if not MODULDB.exists():
        logger.warning("Moduldatenbank fehlt: %s — Steuerfrage bleibt offen", MODULDB)
        return []

    import sqlite3

    con = sqlite3.connect(f"file:{MODULDB}?mode=ro", uri=True)
    zeilen = con.execute(
        """select supplier_name, invoice_date, vat_amount, net_amount, is_foreign,
                  product_description
           from documents
           where supplier_name is not null and invoice_date is not null"""
    ).fetchall()
    con.close()

    gelesen: list[tuple[str, date, bool | None, bool, str]] = []
    for name, datum, mwst, netto, auslaendisch, wofuer in zeilen:
        try:
            tag = date.fromisoformat(str(datum)[:10])
        except ValueError:
            continue
        if mwst is None:
            # Fehlt der MWST-Betrag, entscheidet das **Netto**, ob die
            # Extraktion die Betragszeilen ueberhaupt aufgeschluesselt hat.
            # Der Bruttobetrag taugt dafuer nicht: die drei Google-Cloud-Belege
            # tragen nur ein Total (5.09 CHF), kein Netto und keine MWST. Als
            # «keine MWST ausgewiesen» gelesen brachen sie die Reihe von 37
            # Google-Rechnungen und erzeugten einen Wechsel, den es nicht gibt.
            steuer = None if netto is None else False
        else:
            steuer = float(mwst) > 0
        gelesen.append((name, tag, steuer, bool(auslaendisch), str(wofuer or "")))
    return gelesen


def _rhythmus(monate: set[str], von: date, bis: date) -> str:
    """Aus der Dichte belegter Monate, nicht aus der Zahl der Buchungen."""
    spanne = max((bis.year - von.year) * 12 + bis.month - von.month + 1, 1)
    if spanne < 4:
        return "unbekannt"
    anteil = len(monate) / spanne
    if anteil >= 0.7:
        return "monatlich"
    if anteil >= 0.2:
        return "unregelmaessig"
    return "jaehrlich"


def auswerten(zeilen: list[tuple]) -> tuple[dict, list[tuple], dict]:
    """Ordnet jede Zeile einem Ordner zu und verdichtet je Lieferant."""
    # Laengste Schreibweise zuerst, damit «Cursor Pro» nicht an «Cursor» faellt
    muster = sorted(
        ((s, ordner) for ordner, ss in ZUORDNUNG.items() for s in ss),
        key=lambda p: -len(p[0]),
    )
    befund: dict[str, dict] = {}
    getroffen: set[str] = set()
    offen: dict[str, int] = {}

    for beschreibung, soll, haben, tag in zeilen:
        text = (beschreibung or "").strip()
        if text.startswith(PRAEFIX):
            text = text[len(PRAEFIX):].strip()
        if soll in KEIN_BELEG:
            continue
        ordner = next((o for s, o in muster if text.startswith(s)), None)
        if ordner is None:
            if tag >= LUECKE_AB:
                stamm = re.split(r"[,;0-9]", text)[0].strip()[:40] or "(leer)"
                offen[stamm] = offen.get(stamm, 0) + 1
            continue
        getroffen.update(s for s, o in muster if o == ordner and text.startswith(s))
        e = befund.setdefault(
            ordner,
            {"konten": {}, "zahlwege": set(), "bezugssteuer": [],
             "monate": set(), "von": tag, "bis": tag, "n": 0},
        )
        if haben == "2203":
            e["bezugssteuer"].append(tag)
        else:
            e["konten"][soll] = e["konten"].get(soll, 0) + 1
            e["zahlwege"].add(ZAHLWEGE[haben])
            e["n"] += 1
        e["monate"].add(tag.strftime("%Y-%m"))
        e["von"], e["bis"] = min(e["von"], tag), max(e["bis"], tag)

    tot = [(s, o) for s, o in muster if s not in getroffen]
    return befund, tot, offen


def _muster() -> list[tuple[str, str]]:
    """Schreibweise -> Ordner, laengste zuerst.

    Die Laenge entscheidet, damit ``Cursor Pro`` nicht an ``Cursor`` faellt.
    """
    return sorted(
        ((s, ordner) for ordner, ss in ZUORDNUNG.items() for s in ss),
        key=lambda p: -len(p[0]),
    )


def steuer_je_ordner() -> tuple[
    dict[str, tuple[str, date, int, int]], list[tuple[str, str]]
]:
    """Entscheidet die Steuerbehandlung je Archivordner aus den Rechnungen.

    Gruppiert wird **nach Ordner, nicht nach Lieferantenname** -- das Modul
    fuehrt ``Cursor``, ``Anysphere, Inc.`` und ``Cursor (Anysphere, Inc.)``
    nebeneinander, und wer je Name entscheidet, teilt die Beweislage auf und
    kann den Widerspruch nicht mehr sehen.

    Vier Ergebnisse. Belege ohne gelesenen Betrag zaehlen bei keinem mit --
    Schweigen ist kein Beweis:

    - alle Rechnungen mit Schweizer MWST -> ``inland_mwst``
    - keine mit MWST, Sitz im Ausland -> ``bezugssteuer``
    - keine mit MWST, Sitz im Inland -> ``ohne_mwst`` (Art. 21 MWSTG)
    - **gemischt** -> keine Deklaration, sondern eine Frage im Wortlaut

    Die dritte Unterscheidung war noetig, weil sonst die Ausgleichskasse (0
    von 77) und VZ (0 von 17) als Widerspruch gemeldet wurden. Eine
    steuerbefreite inlaendische Stelle ist kein Widerspruch, und vier falsche
    Fragen in einer Liste von fuenfzehn machen die ganze Liste unglaubwuerdig.

    Der vierte Fall bleibt trotzdem der wichtigste. Freepik weist 2023 MWST
    aus und 2025 keine; ein Mittelwert stimmt in keinem der beiden Jahre. Ein
    solcher Wechsel ist eine Aenderung beim Lieferanten und braucht ein Datum,
    das nur ein Mensch kennt.
    """
    muster = _muster()
    je: dict[str, list[tuple[date, bool | None, bool, str]]] = {}
    for name, tag, mwst, auslaendisch, wofuer in rechnungen_lesen():
        ziel = next((o for s, o in muster if name.startswith(s)), None)
        if ziel:
            je.setdefault(ziel, []).append((tag, mwst, auslaendisch, wofuer))

    ergebnis: dict[str, tuple[str, date, int, int]] = {}
    fragen: list[tuple[str, str]] = []
    for ziel, alle in sorted(je.items()):
        eintraege = sorted(
            ((t, m, a, w) for t, m, a, w in alle if m is not None), key=lambda e: e[0]
        )
        if not eintraege:
            continue
        auslaendisch = any(a for _, _, a, _ in eintraege)

        # Der laufende Zustand: der laengste einheitliche Schwanz der nach
        # Datum sortierten Reihe. Gefragt ist die Erwartung an die **naechste**
        # Rechnung, und die beantwortet nicht das Mehrheitsverhaeltnis ueber
        # sieben Jahre, sondern was seit dem letzten Wechsel gilt. Salt zeigt
        # 38 von 40 mit MWST -- als Mehrheitsfrage gestellt war das ein
        # Widerspruch, als Zustandsfrage ist es keiner: die beiden Ausreisser
        # liegen 2022 und 2023, seither ausnahmslos MWST.
        zustand = eintraege[-1][1]
        schwanz = eintraege[len(eintraege) - _laenge(eintraege, zustand):]
        ab = schwanz[0][0]
        vorher = eintraege[: len(eintraege) - len(schwanz)]

        if len(schwanz) < 2 and vorher:
            # Ein einziger Beleg im neuen Zustand ist zu wenig fuer eine
            # Erwartung -- es kann ein Wechsel sein oder ein Lesefehler.
            fragen.append((ziel,
                f"{ziel}: die letzte Rechnung ({schwanz[0][0]}, "
                f"{schwanz[0][3] or 'ohne Beschreibung'}) weist "
                f"{'MWST aus' if zustand else 'keine MWST aus'}, die "
                f"{len(vorher)} davor "
                f"{'nicht' if zustand else 'schon'} — "
                + "; ".join(
                    f"{t} {w or 'ohne Beschreibung'}" for t, _, _, w in vorher
                )
                + f". Ein Wechsel, ein Lesefehler, oder hängt die Behandlung "
                f"an der Leistung statt am Lieferanten? Ein einzelner Beleg "
                f"trägt keine Erwartung — was im Eintrag steht, kommt aus dem "
                f"Journal, nicht aus den Rechnungen."
            ))
            continue

        art = (
            "inland_mwst"
            if zustand
            else ("bezugssteuer" if auslaendisch else "ohne_mwst")
        )
        ergebnis[ziel] = (art, ab, len(schwanz), len(eintraege))
        if vorher:
            # Der Wechsel wird nicht verschwiegen. Er steht als Frage, aber als
            # eine mit Vorschlag -- die Deklaration gilt schon, das Datum ist
            # zu bestaetigen.
            #
            # Gezaehlt wird, wie viele der aelteren Belege wirklich abweichen.
            # «davor N Rechnungen ohne» war falsch: ``vorher`` ist alles vor dem
            # Schwanz, nicht alles im Gegenzustand. Bei Google behauptete die
            # Frage 36 Rechnungen ohne MWST, wo es drei waren -- eine Zahl, die
            # der Leser in einer Minute widerlegt, und danach glaubt er der
            # ganzen Liste nicht mehr.
            anders = sum(1 for _, m, _, _ in vorher if m != zustand)
            fragen.append((ziel,
                f"{ziel}: seit {ab} durchgehend "
                f"{'mit' if zustand else 'ohne'} ausgewiesene MWST "
                f"({len(schwanz)} Rechnungen). Davor {anders} von {len(vorher)} "
                f"Rechnungen {'ohne' if zustand else 'mit'}, letzte am "
                f"{vorher[-1][0]}. Als «{art} ab {ab}» eingetragen — stimmt das "
                f"Datum, oder ist die ältere Reihe ein Lesefehler?"
            ))
    return ergebnis, fragen


def _laenge(eintraege: list[tuple[date, bool | None, bool, str]], zustand: bool) -> int:
    """Wie viele Belege am Ende der Reihe denselben Zustand haben."""
    n = 0
    for _, m, *_ in reversed(eintraege):
        if m != zustand:
            break
        n += 1
    return n


def bestand_lesen() -> tuple[dict[str, str], dict[str, str]]:
    """Was in der bestehenden Datei steht -- **jeder** Eintrag, wörtlich.

    Liefert die Textblöcke aller Einträge und die Schlüssel der beendeten
    Lieferanten.

    Ohne das war der Erzeuger ein Radiergummi: er schreibt die Datei ganz neu,
    und der zweite Lauf haette jedes ``bestaetigt: true`` und jede Korrektur
    weggeputzt -- lautlos, weil die Datei danach genauso gut aussieht wie
    vorher. Es gilt dieselbe Grenze wie beim Kundenschluessel: **eine Maschine
    darf hinzufuegen, nie aendern oder entfernen.**

    Bis zum 25.09.2026 galt das nur für bestätigte Einträge. Das reichte nicht:
    die Aufteilung von Google in fünf Dienste, die Leistungen und die
    Schreibweisen stehen an unbestätigten Einträgen und wären mit dem nächsten
    Lauf verschwunden. Seither wird nur erzeugt, was es noch nicht gibt.
    """
    if not ZIEL.exists():
        return {}, {}

    text = ZIEL.read_text(encoding="utf-8")
    bloecke: dict[str, str] = {}
    beendet: dict[str, str] = {}
    for roh in text.split("\n  - schluessel: ")[1:]:
        # Die erste Zeile ist der Schluessel selbst und steht ohne
        # Einrueckung; erst ab der zweiten gilt «eingerueckt heisst dazu».
        # Beim letzten Eintrag haengt sonst der ganze Fussteil mit dran.
        kopf, *rest = roh.split("\n")
        schluessel = kopf.strip()
        zeilen = [kopf]
        for zeile in rest:
            if zeile and not zeile.startswith(" "):
                break
            zeilen.append(zeile)
        bloecke[schluessel] = "\n".join(zeilen).rstrip()
        for zeile in zeilen:
            if zeile.strip().startswith("beendet:"):
                # Wörtlich merken, nicht nur die Tatsache: die Zeile trägt das
                # Datum und den Grund, und beides geht sonst beim Schreiben
                # verloren -- die Angabe wäre nach einem Lauf wieder weg.
                beendet[schluessel] = zeile.strip()
    return bloecke, beendet


def yaml_schreiben(
    ordner: dict, befund: dict, tot: list, offen: dict,
    aus_rechnungen: dict[str, tuple[str, date, int, int]] | None = None,
    fragen: list[str] | None = None,
    bestaetigt: dict[str, str] | None = None,
    beendet: dict[str, str] | None = None,
) -> str:
    bestand = bestaetigt or {}
    beendet = beendet or {}
    kopf = [KOPF, f"version: 1\nstand: {date.today().isoformat()}\n", "lieferanten:"]
    gefragt: set[str] = set()
    # Je Eintrag eine eigene Zeilenliste, sortiert nach Ordner: so stehen
    # bewahrte und neue Einträge in einer Reihenfolge, statt dass die
    # bewahrten am Ende kleben.
    eintraege: list[tuple[str, list[str]]] = []
    erzeugt: set[str] = set()

    for name in sorted(ordner, key=str.lower):
        angaben = ordner[name]
        if angaben.get("sonderordner"):
            continue
        schluessel = schluessel_aus(name)
        erzeugt.add(schluessel)
        aus: list[str] = []
        eintraege.append((name.lower(), aus))
        if schluessel in bestand:
            aus.append(f"\n  - schluessel: {bestand[schluessel]}")
            # Bestätigt heisst beantwortet -- dazu wird nicht mehr gefragt.
            if (
                "bestaetigt: true" not in bestand[schluessel]
                and schluessel not in beendet
                and any(j in ("2025", "2026") for j in angaben["jahre"])
            ):
                gefragt.add(schluessel)
            continue
        e = befund.get(name)
        jahre = [j for j in angaben["jahre"] if j.isdigit()]
        neben = [j for j in angaben["jahre"] if not j.isdigit()]
        # «Aktiv» heisst: von hier kommen noch Rechnungen. Das Journal kann das
        # nur vermuten -- es sieht die letzte Buchung, nicht die Kuendigung.
        # DropBox und Freepik haben 2025 gebucht und sind trotzdem beendet.
        # Deshalb schlaegt eine menschliche Angabe die Ableitung.
        aktiv = any(j in ("2025", "2026") for j in jahre) and schluessel not in beendet
        if aktiv:
            gefragt.add(schluessel)

        aus.append(f"\n  - schluessel: {schluessel_aus(name)}")
        aus.append(f"    ordner: {_wert(name)}")
        if schluessel in beendet:
            aus.append(f"    {beendet[schluessel]}")
        if neben:
            aus.append(f"    nebenordner: [{', '.join(_wert(n) for n in neben)}]")
        if not e:
            aus.append("    # Keine eigene Buchung im Journal seit 2019 —")
            aus.append("    # der Ordner besteht, die Deklaration wartet auf den ersten Beleg.")
            aus.append(f"    belege_bis: {jahre[-1] if jahre else 'null'}")
            aus.append("    bestaetigt: false")
            continue

        konten = sorted(e["konten"].items(), key=lambda p: -p[1])
        if len(konten) == 1:
            aus.append(f"    sollkonto: \"{konten[0][0]}\"   # einstimmig, {konten[0][1]} Buchungen")
        else:
            liste = ", ".join(f'"{k}"' for k, _ in konten)
            zahlen = ", ".join(f"{k}: {n}" for k, n in konten)
            aus.append(f"    sollkonto_kandidaten: [{liste}]   # {zahlen}")
            aus.append("    sollkonto_entscheid: je_rechnung")
        if name in REGELN:
            aus.append(f"    regel: >-\n      {REGELN[name]}")

        wege = sorted(e["zahlwege"])
        aus.append(f"    zahlweg: {wege[0] if len(wege) == 1 else '[' + ', '.join(wege) + ']'}")

        # Die Steuerbehandlung entscheidet die **Rechnung**, nicht die Buchung.
        # Aus «keine Zeile auf 2203» folgt nur, dass keine Bezugssteuer gebucht
        # wurde -- nicht, dass keine anfaellt. Wer das Fehlen als Erwartung
        # hinschreibt, laesst den Abgleich genau den Fehler absegnen, den er
        # finden soll. Deshalb zuerst die Rechnungen fragen, dann das Journal,
        # und sonst offen lassen.
        aus.append("    steuer:")
        rechnung = (aus_rechnungen or {}).get(name)
        if rechnung:
            art, ab, mit, gesamt = rechnung
            grund = {
                "inland_mwst": f"{mit} von {gesamt} Rechnungen weisen MWST aus",
                "bezugssteuer": f"keine von {gesamt} Rechnungen weist MWST aus, Sitz im Ausland",
                "ohne_mwst": f"keine von {gesamt} Rechnungen weist MWST aus, inländisch befreit",
            }[art]
            aus.append(f"      - ab: {ab}\n        behandlung: {art}   # {grund}")
        elif e["bezugssteuer"]:
            ab = min(e["bezugssteuer"]).isoformat()
            aus.append(f"      - ab: {ab}\n        behandlung: bezugssteuer"
                       f"   # {len(e['bezugssteuer'])} Gegenzeilen auf 2203, keine Rechnung gelesen")
        else:
            aus.append("      - ab: null\n        behandlung: unbekannt"
                       "   # weder Rechnung noch Buchung geben Auskunft")

        aus.append(f"    rhythmus: {_rhythmus(e['monate'], e['von'], e['bis'])}"
                   f"   # {len(e['monate'])} belegte Monate, {e['von']}..{e['bis']}")
        aus.append(f"    aktiv: {'true' if aktiv else 'false'}")
        aus.append("    bestaetigt: false")

    # Was es in der Datei gibt, aber nicht als Ordner im Verzeichnisstand:
    # ein neuer Ordner wie «Google Gemini», bevor das Verzeichnis neu gelesen
    # wurde, oder ein Lieferant, dessen Ordner verschwand. Entfernen darf der
    # Erzeuger nicht -- also bleibt der Eintrag, wörtlich.
    for schluessel, block in bestand.items():
        if schluessel in erzeugt:
            continue
        treffer = re.search(r"^\s*ordner:\s*\"?([^\"\n#]+?)\"?\s*(#.*)?$", block, re.M)
        sortiert = (treffer.group(1) if treffer else schluessel).lower()
        eintraege.append((sortiert, [f"\n  - schluessel: {block}"]))
        if re.search(r"^\s*aktiv:\s*true", block, re.M) and "bestaetigt: true" not in block:
            gefragt.add(schluessel)

    aus = kopf
    for _, zeilen in sorted(eintraege, key=lambda p: p[0]):
        aus.extend(zeilen)

    aus.append("\n# Was niemand entscheiden konnte. Steht im Wortlaut, nicht als Etikett —")
    aus.append("# eine Frage, die niemand hört, ist so gut wie keine.")
    aus.append("offen:")
    for frage in OFFEN:
        aus.append(f"  - {frage}")
    # Widersprüche aus den Rechnungen selbst. Sie stehen hier und nicht als
    # «unbekannt» am Lieferanten, weil die Datei sonst verschweigt, dass es
    # Beweise gibt -- nur widersprüchliche.
    #
    # Gefragt wird nur zu Lieferanten, von denen noch Rechnungen kommen. Eine
    # Erwartung für einen gekündigten Dienst ist Arbeit ohne Gegenwert, und
    # drei solche Fragen in einer Liste von sechs kosten die Aufmerksamkeit
    # für die drei echten.
    for ziel, frage in fragen or []:
        if schluessel_aus(ziel) in gefragt:
            aus.append(f"  - >-\n      {frage}")

    aus.append("\n# ── Was die Prüfung ergab ───────────────────────────────────")
    aus.append(f"# Tote Zuordnungen (Schreibweise ohne Treffer): {len(tot)}")
    for s, o in tot:
        aus.append(f"#   {o}: {s!r}")
    grosse = sorted(offen.items(), key=lambda p: -p[1])[:25]
    aus.append(f"# Nicht zugeordnete Buchungen seit {LUECKE_AB}: "
               f"{sum(offen.values())} auf {len(offen)} Schreibweisen. Die häufigsten:")
    for s, n in grosse:
        aus.append(f"#   {n:4d}x {s}")
    return "\n".join(aus) + "\n"


def _wert(text: str) -> str:
    return f'"{text}"' if re.search(r"[:#\"'{}\[\],&*?|<>=!%@`]|^\s|\s$", text) else text


KOPF = """\
# Kreditorenlieferanten — was wir über einen Lieferanten erwarten
#
# Die Datei hält fest, was bei einer eingehenden Rechnung erwartet wird: in
# welchen Archivordner sie gehört, auf welches Konto sie üblicherweise läuft,
# ob Bezugssteuer anfällt, wie bezahlt wird und in welchem Takt etwas kommt.
#
# ## Eine Erwartung ist keine Tatsache
#
# Jeder Wert hier ist ein **Vorschlag mit Datum**, keine Regel. Ein Lieferant
# kann anfangen, Schweizer Mehrwertsteuer auszuweisen; dann ist die bisherige
# Bezugssteuer nicht falsch gewesen, sondern überholt — und bekommt eine neue
# Zeile mit `ab:`, statt dass die alte überschrieben wird. Eine Abweichung ist
# deshalb eine **Frage**, deren Antwort diese Datei fortschreibt, und kein
# Fehler.
#
# ## Das Konto hängt an der Rechnung, nicht am Lieferanten
#
# Gemessen am 21.09.2026: bei 87 % der Kartenbuchungen 2026 hat der Lieferant
# historisch immer dasselbe Konto — dort trägt ein Vorschlag. Bei Hosttech und
# Metanet trägt er nicht, weil dieselbe Domain einmal eigener Aufwand (6512)
# und einmal weiterverrechnete Leistung (4200) ist. Solche Einträge führen
# `sollkonto_kandidaten` statt `sollkonto` und verlangen eine Entscheidung je
# Rechnung. Ein einzelner erwarteter Wert wäre dort Scheingenauigkeit.
#
# ## Die Schreibweisen sind eine Altlast mit Verfallsdatum
#
# Solange die Treuhänderin bucht, ist der Beschreibungstext die einzige Spur
# vom Beleg zur Buchung — und das Journal führt `Anthropic` neben `Antrophic`,
# `Euromaster` neben `Eruomaster`. Sobald TaskPilot selbst bucht, schreibt es
# seine eigene Kennung in die Buchung, und der Abgleich hängt nicht mehr am
# Namen.
#
# ## Gepflegt wird sie nicht von Hand
#
# Sie wird geschrieben, nicht editiert — wie `kundenschluessel.yaml`. Eine
# Maschine darf **hinzufügen, nie ändern oder entfernen**. `bestaetigt: false`
# heisst: aus der Historie vorgeschlagen, von niemandem geprüft.
"""


def main() -> None:
    ordner = json.loads(ORDNERDATEI.read_text(encoding="utf-8"))
    con = duckdb.connect()
    befund, tot, offen = auswerten(zeilen_holen(con))

    fehlt = [o for o in ZUORDNUNG if o not in ordner]
    if fehlt:
        print(f"WARNUNG: zugeordnete Ordner, die es im Archiv nicht gibt: {fehlt}")

    steuer, steuerfragen = steuer_je_ordner()
    bewahrt, beendet = bestand_lesen()
    ZIEL.write_text(
        yaml_schreiben(
            ordner, befund, tot, offen, steuer, steuerfragen, bewahrt, beendet
        ),
        encoding="utf-8",
    )
    print(f"Geschrieben: {ZIEL}")
    print(f"  Steuerbehandlung aus Rechnungen belegt: {len(steuer)} Lieferanten")
    print(f"  bestehende Einträge unverändert übernommen: {len(bewahrt)}")
    print(f"  als beendet gemeldet, keine Fragen mehr: {len(beendet)}")
    print(f"  Lieferanten gesamt: {sum(1 for v in ordner.values() if not v['sonderordner'])}")
    print(f"  davon mit Journalbeleg: {len(befund)}")
    print(f"  tote Zuordnungen: {len(tot)}  {[s for s, _ in tot]}")
    print(f"  nicht zugeordnete Buchungen: {sum(offen.values())} auf {len(offen)} Schreibweisen")


if __name__ == "__main__":
    main()
