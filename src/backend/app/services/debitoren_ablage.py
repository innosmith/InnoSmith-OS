"""Die versendete Rechnung ins Kundenarchiv legen.

## Was die Vorlage hier tat — und was davon bleibt

`D3_prepareInvoices.py` baute unter `_GENERATION/{Jahr}/{Monat}/sorted/` eine
Verzeichnisstruktur je Kunde und Jahr. Das sah aus wie eine Ablage, war aber
ein **Arbeitsordner**: sein einziger Zweck war, Rechnung und Leistungsrapport
nebeneinander zu legen, damit der Merge und der Mailentwurf das Paar überhaupt
wiederfinden — über Dateinamen, PDF-Text und Projektstichwörter. Ins eigentliche
Kundenarchiv wurde danach von Hand verschoben.

Der ganze Apparat entfällt hier, und zwar nicht weil er ersetzt wurde, sondern
weil die Frage nicht mehr entsteht: ``debitoren_versand`` erzeugt Rechnung und
Rapport aus demselben Abruf und führt sie sofort zusammen. Das Paar wird nie
getrennt, also muss es auch nie wiedergefunden werden.

Was bleibt, ist der Handgriff danach — und der ist neu.

## Wohin (gemessen am 21.09.2026, nicht angenommen)

``Finanzen/Debitoren/{Kunde}/{Jahr}/`` und, bei Kundschaft mit mehr als einem
aktiven Vertrag, darunter ``{Projekt}/``. Der Projektname ist die
Vertragsbezeichnung, und bei der BFH trägt sie den Durchgang: ``CAS IBD HS26``,
``CAS TCM FS26``. Abgelegt werden **zwei** Dateien:

| Datei | Inhalt |
|---|---|
| ``RE-00703 InnoSmith Rechnung Aug 2026.pdf`` | das verschmolzene Dokument |
| ``Leistungsrapport Aug 2026 OnboardingQS.pdf`` | der Rapport allein |

Dass die Rechnungsdatei bereits das verschmolzene Dokument ist, war nicht
offensichtlich und wurde nachgemessen: die archivierte ``RE-00703`` hat drei
Seiten, die letzte trägt «Leistungsrapport August 2026». Eine Rechnung ohne
Rapportpflicht liegt dagegen unverändert dort — BFH ``RE-00706`` ist mit
310 436 Bytes byteidentisch mit dem Bexio-Abruf.

## Die drei Regeln

**Nur nach bestätigtem Versand.** Das Archiv ist die Ablage dessen, was die
Kundschaft hat. Etwas dort abzulegen, das noch im Entwurfsordner liegt, macht
aus dem Archiv eine Absichtserklärung.

**Nie überschreiben.** ``conflictBehavior=fail`` bis hinunter zum Graph-Aufruf.
Eine überschriebene Rechnung ist nicht wiederherstellbar, und dass sie es war,
sieht man ihr nicht an. Eine schon vorhandene Datei ist deshalb kein Fehler,
sondern der Vermerk «lag schon da».

**Danach nachzählen.** Nach dem Schreiben wird gelesen und die Grösse
verglichen. Ein Upload, der mit 200 antwortet und nichts abgelegt hat, ist der
Fehler, den man erst im nächsten Steuerjahr bemerkt.

## Warum kein Rückfall auf den Schlüssel

``Kunde.ordner`` liefert ``None``, wenn kein Alias gepflegt ist, und dann wird
nicht abgelegt. Früher fiel der Wert auf den technischen Schlüssel zurück;
gemessen hätten elf von 25 aktiven Verträgen damit einen **neuen** Ordner
angelegt — ``bfh`` neben ``Berner Fachhochschule``. Eine Ablage, die einen
Ordner anlegt, sieht aus wie eine, die abgelegt hat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("taskpilot.debitoren.ablage")

WURZEL = "Finanzen/Debitoren"

MONAT_KURZ = {
    1: "Jan", 2: "Feb", 3: "Mär", 4: "Apr", 5: "Mai", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Okt", 11: "Nov", 12: "Dez",
}
"""Die Abkürzungen aus ``config.yml`` der Vorlage — «Mär», nicht «Mrz».

