"""Die Antwort der Toggl-Reports-API auffalten — eine Zeile je Buchung.

Liegt hier und nicht im Backend, weil zwei Wege dieselbe Antwort auswerten: der
Datenraum-Abgleich (``services/datenraum.py``) für die Auswertungsansichten und
der Rechnungslauf für die Prüfung vor dem Versand. Dieselbe Deutung an zwei
Stellen zu schreiben war schon einmal ein Befund — ``routers/debtors.py`` hielt
eigene Kopien von ``_parse_invoice_total`` und ``_invoice_is_open``, und zwei
Ansichten desselben Hauses konnten dieselbe Kennzahl verschieden beantworten.
Die Kopien wurden damals nicht korrigiert, sondern entfernt. Dasselbe gilt hier.

## Die Falle, gegen die dieses Modul geschrieben ist

Die Reports-API v3 antwortet **gruppiert**, nicht als flache Liste. Eine Zeile
trägt ``project_id``, ``description``, ``billable`` und darunter ein Feld
``time_entries`` mit den eigentlichen Buchungen (``id``, ``start``, ``seconds``).

Zwei Felder, die man auf der obersten Ebene erwartet, gibt es dort nicht:
``start`` und ``client_id``. Der erste Entwurf las beide dort — mit dem Ergebnis,
dass **alle 2639 Zeiteinträge** ohne Datum und ohne Kunde in den Datenraum
gingen. Die Tabelle war vollzählig, und jede Zeitfrage wäre trotzdem falsch
beantwortet worden: nach Stunden für einen Kunden gefragt, hätte sie null
ergeben. Ein Fehler, der eine plausible Zahl erzeugt statt einer Fehlermeldung.

Der Kunde hängt in Toggl am **Projekt**, nicht am Zeiteintrag, und wird deshalb
über das Projekt aufgelöst. Archivierte Kunden fehlen ohne ``status=both`` — das
gehört zum Abruf und nicht hierher, aber es ist dieselbe Gattung Fehler.
"""

from __future__ import annotations

from typing import Any


def _kennungen(projekte: list[dict], kunden: list[dict]) -> tuple[dict, dict, dict]:
    """Die drei Nachschlagetabellen aus den Rohlisten.

    Absichtlich hier und nicht beim Aufrufer: die Zuordnung «Kunde hängt am
    Projekt» ist genau die Stelle, an der der erste Entwurf danebengriff.
    """
    projektnamen = {p.get("id"): p.get("name") or "" for p in projekte}
    projekt_kunde = {p.get("id"): p.get("client_id") for p in projekte}
    kundennamen = {k.get("id"): k.get("name") or "" for k in kunden}
    return projektnamen, projekt_kunde, kundennamen


def auffalten(
    gruppen: list[dict],
    projekte: list[dict],
    kunden: list[dict],
    tags: list[dict] | None = None,
    aufgaben: dict[int, str] | None = None,
) -> tuple[list[dict], dict]:
    """Gruppen in einzelne Buchungen auflösen und die Lücken zählen.

    Zurück kommt eine Zeile je tatsächlicher Buchung mit aufgelöstem Projekt und
    Kunden, dazu ein Befund. Der Befund zählt den **unaufgelösten Namen**, nicht
    die fehlende Kennung: ein Projekt mit Kundennummer, zu der es keinen Namen
    gibt, sieht in der Tabelle genauso aus wie eines ganz ohne Kundschaft — und
    ist doch ein Mangel statt einer Tatsache.

    ``tags`` und ``aufgaben`` sind Nachschlagetabellen und optional. Fehlen sie,
    bleiben die Kennungen stehen und die Namen leer — sichtbar, nicht geraten.

    **Die Tags tragen die Verrechnungsart.** Im Bestand gibt es genau vier
    («Kunde verrechnet», «Fixpreis», «Extern rapportiert», «keine Verrechnung»),
    und am 21.09.2026 lagen von 1961 Stunden des Jahres 1148.5 auf «Kunde
    verrechnet», 384.5 auf «Fixpreis» und **428 auf gar keinem Tag**. Die
    unbetagte Stunde ist damit keine Randerscheinung, sondern der zweitgrösste
    Posten — sie muss als eigene Menge sichtbar bleiben und darf nicht
    stillschweigend einer Art zugeschlagen werden.

    ``task_id`` ist dagegen fast leer: 35 von 873 Gruppen, alle im beendeten
    Projekt «Administrative Unterstützung». Die Spalte wird trotzdem geführt,
    weil der Leistungsrapport daraus die «Aufschlüsselung nach Bereich» bildet
    und diese Möglichkeit nicht an der aktuellen Belegung sterben soll.
    """
    projektnamen, projekt_kunde, kundennamen = _kennungen(projekte, kunden)
    tagnamen = {t.get("id"): t.get("name") or "" for t in (tags or [])}
    aufgaben = aufgaben or {}

    eintraege: list[dict] = []
    ohne_projekt = 0

    for gruppe in gruppen:
        projekt_id = gruppe.get("project_id")
        if projekt_id is None:
            ohne_projekt += 1
        kunden_id = projekt_kunde.get(projekt_id)

        satz_rappen = gruppe.get("hourly_rate_in_cents") or 0
        untereintraege = gruppe.get("time_entries") or []
        sekunden_gesamt = sum(u.get("seconds") or 0 for u in untereintraege)
        betrag_rappen = gruppe.get("billable_amount_in_cents") or 0

        tag_ids = gruppe.get("tag_ids") or []
        # Sortiert, damit dieselbe Kombination immer denselben Text ergibt —
        # sonst wären «Fixpreis, Kunde verrechnet» und die Umkehrung zwei
        # verschiedene Gruppen in jeder Auswertung.
        tagtext = ", ".join(sorted(tagnamen.get(t) or str(t) for t in tag_ids))
        task_id = gruppe.get("task_id")

        for u in untereintraege:
            sekunden = u.get("seconds") or 0
            # Der Betrag gilt für die Gruppe. Ihn nach Sekunden aufzuteilen ist
            # keine Schätzung, sondern die Umkehrung seiner Entstehung
            # (Satz mal Zeit).
            anteil = (sekunden / sekunden_gesamt) if sekunden_gesamt else 0
            eintraege.append({
                "eintrag_id": u.get("id"),
                "datum": (u.get("start") or "")[:10] or None,
                "beginn": u.get("start") or None,
                "projekt_id": projekt_id,
                "projekt": projektnamen.get(projekt_id, ""),
                "kunden_id": kunden_id,
                "kunde": kundennamen.get(kunden_id, ""),
                "person_id": gruppe.get("user_id"),
                "person": gruppe.get("username") or "",
                "aufgabe_id": task_id,
                "aufgabe": aufgaben.get(task_id, "") if task_id else "",
                "beschreibung": gruppe.get("description") or "",
                "stunden": round(sekunden / 3600, 4),
                "verrechenbar": bool(gruppe.get("billable")),
                "verrechnungsart": tagtext,
                "stundensatz": round(satz_rappen / 100, 2),
                "betrag": round(betrag_rappen * anteil / 100, 2),
                "waehrung": gruppe.get("currency") or "",
            })

    befund: dict[str, Any] = {
        "gruppen": len(gruppen),
        "eintraege": len(eintraege),
        "eintraege_ohne_kundennamen": sum(1 for e in eintraege if not e["kunde"]),
        "stunden_ohne_verrechnungsart": round(
            sum(e["stunden"] for e in eintraege if not e["verrechnungsart"]), 2
        ),
    }
    if ohne_projekt:
        befund["gruppen_ohne_projekt"] = ohne_projekt
    return eintraege, befund


