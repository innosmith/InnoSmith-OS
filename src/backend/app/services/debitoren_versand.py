"""Je Rechnung ein versandfertiges Dokument bauen.

Verkettet die vier Bausteine: Bexio-PDF holen, Leistungsrapport aus den
Buchungen setzen, beides zusammenführen, IBAN prüfen. Nichts davon rechnet —
gerechnet wurde in ``debitoren_pruefung`` und ``debitoren_vorschlag``.

## Was die Vorlage hier tat und warum es entfällt

`D3_prepareInvoices.py` baute die Zuordnung **aus dem erzeugten PDF zurück**:
es öffnete jede heruntergeladene Rechnung, nahm die erste Textzeile als
Kundennamen, normalisierte den Rest nach ASCII und suchte darin nach
Projektnamen und Stichwörtern. Danach legte es Verzeichnisse je Kunde und Jahr
an und suchte den passenden Leistungsrapport über den Dateinamen.

Dieser ganze Apparat — Unicode-Normalisierung, Stichwortlisten, «längster
Treffer gewinnt» auf einem PDF-Text, Verzeichnisse als Zwischenspeicher —
beantwortete eine Frage, die hier gar nicht entsteht. Kundschaft und Projekt
sind **bekannt**, bevor das PDF existiert: sie stehen an der Rechnung in Bexio
und am Vertrag in den Stammdaten. Aus einem Dokument zurückzuschliessen, was
man beim Erzeugen wusste, ist der Umweg, auf dem die Fehler sitzen.

Damit entfallen auch zwei Sonderbehandlungen der Vorlage: die Liste
``EXCLUDED_PROJECT_PREFIXES = ("UNKNOWN", "MS4")`` und ``skip_leistungsrapport``.
«UNKNOWN» war der Ersatzname für ein Projekt, das nicht aufgelöst werden
konnte — ein Zustand, den es hier nicht gibt. Und ob ein Vertrag einen Rapport
bekommt, steht als ``leistungsrapport`` am Vertrag.

## Die Feinheit, die leicht verloren geht

Der Rapport zeigt **nur verrechenbare** Buchungen (``billable`` in Toggl), die
Prüfung zählt **alle**. Das ist kein Widerspruch, sondern zwei verschiedene
Fragen: was der Kundschaft gezeigt wird, und was ein Fixvertrag verbraucht —
ein Fixvertrag verbraucht auch nicht verrechenbare Zeit. Stünde beides gleich,
wäre entweder der Rapport zu lang oder die Kapazitätsrechnung zu kurz, und
beides fiele nicht auf.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from app.services import debitoren_dokumente as dok
from app.services import leistungsrapport as lr

logger = logging.getLogger("taskpilot.debitoren.versand")


@dataclass
class Beilage:
    """Ein versandfertiges Dokument samt Herkunft."""

    rechnung_id: int
    nummer: str
    vertrag_schluessel: str
    bezeichnung: str
    dokument: bytes
    seiten: int
    mit_rapport: bool
    rapport: bytes | None = None
    """Der Leistungsrapport **allein**, für die Ablage ins Kundenarchiv.

    Im ``dokument`` steckt er bereits. Er steht hier trotzdem einzeln, weil das
    Archiv beide Formen führt: das verschmolzene Dokument unter dem
    Rechnungsnamen und den Rapport daneben. Ihn dort aus dem Dokument
    zurückzuschneiden hiesse, ein zweites Mal zu bauen, was man in der Hand hat.
    """
    rapport_stunden: float = 0.0
    """Die Summe **auf dem Rapport** — nur verrechenbare Buchungen. Weicht
    bewusst von der geprüften Stundenzahl ab, siehe Modulkopf."""
    rapport_zeilen: int = 0
    iban_praefix: str | None = None
    hindernisse: list[str] = field(default_factory=list)


@dataclass
class Ergebnis:
    beilagen: list[Beilage] = field(default_factory=list)
    uebergangen: list[dict] = field(default_factory=list)
    """Rechnungen, für die bewusst kein Dokument gebaut wurde — mit Grund."""
    fehler: list[str] = field(default_factory=list)
    """Rechnungen, bei denen es schiefging. Ein Fehlschlag bei einer Rechnung
    darf die anderen neunzehn nicht kosten."""


def _als_datum(wert: object) -> date | object:
    """ISO-Datum aus der Auffaltung in ein ``date`` — sonst steht
    ``2026-08-14`` auf dem Rapport statt ``14.08.2026``.

    Die Buchungen tragen das Datum als Zeichenkette, weil die Reports-API so
    antwortet. ``aufbereiten`` formatiert ein ``date`` schweizerisch und lässt
    alles andere unverändert durch. Ohne diese Umwandlung ginge das
    amerikanische Format an die Kundschaft.
    """
    if isinstance(wert, datetime):
        return wert.date()
    if isinstance(wert, date):
        return wert
    if isinstance(wert, str) and len(wert) >= 10:
        try:
            return date.fromisoformat(wert[:10])
        except ValueError:
            return wert
    return wert


def _buchungen_des_vertrags(
    buchungen: list[dict], bestand, schluessel: str
) -> list[dict]:
    """Die Buchungen, die auf diesen Vertrag zeigen — nur verrechenbare.

    Zugeordnet wird über denselben Weg wie in der Prüfung: der Toggl-Projektname
    geht durch ``bestand.finden``. Eine zweite Zuordnungsart hier wäre die
    Gelegenheit, dass Rapport und Prüfung verschiedene Stunden zeigen.
    """
    passend: list[dict] = []
    for b in buchungen:
        if not b.get("verrechenbar"):
            continue
        vertrag = bestand.finden(b.get("projekt") or "")
        if vertrag is not None and vertrag.schluessel == schluessel:
            passend.append(b)

    # Nach Datum sortiert — die Reihenfolge der Buchungen aus der Reports-API
    # folgt der Gruppierung und nicht dem Kalender. Auf dem Rapport wäre das
    # ein Dokument, in dem der 19. vor dem 3. steht.
    passend.sort(key=lambda b: (b.get("datum") or "", b.get("beginn") or ""))
    return passend


def rapport_bauen(
    buchungen: list[dict], bestand, schluessel: str, *, jahr: int, monat: int
) -> tuple[bytes | None, lr.Rapport | None]:
    """Den Leistungsrapport für einen Vertrag setzen — oder nichts.

    ``None`` heisst «keine verrechenbaren Stunden in diesem Monat». Ein leerer
    Rapport wäre schlechter als keiner: er behauptet, es sei nichts geleistet
    worden, und geht so an die Kundschaft.
    """
    passend = _buchungen_des_vertrags(buchungen, bestand, schluessel)
    if not passend:
        return None, None

    rapport = lr.aufbereiten(
        [
            {
                "datum": _als_datum(b.get("datum")),
                "beschreibung": b.get("beschreibung"),
                "dauer_stunden": b.get("stunden") or 0.0,
                "task_id": b.get("aufgabe_id"),
                "user_id": b.get("person_id"),
            }
            for b in passend
        ],
        jahr=jahr, monat=monat,
        bereiche={
            b["aufgabe_id"]: b["aufgabe"]
            for b in passend if b.get("aufgabe_id") and b.get("aufgabe")
        },
        personen={
            b["person_id"]: b["person"]
            for b in passend if b.get("person_id") and b.get("person")
        },
    )
    return lr.erzeugen(rapport), rapport


async def dokumente_bauen(
    bexio,
    *,
    entwuerfe: list[dict],
    buchungen: list[dict],
    bestand,
    jahr: int,
    monat: int,
    nur: set[int] | None = None,
) -> Ergebnis:
    """Für jeden Entwurf das versandfertige Dokument bauen.

    ``nur`` beschränkt auf einzelne Bexio-Kennungen — der Weg für eine einzelne
    Rechnung ausserhalb des Monatslaufs.

    **Jede Rechnung wird einzeln gefangen.** Ein unlesbares PDF oder ein
    Zeitausfall bei einer Rechnung darf die anderen neunzehn nicht kosten; im
    Protokoll steht dann eine Zeile und im Ergebnis neunzehn Dokumente.
    """
    erwartete_iban = (getattr(bestand, "vorgaben", None) or {}).get("iban_praefix")
    ergebnis = Ergebnis()

    for entwurf in entwuerfe:
        kennung = entwurf.get("rechnung_id")
        nummer = entwurf.get("nummer") or ""
        if nur is not None and kennung not in nur:
            continue

        vertrag = bestand.finden(entwurf.get("titel") or "")
        if vertrag is None:
            # Ohne Vertrag ist weder der Empfänger noch die Rapportpflicht
            # bekannt. Ein Dokument «auf Verdacht» wäre das Gegenteil von
            # hilfreich — es sähe fertig aus.
            ergebnis.uebergangen.append({
                "rechnung_id": kennung, "nummer": nummer,
                "titel": entwurf.get("titel"),
                "grund": "kein Vertrag hinterlegt — Empfänger und Rapportpflicht unbekannt",
            })
            continue

        try:
            rechnung_pdf = await bexio.get_invoice_pdf(int(kennung))
        except Exception as exc:  # noqa: BLE001 - je Rechnung gefangen
            logger.warning("PDF zu %s nicht abrufbar: %s", nummer, exc)
            ergebnis.fehler.append(
                f"{nummer}: Rechnungs-PDF nicht abrufbar ({type(exc).__name__}: {exc})"
            )
            continue

        rapport_pdf: bytes | None = None
        rapport: lr.Rapport | None = None
        if vertrag.leistungsrapport:
            try:
                rapport_pdf, rapport = rapport_bauen(
                    buchungen, bestand, vertrag.schluessel, jahr=jahr, monat=monat
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Rapport zu %s gescheitert: %s", nummer, exc)
                ergebnis.fehler.append(
                    f"{nummer}: Leistungsrapport nicht erzeugbar "
                    f"({type(exc).__name__}: {exc})"
                )
                continue

        try:
            fertig = dok.zusammenfuehren(
                rechnung_pdf, rapport_pdf, erwartete_iban=erwartete_iban
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Zusammenführen zu %s gescheitert: %s", nummer, exc)
            ergebnis.fehler.append(
                f"{nummer}: Dokument nicht zusammensetzbar "
                f"({type(exc).__name__}: {exc})"
            )
            continue

        hindernisse = list(fertig.hindernisse)
        if vertrag.leistungsrapport and rapport_pdf is None:
            # Der Vertrag verlangt einen Rapport, und es gibt keine
            # verrechenbaren Stunden. Das ist kein Fehler beim Bauen, sondern
            # eine Aussage über den Monat — und sie gehört vor die Augen des
            # Menschen, nicht in ein Protokoll.
            hindernisse.append(
                "Der Vertrag verlangt einen Leistungsrapport, im Monat sind "
                "aber keine verrechenbaren Stunden erfasst."
            )

        ergebnis.beilagen.append(Beilage(
            rechnung_id=int(kennung),
            nummer=nummer,
            vertrag_schluessel=vertrag.schluessel,
            bezeichnung=vertrag.bezeichnung,
            dokument=fertig.inhalt,
            seiten=fertig.seiten,
            mit_rapport=rapport_pdf is not None,
            rapport=rapport_pdf,
            rapport_stunden=rapport.summe if rapport else 0.0,
            rapport_zeilen=len(rapport.zeilen) if rapport else 0,
            iban_praefix=fertig.iban_praefix,
            hindernisse=hindernisse,
        ))

    return ergebnis


@dataclass
class MailVermerk:
    """Was aus einer Beilage im Postfach geworden ist — oder warum nicht."""

    rechnung_id: int
    nummer: str
    bezeichnung: str
    mailentwurf_id: str = ""
    hindernis: str = ""


@dataclass
class MailErgebnis:
    angelegt: list[MailVermerk] = field(default_factory=list)
    uebergangen: list[MailVermerk] = field(default_factory=list)
    fehler: list[MailVermerk] = field(default_factory=list)


async def mails_anlegen(
    graph, beilagen: list[Beilage], bestand, *, jahr: int, monat: int
) -> MailErgebnis:
    """Zu jeder Beilage einen Mailentwurf anlegen.

    **Das Dokument wird nicht erneut gebaut.** Wer diese Funktion aufruft,
    hat es gerade in der Hand — ein zweiter Bau in der Zwischenzeit könnte
    andere Stunden zeigen als der Anhang.

    Ein Fehlschlag bei einer Rechnung lässt die übrigen weiterlaufen. Der
    Rückbau eines angefangenen Entwurfs ohne Anhang sitzt in
    ``entwurf_anlegen`` und gilt hier mit.
    """
    from app.services import debitoren_mail as mail

    ergebnis = MailErgebnis()
    for beilage in beilagen:
        vertrag = bestand.vertraege.get(beilage.vertrag_schluessel)
        if vertrag is None:
            ergebnis.uebergangen.append(MailVermerk(
                rechnung_id=beilage.rechnung_id, nummer=beilage.nummer,
                bezeichnung=beilage.bezeichnung,
                hindernis="Vertrag zwischenzeitlich nicht mehr auffindbar",
            ))
            continue
        try:
            kennung = await mail.entwurf_anlegen(
                graph, vertrag=vertrag, nummer=beilage.nummer,
                jahr=jahr, monat=monat, dokument=beilage.dokument,
            )
        except Exception as exc:  # noqa: BLE001 - eine Mail darf den Lauf nicht reissen
            logger.warning("Mailentwurf zu %s gescheitert: %s", beilage.nummer, exc)
            ergebnis.fehler.append(MailVermerk(
                rechnung_id=beilage.rechnung_id, nummer=beilage.nummer,
                bezeichnung=beilage.bezeichnung,
                hindernis=f"{type(exc).__name__}: {exc}",
            ))
            continue
        ergebnis.angelegt.append(MailVermerk(
            rechnung_id=beilage.rechnung_id, nummer=beilage.nummer,
            bezeichnung=beilage.bezeichnung, mailentwurf_id=kennung,
        ))
    return ergebnis
