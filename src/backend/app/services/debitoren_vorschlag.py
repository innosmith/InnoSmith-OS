"""Was im Entwurf stehen müsste — berechnet, nicht geschrieben.

Die Prüfung sagt, dass eine Zahl nicht stimmt. Dieses Modul sagt, welche Zeile
welchen Wert tragen müsste, damit sie stimmt. Getrennt gehalten, weil das eine
ein Urteil ist und das andere ein Eingriff: der Vorschlag wird angezeigt,
verglichen und erst nach Bestätigung angewendet (L1, externe Wirkung).

## Der Anlass

Bei den Verträgen **nach Aufwand** — OnboardingQS, sbKreditorenBot, COflow,
AKV-Bot, KI-Pilotprojekte — trug der Altbestand die Stunden nie ein. Der
Entwurf kam aus dem Bexio-Auftrag mit der Menge des Vormonats, und die
richtige Zahl schrieb der Mensch von Hand ab. Im August 2026 waren das fünf von
elf Rechnungen; jede Zahl stand in Toggl bereit.

Bei den **Pauschalverträgen** tat ``D2_updateDraftRechnungen.py`` es schon: es
setzte die Zusatzstunden und schrieb den Übertragssatz um. Diese Hälfte wird
hier portiert, nicht neu erfunden — bis hin zum Wortlaut des Satzes, denn den
liest die Kundschaft.

## Was hier nicht entschieden wird

**Der Stundensatz wird nie erfunden.** Er steht in der Position, die schon da
ist, und bleibt unangetastet; geändert wird allein die Menge. Fehlt die Zeile
ganz, gibt es keinen Vorschlag, sondern eine Meldung — ein geratener Satz
erzeugte eine plausible falsche Rechnung statt einer Fehlermeldung, und das ist
der teuerste Fehler dieses Prozesses.

**Mehrdeutigkeit wird gemeldet, nicht aufgelöst.** «CAS TCM FS26 Lektionen»
trägt drei Stundenzeilen (drei Module). Wie sich 17.25h darauf verteilen, weiss
nur der Mensch. Eine Maschine, die hier eine Zeile auswählt, hat in vier von
fünf Fällen recht — und im fünften schweigend unrecht.

**Keine Stunden heisst kein Entwurf.** Steht für einen Aufwandsvertrag eine
Rechnung, aber Toggl kennt keine Stunden, wird nicht auf null gesetzt, sondern
gemeldet: die Rechnung gehört gar nicht erzeugt. Sie stillschweigend zu nullen
wäre die Antwort auf eine Frage, die niemand gestellt hat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.services import debitoren_positionen as dp
from app.services import debitoren_uebertrag as ue
from app.services.debitorenvertraege import Vertragsdaten

UEBERTRAGSSATZ = (
    "Per {datum} sind {stunden}h verrechnet aber noch nicht geleistet, "
    "welche dem nächsten Monat angerechnet werden."
)
"""Wortlaut aus ``D2_updateDraftRechnungen.py``. Unverändert übernommen: die
Kundschaft liest diesen Satz seit Jahren, und ``debitoren_positionen`` erkennt
ihn im Folgemonat an genau dieser Form wieder."""

TOLERANZ = 0.01


@dataclass(frozen=True)
class Aenderung:
    """Eine Zeile, die anders lauten müsste."""

    positionsart: str
    """``custom`` oder ``text`` — bestimmt den Bexio-Endpunkt."""
    position_id: int
    handlung: str
    """``aendern`` oder ``entfernen``."""
    feld: str
    alt: str
    neu: str
    begruendung: str
    nutzdaten: dict = field(default_factory=dict)
    """Genau das, was an Bexio ginge. Hier berechnet und nicht erst beim
    Anwenden, damit im Vergleich steht, was tatsächlich gesendet wird."""


@dataclass
class Vorschlag:
    """Was an einer Rechnung zu tun wäre."""

    rechnung: str
    projekt: str
    aenderungen: list[Aenderung] = field(default_factory=list)
    hindernisse: list[str] = field(default_factory=list)
    """Warum etwas **nicht** vorgeschlagen wird. Kein Fehler, sondern die
    Stelle, an der ein Mensch entscheiden muss."""
    hinweise: list[str] = field(default_factory=list)

    @property
    def offen(self) -> bool:
        """Gibt es etwas anzuwenden?"""
        return bool(self.aenderungen)


def _menge(wert: float) -> str:
    """Bexio nimmt Mengen als Zeichenkette. ``19.5`` statt ``19.500000``."""
    return f"{wert:g}"


def _mengenaenderung(position: dict, neu: float, begruendung: str) -> Aenderung:
    """Die Menge einer ``custom``-Zeile ändern, sonst nichts.

    Bexio verlangt beim ``PUT`` die Pflichtfelder mit; sie werden aus der
    bestehenden Zeile übernommen und **nicht** durch Vorgabewerte ersetzt. Der
    Altbestand setzte hier ``unit_price`` notfalls auf ``'200'`` und ``tax_id``
    auf ``6`` — bei einem Vertrag zu 250 CHF wären das 50 CHF je Stunde
    Schaden, ohne Meldung.
    """
    return Aenderung(
        positionsart="custom",
        position_id=int(position["id"]),
        handlung="aendern",
        feld="amount",
        alt=_menge(dp.betrag(position.get("amount"))),
        neu=_menge(neu),
        begruendung=begruendung,
        nutzdaten={
            "amount": _menge(neu),
            "unit_price": position.get("unit_price"),
            "tax_id": position.get("tax_id"),
            "text": position.get("text"),
            "unit_id": position.get("unit_id"),
        },
    )


def vorschlagen(
    *,
    vertrag: Vertragsdaten,
    rechnung: str,
    deutung: dp.Positionsdeutung,
    geleistet: float | None,
    uebertrag_vormonat: float | None,
    stichtag: date,
    formel: ue.Formel = ue.BESTAETIGT,
) -> Vorschlag:
    """Den Soll-Zustand eines Entwurfs berechnen.

    Reine Funktion ohne Netz. ``stichtag`` ist der letzte Tag des
    Leistungsmonats — dasselbe Datum, das die Rechnung trägt.
    """
    v = Vorschlag(rechnung=rechnung, projekt=vertrag.bezeichnung)

    if not vertrag.stunden_pruefen:
        v.hinweise.append(
            "Für diesen Vertrag werden die Stunden nicht gegen Toggl gehalten."
        )
        return v
    if geleistet is None:
        v.hindernisse.append("Für dieses Projekt sind keine Toggl-Stunden bekannt.")
        return v

    if vertrag.fix_stunden is None:
        _nach_aufwand(v, deutung, geleistet)
    else:
        _pauschale(v, vertrag, deutung, geleistet, uebertrag_vormonat, stichtag, formel)
    return v


def _nach_aufwand(v: Vorschlag, deutung: dp.Positionsdeutung, geleistet: float) -> None:
    """Abrechnung nach Aufwand: die eine Stundenzeile trägt die Monatsstunden."""
    zeilen = deutung.stundenpositionen

    if geleistet <= TOLERANZ:
        v.hindernisse.append(
            "Toggl weist für diesen Monat keine Stunden aus — dann gehört auch "
            "keine Rechnung erzeugt. Zu prüfen ist der Entwurf, nicht die Menge."
        )
        return
    if not zeilen:
        v.hindernisse.append(
            "Der Entwurf hat keine Zeile mit Stundeneinheit. Eine anzulegen hiesse, "
            "den Stundensatz zu erfinden — das entscheidet der Mensch."
        )
        return
    if len(zeilen) > 1:
        v.hindernisse.append(
            f"{len(zeilen)} Stundenzeilen im Entwurf. Wie sich {geleistet:g}h darauf "
            "verteilen, steht nirgends — die Aufteilung bleibt von Hand."
        )
        return

    zeile = zeilen[0]
    ist = dp.betrag(zeile.get("amount"))
    if abs(ist - geleistet) <= TOLERANZ:
        v.hinweise.append(f"Die Stundenzeile steht bereits auf {geleistet:g}h.")
        return
    v.aenderungen.append(_mengenaenderung(
        zeile, geleistet,
        f"Toggl weist {geleistet:g}h aus, der Entwurf trägt {ist:g}h.",
    ))


def _pauschale(
    v: Vorschlag,
    vertrag: Vertragsdaten,
    deutung: dp.Positionsdeutung,
    geleistet: float,
    uebertrag_vormonat: float | None,
    stichtag: date,
    formel: ue.Formel,
) -> None:
    """Monatsabo: Zusatzstunden und Übertragssatz nachführen."""
    if vertrag.uebertragbar and uebertrag_vormonat is None:
        v.hindernisse.append(
            "Der Übertrag aus dem Vormonat ist nicht belegt; ohne ihn steht weder "
            "die Zusatzstundenzahl noch der neue Übertrag fest."
        )
        return

    ergebnis = ue.rechnen(
        fix_stunden=vertrag.fix_stunden or 0.0,
        geleistet=geleistet,
        uebertrag_vormonat=uebertrag_vormonat or 0.0,
        formel=formel,
    )

    _zusatzzeile(v, deutung, ergebnis.zusatz_stunden, vertrag, uebertrag_vormonat)

    if not vertrag.uebertragbar:
        return
    _uebertragszeile(v, deutung, ergebnis.neuer_uebertrag, stichtag)


def _zusatzzeile(
    v: Vorschlag,
    deutung: dp.Positionsdeutung,
    zusatz: float,
    vertrag: Vertragsdaten,
    uebertrag_vormonat: float | None,
) -> None:
    herleitung = (
        f"{vertrag.fix_stunden:g}h fix"
        + (f" plus {uebertrag_vormonat:g}h Übertrag" if uebertrag_vormonat else "")
    )
    zeile = deutung.zusatz_position

    if zeile is None:
        if zusatz > TOLERANZ:
            v.hindernisse.append(
                f"{zusatz:g}h liegen über der Kapazität ({herleitung}), aber der "
                "Entwurf hat keine Zeile «Variable Zusatzstunden». Sie anzulegen "
                "hiesse, den Stundensatz zu erfinden."
            )
        return

    ist = dp.betrag(zeile.get("amount"))
    if zusatz <= TOLERANZ:
        if ist <= TOLERANZ:
            return
        v.aenderungen.append(Aenderung(
            positionsart="custom",
            position_id=int(zeile["id"]),
            handlung="entfernen",
            feld="amount",
            alt=_menge(ist),
            neu="—",
            begruendung=(
                f"Die Kapazität ({herleitung}) deckt den Monat; es fallen keine "
                f"Zusatzstunden an, der Entwurf trägt aber {ist:g}h."
            ),
        ))
        return

    if abs(ist - zusatz) <= TOLERANZ:
        return
    v.aenderungen.append(_mengenaenderung(
        zeile, zusatz,
        f"Über die Kapazität ({herleitung}) hinaus fallen {zusatz:g}h an, "
        f"der Entwurf trägt {ist:g}h.",
    ))


def _uebertragszeile(
    v: Vorschlag, deutung: dp.Positionsdeutung, neuer: float, stichtag: date
) -> None:
    zeile = deutung.uebertrag_position
    datum = stichtag.strftime("%d.%m.%Y")

    if zeile is None:
        if neuer > TOLERANZ:
            v.hindernisse.append(
                f"{neuer:g}h gehören in den Folgemonat, aber der Entwurf trägt "
                "keinen Übertragssatz. Ihn anzulegen ändert, was die Kundschaft "
                "liest — das entscheidet der Mensch."
            )
        return

    if neuer <= TOLERANZ:
        v.aenderungen.append(Aenderung(
            positionsart="text",
            position_id=int(zeile["id"]),
            handlung="entfernen",
            feld="text",
            alt=str(zeile.get("text") or ""),
            neu="—",
            begruendung="Es bleibt nichts übertragen; der Satz gehört von der Rechnung.",
        ))
        return

    neu = UEBERTRAGSSATZ.format(datum=datum, stunden=_menge(neuer))
    alt = str(zeile.get("text") or "")
    if dp.klartext(alt) == dp.klartext(neu):
        return
    v.aenderungen.append(Aenderung(
        positionsart="text",
        position_id=int(zeile["id"]),
        handlung="aendern",
        feld="text",
        alt=alt,
        neu=neu,
        begruendung=f"{neuer:g}h gehen in den Folgemonat.",
        nutzdaten={"text": neu},
    ))
