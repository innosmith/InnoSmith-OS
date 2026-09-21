"""Rechnungspositionen deuten: welche Zeile ist Fix, welche Zusatz, welche Übertrag.

Portiert aus ``D2_updateDraftRechnungen.is_relevant_invoice`` und
``extract_fix_stunden_from_positions``. Die Erkennungsmerkmale sind unverändert
übernommen, weil sie gegen den Echtbestand erprobt sind — was sich ändert, ist
der Umgang mit dem Fall, in dem sie **nicht** greifen.

## Warum die Textsuche ein Übergang ist

Eine Rechnungsposition trägt in Bexio keine Kennzeichnung, wofür sie steht. Der
Altbestand erschliesst das aus dem Positionstext:

* «Variable Zusatzstunden …» am Anfang ⟶ die Zusatzstundenposition
* «Per … verrechnet aber noch nicht geleistet …» ⟶ der Übertragssatz
* ``20h/Monat fix`` irgendwo im Text ⟶ die vereinbarten Stunden

Das ist Bedeutung aus einer Zeichenkette, und es scheitert leise: greift die
Erkennung daneben, bleibt die Zusatzposition auf null stehen, die Stunden gehen
nicht in Rechnung, und niemand erfährt davon. Deshalb liefert dieses Modul neben
den Zahlen immer eine Liste von ``auffaelligkeiten``. Was nicht eindeutig
bestimmbar war, steht dort — statt als stille Null im Ergebnis.

Mittelfristig kommen Fixstunden und Übertragbarkeit aus dem Vertragsstammdatum
und der Text wird nur noch zum Abgleich gelesen. Bis dahin ist die Textsuche die
einzige Quelle, und sie muss wenigstens laut scheitern.

## Zwei Abweichungen von der Vorlage

**Halbe Stunden.** Die Vorlage verlangte ``(\\d+)h`` — nur ganze Zahlen. Ein
Vertrag über 7.5 Stunden war damit nicht erkennbar, und das Ergebnis war nicht
etwa ein Fehler, sondern ``0.0``. Hier sind Dezimalstellen zugelassen.

**Ein Muster statt zweier.** ``admin_core`` suchte ``(\\d+)h fix inklusive``,
``D2`` begnügte sich mit ``(\\d+)h\\s*(?:/monat\\s*)?fix``. Dieselbe Rechnung
konnte damit je nach Programm 20 Stunden oder gar keine haben. Übernommen ist
das weiter gefasste Muster aus ``D2``; ``admin_core`` hätte eine echte Teilmenge
erkannt.

## Was eine Stundenposition ist

``unit_id == 2``. Diese Kennung steht so in ``D2_updateDraftRechnungen`` als
Rückfallwert mit dem Kommentar «2 = Stunden (h)» und ist damit deklariertes
Wissen aus dem Altbestand, keine Annahme dieses Moduls. Eine Produktposition mit
einer anderen Einheit wird **nicht** mitsummiert, aber gemeldet: eine Pauschale
in der Stundensumme wäre ein stiller Fehler, eine unerwähnte Position auch.
"""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "bexio"))

from rechnungen import betrag  # noqa: E402  -- geteilte Bibliothek, siehe sys.path oben

STUNDENEINHEIT = 2
"""Bexio-``unit_id`` für Stunden. Siehe Modulkopf."""

ZUSATZ_ANFANG = "variable zusatzstunden"
UEBERTRAG_ANFANG = "per"
UEBERTRAG_ENTHAELT = "verrechnet aber noch nicht geleistet"

FIX_MUSTER = re.compile(r"(\d+(?:[.,]\d+)?)\s*h\s*(?:/\s*monat\s*)?fix", re.IGNORECASE)
UEBERTRAG_MUSTER = re.compile(r"sind\s+(\d+(?:[.,]\d+)?)\s*h", re.IGNORECASE)

_HTML = re.compile(r"<[^>]+>")


def klartext(*teile: object) -> str:
    """Positionstext ohne HTML, klein geschrieben, Leerraum zusammengefasst.

    Bexio speichert Positionstexte mit Auszeichnung; ohne das Entfernen begänne
    ein Text mit ``<p>`` statt mit ``Variable``, und jeder Anfangsvergleich ginge
    ins Leere.

    Entitäten werden aufgelöst, **nachdem** die Auszeichnung weg ist. Umgekehrt
    machte ein ``&lt;`` im Text nachträglich eine spitze Klammer, die dann wie
    Auszeichnung aussähe. Ohne das Auflösen steht in jeder Meldung
    «unterst&uuml;tzung», und ein Muster auf «für» träfe nie ein «f&uuml;r».
    """
    roh = " ".join(str(t) for t in teile if t)
    return " ".join(html.unescape(_HTML.sub(" ", roh)).split()).lower()


