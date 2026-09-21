"""Den Rechnungslauf zusammentragen — Entwürfe, Positionen und Stunden.

Die einzige Stelle, die für einen Leistungsmonat alles einsammelt und durch
``debitoren_pruefung.pruefen`` schickt. Getrennt in zwei Hälften, damit das
Urteil prüfbar bleibt: ``abrufen`` spricht mit Bexio und Toggl und tut sonst
nichts, ``auswerten`` ist eine reine Funktion ohne Netz.

## Warum hier live gelesen wird und nicht aus dem Datenraum

Der Datenraum trägt, was **analytisch** ausgewertet wird — Umsatz, offene Posten,
Stunden je Kunde. Rechnungspositionen gehören nicht dazu; sie sind ein operatives
Detail eines Entwurfs, der in wenigen Tagen verschwindet.

Der zweite Grund wiegt schwerer: Bexio und Toggl gleichen stündlich ab. Wer
während des Laufs eine Position in Bexio korrigiert oder eine Zeitbuchung in
Toggl nachträgt, müsste bis zu einer Stunde warten, bis die Prüfung es sieht —
und würde in der Zwischenzeit ein Urteil über einen Zustand lesen, den es nicht
mehr gibt. Für eine Ansicht, die Rechnungen freigibt, ist das die falsche
Reihenfolge.

Dass beides nebeneinander bestehen darf, ist im Haus schon entschieden:
``GET /api/finance/validate`` hält Datenraumzahlen gegen dieselbe Zahl live. Die
Grenze ist nicht die Datenquelle, sondern die Frage. Was **nicht** zweimal
existieren darf, ist die Deutung — Beträge über ``bexio.rechnungen``, Stunden
über ``toggl.zeiteintraege``, Positionen über ``debitoren_positionen``.

## Zwei Fallen, die beim Bauen auffielen

**Nicht nach dem Feld filtern, das geprüft wird.** Die Entwürfe liessen sich mit
``search_invoices(from_date=…, to_date=…)`` auf den Monat einschränken. Gefiltert
würde dabei über ``is_valid_from`` — also über genau das Rechnungsdatum, dessen
Richtigkeit die Regel ``rechnungsdatum`` feststellen soll. Eine Rechnung mit
falschem Datum fiele aus dem Filter und damit aus der Prüfung: der Fehler
versteckt sich hinter der Abfrage, die ihn finden sollte. Deshalb werden **alle**
Entwürfe geholt und die ausserhalb der Periode getrennt ausgewiesen, statt sie
wegzulassen.

**Der Steuersatz steht nicht als Satz in der Antwort.** Bexio liefert Netto und
Steuerbetrag; der Satz wird daraus hergeleitet. Bei einem Nettobetrag von null
ist er nicht herleitbar — dann bleibt er ``None`` und die Regel meldet «offen»
statt einer Null, die wie ein steuerfreier Umsatz aussähe.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "bexio"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "toggl"))

from app.services import debitoren_positionen as dp  # noqa: E402
from app.services import debitoren_pruefung as pr  # noqa: E402
from app.services import debitoren_uebertrag as ue  # noqa: E402
from app.services import debitoren_vorschlag as vs  # noqa: E402
from app.services.debitorenvertraege import Bestand  # noqa: E402

logger = logging.getLogger("taskpilot.debitoren.lauf")

GLEICHZEITIG = 4
"""Wie viele Positionsabrufe parallel laufen. Bei rund zwanzig Entwürfen ist das
der Unterschied zwischen zwei Sekunden und zehn, ohne Bexio zu überfahren."""


# ── Rohdaten ─────────────────────────────────────────────


@dataclass(frozen=True)
class Rohdaten:
    """Was für einen Monat bei den Fachsystemen geholt wurde."""

    entwuerfe: list[dict] = field(default_factory=list)
    """Normalisierte Rechnungszeilen, **alle** Entwürfe ohne Datumsfilter."""
    positionen: dict[int, list[dict]] = field(default_factory=dict)
    stunden: dict[int, dict] = field(default_factory=dict)
    """Toggl-Projektkennung auf verdichtete Stunden."""
    buchungen: list[dict] = field(default_factory=list)
    """Die einzelnen Zeiteinträge, nicht verdichtet.

    Die Prüfung rechnet mit ``stunden``; der **Leistungsrapport** braucht jede
    Zeile einzeln — Datum, Beschreibung, Person, Aufgabe. Beides aus demselben
    Abruf, weil ein zweiter Abruf zu einem anderen Zeitpunkt eine andere
    Stundenzahl liefern könnte als die, gegen die geprüft wurde.
    """
    auftraege: list[Any] = field(default_factory=list)
    """Bexio-Aufträge als ``bexio.auftraege.Auftrag``. Die dritte Blickrichtung
    auf dieselbe Lücke — siehe ``auftragslage``."""
    rechnungen_der_periode: list[dict] = field(default_factory=list)
    """Alle Rechnungen des Monats, nicht nur die Entwürfe. Für die Auftragslücke
    nötig: eine bereits gestellte Rechnung schliesst die Lücke genauso wie ein
    Entwurf, taucht in ``entwuerfe`` aber nicht auf."""
    fehler: list[str] = field(default_factory=list)
    """Was nicht geholt werden konnte. Ein Teilausfall bricht den Lauf nicht ab,
    darf aber nicht als «nichts gefunden» durchgehen."""


def periodengrenzen(jahr: int, monat: int) -> tuple[date, date]:
    return date(jahr, monat, 1), date(jahr, monat, calendar.monthrange(jahr, monat)[1])


async def abrufen(
    bexio,
    toggl,
    *,
    jahr: int,
    monat: int,
    workspace: int | None = None,
    status: str = "entwurf",
) -> Rohdaten:
    """Entwürfe, deren Positionen und die Monatsstunden holen.

    Die einzige Hälfte mit Netzzugriff. Fehlschläge werden gesammelt und
    gemeldet, nicht geworfen: eine unerreichbare Quelle darf den Lauf nicht
    reissen, aber sie darf auch nicht wie eine leere Antwort aussehen.

    ``status`` ist im Betrieb immer ``entwurf`` — geprüft wird, was noch nicht
    beim Kunden ist. Der Rücklauf gegen abgeschlossene Monate braucht dieselbe
    Mechanik für ``offen`` und ``bezahlt``, und eine zweite Abruffassung dafür
    wäre die schlechtere Antwort: sie würde mit der ersten auseinanderlaufen,
    und dann prüfte der Rücklauf etwas anderes als der Lauf.
    """
    from auftraege import auftragszeilen
    from rechnungen import betraege, kontaktnamen
    from zeiteintraege import auffalten, je_projekt

    von, bis = periodengrenzen(jahr, monat)
    fehler: list[str] = []
    alle_rechnungen: list[dict] = []

    # ── Entwürfe, bewusst ohne Datumsfilter (siehe Modulkopf) ──
    entwuerfe: list[dict] = []
    positionen: dict[int, list[dict]] = {}
    try:
        roh = await bexio.search_invoices(status=status)
        namen = await kontaktnamen(bexio)
        for r in roh:
            kunden_id = r.get("contact_id")
            netto, steuer, brutto = betraege(r)
            entwuerfe.append({
                "rechnung_id": r.get("id"),
                "nummer": r.get("document_nr") or "",
                "datum": r.get("is_valid_from") or None,
                "kunden_id": kunden_id,
                "kunde": namen.get(int(kunden_id)) if kunden_id is not None else None,
                "titel": r.get("title") or "",
                "netto": netto,
                "steuer": steuer,
                "brutto": brutto,
                "referenz": r.get("api_reference") or r.get("reference") or None,
            })
    except Exception as exc:  # noqa: BLE001 -- Teilausfall ist eingeplant
        logger.warning("Rechnungslauf: Entwürfe nicht abrufbar: %s", exc)
        fehler.append(f"Bexio-Entwürfe: {type(exc).__name__}: {exc}")

    if entwuerfe:
        positionen, positionsfehler = await _positionen_holen(bexio, entwuerfe)
        fehler.extend(positionsfehler)

    # ── Aufträge und alle Rechnungen des Monats (für die Auftragslücke) ──
    #
    # Hier **wird** nach ``is_valid_from`` gefiltert, anders als bei den
    # Entwürfen. Der Unterschied ist die Frage: oben soll ein falsches
    # Rechnungsdatum gefunden werden, hier lautet die Frage «trägt dieser Monat
    # eine Rechnung» — und dafür ist genau dieses Feld die Antwort.
    auftragszeilen_: list = []
    try:
        auftragszeilen_, auftragsbefund = auftragszeilen(await bexio.alle_auftraege())
        if auftragsbefund.get("unbekannte_status"):
            fehler.append(
                "Bexio-Aufträge mit unbekanntem Status: "
                f"{auftragsbefund['unbekannte_status']}"
            )
        alle_rechnungen = await bexio.search_invoices(
            from_date=von.isoformat(), to_date=bis.isoformat()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rechnungslauf: Aufträge nicht abrufbar: %s", exc)
        fehler.append(f"Bexio-Aufträge: {type(exc).__name__}: {exc}")

    # ── Stunden des Monats ──
    #
    # Beide Behälter **vor** dem Versuch: ein Toggl-Ausfall soll die Stunden
    # kosten und nicht den Lauf. Ohne die Vorbelegung wäre ``buchungen`` im
    # Fehlerfall ungebunden, und aus einer gemeldeten Teilstörung würde ein
    # Abbruch — genau das, was der Sammelfehler verhindern soll.
    stunden: dict[int, dict] = {}
    buchungen: list[dict] = []
    try:
        projekte = await toggl.list_projects(active="both")
        kunden = await toggl.list_clients(status="both")
        tags = await toggl.list_tags(workspace)
        gruppen = await toggl.search_all_time_entries(
            workspace, von.isoformat(), bis.isoformat()
        )
        buchungen, _ = auffalten(gruppen, projekte, kunden, tags)
        stunden = je_projekt(buchungen)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rechnungslauf: Toggl-Stunden nicht abrufbar: %s", exc)
        fehler.append(f"Toggl-Stunden: {type(exc).__name__}: {exc}")

    return Rohdaten(
        entwuerfe=entwuerfe,
        positionen=positionen,
        stunden=stunden,
        buchungen=buchungen,
        auftraege=auftragszeilen_,
        rechnungen_der_periode=alle_rechnungen,
        fehler=fehler,
    )


def _vor_dem_stichtag(datum: object, bis: date) -> bool:
    """Lag der Auftrag am Periodenende schon vor? Unlesbares zählt als ja."""
    try:
        return date.fromisoformat(str(datum)[:10]) <= bis
    except (TypeError, ValueError):
        return True


def auftragslage(
    auftraege: list,
    bestand: Bestand,
    *,
    vertraege_mit_rechnung: set[str],
    vertraege_mit_stunden: set[str],
    bis: date | None = None,
) -> list[dict]:
    """Was die Bexio-Aufträge über den Monat aussagen. Reine Funktion.

    Drei Blickrichtungen, und **nur** drei. Die vierte, die sich anbietet --
    «Aktivität, aber gar kein Auftrag» -- bleibt bewusst weg: sie meldete im
    August 2026 die beiden CAS-Lehrgänge und eine Basisschulung, alle drei ohne
    Auftrag, weil sie von Hand fakturiert werden. Drei Dauergäste in einer
    Mängelliste lehren, über die Liste hinwegzulesen, und danach fällt der echte
    Befund auch nicht mehr auf.

    Die drei, die bleiben, sind am selben Monat gemessen und ergaben zusammen
    **null** Meldungen -- nicht weil die Regel tot ist, sondern weil es stimmte:
    elf laufende Daueraufträge, elf Rechnungen. Am April 2026 ergaben dieselben
    Regeln vier Befunde, drei davon zutreffend.

    ``bis`` ist das Periodenende. Ohne diese Grenze mahnte der April 2026 drei
    Aufträge an, die erst im Juni erfasst wurden -- rückwirkend eine Rechnung zu
    verlangen für eine Vereinbarung, die es damals nicht gab. Ein Auftrag ohne
    lesbares Datum bleibt in der Prüfung: ihn wegzulassen hiesse, einen Mangel
    mit einem zweiten zuzudecken.
    """
    befunde: list[dict] = []
    je_vertrag: dict[str, list] = {}

    for auftrag in auftraege:
        if bis is not None and not _vor_dem_stichtag(auftrag.datum, bis):
            continue
        vertrag = bestand.finden(auftrag.titel)
        if vertrag is None:
            # Nur die laufenden. Ein erledigter Auftrag von 2020 ohne Vertrag
            # ist kein Mangel, sondern Geschichte.
            if auftrag.laeuft:
                befunde.append({
                    "art": "auftrag_ohne_vertrag",
                    "auftrag": auftrag.nummer,
                    "titel": auftrag.titel,
                    "grund": "Laufender Auftrag, aber kein Vertrag hinterlegt.",
                })
            continue
        je_vertrag.setdefault(vertrag.schluessel, []).append(auftrag)

    for schluessel, liste in sorted(je_vertrag.items()):
        vertrag = bestand.vertraege[schluessel]
        hat_rechnung = schluessel in vertraege_mit_rechnung
        hat_stunden = schluessel in vertraege_mit_stunden
        laufend = [a for a in liste if a.laeuft]
        nummern = [a.nummer for a in liste]

        # Zwei Bedingungen, und beide müssen aus einer Vereinbarung stammen,
        # nicht aus einer Vermutung. Der Auftrag muss wiederkehrend sein -- ein
        # einmaliger läuft über Meilensteine, ihn monatlich anzumahnen hiesse,
        # eine Erwartung zu erfinden, die nie vereinbart war. Und der Vertrag
        # muss für genau diesen Monat eine Rechnung erwarten lassen: ImpulsKöniz
        # rechnete Januar bis Juli 2026 je Quartal ab, AKV-Bot rechnet nur ab,
        # wenn Leistung erbracht wurde.
        takt = vertrag.konditionen_am(bis).rhythmus
        faellig = takt.erwartet_rechnung(
            bis.month if bis else None, stunden_vorhanden=hat_stunden
        )
        wiederkehrend = [a for a in laufend if a.wiederkehrend]

        # Ein ruhender Vertrag erwartet keine Rechnung mehr -- das ist die
        # Bedeutung von ``ruhend``. Läuft der Dauerauftrag trotzdem weiter, ist
        # nicht die fehlende Rechnung der Befund, sondern der offene Auftrag.
        # Der Unterschied ist nicht kosmetisch: die eine Meldung stünde jeden
        # Monat neu da und wäre nie erledigt, die andere verschwindet, sobald
        # der Auftrag in Bexio geschlossen ist.
        if vertrag.ruhend and wiederkehrend:
            befunde.append({
                "art": "auftrag_laeuft_weiter",
                "auftrag": ", ".join(a.nummer for a in wiederkehrend),
                "vertrag": schluessel,
                "titel": vertrag.bezeichnung,
                "grund": (
                    "Der Vertrag ist beendet, der Dauerauftrag läuft in Bexio "
                    "weiter."
                ),
            })
        elif wiederkehrend and faellig and not hat_rechnung:
            befunde.append({
                "art": "auftrag_ohne_rechnung",
                "auftrag": ", ".join(a.nummer for a in wiederkehrend),
                # Die Kennungen, nicht nur die Nummern: aus diesem Befund
                # entsteht der Entwurf, und erzeugt wird über die Kennung.
                "auftrag_ids": [a.auftrag_id for a in wiederkehrend if a.auftrag_id],
                "vertrag": schluessel,
                "titel": vertrag.bezeichnung,
                "stunden_vorhanden": hat_stunden,
                "grund": (
                    "Wiederkehrender Auftrag läuft, aber der Monat trägt keine "
                    "Rechnung."
                ),
            })

        # Ein ruhender Vertrag braucht keine Verlängerung -- er ist beendet, und
        # dass der Auftrag dazu abgeschlossen ist, ist die Übereinstimmung und
        # nicht der Mangel.
        #
        # ``ruhend`` kennt keine Zeitachse, und das kostet in beide Richtungen:
        # ein Monat, in dem der Vertrag noch lief, wird rückblickend nicht mehr
        # als Verlängerungsfall gemeldet, und umgekehrt trägt jeder rückwärts
        # geprüfte Monat den Hinweis auf den offenen Auftrag, obwohl der Vertrag
        # damals lief (Sympholio wurde erst per 31.08.2026 beendet). Im Betrieb
        # wird ein Monat geprüft, nicht neun -- dort ist der Hinweis richtig und
        # erledigt sich, sobald der Auftrag in Bexio geschlossen ist. Eine
        # dritte Zeitachse dafür wäre teurer als der Befund wert ist.
        if not laufend and (hat_rechnung or hat_stunden) and not vertrag.ruhend:
            woher = " und ".join(
                w for w, ja in (("eine Rechnung", hat_rechnung), ("Stunden", hat_stunden)) if ja
            )
            befunde.append({
                "art": "auftrag_abgelaufen",
                "auftrag": ", ".join(nummern),
                "vertrag": schluessel,
                "titel": vertrag.bezeichnung,
                "grund": (
                    f"Alle Aufträge sind abgeschlossen, der Monat trägt aber {woher}. "
                    "Vermutlich fehlt eine Verlängerung."
                ),
            })

    return befunde


@dataclass
class Uebertragsstand:
    """Was zu Beginn eines Leistungsmonats offen steht, je Vertrag."""

    staende: dict[str, float] = field(default_factory=dict)
    herkunft: dict[str, str] = field(default_factory=dict)
    """Aus welchem Monat der Stand stammt — bei einer Pause nicht der Vormonat."""
    hinweise: list[str] = field(default_factory=list)
    """Nur, was Aufmerksamkeit braucht. Eine belegte Pause steht nicht hier."""


def _monat_zurueck(jahr: int, monat: int, schritte: int = 1) -> tuple[int, int]:
    gesamt = jahr * 12 + (monat - 1) - schritte
    return gesamt // 12, gesamt % 12 + 1


async def uebertrag_zu_monatsbeginn(
    bexio,
    bestand: Bestand,
    *,
    jahr: int,
    monat: int,
    toggl=None,
    workspace: int | None = None,
    rueckblick: int = 12,
) -> Uebertragsstand:
    """Den Übertragsstand je Vertrag aus der jüngsten Rechnung davor lesen.

    Die Rechnung sagt selbst, was offen bleibt: «Per 31.08.2026 sind 7h
    verrechnet aber noch nicht geleistet, welche dem nächsten Monat angerechnet
    werden.» Damit ist der Startwert im Bestand belegt und muss weder aus
    ``historie.json`` übernommen noch geschätzt werden — jene Datei trägt
    bekannte Fehler, und ein geschätzter Übertrag erzeugt eine plausible falsche
    Rechnung statt einer Fehlermeldung.

    Fehlt der Satz auf einer gefundenen Rechnung, ist der Übertrag **null**:
    «nichts zu übertragen» und «kein Satz» sind dasselbe.

    ## Eine Pause ist keine Lücke

    Eine frühere Fassung sah nur den unmittelbaren Vormonat an und meldete jeden
    fehlenden als «Stand nicht belegt». Bei «Digitale Evolution mit KI» ist der
    Juli 2026 aber vereinbarungsgemäss leer — Sommerpause, keine Leistung, keine
    Rechnung. Das als Mangel zu führen wäre nicht bloss unnütz: ein
    wiederkehrender Dauerbefund lehrt, über die Liste hinwegzulesen.

    Deshalb wird zurückgegangen, bis eine Rechnung kommt, und die übersprungenen
    Monate werden **gegen Toggl geprüft**. Ohne erfasste Stunden ist die Pause
    belegt und der Stand wandert unverändert weiter. Mit Stunden fehlt eine
    Rechnung, und dann gibt es keinen Stand — nur die Meldung. Genau diese
    Differenz zwischen den Systemen soll die Ansicht zeigen.

    Ohne ``toggl`` wird eine Pause nicht überbrückt: eine unbelegte Annahme wäre
    schlechter als eine fehlende Angabe.
    """
    stand = Uebertragsstand()
    beginn = date(jahr, monat, 1)

    try:
        alle = await bexio.search_invoices()
    except Exception as exc:  # noqa: BLE001 -- Teilausfall ist eingeplant
        logger.warning("Übertragsstand nicht lesbar: %s", exc)
        stand.hinweise.append(f"Übertragsstand: {type(exc).__name__}: {exc}")
        return stand

    # Rechnungen je Vertrag und Monat, nur was vor der Periode liegt.
    je_vertrag: dict[str, dict[str, list[dict]]] = {}
    for r in alle:
        datum = str(r.get("is_valid_from") or "")
        if not datum or datum >= beginn.isoformat():
            continue
        vertrag = bestand.finden(r.get("title") or "")
        if vertrag is None or not vertrag.uebertragbar:
            continue
        je_vertrag.setdefault(vertrag.schluessel, {}).setdefault(datum[:7], []).append(r)

    # Je Vertrag den jüngsten Monat mit Rechnung und die Monate der Pause.
    #
    # Gesucht wird über **alle** übertragbaren Verträge, auch ruhende: Sympholio
    # endete per 31.08.2026, und für den August braucht seine Rechnung trotzdem
    # einen Startwert. Nur die Meldung «gar keine Rechnung gefunden» bleibt den
    # aktiven vorbehalten — bei einem beendeten Vertrag ist das kein Mangel.
    gesucht: dict[str, tuple[str, list[str]]] = {}
    for vertrag in bestand.vertraege.values():
        if not vertrag.uebertragbar:
            continue
        monate = je_vertrag.get(vertrag.schluessel, {})
        pause: list[str] = []
        for schritt in range(1, rueckblick + 1):
            j, m = _monat_zurueck(jahr, monat, schritt)
            schluessel = f"{j:04d}-{m:02d}"
            if schluessel in monate:
                gesucht[vertrag.schluessel] = (schluessel, pause)
                break
            pause.append(schluessel)
        else:
            if not vertrag.ruhend:
                stand.hinweise.append(
                    f"Vertrag '{vertrag.bezeichnung}': in den letzten {rueckblick} "
                    "Monaten keine Rechnung — der Übertragsstand ist nicht belegt."
                )

    if not gesucht:
        return stand

    # ── Pausen gegen Toggl halten ────────────────────────
    ohne_beleg = await _pausen_pruefen(toggl, bestand, gesucht, workspace, stand)

    # ── Den Satz der jüngsten Rechnung lesen ─────────────
    kandidaten = [
        {"rechnung_id": r.get("id"), "nummer": r.get("document_nr") or "",
         "vertrag": schluessel}
        for schluessel, (quelle, _) in gesucht.items()
        if schluessel not in ohne_beleg
        for r in je_vertrag[schluessel][quelle]
    ]
    if not kandidaten:
        return stand

    positionen, fehler = await _positionen_holen(bexio, kandidaten)
    stand.hinweise.extend(fehler)

    verworfen: set[str] = set()
    for k in kandidaten:
        pos = positionen.get(int(k["rechnung_id"]))
        if pos is None or k["vertrag"] in verworfen:
            continue
        wert = dp.deuten(pos).uebertrag_angabe or 0.0
        vorher = stand.staende.get(k["vertrag"])
        if vorher is not None and abs(vorher - wert) > 0.01:
            # Zwei Rechnungen desselben Vertrags im selben Monat mit
            # verschiedenen Ständen: geraten wird hier nicht. Der Vertrag bleibt
            # danach draussen, sonst setzte ihn eine dritte Rechnung wieder.
            quelle = gesucht[k["vertrag"]][0]
            stand.hinweise.append(
                f"Vertrag '{k['vertrag']}': zwei Rechnungen im {quelle} nennen "
                f"verschiedene Überträge ({vorher:g}h und {wert:g}h)."
            )
            verworfen.add(k["vertrag"])
            stand.staende.pop(k["vertrag"], None)
            stand.herkunft.pop(k["vertrag"], None)
            continue
        stand.staende[k["vertrag"]] = wert
        stand.herkunft[k["vertrag"]] = gesucht[k["vertrag"]][0]

    return stand


async def _pausen_pruefen(
    toggl,
    bestand: Bestand,
    gesucht: dict[str, tuple[str, list[str]]],
    workspace: int | None,
    stand: Uebertragsstand,
) -> set[str]:
    """Verträge aussondern, deren Pause nicht belegt ist.

    Ein Abruf über die ganze Spanne, nicht einer je Monat: die Buchungen tragen
    ihr Datum mit sich, und jeder Vertrag wird gegen **seine** Pause gefiltert.
    """
    from zeiteintraege import auffalten

    mit_pause = {s: p for s, (_, p) in gesucht.items() if p}
    if not mit_pause:
        return set()

    if toggl is None or workspace is None:
        for schluessel, pause in mit_pause.items():
            stand.hinweise.append(
                f"Vertrag '{schluessel}': keine Rechnung in {', '.join(sorted(pause))} "
                "und kein Zugriff auf Toggl — die Pause ist nicht belegt."
            )
        return set(mit_pause)

    alle_monate = sorted({m for p in mit_pause.values() for m in p})
    von = date(int(alle_monate[0][:4]), int(alle_monate[0][5:]), 1)
    letzter = alle_monate[-1]
    bis = date(
        int(letzter[:4]), int(letzter[5:]),
        calendar.monthrange(int(letzter[:4]), int(letzter[5:]))[1],
    )

    try:
        projekte = await toggl.list_projects(active="both")
        kunden = await toggl.list_clients(status="both")
        gruppen = await toggl.search_all_time_entries(
            workspace, von.isoformat(), bis.isoformat()
        )
        eintraege, _ = auffalten(gruppen, projekte, kunden)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pausenprüfung: Toggl nicht erreichbar: %s", exc)
        stand.hinweise.append(
            f"Pausenprüfung: Toggl nicht erreichbar ({type(exc).__name__}) — "
            "die Überträge über eine Pause hinweg bleiben unbelegt."
        )
        return set(mit_pause)

    # Stunden je Vertrag und Monat.
    geleistet: dict[tuple[str, str], float] = {}
    for e in eintraege:
        vertrag = bestand.finden(e.get("projekt") or "")
        datum = e.get("datum") or ""
        if vertrag is None or len(datum) < 7:
            continue
        schluessel = (vertrag.schluessel, datum[:7])
        geleistet[schluessel] = geleistet.get(schluessel, 0.0) + (e.get("stunden") or 0.0)

    ohne_beleg: set[str] = set()
    for schluessel, pause in mit_pause.items():
        mit_stunden = {
            m: round(geleistet[(schluessel, m)], 2)
            for m in pause if geleistet.get((schluessel, m), 0.0) > 0.01
        }
        if mit_stunden:
            ohne_beleg.add(schluessel)
            stand.hinweise.append(
                f"Vertrag '{schluessel}': "
                + ", ".join(f"{m} mit {h:g}h" for m, h in sorted(mit_stunden.items()))
                + " in Toggl, aber keine Rechnung — der Übertragsstand ist nicht belegt."
            )
    return ohne_beleg


async def _positionen_holen(bexio, entwuerfe: list[dict]) -> tuple[dict[int, list[dict]], list[str]]:
    """Die Positionen aller Entwürfe, begrenzt parallel.

    Je Rechnung zwei Abrufe (Produkt- und Textzeilen). Scheitert einer, fehlt
    **diese** Rechnung und wird gemeldet — die übrigen bleiben brauchbar.
    """
    zaehler = asyncio.Semaphore(GLEICHZEITIG)
    ergebnis: dict[int, list[dict]] = {}
    fehler: list[str] = []

    async def einer(entwurf: dict) -> None:
        kennung = entwurf.get("rechnung_id")
        if kennung is None:
            return
        async with zaehler:
            try:
                ergebnis[int(kennung)] = await bexio.get_invoice_positions(int(kennung))
            except Exception as exc:  # noqa: BLE001
                fehler.append(
                    f"Positionen zu {entwurf.get('nummer') or kennung}: "
                    f"{type(exc).__name__}: {exc}"
                )

    await asyncio.gather(*(einer(e) for e in entwuerfe))
    if fehler:
        logger.warning("Rechnungslauf: %d Positionsabruf(e) fehlgeschlagen", len(fehler))
    return ergebnis, fehler


# ── Auswerten ────────────────────────────────────────────


def steuersatz(netto: float, steuer: float) -> float | None:
    """Den Satz aus Netto und Steuerbetrag herleiten.

    Bexio nennt keinen Satz, nur die beiden Beträge. Ohne Nettobetrag ist der
    Satz nicht herleitbar — dann ``None`` und nicht ``0.0``: eine Null sähe wie
    ein steuerfreier Umsatz aus und wäre für die BFH-Verträge sogar richtig,
    womit der Fehler unsichtbar würde.
    """
    if not netto:
        return None
    return round(steuer / netto * 100, 2)


@dataclass
class Auswertung:
    """Das Ergebnis eines Laufs über einen Leistungsmonat."""

    jahr: int
    monat: int
    ergebnisse: list[pr.Ergebnis] = field(default_factory=list)
    ohne_vertrag: list[dict] = field(default_factory=list)
    """Entwürfe, deren Titel auf keinen Vertrag passt. Die gefährliche Richtung:
    ungeprüft sieht aus wie unbeanstandet."""
    ohne_entwurf: list[dict] = field(default_factory=list)
    """Projekte mit Stunden im Monat, zu denen kein Entwurf existiert."""
    ausserhalb: list[dict] = field(default_factory=list)
    """Entwürfe, die in einen anderen Monat datiert sind."""
    intern: list[str] = field(default_factory=list)
    """Projekte ohne Vertrag, deren Stunden **alle** unverrechenbar sind.

    «Strategy & Planning», «Sales & Networking», «Finance & Admin» — eigene
    Arbeit. Sie als fehlende Rechnung zu melden wäre nicht bloss falsch,
    sondern schädlich: drei Dauergäste in der Mängelliste lehren, über die
    Liste hinwegzulesen, und dann fällt die echte vergessene Rechnung auch
    nicht mehr auf. Genannt werden sie trotzdem, denn stillschweigend
    weglassen ist die andere Art, etwas zu verlieren."""
    auffaelligkeiten: dict[str, list[str]] = field(default_factory=dict)
    """Je Rechnungsnummer, was beim Deuten der Positionen unklar blieb."""
    vorschlaege: list[vs.Vorschlag] = field(default_factory=list)
    """Je Rechnung, welche Zeile welchen Wert tragen müsste. Nur berechnet —
    angewendet wird nach Bestätigung, in einem eigenen Schritt."""
    auftragsluecken: list[dict] = field(default_factory=list)
    """Was die Bexio-Aufträge über den Monat sagen — siehe ``auftragslage``."""
    fehler: list[str] = field(default_factory=list)

    @property
    def versandbereit(self) -> int:
        return sum(1 for e in self.ergebnisse if e.versandbereit)

    @property
    def blockiert(self) -> int:
        return len(self.ergebnisse) - self.versandbereit


def auswerten(
    roh: Rohdaten,
    bestand: Bestand,
    *,
    jahr: int,
    monat: int,
    massstab: pr.Massstab | None = None,
    uebertrag_vormonat: dict[str, float] | None = None,
    dokumente_erzeugt: bool = False,
) -> Auswertung:
    """Die Rohdaten zu Prüfergebnissen verbinden. Reine Funktion.

    ``uebertrag_vormonat`` je Vertragsschlüssel. Fehlt er, meldet die
    Übertragsregel «offen» statt mit null zu rechnen — ein angenommener Übertrag
    von null ist eine Behauptung über einen Vertrag und keine fehlende Angabe.

    ``dokumente_erzeugt`` unterscheidet die Prüfung vor und nach dem Erzeugen von
    Rechnung und Leistungsrapport. Vorher kann es weder Rapport noch IBAN geben,
    weil beide aus der PDF gelesen werden — «fehlt» wäre die falsche Auskunft.
    """
    von, bis = periodengrenzen(jahr, monat)
    uebertraege = uebertrag_vormonat or {}
    massstab = massstab or pr.Massstab(
        iban_erwartet=(bestand.vorgaben or {}).get("iban_praefix"),
        uebertragsformel=ue.BESTAETIGT,
    )

    auswertung = Auswertung(jahr=jahr, monat=monat, fehler=list(roh.fehler))

    # Stunden je Vertrag: über den Toggl-Projektnamen auf den Vertrag abbilden.
    stunden_je_vertrag: dict[str, dict] = {}
    for eintrag in roh.stunden.values():
        vertrag = bestand.finden(eintrag.get("projekt") or "")
        if vertrag is None:
            # Unverrechenbar **und** ohne Vertrag heisst: eigene Arbeit. Das
            # Häkchen in Toggl entscheidet das, keine Namensliste — die alte
            # Fassung filterte aus demselben Grund vorab auf ``billable``.
            if not (eintrag.get("verrechenbare_stunden") or 0):
                auswertung.intern.append(eintrag.get("projekt") or "")
                continue
            auswertung.ohne_entwurf.append({
                "quelle": "toggl",
                "projekt": eintrag.get("projekt") or "",
                "kunde": eintrag.get("kunde") or "",
                "stunden": eintrag.get("stunden"),
                "grund": "kein Vertrag hinterlegt",
            })
            continue
        gesammelt = stunden_je_vertrag.setdefault(
            vertrag.schluessel, {"stunden": 0.0, "projekte": []}
        )
        gesammelt["stunden"] += eintrag.get("stunden") or 0.0
        gesammelt["projekte"].append(eintrag.get("projekt") or "")

    mit_entwurf: set[str] = set()

    for entwurf in roh.entwuerfe:
        datum = _datum(entwurf.get("datum"))
        if datum is not None and not (von <= datum <= bis):
            auswertung.ausserhalb.append({
                "nummer": entwurf.get("nummer"),
                "titel": entwurf.get("titel"),
                "kunde": entwurf.get("kunde"),
                "datum": datum.isoformat(),
            })
            continue

        vertrag = bestand.finden(entwurf.get("titel") or "")
        if vertrag is None:
            auswertung.ohne_vertrag.append({
                "nummer": entwurf.get("nummer"),
                "titel": entwurf.get("titel"),
                "kunde": entwurf.get("kunde"),
                "brutto": entwurf.get("brutto"),
            })
            continue
        mit_entwurf.add(vertrag.schluessel)

        kennung = entwurf.get("rechnung_id")
        deutung = dp.deuten(roh.positionen.get(int(kennung), []) if kennung else [])
        if deutung.auffaelligkeiten:
            auswertung.auffaelligkeiten[entwurf.get("nummer") or str(kennung)] = list(
                deutung.auffaelligkeiten
            )

        geleistet = stunden_je_vertrag.get(vertrag.schluessel, {}).get("stunden")
        auswertung.ergebnisse.append(pr.pruefen(
            vertrag.als_vertrag(bis),
            pr.Rechnung(
                nummer=entwurf.get("nummer") or "",
                datum=datum,
                mwst_satz=steuersatz(
                    entwurf.get("netto") or 0.0, entwurf.get("steuer") or 0.0
                ),
                # Die IBAN steht erst im erzeugten PDF, nicht in der Antwort der
                # Schnittstelle. Bis dahin ist die Regel offen und nicht grün.
                iban=None,
                referenz=entwurf.get("referenz"),
                fix_stunden=deutung.fix_stunden,
                zusatz_stunden=deutung.zusatz_stunden,
                uebertrag_angabe=deutung.uebertrag_angabe,
                verrechnete_stunden=deutung.verrechnete_stunden,
            ),
            pr.Periode(
                jahr=jahr,
                monat=monat,
                geleistet=None if geleistet is None else round(geleistet, 2),
                uebertrag_vormonat=uebertraege.get(vertrag.schluessel),
                dokumente_erzeugt=dokumente_erzeugt,
            ),
            massstab,
        ))

        vorschlag = vs.vorschlagen(
            vertrag=vertrag,
            rechnung=entwurf.get("nummer") or "",
            deutung=deutung,
            geleistet=None if geleistet is None else round(geleistet, 2),
            uebertrag_vormonat=uebertraege.get(vertrag.schluessel),
            stichtag=bis,
            formel=massstab.uebertragsformel or ue.BESTAETIGT,
        )
        if vorschlag.aenderungen or vorschlag.hindernisse:
            auswertung.vorschlaege.append(vorschlag)

    # Die andere Richtung: Stunden erfasst, aber kein Entwurf vorhanden.
    for schluessel, gesammelt in sorted(stunden_je_vertrag.items()):
        if schluessel in mit_entwurf or gesammelt["stunden"] <= 0:
            continue
        vertrag = bestand.vertraege[schluessel]
        auswertung.ohne_entwurf.append({
            "quelle": "vertrag",
            "vertrag": schluessel,
            "projekt": vertrag.bezeichnung,
            "kunde": vertrag.kunde.schluessel,
            "stunden": round(gesammelt["stunden"], 2),
            "grund": "Stunden erfasst, kein Entwurf in Bexio",
        })

    # Die dritte Richtung: was die Aufträge über den Monat sagen. «Rechnung
    # vorhanden» zählt hier über **alle** Rechnungen des Monats, nicht nur die
    # Entwürfe -- eine bereits gestellte schliesst die Lücke genauso.
    if roh.auftraege:
        mit_rechnung = {
            v.schluessel
            for r in roh.rechnungen_der_periode
            if (v := bestand.finden(r.get("title") or "")) is not None
        } | mit_entwurf
        auswertung.auftragsluecken = auftragslage(
            roh.auftraege,
            bestand,
            vertraege_mit_rechnung=mit_rechnung,
            vertraege_mit_stunden=set(stunden_je_vertrag),
            bis=bis,
        )

    return auswertung


def _datum(wert: Any) -> date | None:
    if isinstance(wert, date):
        return wert
    if isinstance(wert, str) and wert:
        try:
            return date.fromisoformat(wert[:10])
        except ValueError:
            return None
    return None
