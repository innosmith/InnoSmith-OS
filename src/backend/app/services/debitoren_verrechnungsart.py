"""Die Verrechnungsart in Toggl nachtragen — nach bestätigtem Versand.

## Warum erst danach

Toggl ist im Rechnungslauf die **Beweisgrundlage**. Solange eine Rechnung noch
im Entwurf oder im Postfach liegt, muss jederzeit auf unveränderte Zeitdaten
zurückgegriffen werden können: geht im Lauf etwas schief, ist Toggl die
Instanz, die sagt, was tatsächlich geleistet wurde. Wer die Tags vorher setzt,
hat die Beweisgrundlage angefasst, bevor er sie gebraucht hat.

Darum ist ``versendet_am`` die Schranke, und sie liegt im Laufbuch — nicht in
einer Reihenfolge, die man einhalten müsste, sondern als Bedingung, die der
Endpunkt prüft.

## Was hier eine Entscheidung ist und was nicht

Welche Art gilt, ist **deklariert** und wird nicht erschlossen: sie steht am
Vertrag, erbt von der Kundschaft und zuletzt von der Vorgabe
(``docs/debitorenvertraege.yaml``). Abgelesen wurde sie aus Januar bis August
2026 — dort trägt kein Projekt zwei verschiedene Arten, die Zuordnung ist also
eine geschlossene Menge und gehört deklariert.

Drei Dinge werden ausdrücklich **nicht** entschieden:

* **Ein vorhandener, anderer Tag wird nie überschrieben.** Er ist eine
  Menschenentscheidung; ihn zu ersetzen hiesse, ein Urteil zu überstimmen. Er
  wird gemeldet, und zwar mit beiden Werten.
* **Ein Projekt ohne Zuordnung wird nicht getaggt.** Weder Vertrag noch
  ``interne_projekte`` — dann ist es ein Stammdatenmangel und keine Tatsache.
* **Ein unbekannter Tagname wird nicht angelegt.** Geschrieben wird über
  Kennungen; einen Namen zu schicken, den Toggl nicht kennt, legte dort
  stillschweigend einen neuen Tag an.

## Nachgelesen wird, nicht gehofft

Der Schreibweg ist ein ``PUT`` auf den Zeiteintrag. Ein ``PUT``, der mehr
ändert als das Tag, wäre ein stiller Datenverlust — Beschreibung weg, Dauer
auf null, und auffallen würde es erst beim nächsten Leistungsrapport. Deshalb
wird die Antwort des Aufrufs gegen den Zustand davor gehalten: Beginn,
Beschreibung, Projekt, Dauer. Das kostet keinen zusätzlichen Aufruf, weil
Toggl den geänderten Eintrag zurückgibt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.debitorenvertraege import Bestand, VERRECHNUNGSARTEN

logger = logging.getLogger("taskpilot.debitoren.verrechnungsart")


def _vorhandene(buchung: dict) -> tuple[str, ...]:
    """Die Tags eines Zeiteintrags als Menge von Namen.

    ``auffalten`` liefert sie als einen mit ``", "`` verbundenen Text, und hier
    wird er wieder zerlegt. Das ist zulässig, weil keiner der vier erklärten
    Namen ein Komma enthält — ``test_kein_name_enthaelt_ein_komma`` hält diese
    Bedingung fest, statt sie anzunehmen.
    """
    text = (buchung.get("verrechnungsart") or "").strip()
    if not text:
        return ()
    return tuple(t.strip() for t in text.split(",") if t.strip())


@dataclass(frozen=True)
class Vorschlag:
    """Ein Zeiteintrag und die Art, die er tragen soll."""

    eintrag_id: int
    datum: str
    projekt: str
    beschreibung: str
    stunden: float
    soll: str
    vorhanden: tuple[str, ...]
    grund: str
    """Woher die Art kommt — «Vertrag cheetah» oder «internes Projekt»."""


@dataclass
class Plan:
    """Was zu tun ist, was schon stimmt und was nicht entscheidbar ist."""

    zu_setzen: list[Vorschlag] = field(default_factory=list)
    schon_richtig: list[Vorschlag] = field(default_factory=list)
    fremd: list[Vorschlag] = field(default_factory=list)
    """Trägt schon eine **andere** Art. Wird gemeldet, nicht überschrieben."""
    ohne_zuordnung: list[Vorschlag] = field(default_factory=list)
    """Projekt ist weder Vertrag noch als intern deklariert."""

    @property
    def offen(self) -> int:
        return len(self.zu_setzen)

    def stunden(self, welche: list[Vorschlag]) -> float:
        return round(sum(v.stunden for v in welche), 2)


def planen(
    buchungen: list[dict],
    bestand: Bestand,
    *,
    vertraege: set[str] | None = None,
    intern: bool = False,
) -> Plan:
    """Aus den Zeiteinträgen eines Monats den Plan bauen.

    ``vertraege`` wählt nach Vertragsschlüssel aus — so trägt ein Aufruf genau
    die Stunden nach, deren Rechnung bestätigt versendet ist. ``intern``
    nimmt zusätzlich die eigenen Projekte hinzu; sie hängen an keiner Rechnung
    und kommen darum erst, wenn der Monatslauf durch ist.

    Ohne beides ist der Plan leer. Das ist Absicht: «alles» wäre die Voreinstellung,
    bei der ein Fehlgriff den ganzen Monat betaggt.
    """
    plan = Plan()
    intern_namen = {" ".join(n.split()).casefold() for n in bestand.interne_projekte}

    for buchung in buchungen:
        kennung = buchung.get("eintrag_id")
        if kennung is None:
            continue
        projekt = buchung.get("projekt") or ""
        vertrag = bestand.finden(projekt) if projekt else None
        ist_intern = " ".join(projekt.split()).casefold() in intern_namen

        if vertrag is not None and ist_intern:
            # Beides zugleich ist ein Widerspruch in den Stammdaten. Er wird
            # beim Laden gemeldet; hier wird nicht geraten, welche Seite gilt.
            soll, grund = "", "Projekt ist Vertrag und intern zugleich"
        elif vertrag is not None:
            soll = vertrag.verrechnungsart
            grund = f"Vertrag {vertrag.schluessel}"
        elif ist_intern:
            soll = "keine Verrechnung"
            grund = "internes Projekt"
        else:
            soll, grund = "", "kein Vertrag und nicht als intern deklariert"

        vorschlag = Vorschlag(
            eintrag_id=int(kennung),
            datum=buchung.get("datum") or "",
            projekt=projekt,
            beschreibung=buchung.get("beschreibung") or "",
            stunden=float(buchung.get("stunden") or 0.0),
            soll=soll,
            vorhanden=_vorhandene(buchung),
            grund=grund,
        )

        if not soll or soll not in VERRECHNUNGSARTEN:
            plan.ohne_zuordnung.append(vorschlag)
            continue

        # Die Auswahl greift erst hier: ein Projekt ohne Zuordnung soll auch
        # dann auffallen, wenn seine Rechnung gar nicht Gegenstand des Aufrufs
        # ist. Sonst verschwiege eine Einzelauswahl den Stammdatenmangel.
        if vertrag is not None:
            if vertraege is None or vertrag.schluessel not in vertraege:
                continue
        elif not intern:
            continue

        if soll in vorschlag.vorhanden:
            plan.schon_richtig.append(vorschlag)
        elif vorschlag.vorhanden:
            plan.fremd.append(vorschlag)
        else:
            plan.zu_setzen.append(vorschlag)

    return plan


# ── Schreiben ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gesetzt:
    """Ein einzelner Schreibversuch und sein Ausgang."""

    eintrag_id: int
    projekt: str
    datum: str
    art: str
    gelungen: bool = False
    hindernis: str = ""


def _abweichung(vorher: Vorschlag, nachher: dict, art: str) -> str:
    """Was der ``PUT`` ausser dem Tag verändert hat. Leer heisst «nichts».

    Geprüft wird gegen den Zustand, der aus demselben Abruf stammt, gegen den
    auch die Rechnung geprüft wurde. Eine Abweichung ist ein Datenverlust und
    kein Schönheitsfehler — sie muss den Lauf anhalten, bevor sie sich über
    achtzig Einträge wiederholt.
    """
    if not isinstance(nachher, dict) or not nachher:
        return "Toggl hat den geänderten Eintrag nicht zurückgegeben"

    namen = {t for t in (nachher.get("tags") or []) if isinstance(t, str)}
    if art not in namen:
        return f"Tag «{art}» steht danach nicht am Eintrag (Tags: {sorted(namen)})"

    if (dauer := nachher.get("duration")) is not None and int(dauer) <= 0:
        return f"Dauer ist nach dem Schreiben {dauer} — der Eintrag wäre entwertet"

    beschreibung = nachher.get("description")
    if beschreibung is not None and (beschreibung or "") != vorher.beschreibung:
        return (
            f"Beschreibung geändert: {vorher.beschreibung!r:.40} → "
            f"{beschreibung!r:.40}"
        )

    return ""


async def setzen(
    toggl,
    workspace: int | None,
    plan: Plan,
    *,
    tags: list[dict],
) -> list[Gesetzt]:
    """Die geplanten Tags schreiben, einen Eintrag nach dem anderen.

    Ein Fehlschlag betrifft einen Eintrag und hält den Lauf nicht auf — das ist
    die übliche Fehlerisolation. **Eine Abweichung ausserhalb des Tags dagegen
    hält an**: sie bedeutet, dass der Schreibweg mehr verändert als gedacht,
    und dann wäre jeder weitere Aufruf ein weiterer Verlust.
    """
    kennungen = {
        (t.get("name") or ""): t.get("id")
        for t in tags
        if t.get("id") is not None
    }

    ergebnisse: list[Gesetzt] = []
    for vorschlag in plan.zu_setzen:
        tag_id = kennungen.get(vorschlag.soll)
        if tag_id is None:
            ergebnisse.append(Gesetzt(
                eintrag_id=vorschlag.eintrag_id, projekt=vorschlag.projekt,
                datum=vorschlag.datum, art=vorschlag.soll,
                hindernis=(
                    f"Tag «{vorschlag.soll}» gibt es im Workspace nicht — "
                    "er wird nicht angelegt"
                ),
            ))
            continue

        try:
            antwort = await toggl.add_time_entry_tag(
                vorschlag.eintrag_id, int(tag_id), workspace
            )
        except Exception as exc:  # noqa: BLE001 -- ein Eintrag, nicht der Lauf
            logger.warning(
                "Verrechnungsart: Eintrag %s nicht gesetzt: %s",
                vorschlag.eintrag_id, exc,
            )
            ergebnisse.append(Gesetzt(
                eintrag_id=vorschlag.eintrag_id, projekt=vorschlag.projekt,
                datum=vorschlag.datum, art=vorschlag.soll,
                hindernis=f"{type(exc).__name__}: {exc}",
            ))
            continue

        if abweichung := _abweichung(vorschlag, antwort, vorschlag.soll):
            ergebnisse.append(Gesetzt(
                eintrag_id=vorschlag.eintrag_id, projekt=vorschlag.projekt,
                datum=vorschlag.datum, art=vorschlag.soll,
                hindernis=abweichung,
            ))
            logger.error(
                "Verrechnungsart: Schreibweg verändert mehr als das Tag (%s) — "
                "abgebrochen nach %d Einträgen",
                abweichung, len(ergebnisse),
            )
            break

        ergebnisse.append(Gesetzt(
            eintrag_id=vorschlag.eintrag_id, projekt=vorschlag.projekt,
            datum=vorschlag.datum, art=vorschlag.soll, gelungen=True,
        ))

    return ergebnisse