def _zahl(text: str, muster: re.Pattern[str]) -> float | None:
    treffer = muster.search(text)
    if not treffer:
        return None
    try:
        return float(treffer.group(1).replace(",", "."))
    except ValueError:
        return None


@dataclass
class Positionsdeutung:
    """Was sich aus den Positionen einer Rechnung ablesen lässt."""

    fix_stunden: float | None = None
    """Menge der Fixposition — die tatsächlich verrechnete, nicht die im Text."""
    fix_stunden_laut_text: float | None = None
    """Die Zahl aus dem Positionstext, zum Abgleich."""
    zusatz_stunden: float | None = None
    uebertrag_angabe: float | None = None
    verrechnete_stunden: float | None = None
    """Summe aller Positionen mit Stundeneinheit."""
    auffaelligkeiten: list[str] = field(default_factory=list)

    # Die Zeilen selbst, für alles, was sie nicht nur lesen, sondern ändern will.
    # Sie hier mitzugeben ist billiger als richtig: wer sie sich selbst
    # heraussucht, deutet ein zweites Mal — und zwei Deutungen desselben Textes
    # laufen auseinander, sobald eine von beiden verbessert wird.
    fix_position: dict | None = None
    zusatz_position: dict | None = None
    uebertrag_position: dict | None = None
    stundenpositionen: list[dict] = field(default_factory=list)
    """Alle ``custom``-Zeilen mit Stundeneinheit, in Positionsreihenfolge."""

    @property
    def eindeutig(self) -> bool:
        return not self.auffaelligkeiten


