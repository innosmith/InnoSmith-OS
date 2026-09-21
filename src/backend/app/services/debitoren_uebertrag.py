"""Der Übertrag — zwei Lesarten im Altbestand, die Entscheidung steht aus.

InnoSmithAdmin rechnet den Stundenübertrag an **zwei** Stellen, und die beiden
Stellen rechnen nicht dasselbe. Das fiel nie auf, weil jede für sich schlüssig
ist und sie nur in einem schmalen Fenster auseinanderlaufen.

``D2_updateDraftRechnungen.calculate_adjustments`` schreibt die Zahlen in die
Rechnung::

    verfuegbar = fix + uebertrag_vormonat
    geleistet >= verfuegbar -> zusatz = geleistet - verfuegbar, neuer = 0
    sonst                   -> zusatz = 0,                      neuer = verfuegbar - geleistet

``admin_core.validate_invoice`` prüft dieselbe Rechnung hinterher::

    geleistet <= fix -> erwartet = geleistet - uebertrag_vormonat
                        neuer    = fix - geleistet + uebertrag_vormonat
    sonst            -> erwartet = fix + zusatz_aus_rechnung - uebertrag_vormonat
                        neuer    = 0

## Wo genau sie sich widersprechen

Beim neuen Übertrag stimmen beide überein — **ausser** im Fenster

    fix < geleistet < fix + uebertrag_vormonat

Dort trägt D2 den Rest weiter (``fix + uebertrag_vormonat - geleistet``), während
admin_core ihn auf null setzt, weil für diese Fassung schon das Überschreiten der
Fixstunden den Übertrag löscht. Genau dort schlägt Regel 2 an: die Rechnung
enthält eine Zahl, die Prüfung erwartet null, und die Rechnung gilt als fehlerhaft
— obwohl D2 in sich stimmig gerechnet hat. Ausserhalb des Fensters sind beide
Fassungen einig.

Der zweite Unterschied betrifft den **erwarteten Rechnungsbetrag** unterhalb der
Kapazität: D2 stellt ``fix`` in Rechnung, admin_core erwartet
``geleistet - uebertrag_vormonat``. Dieser Widerspruch wird nie gemeldet, weil
``validate_invoice`` Differenzen bei Vertragsart A zu einem Detail herabstuft
statt zu einem Fehler. Die Abweichung existiert also, sie war nur unsichtbar.

## Was die zweite Fassung offenlässt

Oberhalb der Fixstunden bestimmt ``VERFALL`` **keinen** Rechnungsbetrag: sie
nimmt die Zusatzstunden aus der Rechnung entgegen und rechnet nur den Übertrag
ab. Als Berechnungsvorschrift ist sie dort unvollständig — sie kann prüfen, aber
nicht vorgeben. Deshalb steht in diesem Fall ``None`` und nicht eine Formel, die
plausibel aussähe und in der Vorlage nicht existiert.

Unterhalb der Fixstunden ergibt dieselbe Fassung eine Zusatzstundenmenge, die
negativ ist (``geleistet - uebertrag_vormonat - fix``). Eine negative Menge lässt
sich auf keiner Rechnung darstellen. Der Wert wird trotzdem ausgewiesen, damit die
Eigenschaft sichtbar ist, statt hinter einem ``max(0, …)`` zu verschwinden.

## Die Entscheidung ist gefallen

Am 20.09.2026 hat Anthony Smith beide Fälle bestätigt, und zwar gegen die
Prüffassung:

1. *20h vereinbart, 5h Übertrag, 18h geleistet* — in Rechnung gehen die **20h**
   Fixstunden, nicht die 13h aus «geleistet abzüglich Übertrag».
2. *20h vereinbart, 5h Übertrag, 22h geleistet* — die verbleibenden **3h wandern
   weiter**; der Übertrag verfällt nicht, solange die Kapazität nicht
   ausgeschöpft ist.

Damit gilt ``KAPAZITAET``, also die Fassung, nach der ``D2`` die Rechnungen schon
immer geschrieben hat. Die Konsequenz ist keine Korrektur an den Rechnungen,
sondern an der Prüfung: ``admin_core`` hat richtig gestellte Rechnungen als
fehlerhaft gemeldet, und weil ein Fehlerbefund die Fortschreibung der Historie
unterband, pflanzte sich der falsche Befund in den Folgemonat fort.

``VERFALL`` bleibt trotzdem im Code. Ohne diese Fassung liesse sich nicht
nachvollziehen, warum abgeschlossene Monate im Altbestand als fehlerhaft
markiert sind — beim Rücklauf gegen die Vergangenheit ist sie der Massstab, mit
dem damals gemessen wurde.

## Warum hier trotzdem nichts voreingestellt ist

``rechnen`` verlangt die Lesart weiterhin als Argument und kennt keinen
Standardwert. Der bestätigte Entscheid steht in ``BESTAETIGT`` und wird von der
aufrufenden Stelle gesetzt — sichtbar, an einer Stelle, mit Datum. Ein
Vorgabewert im Rechenkern hiesse, dass ein vergessenes Argument stillschweigend
die richtige Antwort gäbe; dann fiele auch ein vergessenes falsches Argument
nicht auf.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Formel(str, Enum):
    """Die beiden Lesarten, benannt nach dem, was sie über den Übertrag sagen."""

    KAPAZITAET = "kapazitaet"
    """Der Übertrag erhöht die Kapazität des Folgemonats und verfällt erst, wenn
    sie ausgeschöpft ist (``D2_updateDraftRechnungen``)."""

    VERFALL = "verfall"
    """Der Übertrag verfällt, sobald mehr als die Fixstunden geleistet wurden
    (``admin_core.validate_invoice``)."""


BESTAETIGT: Formel = Formel.KAPAZITAET
"""Die geltende Lesart, bestätigt am 20.09.2026 — siehe Modulkopf.