def je_verrechnungsart(eintraege: list[dict]) -> dict[str, float]:
    """Stunden nach Verrechnungsart — die unbetagte Stunde als eigene Menge.

    Sie heisst ``(ohne Tag)`` und nicht ``''``: eine leere Beschriftung sieht in
    jeder Darstellung wie ein Darstellungsfehler aus, und 428 Stunden sind zu
    viel, um wie ein Fehler auszusehen.
    """
    summe: dict[str, float] = {}
    for e in eintraege:
        art = e.get("verrechnungsart") or "(ohne Tag)"
        summe[art] = round(summe.get(art, 0.0) + (e.get("stunden") or 0.0), 4)
    return summe


def projektzeilen(projekte: list[dict], kunden: list[dict]) -> list[dict]:
    """Die Projekte als Tabellenzeilen, mit aufgelöstem Kundennamen."""
    kundennamen = {k.get("id"): k.get("name") or "" for k in kunden}
    return [{
        "projekt_id": p.get("id"),
        "projekt": p.get("name") or "",
        "kunden_id": p.get("client_id"),
        "kunde": kundennamen.get(p.get("client_id"), ""),
        "aktiv": bool(p.get("active")),
        "verrechenbar": bool(p.get("billable")),
    } for p in projekte]


def je_projekt(eintraege: list[dict]) -> dict[int, dict]:
    """Die aufgefalteten Buchungen zu Stunden je Projekt verdichten.

    Für den Rechnungslauf: dort wird je Projekt gegen die Rechnung gehalten, und
    zwar **alle** Stunden, nicht nur die verrechenbaren. Ob eine Stunde in
    Rechnung geht, entscheidet der Vertrag und nicht das Häkchen in Toggl — ein
    Fixvertrag verbraucht auch nicht verrechenbare Zeit.
    """
    gesammelt: dict[int, dict] = {}
    for e in eintraege:
        pid = e.get("projekt_id")
        if pid is None:
            continue
        eintrag = gesammelt.setdefault(pid, {
            "projekt_id": pid,
            "projekt": e.get("projekt") or "",
            "kunden_id": e.get("kunden_id"),
            "kunde": e.get("kunde") or "",
            "stunden": 0.0,
            "verrechenbare_stunden": 0.0,
            "betrag": 0.0,
        })
        eintrag["stunden"] += e.get("stunden") or 0.0
        if e.get("verrechenbar"):
            eintrag["verrechenbare_stunden"] += e.get("stunden") or 0.0
            eintrag["betrag"] += e.get("betrag") or 0.0

    for eintrag in gesammelt.values():
        eintrag["stunden"] = round(eintrag["stunden"], 4)
        eintrag["verrechenbare_stunden"] = round(eintrag["verrechenbare_stunden"], 4)
        eintrag["betrag"] = round(eintrag["betrag"], 2)
    return gesammelt
