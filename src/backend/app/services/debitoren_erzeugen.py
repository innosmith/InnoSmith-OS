"""Entwürfe aus Bexio-Aufträgen erzeugen und richtig datieren.

Der einzige Schritt des Rechnungslaufs, der in Bexio etwas **anlegt**. Bisher
lief er von Hand über «Aufträge → Rechnungen generieren» in der Oberfläche.

## Warum das Datum der eigentliche Gegenstand ist

Die Erzeugung selbst ist ein einzelner Aufruf. Das Heikle folgt danach: Bexio
datiert die neue Rechnung auf **heute**, und der Lauf findet am ersten oder
zweiten Tag des Folgemonats statt. Ohne Korrektur trüge jede Rechnung des
Septembers ein Oktoberdatum — die Umsatzabgrenzung wäre um einen Monat
verschoben, und zwar bei allen Rechnungen gleichzeitig und ohne Fehlermeldung.
Von 182 Rechnungen seit 2025 tragen 178 den Monatsletzten; das ist die Regel
des Hauses und keine Beobachtung.

## Die Zahlungsfrist wird übernommen, nicht berechnet

Naheliegend wäre, das Fälligkeitsdatum als «Rechnungsdatum plus 29 Tage» zu
setzen — so steht es in 175 der 182 Rechnungen. Genau das wäre der Fehler: eine
am Bestand abgelesene Konstante, die überall dort danebenliegt, wo eine andere
Zahlungsfrist vereinbart ist. Sechs Rechnungen weichen bereits ab.

Stattdessen wird die Frist **gemessen, die Bexio beim Erzeugen selbst gewählt
hat**, und beide Daten um denselben Betrag verschoben. Damit stammt die Frist
aus der Zahlungsbedingung des Kontakts und nicht aus einer Zahl in diesem
Modul. Lässt sich die Frist nicht lesen, wird sie nicht erfunden: das
Rechnungsdatum wird gesetzt, die Fälligkeit bleibt stehen, und der Fall kommt
als Hindernis in das Protokoll.

## Was dieses Modul nicht tut

Es entscheidet **nicht**, für welche Aufträge eine Rechnung fällig ist. Diese
Frage beantwortet ``debitoren_lauf.auftragslage`` anhand von Vertrag, Takt und
Periode. Hier steht nur die Ausführung — getrennt, damit die Entscheidung ohne
Netz prüfbar bleibt und die Ausführung ohne Fachwissen.

Es stellt auch **nichts aus**. Die erzeugte Rechnung ist ein Entwurf und bleibt
es, bis sie geprüft, versendet und von Hand auf «ausgestellt» gesetzt wird.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger("taskpilot.debitoren.erzeugen")


@dataclass
class Erzeugt:
    """Was für einen Auftrag geschehen ist. Genau eines von beidem trifft zu."""

    auftrag: str
    titel: str = ""
    rechnung_id: int | None = None
    nummer: str = ""
    datum: str | None = None
    faellig: str | None = None
    hindernis: str = ""
    """Warum nichts oder nur die Hälfte geschah. Leer heisst: vollständig."""

    @property
    def gelungen(self) -> bool:
        return self.rechnung_id is not None and not self.hindernis


@dataclass
class Protokoll:
    """Das Ergebnis eines Erzeugungslaufs."""

    stichtag: date
    eintraege: list[Erzeugt] = field(default_factory=list)

    @property
    def erzeugt(self) -> int:
        return sum(1 for e in self.eintraege if e.rechnung_id is not None)

    @property
    def gescheitert(self) -> int:
        return sum(1 for e in self.eintraege if e.rechnung_id is None)


def _tag(wert: object) -> date | None:
    try:
        return date.fromisoformat(str(wert)[:10])
    except (TypeError, ValueError):
        return None


async def rechnungsdatum_setzen(bexio, rechnung: dict, stichtag: date) -> tuple[dict, str]:
    """Eine Rechnung auf den Stichtag umdatieren. Liefert (Rechnung, Hindernis).

    Verschoben werden Rechnungs- und Fälligkeitsdatum um **denselben** Betrag,
    damit die Zahlungsfrist erhalten bleibt, die Bexio aus der
    Zahlungsbedingung des Kontakts abgeleitet hat.

    Steht das Datum schon richtig, wird nicht geschrieben — ein Aufruf ohne
    Wirkung ist ein Eintrag im Audit-Log, der eine Änderung vortäuscht.
    """
    rechnung_id = rechnung.get("id")
    von = _tag(rechnung.get("is_valid_from"))
    bis = _tag(rechnung.get("is_valid_to"))

    if rechnung_id is None:
        return rechnung, "Die erzeugte Rechnung trägt keine Kennung."
    if von == stichtag:
        return rechnung, ""

    felder: dict[str, str] = {"is_valid_from": stichtag.isoformat()}
    hindernis = ""
    if von is not None and bis is not None:
        felder["is_valid_to"] = (stichtag + (bis - von)).isoformat()
    else:
        hindernis = (
            f"Die Zahlungsfrist liess sich nicht lesen (von {rechnung.get('is_valid_from')!r} "
            f"bis {rechnung.get('is_valid_to')!r}); das Fälligkeitsdatum steht noch auf dem "
            "alten Wert und ist zu prüfen."
        )

    aktualisiert = await bexio.update_invoice(rechnung_id, **felder)
    return (aktualisiert or rechnung), hindernis


async def erzeugen(
    bexio,
    auftraege: list[tuple[int, str]],
    *,
    stichtag: date,
) -> Protokoll:
    """Für jeden Auftrag einen datierten Entwurf anlegen.

    ``auftraege`` sind Paare aus Bexio-Auftragskennung und Beschriftung; die
    Beschriftung dient allein dem Protokoll.

    Ein Fehlschlag bleibt **örtlich**: schlägt ein Auftrag fehl, laufen die
    übrigen weiter. Die Alternative wäre ein halb erzeugter Lauf, bei dem
    niemand weiss, welche Rechnungen schon stehen — und der zweite Versuch
    erzeugte die ersten ein zweites Mal.
    """
    protokoll = Protokoll(stichtag=stichtag)

    for auftrag_id, titel in auftraege:
        eintrag = Erzeugt(auftrag=str(auftrag_id), titel=titel)
        try:
            rechnung = await bexio.create_invoice_from_order(auftrag_id)
        except Exception as exc:  # noqa: BLE001 -- Teilausfall ist eingeplant
            logger.warning("Auftrag %s: Rechnung nicht erzeugt: %s", auftrag_id, exc)
            eintrag.hindernis = f"Bexio lehnte die Erzeugung ab: {type(exc).__name__}: {exc}"
            protokoll.eintraege.append(eintrag)
            continue

        eintrag.rechnung_id = rechnung.get("id")
        eintrag.nummer = rechnung.get("document_nr") or ""

        # Ab hier steht die Rechnung bereits in Bexio. Ein Fehlschlag beim
        # Umdatieren darf sie deshalb nicht verschweigen -- sonst legte der
        # nächste Lauf eine zweite an.
        try:
            rechnung, hindernis = await rechnungsdatum_setzen(bexio, rechnung, stichtag)
            eintrag.hindernis = hindernis
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rechnung %s: Datum nicht gesetzt: %s", eintrag.nummer, exc)
            eintrag.hindernis = (
                f"Die Rechnung wurde erzeugt, trägt aber noch das Datum von Bexio "
                f"({rechnung.get('is_valid_from')}) — Umdatieren schlug fehl: "
                f"{type(exc).__name__}: {exc}"
            )

        eintrag.datum = rechnung.get("is_valid_from")
        eintrag.faellig = rechnung.get("is_valid_to")
        protokoll.eintraege.append(eintrag)

    return protokoll