def deuten(positionen: list[dict]) -> Positionsdeutung:
    """Positionen einer Bexio-Rechnung auf Fix, Zusatz und Übertrag abbilden.

    Erwartet die Zeilen aus ``BexioClient.get_invoice_positions`` — also mit dem
    Feld ``positionsart``. Reine Funktion, damit sie gegen echte Positionen
    geprüft werden kann, ohne Bexio anzufragen.
    """
    deutung = Positionsdeutung()

    stundenposten: list[dict] = []
    andere_posten: list[dict] = []
    zusatz: list[dict] = []
    fix: list[dict] = []
    fix_laut_text: list[float] = []
    uebertrag: list[dict] = []

    for pos in positionen:
        text = klartext(pos.get("text"), pos.get("name"))

        if text.startswith(UEBERTRAG_ANFANG) and UEBERTRAG_ENTHAELT in text:
            uebertrag.append(pos)
            continue

        if pos.get("positionsart") != "custom":
            continue

        einheit = pos.get("unit_id")
        if einheit == STUNDENEINHEIT:
            stundenposten.append(pos)
        elif betrag(pos.get("amount")):
            andere_posten.append(pos)

        if text.startswith(ZUSATZ_ANFANG):
            zusatz.append(pos)
        elif (laut_text := _zahl(text, FIX_MUSTER)) is not None:
            # Die Zahl fällt bei der Zuordnung an: dieselbe Mustersuche
            # entscheidet, ob es eine Fixposition ist, und liefert ihre Stunden.
            # Getrennt gesucht, gäbe es einen Zweig «ist eine Fixposition, nennt
            # aber keine Zahl», den kein Text erreichen kann.
            fix.append(pos)
            fix_laut_text.append(laut_text)

    # ── Fixposition ──────────────────────────────────────
    #
    # Die Pauschale ist der Regelfall, nicht die Ausnahme: sie steht als eine
    # Einheit zum Monatspreis in der Rechnung («1 x 3198.70»), und die Stunden
    # nennt allein der Text. Eine frühere Fassung verlangte die Stundeneinheit
    # und verwarf die Textzahl als «nicht verwertbar» — damit trug im Rücklauf
    # gegen den August jede der sechs Pauschalrechnungen eine Warnung, und die
    # Stundenprüfung verglich gegen eine Erwartung ohne Grundlage.
    pauschale = False
    if len(fix) > 1:
        deutung.auffaelligkeiten.append(
            f"{len(fix)} Positionen sehen nach Fixstunden aus; keine davon ist eindeutig."
        )
    elif fix:
        deutung.fix_position = fix[0]
        deutung.fix_stunden_laut_text = fix_laut_text[0]
        if fix[0].get("unit_id") == STUNDENEINHEIT:
            deutung.fix_stunden = round(betrag(fix[0].get("amount")), 2)
            if abs(deutung.fix_stunden - deutung.fix_stunden_laut_text) > 0.005:
                deutung.auffaelligkeiten.append(
                    f"Die Fixposition ist mit {deutung.fix_stunden:g}h verrechnet, "
                    f"der Text nennt {deutung.fix_stunden_laut_text:g}h."
                )
        else:
            deutung.fix_stunden = deutung.fix_stunden_laut_text
            pauschale = True

    # ── Stundensumme ─────────────────────────────────────
    #
    # Verrechnet ist, was die Stundenpositionen ausweisen **plus** die im
    # Monatspreis enthaltenen Stunden. Ohne den zweiten Summanden meldete die
    # Prüfung für ImpulsKöniz «7.25h geleistet, 0.25h verrechnet» — richtig
    # gezählt und trotzdem falsch, weil die 7h der Pauschale fehlten.
    aus_positionen = (
        round(sum(betrag(p.get("amount")) for p in stundenposten), 2)
        if stundenposten else None
    )
    if pauschale:
        deutung.verrechnete_stunden = round(
            (deutung.fix_stunden or 0.0) + (aus_positionen or 0.0), 2
        )
    elif aus_positionen is not None:
        deutung.verrechnete_stunden = aus_positionen
    elif positionen:
        deutung.auffaelligkeiten.append(
            "Keine Position mit Stundeneinheit — die verrechneten Stunden sind "
            "nicht feststellbar."
        )

    # Die Pauschale zählt hier nicht als übergangene Position: ihre Stunden sind
    # oben eingerechnet. Was übrig bleibt, sind echte Fremdkörper in der
    # Stundenrechnung — etwa die monatlichen Hostingkosten auf der
    # Sympholio-Rechnung, und die gehören genannt.
    uebergangen = [p for p in andere_posten if not (pauschale and p is fix[0])]
    if uebergangen:
        deutung.auffaelligkeiten.append(
            "Nicht in der Stundensumme enthalten, weil ohne Stundeneinheit: "
            + ", ".join(_kurz(klartext(p.get("text"), p.get("name"))) for p in uebergangen)
        )

    # ── Zusatzposition ───────────────────────────────────
    if len(zusatz) > 1:
        deutung.auffaelligkeiten.append(
            f"{len(zusatz)} Positionen beginnen mit «{ZUSATZ_ANFANG}»; "
            "welche die Zusatzstunden trägt, ist nicht entscheidbar."
        )
    elif zusatz:
        deutung.zusatz_position = zusatz[0]
        deutung.zusatz_stunden = round(betrag(zusatz[0].get("amount")), 2)
    elif fix and len(stundenposten) > (0 if pauschale else 1):
        # Eine Stundenposition neben der Pauschale, die nicht «Variable
        # Zusatzstunden» heisst: dort griff die Erkennung im Altbestand daneben,
        # ohne dass es auffiel.
        #
        # Ohne Pauschale gilt das nicht. Auf einer rein variablen Rechnung ist
        # die Stundenposition die Abrechnung selbst und heisst nach dem Projekt
        # («COflow – Effektiver Aufwand»). Die frühere Bedingung meldete genau
        # diesen Normalfall — bei AKV-Bot und COflow ein Fehlalarm aus nichts.
        deutung.auffaelligkeiten.append(
            "Eine Stundenposition neben der Pauschale ist nicht als Zusatzposition "
            "erkennbar — die Zuordnung über den Positionstext hat nicht gegriffen."
        )

    # ── Übertragssatz ────────────────────────────────────
    if len(uebertrag) > 1:
        deutung.auffaelligkeiten.append(
            f"{len(uebertrag)} Übertragssätze auf einer Rechnung."
        )
    elif uebertrag:
        deutung.uebertrag_position = uebertrag[0]
        text = klartext(uebertrag[0].get("text"))
        deutung.uebertrag_angabe = _zahl(text, UEBERTRAG_MUSTER)
        if deutung.uebertrag_angabe is None:
            deutung.auffaelligkeiten.append(
                "Der Übertragssatz nennt keine lesbare Stundenzahl."
            )

    deutung.stundenpositionen = stundenposten
    return deutung


def _kurz(text: str, laenge: int = 40) -> str:
    return text if len(text) <= laenge else text[: laenge - 1] + "…"