Eine Konstante und kein Vorgabewert: so steht der Entscheid an einer Stelle, mit
Datum und Begründung, statt als stille Annahme in einer Signatur.
"""


BESCHREIBUNG: dict[Formel, str] = {
    Formel.KAPAZITAET: (
        "Der Übertrag aus dem Vormonat erhöht die verfügbare Kapazität. "
        "Zusatzstunden entstehen erst oberhalb von Fixstunden plus Übertrag; "
        "was darunter offen bleibt, wandert in den Folgemonat."
    ),
    Formel.VERFALL: (
        "Der Übertrag aus dem Vormonat verfällt, sobald mehr als die Fixstunden "
        "geleistet wurden. Nur unterhalb der Fixstunden wächst er weiter."
    ),
}


@dataclass(frozen=True)
class Ergebnis:
    """Was eine Lesart für einen Monat ergibt — samt Herleitung in einem Satz."""

    formel: Formel
    fix_stunden: float
    geleistet: float
    uebertrag_vormonat: float
    verfuegbar: float

    neuer_uebertrag: float
    """Der Übertrag in den Folgemonat. Beide Lesarten bestimmen ihn."""

    zusatz_stunden: float | None
    """Menge der Zusatzstundenposition, ``None`` wo die Lesart sie nicht bestimmt.

    Bei ``VERFALL`` unterhalb der Fixstunden rechnerisch negativ — eine Menge, die
    auf keiner Rechnung darstellbar ist; siehe Modulkopf.
    """

    verrechnet_erwartet: float | None
    """Summe der Stunden, die in Rechnung gehören, ``None`` wo unbestimmt."""

    herleitung: str


def rechnen(
    *,
    fix_stunden: float,
    geleistet: float,
    uebertrag_vormonat: float,
    formel: Formel,
) -> Ergebnis:
    """Übertrag und Zusatzstunden eines Monats nach der gewählten Lesart.

    Schlüsselwortargumente, weil vier Zahlen desselben Typs nebeneinander stehen:
    eine vertauschte Reihenfolge ergäbe ein plausibles, falsches Ergebnis ohne
    Fehlermeldung — genau die Fehlerart, gegen die dieses Modul gebaut ist.
    """
    verfuegbar = round(fix_stunden + uebertrag_vormonat, 2)
    zusatz: float | None
    verrechnet: float | None

    if formel is Formel.KAPAZITAET:
        if geleistet >= verfuegbar:
            zusatz = round(geleistet - verfuegbar, 2)
            neuer = 0.0
            herleitung = (
                f"{geleistet:g}h geleistet erreichen die Kapazität von {verfuegbar:g}h "
                f"({fix_stunden:g}h fix + {uebertrag_vormonat:g}h Übertrag); "
                f"{zusatz:g}h gehen als Zusatzstunden in Rechnung, der Übertrag ist aufgebraucht."
            )
        else:
            zusatz = 0.0
            neuer = round(verfuegbar - geleistet, 2)
            herleitung = (
                f"{geleistet:g}h geleistet bleiben unter der Kapazität von {verfuegbar:g}h "
                f"({fix_stunden:g}h fix + {uebertrag_vormonat:g}h Übertrag); "
                f"verrechnet werden die {fix_stunden:g}h fix, {neuer:g}h wandern in den Folgemonat."
            )
        verrechnet = round(fix_stunden + zusatz, 2)

    else:  # Formel.VERFALL
        if geleistet <= fix_stunden:
            verrechnet = round(geleistet - uebertrag_vormonat, 2)
            zusatz = round(verrechnet - fix_stunden, 2)
            neuer = round(fix_stunden - geleistet + uebertrag_vormonat, 2)
            herleitung = (
                f"{geleistet:g}h geleistet bleiben unter den {fix_stunden:g}h fix; "
                f"verrechnet werden {geleistet:g}h abzüglich {uebertrag_vormonat:g}h Übertrag "
                f"= {verrechnet:g}h, der Übertrag wächst auf {neuer:g}h."
            )
        else:
            # Die Vorlage liest die Zusatzstunden hier aus der Rechnung und gibt
            # sie nicht vor. Eine Zahl zu erfinden hiesse, eine Vorschrift zu
            # behaupten, die es nicht gibt.
            verrechnet = None
            zusatz = None
            neuer = 0.0
            herleitung = (
                f"{geleistet:g}h geleistet überschreiten die {fix_stunden:g}h fix; "
                f"der Übertrag von {uebertrag_vormonat:g}h verfällt. Welcher Betrag in "
                f"Rechnung gehört, bestimmt diese Lesart nicht — sie prüft ihn nur."
            )

    return Ergebnis(
        formel=formel,
        fix_stunden=fix_stunden,
        geleistet=geleistet,
        uebertrag_vormonat=uebertrag_vormonat,
        verfuegbar=verfuegbar,
        neuer_uebertrag=neuer,
        zusatz_stunden=zusatz,
        verrechnet_erwartet=verrechnet,
        herleitung=herleitung,
    )


def im_streitfenster(
    *, fix_stunden: float, geleistet: float, uebertrag_vormonat: float
) -> bool:
    """Ob die beiden Lesarten für diesen Monat verschiedene Überträge ergeben.

    Das ist genau das Fenster ``fix < geleistet < fix + uebertrag_vormonat``. Die
    Funktion existiert, damit die Messung zählen kann, wie oft es überhaupt
    eintritt: liegt die Zahl bei null, ist die Entscheidung folgenlos; liegt sie
    hoch, ist sie dringend. Beides sollte man wissen, bevor man entscheidet.
    """
    return fix_stunden < geleistet < fix_stunden + uebertrag_vormonat


def vergleichen(
    *, fix_stunden: float, geleistet: float, uebertrag_vormonat: float
) -> dict[Formel, Ergebnis]:
    """Beide Lesarten auf dieselbe Ausgangslage anwenden.

    Grundlage der Messung: über abgeschlossene Monate gerechnet zeigt sie, wo die
    Fassungen auseinanderlaufen und um wie viel — statt die Frage am Schreibtisch
    zu entscheiden.
    """
    return {
        f: rechnen(
            fix_stunden=fix_stunden,
            geleistet=geleistet,
            uebertrag_vormonat=uebertrag_vormonat,
            formel=f,
        )
        for f in Formel
    }
