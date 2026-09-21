"""Der Leistungsrapport als PDF — die Beilage zur Rechnung.

Portiert aus ``D1_createLeistungsrapporte.py``. **Das Layout ist wortgetreu
übernommen**, bis auf Millimeter, Graustufen und Schriftgrade. Das ist keine
Bequemlichkeit: das Dokument liegt seit Jahren bei der Kundschaft im Ordner,
und ein Rapport, der plötzlich anders aussieht, wirft Fragen auf, die niemand
stellen wollte. Wer hier etwas verbessert, ändert ein Kundendokument.

## Zwei Teile, die getrennt bleiben

``aufbereiten`` macht aus Toggl-Zeiteinträgen die Zahlen — rein, ohne
Seiteneffekte, vollständig prüfbar. ``erzeugen`` macht daraus Seiten. Die
Trennung erlaubt, die Zahlen zu testen, ohne ein PDF zu lesen; ein Test, der
Beträge aus einem PDF zurückliest, prüft am Ende die Schriftart mit.

## Was sich gegenüber der Vorlage geändert hat — und warum

**Bytes statt Datei.** Die Vorlage schrieb in ein Verzeichnis auf dem Mac. Hier
kommt das PDF als ``bytes`` zurück: es wird an eine Mail gehängt und ins
Kundenarchiv gelegt, und beides braucht keinen Zwischenschritt über die Platte.
Eine Datei, die nur entsteht, um gleich wieder gelesen zu werden, ist eine
Fehlerquelle ohne Gegenwert.

**Fehler werden geworfen.** Die Vorlage fing jede Ausnahme und gab ``False``
zurück. Im Rechnungslauf hiesse das: ein Rapport fehlt, die Rechnung geht
trotzdem raus, und niemand merkt es bis zur Rückfrage der Kundschaft.

**Die Schriften sind Pflicht, nicht Kür.** Die Vorlage wich stillschweigend auf
Helvetica aus, wenn Inter fehlte. Das ergibt ein Dokument, das *fast* wie die
anderen aussieht — die schlechteste aller Varianten, weil es weder auffällt
noch stimmt. Fehlt die Schrift, bricht es ab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from fpdf import FPDF

logger = logging.getLogger("taskpilot.leistungsrapport")

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "leistungsrapport"
SCHRIFT_NORMAL = ASSETS / "fonts" / "Inter_18pt-Light.ttf"
SCHRIFT_FETT = ASSETS / "fonts" / "Inter_18pt-Bold.ttf"
LOGO = ASSETS / "innosmith-logo.png"

MONATE = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November",
    12: "Dezember",
}

OHNE_BEREICH = "(Allgemein)"
"""Sammelname für Einträge ohne Toggl-Task. Steht in der Aufschlüsselung immer
zuletzt, weil er kein Bereich ist, sondern deren Abwesenheit."""


@dataclass
class Zeile:
    datum: str
    """Bereits formatiert als TT.MM.JJJJ — die Reihenfolge der Zeilen ist die
    des Rapports und nicht mehr sortierbar."""
    taetigkeit: str
    stunden: float


@dataclass
class Rapport:
    """Die fertigen Zahlen eines Rapports, bevor daraus Seiten werden."""

    zeitraum: str
    """«August 2026» — steht im Titel."""
    zeilen: list[Zeile] = field(default_factory=list)
    summe: float = 0.0
    mitarbeitende: str | None = None
    """Nur gesetzt, wenn **eine** Person gebucht hat. Bei mehreren steht der
    Name nicht im Kopf, sondern als Kürzel an jeder Zeile — sonst behauptete
    der Kopf eine Zuordnung, die die Zeilen widerlegen."""
    nach_bereich: list[tuple[str, float]] = field(default_factory=list)
    nach_person: list[tuple[str, float]] = field(default_factory=list)


def kuerzel(name: str) -> str:
    """«Anthony Smith» → «AS», «Hans-Peter Müller» → «HPM»."""
    if not name:
        return "??"
    teile = name.replace("-", " ").split()
    return "".join(t[0].upper() for t in teile if t)


def taetigkeit(
    bereich: str | None, beschreibung: str | None, person: str | None = None
) -> str:
    """Bereich, Beschreibung und Kürzel zu einer Zeile verbinden.

    ``person`` wird nur übergeben, wenn mehrere Personen gebucht haben —
    andernfalls stünde an jeder Zeile dasselbe Kürzel, und das trägt nichts.
    """
    bereich = bereich.strip() if bereich and bereich.strip() else None
    beschreibung = beschreibung.strip() if beschreibung and beschreibung.strip() else None

    if bereich and beschreibung:
        text = f"{bereich}: {beschreibung}"
    elif bereich:
        text = bereich
    elif beschreibung:
        text = beschreibung
    else:
        text = "(Keine Angabe)"

    return f"{text} ({person})" if person else text


def aufbereiten(
    eintraege: list[dict], *, jahr: int, monat: int,
    bereiche: dict | None = None, personen: dict | None = None,
) -> Rapport:
    """Aus Toggl-Zeiteinträgen die Zahlen des Rapports machen.

    Erwartet je Eintrag ``datum`` (``date``), ``dauer_stunden``,
    ``beschreibung`` und optional ``task_id`` sowie ``user_id``.

    ``bereiche`` bildet ``task_id`` auf den Namen ab, ``personen`` die
    ``user_id`` auf den Namen. Fehlt eine Zuordnung, wird sie **nicht geraten**:
    der Eintrag zählt unter ``(Allgemein)`` beziehungsweise ``(Unbekannt)``.
    Beides ist sichtbar und damit korrigierbar; ein erratener Name wäre es nicht.

    Die Zeilen bleiben in der übergebenen Reihenfolge — die Vorlage sortiert
    vorher nach Projekt und Startzeit, und diese Reihenfolge ist die des
    Rapports.
    """
    bereiche = bereiche or {}
    personen = personen or {}

    # Kürzel nur zeigen, wenn es etwas zu unterscheiden gibt.
    beteiligte = {e.get("user_id") for e in eintraege if e.get("user_id")}
    mehrere = len(beteiligte) > 1

    zeilen: list[Zeile] = []
    je_bereich: dict[str, float] = {}
    je_person: dict[str, float] = {}
    summe = 0.0

    for e in eintraege:
        stunden = float(e.get("dauer_stunden") or 0.0)
        summe += stunden

        task_id = e.get("task_id")
        bereich = bereiche.get(task_id) or bereiche.get(str(task_id)) if task_id else None
        je_bereich[bereich or OHNE_BEREICH] = je_bereich.get(bereich or OHNE_BEREICH, 0.0) + stunden

        user_id = e.get("user_id")
        name = personen.get(user_id) or personen.get(str(user_id)) if user_id else None
        marke = f"{name} ({kuerzel(name)})" if name else "(Unbekannt)"
        je_person[marke] = je_person.get(marke, 0.0) + stunden

        datum = e.get("datum")
        zeilen.append(Zeile(
            datum=datum.strftime("%d.%m.%Y") if hasattr(datum, "strftime") else str(datum or ""),
            taetigkeit=taetigkeit(
                bereich, e.get("beschreibung"),
                kuerzel(name) if (mehrere and name) else None,
            ),
            stunden=stunden,
        ))

    # Absteigend nach Stunden, «(Allgemein)» ans Ende — es ist kein Bereich.
    sortiert = sorted(je_bereich.items(), key=lambda p: p[1], reverse=True)
    nach_bereich = [
        (n, round(h, 2)) for n, h in sortiert if n != OHNE_BEREICH
    ] + [(n, round(h, 2)) for n, h in sortiert if n == OHNE_BEREICH]

    einzelne = None
    if not mehrere and beteiligte:
        nur = next(iter(beteiligte))
        einzelne = personen.get(nur) or personen.get(str(nur))

    return Rapport(
        zeitraum=f"{MONATE[monat]} {jahr}",
        zeilen=zeilen,
        summe=round(summe, 2),
        mitarbeitende=einzelne,
        # Ein einziger Bereich ist keine Aufschlüsselung, sondern eine
        # Wiederholung der Gesamtsumme.
        nach_bereich=nach_bereich if any(n != OHNE_BEREICH for n, _ in nach_bereich) else [],
        nach_person=sorted(je_person.items(), key=lambda p: p[1], reverse=True) if mehrere else [],
    )


class _Seite(FPDF):
    """Das Layout. Masse und Graustufen sind aus der Vorlage übernommen."""

    def __init__(self) -> None:
        super().__init__()
        for pfad in (SCHRIFT_NORMAL, SCHRIFT_FETT):
            if not pfad.exists():
                raise FileNotFoundError(
                    f"Schrift für den Leistungsrapport fehlt: {pfad}. "
                    "Ohne sie entstünde ein Dokument, das fast wie die "
                    "bisherigen aussieht — und genau das fällt niemandem auf."
                )
        self.add_font("Inter", "", str(SCHRIFT_NORMAL))
        self.add_font("Inter", "B", str(SCHRIFT_FETT))
        self.set_margins(left=15, top=15, right=15)
        self.set_auto_page_break(auto=True, margin=20)

    def footer(self) -> None:
        # Seitenzahl erst ab der zweiten Seite: auf einem einseitigen Rapport
        # ist «Seite 1 von 1» Lärm.
        if self.page > 1:
            self.set_y(-15)
            self.set_font("Inter", "", 8)
            self.set_text_color(120, 120, 120)
            self.cell(0, 10, f"Seite {self.page} von {{nb}}", align="R")

    def kopf(self, titel: str, mitarbeitende: str | None) -> None:
        if LOGO.exists():
            self.image(str(LOGO), x=155, y=12, w=40)
        self.set_font("Inter", "B", 14)
        self.set_text_color(51, 51, 51)
        self.cell(140, 7, titel, new_x="LMARGIN", new_y="NEXT")
        if mitarbeitende:
            self.set_font("Inter", "", 10)
            self.set_text_color(90, 90, 90)
            self.cell(140, 5, mitarbeitende, new_x="LMARGIN", new_y="NEXT")
        self.ln(18)

    def tabellenkopf(self) -> None:
        self.set_fill_color(90, 90, 90)
        self.set_text_color(255, 255, 255)
        self.set_font("Inter", "B", 8)
        self.cell(22, 6, "Datum", border=0, fill=True)
        self.cell(140, 6, "Beschreibung", border=0, fill=True)
        self.cell(18, 6, "Dauer [h]", border=0, fill=True, align="R")
        self.ln()
        self.set_text_color(51, 51, 51)

    def zeile(self, z: Zeile) -> None:
        self.set_font("Inter", "", 8)
        self.set_text_color(51, 51, 51)
        hoehe = 6

        self.set_draw_color(230, 230, 230)
        self.set_line_width(0.1)
        self.line(15, self.get_y(), 195, self.get_y())

        self.cell(22, hoehe, z.datum, border=0)

        # Die Stundenzahl steht auf der **ersten** Zeile der Beschreibung, auch
        # wenn diese umbricht. Darum die Marke vorher merken: nach dem
        # ``multi_cell`` steht der Zeiger weiter unten, und die Zahl landete
        # sonst auf Höhe der letzten Textzeile.
        x, y = self.get_x(), self.get_y()
        self.multi_cell(140, hoehe, z.taetigkeit, border=0, align="L")
        danach = self.get_y()
        self.set_xy(x + 140, y)
        self.cell(18, hoehe, f"{z.stunden:.2f}", border=0, align="R")
        self.set_y(max(danach, y + hoehe))

    def summenzeile(self, summe: float) -> None:
        self.ln(2)
        self.set_draw_color(51, 51, 51)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(3)
        self.set_font("Inter", "B", 8)
        self.set_text_color(51, 51, 51)
        self.cell(162, 6, "TOTAL", border=0, align="R")
        self.cell(18, 6, f"{summe:.2f}h", border=0, align="R")
        self.ln()
        self.set_font("Inter", "", 8)

    def aufschluesselung(self, titel: str, posten: list[tuple[str, float]]) -> None:
        if not posten:
            return
        self.ln(8)
        self.set_font("Inter", "", 8)
        self.set_text_color(100, 100, 100)
        self.cell(0, 4, titel, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self.set_draw_color(180, 180, 180)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(3)
        self.set_text_color(51, 51, 51)
        for name, stunden in posten:
            self.set_font("Inter", "", 8)
            self.cell(162, 5, f"• {name}", border=0)
            self.set_font("Inter", "B", 8)
            self.cell(18, 5, f"{stunden:.2f}h", border=0, align="R")
            self.ln()


def erzeugen(rapport: Rapport) -> bytes:
    """Aus den Zahlen ein PDF machen. Liefert die Bytes.

    Ein Fehler wird **geworfen**. Die Vorlage gab ``False`` zurück, und im
    Rechnungslauf hiesse das: der Rapport fehlt, die Rechnung geht trotzdem
    raus.
    """
    seite = _Seite()
    seite.alias_nb_pages()
    seite.add_page()
    seite.kopf(f"Leistungsrapport {rapport.zeitraum}", rapport.mitarbeitende)
    seite.tabellenkopf()
    for z in rapport.zeilen:
        seite.zeile(z)
    seite.summenzeile(rapport.summe)
    seite.aufschluesselung("Aufschlüsselung nach Bereich", rapport.nach_bereich)
    seite.aufschluesselung("Aufschlüsselung nach Mitarbeitenden", rapport.nach_person)

    roh = seite.output()
    return bytes(roh)