Sie stehen so in den Dateinamen von sechs Jahren Archiv. Eine andere
Abkürzung erzeugte keinen Fehler, sondern eine zweite Schreibweise
neben der ersten.
"""

# Zeichen, die OneDrive im Namen nicht zulaesst. Der Doppelpunkt fehlt hier
# nicht aus Versehen -- er steht in der Liste, weil er in Projektnamen
# vorkommt und Graph ihn stillschweigend ablehnt.
_VERBOTEN = '"*:<>?/\\|'


def sauber(name: str) -> str:
    """Einen Namensbestandteil für OneDrive entschärfen.

    Ersetzt wird durch ein Leerzeichen und nicht entfernt: «KI/ML» soll «KI ML»
    werden und nicht «KIML».
    """
    for zeichen in _VERBOTEN:
        name = name.replace(zeichen, " ")
    return " ".join(name.split()).strip(" .")


def rechnungsname(nummer: str, jahr: int, monat: int) -> str:
    """«RE-00703 InnoSmith Rechnung Aug 2026.pdf» — wie im Archiv seit 2019.

    Ohne Projektnamen, auch wenn dieselbe Kundschaft im selben Monat mehrere
    Rechnungen bekommt: die Rechnungsnummer unterscheidet sie, und sie ist das,
    wonach gesucht wird.
    """
    return f"{sauber(nummer)} InnoSmith Rechnung {MONAT_KURZ[monat]} {jahr}.pdf"


def rapportname(bezeichnung: str, jahr: int, monat: int) -> str:
    """«Leistungsrapport Aug 2026 OnboardingQS.pdf»."""
    return f"Leistungsrapport {MONAT_KURZ[monat]} {jahr} {sauber(bezeichnung)}.pdf"


def kundenordner(vertrag, *, jahr: int) -> str | None:
    """``Finanzen/Debitoren/{Kunde}/{Jahr}``. ``None`` heisst «nicht ablegen».

    Die Gegenpartei **desselben Jahres**, nicht die heutige: ``deinklima`` lief
    bis Ende 2025 über die Wyss Academy und seit 2026 über das AUE. Mit der
    heutigen Zuordnung landete eine Nachfakturierung für 2025 im falschen
    Ordner.
    """
    from datetime import date

    kunde = vertrag.kunde_am(date(jahr, 12, 31))
    if not kunde.ordner:
        return None
    return f"{WURZEL}/{sauber(kunde.ordner)}/{jahr}"


def geschwister_zahl(bestand, vertrag, *, jahr: int) -> int:
    """Wie viele aktive Verträge dieselbe Kundschaft in diesem Jahr hat.

    Gezählt werden die **aktiven**, nicht alle: MBA trägt ruhende Einträge aus
    früheren Jahren, und die dürfen keine Ebene erzwingen, die sonst nicht
    entstünde.
    """
    from datetime import date

    stichtag = date(jahr, 12, 31)
    kunde = vertrag.kunde_am(stichtag)
    return len({
        v.schluessel
        for v in bestand.aktive()
        if v.kunde_am(stichtag).schluessel == kunde.schluessel
    })


def zielordner(bestand, vertrag, *, jahr: int, projektebene: bool | None = None) -> str | None:
    """Wohin die Rechnung dieses Vertrags gehört. ``None`` heisst «nicht ablegen».

    ``projektebene`` entscheidet über die Unterebene. ``None`` bedeutet «allein
    aus den Stammdaten bestimmen»: mehr als ein aktiver Vertrag ergibt einen
    Projektordner. Die **Sperrklinke** (einmal Projektordner, immer
    Projektordner) lässt sich hier nicht sehen — sie steht im Archiv, nicht in
    der Vertragsdatei. Dafür ist ``projektebene_gilt`` zuständig.
    """
    pfad = kundenordner(vertrag, jahr=jahr)
    if pfad is None:
        return None
    if projektebene is None:
        projektebene = geschwister_zahl(bestand, vertrag, jahr=jahr) > 1
    if projektebene:
        pfad = f"{pfad}/{sauber(vertrag.bezeichnung)}"
    return pfad


async def projektebene_gilt(
    graph, bestand, vertrag, *, jahr: int, gedaechtnis: dict[str, bool] | None = None
) -> bool:
    """Ob die Rechnung in einen Projektordner gehört — mit Sperrklinke.

    Zwei Gründe, und der zweite steht nicht in den Stammdaten:

    1. **Mehr als ein aktiver Vertrag.** Dann ist die Ebene nötig, damit sich
       die Rechnungen nicht im Jahresordner vermischen. Das ist allein aus der
       Vertragsdatei entscheidbar.

    2. **Die Kundschaft führt schon Projektordner.** Läuft nur noch ein Vertrag,
       fällt die Ablage trotzdem **nicht** auf den Jahresordner zurück — sonst
       lägen bei MBA die Rechnungen eines Jahres teils in Unterordnern, teils
       daneben, je nachdem wie viele Verträge zu jenem Zeitpunkt liefen. Einmal
       Projektordner, immer Projektordner.

    Geprüft wird das laufende Jahr und, wenn es dort noch nichts gibt, das
    Vorjahr: im Januar ist der Jahresordner leer, und ohne den Blick zurück
    begänne jedes Jahr wieder flach.

    ``gedaechtnis`` hält die Antwort je Kundschaft für den Lauf fest — zwanzig
    Rechnungen sollen nicht zwanzig Abfragen erzeugen.
    """
    if geschwister_zahl(bestand, vertrag, jahr=jahr) > 1:
        return True

    basis = kundenordner(vertrag, jahr=jahr)
    if basis is None:
        return False
    kunde_pfad = basis.rsplit("/", 1)[0]

    if gedaechtnis is not None and kunde_pfad in gedaechtnis:
        return gedaechtnis[kunde_pfad]

    entschieden = False
    for pfad in (f"{kunde_pfad}/{jahr}", f"{kunde_pfad}/{jahr - 1}"):
        inhalt = await graph.list_drive_items(pfad, top=200)
        if any("folder" in i for i in inhalt):
            entschieden = True
            break
        if inhalt:
            # Das Jahr ist da und flach — damit ist die Frage beantwortet, und
            # der Blick ins Vorjahr wäre irreführend.
            break

    if gedaechtnis is not None:
        gedaechtnis[kunde_pfad] = entschieden
    return entschieden


@dataclass
class Abgelegt:
    """Was mit einer Datei geschah. ``lag_schon`` ist kein Fehlschlag."""

    pfad: str
    bytes_erwartet: int = 0
    bytes_abgelegt: int = 0
    lag_schon: bool = False
    hindernis: str = ""

    @property
    def gelungen(self) -> bool:
        return not self.hindernis


@dataclass
class Ablageergebnis:
    rechnung_id: int
    nummer: str
    ordner: str = ""
    dateien: list[Abgelegt] = field(default_factory=list)
    hindernis: str = ""

    @property
    def gelungen(self) -> bool:
        return not self.hindernis and all(d.gelungen for d in self.dateien)


async def _eine_datei(
    graph, ordner: str, name: str, inhalt: bytes
) -> Abgelegt:
    """Eine Datei schreiben, wenn sie fehlt — und nachzählen, dass sie da ist."""
    pfad = f"{ordner}/{name}"
    eintrag = Abgelegt(pfad=pfad, bytes_erwartet=len(inhalt))

    vorhanden = await graph.drive_item_by_path(pfad)
    if vorhanden is not None:
        eintrag.lag_schon = True
        eintrag.bytes_abgelegt = int(vorhanden.get("size") or 0)
        return eintrag

    await graph.upload_drive_file(pfad, inhalt)

    # Nachzählen statt der Antwort glauben. Ein Upload, der mit 200 antwortet
    # und nichts abgelegt hat, fiele sonst erst im nächsten Steuerjahr auf.
    nachher = await graph.drive_item_by_path(pfad)
    if nachher is None:
        eintrag.hindernis = "nach dem Hochladen nicht auffindbar"
        return eintrag
    eintrag.bytes_abgelegt = int(nachher.get("size") or 0)
    if eintrag.bytes_abgelegt != eintrag.bytes_erwartet:
        eintrag.hindernis = (
            f"abgelegt mit {eintrag.bytes_abgelegt} Bytes, "
            f"erwartet {eintrag.bytes_erwartet}"
        )
    return eintrag


async def ablegen(
    graph, beilagen, bestand, *, jahr: int, monat: int
) -> list[Ablageergebnis]:
    """Die versandfertigen Dokumente ins Kundenarchiv legen.

    ``beilagen`` sind ``debitoren_versand.Beilage``-Einträge; sie tragen das
    fertige Dokument und — wo vorhanden — den Rapport. Gebaut wird hier
    nichts: ein zweiter Bau könnte andere Stunden zeigen als das, was
    versendet wurde.

    Jede Rechnung wird einzeln gefangen. Ein Zeitausfall bei einer darf die
    anderen neunzehn nicht kosten.
    """
    ergebnisse: list[Ablageergebnis] = []
    gedaechtnis: dict[str, bool] = {}

    for beilage in beilagen:
        ergebnis = Ablageergebnis(
            rechnung_id=beilage.rechnung_id, nummer=beilage.nummer
        )
        vertrag = bestand.vertraege.get(beilage.vertrag_schluessel)
        if vertrag is None:
            ergebnis.hindernis = "Vertrag nicht mehr auffindbar"
            ergebnisse.append(ergebnis)
            continue

        if kundenordner(vertrag, jahr=jahr) is None:
            ergebnis.hindernis = (
                f"Für die Kundschaft «{vertrag.kunde.schluessel}» ist kein "
                "Ablageordner gepflegt — es würde sonst einer erfunden"
            )
            ergebnisse.append(ergebnis)
            continue

        # Die Sperrklinke braucht einen Blick ins Archiv. Scheitert der, wird
        # **nicht** flach abgelegt: das Raten in diese Richtung streut die
        # Rechnungen neben bestehende Projektordner, und dort fallen sie nicht
        # auf. Lieber diese eine Rechnung liegen lassen.
        try:
            ebene = await projektebene_gilt(
                graph, bestand, vertrag, jahr=jahr, gedaechtnis=gedaechtnis
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ebene zu %s unklar: %s", beilage.nummer, exc)
            ergebnis.hindernis = (
                "Archiv nicht lesbar, Projektebene unklar — nicht abgelegt "
                f"({type(exc).__name__}: {exc})"
            )
            ergebnisse.append(ergebnis)
            continue

        ordner = zielordner(bestand, vertrag, jahr=jahr, projektebene=ebene)
        assert ordner is not None  # oben geprüft
        ergebnis.ordner = ordner

        try:
            await graph.ensure_drive_folder(ordner)
            ergebnis.dateien.append(await _eine_datei(
                graph, ordner,
                rechnungsname(beilage.nummer, jahr, monat),
                beilage.dokument,
            ))
            if beilage.rapport:
                ergebnis.dateien.append(await _eine_datei(
                    graph, ordner,
                    rapportname(vertrag.bezeichnung, jahr, monat),
                    beilage.rapport,
                ))
        except Exception as exc:  # noqa: BLE001 - je Rechnung gefangen
            logger.warning("Ablage von %s gescheitert: %s", beilage.nummer, exc)
            ergebnis.hindernis = f"{type(exc).__name__}: {exc}"

        ergebnisse.append(ergebnis)

    return ergebnisse
