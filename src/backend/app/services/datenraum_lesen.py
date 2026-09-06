"""Lesezugriff auf den Datenraum -- die eine Stelle, aus der Finanzzahlen kommen.

Warum es diese Schicht gibt: Bis zum 06.09.2026 holte die Finanzansicht ihre Zahlen
live aus der Bexio- und der Toggl-Schnittstelle und entschied dabei **ein zweites
Mal**, was Umsatz und was Aufwand ist. Zwei Definitionen derselben Grösse in einem
Produkt bleiben nicht lange gleich, und sie blieben es auch hier nicht. Gemessen am
Bestand vom 06.09.2026:

* Die Journal-Aggregationen summierten ``amount`` statt ``base_currency_amount``.
  250 der 5262 Buchungen lauten auf Fremdwährung, und der Aufwand 2026 stand damit
  um **3'520 CHF zu hoch** (269'997 statt 266'476).
* Der Umsatz summierte jede Rechnung ohne Blick auf den Status. Eine
  **Entwurfsrechnung über 8'000 CHF** zählte 2026 als Umsatz, obwohl sie nie
  gestellt wurde.
* Die offenen Debitoren zählten dieselbe Entwurfsrechnung mit: 53'593 statt 45'593
  CHF, und der daraus abgeleitete DSO entsprechend zu hoch.

Keiner dieser drei Fehler erzeugte eine Fehlermeldung. Alle drei erzeugten eine
plausible Zahl -- die teuerste Fehlerart, die es hier gibt. Der Datenraum hat die
Entscheidungen längst getroffen und als Spalten festgehalten (``betrag_chf``,
``ist_umsatz``, ``ist_aufwand``). Diese Schicht liest sie, statt sie erneut zu
treffen.

**Warum PyArrow und nicht DuckDB.** Die Tabellen sind klein (Journal 5262 Zeilen,
109 KB), die Aggregationen der Finanzansicht sind fein austariert und in Python
gewachsen. Sie nach SQL zu übersetzen hätte jede Kennzahl neu zur Diskussion
gestellt, wo doch nur die Datenherkunft zu wechseln war. Gelesen wird deshalb die
ganze Tabelle, gerechnet wird weiter in Python -- und ``pyarrow`` steht für das
Schreiben ohnehin schon in den Abhängigkeiten.

**Zwei Regeln, die nicht verhandelbar sind:**

1. **Kein stiller Nullwert.** Fehlt eine Tabelle oder ist eine gebrauchte Spalte im
   Katalog als durchgehend leer gemeldet, wird ``DatenraumUnbrauchbar`` geworfen.
   ``0 CHF`` sieht wie eine Tatsache aus; eine Fehlermeldung tut das nicht.
2. **Das Alter reist mit.** Jede Antwort trägt ihren Stand, und ``veraltet`` sagt,
   ob er zum erwarteten Takt passt. Ein Dashboard darf alt sein, solange es das
   zugibt -- schweigend alt sein darf es nicht. Deshalb wird Alter auch **nicht**
   zum Abbruchgrund gemacht: eine Ansicht, die wegen eines ausgefallenen Abgleichs
   dunkel wird, hilft niemandem, während ein sichtbarer Hinweis genau das Nötige
   tut.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.services.datenraum import datenraum_pfad, katalog_lesen

logger = logging.getLogger("taskpilot.datenraum_lesen")


class DatenraumUnbrauchbar(RuntimeError):
    """Der Datenraum kann die Frage nicht beantworten -- mit Grund im Text."""


# Welche Spalten die Finanzansicht liest. Die Erklärung steht hier und nicht im
# Aufrufer, weil diese Liste zwei Aufgaben hat: sie begrenzt, was gelesen wird, und
# sie ist der **Spaltenvertrag**, den ``test_finanzansicht_datenraum.py`` gegen den
# echten Katalog prüft. Benennt ein Konnektor eine Spalte um, bricht der Test --
# statt dass das Dashboard eine Null zeigt.
SPALTENVERTRAG: dict[str, tuple[str, ...]] = {
    "bexio_journal": ("datum", "betrag_chf", "soll_konto_nr", "haben_konto_nr"),
    "bexio_rechnungen": (
        "datum", "brutto", "offen", "ist_umsatz", "status", "kunde", "kunden_id",
    ),
    "bexio_konten": ("konto_nr", "konto"),
    "bexio_bankkonten": ("konto_nr", "konto", "name"),
    "bexio_geschaeftsjahre": ("jahr", "von", "bis", "ist_abgeschlossen"),
    "toggl_zeiteintraege": (
        "datum", "stunden", "betrag", "stundensatz", "verrechenbar",
        "projekt", "projekt_id", "kunde",
    ),
}

# Ab wann ein Stand als veraltet gilt. Bexio und Toggl werden stündlich abgeglichen;
# sechs Stunden lassen einen ausgefallenen Lauf und einen Neustart durchgehen, ohne
# einen echten Stillstand zu verschweigen.
WARNSCHWELLE_STUNDEN = 6

# Gelesene Tabellen, gemerkt nach Änderungszeit der Datei. Der Schlüssel ist die
# ``mtime``, nicht eine Ablaufzeit: damit macht ein Abgleich den Vorrat von selbst
# ungültig, und ein neuer Stand kann nie zusammen mit alten Zahlen ausgeliefert
# werden.
_vorrat: dict[str, tuple[int, list[dict]]] = {}


def _pfad(tabelle: str) -> Path:
    return datenraum_pfad() / f"{tabelle}.parquet"


def _zeilen(tabelle: str) -> list[dict]:
    """Eine Tabelle vollständig lesen, auf die vertraglich genutzten Spalten begrenzt."""
    import pyarrow.parquet as pq

    spalten = SPALTENVERTRAG.get(tabelle)
    if spalten is None:
        raise DatenraumUnbrauchbar(f"Tabelle '{tabelle}' steht nicht im Spaltenvertrag")

    pfad = _pfad(tabelle)
    if not pfad.exists():
        raise DatenraumUnbrauchbar(
            f"Tabelle '{tabelle}' fehlt im Datenraum -- läuft der Abgleich?"
        )

    mtime = pfad.stat().st_mtime_ns
    gemerkt = _vorrat.get(tabelle)
    if gemerkt is not None and gemerkt[0] == mtime:
        return gemerkt[1]

    try:
        vorhanden = set(pq.read_schema(pfad).names)
        fehlend = [s for s in spalten if s not in vorhanden]
        if fehlend:
            raise DatenraumUnbrauchbar(
                f"Tabelle '{tabelle}' hat die Spalten {fehlend} nicht -- "
                "der Konnektor hat sie umbenannt oder entfernt"
            )
        zeilen = pq.read_table(pfad, columns=list(spalten)).to_pylist()
    except DatenraumUnbrauchbar:
        raise
    except Exception as exc:  # noqa: BLE001 -- eine kaputte Datei ist ein Befund, kein Absturz
        raise DatenraumUnbrauchbar(f"Tabelle '{tabelle}' nicht lesbar: {exc}") from exc

    _vorrat[tabelle] = (mtime, zeilen)
    return zeilen


def _leere_spalten_pruefen(tabelle: str, gebraucht: tuple[str, ...]) -> None:
    """Auf Spalten stoppen, die der Katalog als durchgehend leer gemeldet hat.

    Der Wächter im Schreibpfad meldet sie bereits; ihn hier zu ignorieren hiesse,
    auf eine Spalte zu rechnen, von der wir wissen, dass sie nichts trägt.
    ``bexio_kreditoren.offen_betrag`` ist der Anlass: bei allen 435 Zeilen 0.00,
    auch bei den offenen.
    """
    eintrag = (katalog_lesen().get("tabellen") or {}).get(tabelle) or {}
    leer = set(eintrag.get("leere_spalten") or [])
    betroffen = sorted(leer & set(gebraucht))
    if betroffen:
        raise DatenraumUnbrauchbar(
            f"'{tabelle}': die Spalten {betroffen} sind über den ganzen Bestand leer "
            "-- eine Zahl daraus wäre erfunden"
        )


def _nummer(wert: object) -> int:
    """Eine Kontonummer als Zahl; unlesbar ergibt 0.

    ``0`` ist kein Aufwands-, Bilanz- oder Bankkonto und fällt damit aus jeder
    Klassifikation heraus -- dasselbe Verhalten, das die Live-Fassung für ein nicht
    auflösbares Konto hatte.
    """
    try:
        return int(str(wert or "").strip())
    except ValueError:
        return 0


# ── Stand ────────────────────────────────────────────────


def stand() -> dict:
    """Alter und Zustand der Tabellen, aus denen die Finanzansicht rechnet."""
    tabellen = katalog_lesen().get("tabellen") or {}
    jetzt = datetime.now(timezone.utc)

    aeltester: str | None = None
    alter_stunden: float | None = None
    for name in SPALTENVERTRAG:
        roh = (tabellen.get(name) or {}).get("stand")
        if not roh:
            continue
        try:
            gemessen = datetime.fromisoformat(roh)
        except ValueError:
            continue
        stunden = (jetzt - gemessen).total_seconds() / 3600
        if alter_stunden is None or stunden > alter_stunden:
            alter_stunden, aeltester = stunden, roh

    veraltet = alter_stunden is not None and alter_stunden > WARNSCHWELLE_STUNDEN
    if veraltet:
        logger.warning(
            "Datenraum: älteste Finanztabelle ist %.1f Stunden alt (Schwelle %d)",
            alter_stunden, WARNSCHWELLE_STUNDEN,
        )
    return {
        "stand": aeltester,
        "alter_stunden": round(alter_stunden, 1) if alter_stunden is not None else None,
        "veraltet": veraltet,
    }


def stand_kennung() -> str:
    """Kennung des aktuellen Standes -- taugt als Teil eines Cache-Schlüssels.

    Bindet den Vorrat der Endpunkte an den Abgleich: nach einem Abgleich ändert sich
    die Kennung, und eine gemerkte Antwort kann nicht mehr getroffen werden. Ohne das
    zeigte die Ansicht einen neuen Stand über alten Zahlen.
    """
    return str(stand().get("stand") or "leer")


# ── Buchhaltung ──────────────────────────────────────────


def journal(von: str, bis: str) -> list[dict]:
    """Buchungen eines Zeitraums, Beträge in **Franken**.

    ``betrag_chf`` und nicht ``betrag``: die Buchungswährung ist über mehrere
    Währungen nicht summierbar. Genau daran scheiterte die Live-Fassung leise.
    """
    _leere_spalten_pruefen("bexio_journal", SPALTENVERTRAG["bexio_journal"])
    von_d, bis_d = date.fromisoformat(von[:10]), date.fromisoformat(bis[:10])

    ergebnis: list[dict] = []
    for z in _zeilen("bexio_journal"):
        tag = z.get("datum")
        if not isinstance(tag, date) or not (von_d <= tag <= bis_d):
            continue
        ergebnis.append({
            "datum": tag.isoformat(),
            "monat": tag.strftime("%Y-%m"),
            "betrag_chf": float(z.get("betrag_chf") or 0.0),
            "soll_nr": _nummer(z.get("soll_konto_nr")),
            "haben_nr": _nummer(z.get("haben_konto_nr")),
        })
    return ergebnis


def kontonamen() -> dict[int, str]:
    """Kontonummer auf Kontobezeichnung, ohne Nummernpräfix."""
    return {
        _nummer(z.get("konto_nr")): str(z.get("konto") or "")
        for z in _zeilen("bexio_konten")
    }


def bankkonten() -> list[dict]:
    """Die Konten, auf denen tatsächlich Geld liegt.

    Bewusst eine eigene Tabelle statt eines Nummernbereichs: ``1090 Transferkonto``
    und ``1099 Unklare Beträge`` liegen im selben Hunderterblock wie die echten
    Bankkonten. Ein Saldo, der sie mitzählt, ist falsch und sieht richtig aus.
    """
    return [
        {
            "konto_nr": _nummer(z.get("konto_nr")),
            "name": str(z.get("name") or z.get("konto") or "").strip(),
        }
        for z in _zeilen("bexio_bankkonten")
        if _nummer(z.get("konto_nr"))
    ]


def rechnungen() -> list[dict]:
    """Alle Kundenrechnungen mit der schon getroffenen Umsatzentscheidung.

    ``ist_umsatz`` ist der Grund, warum diese Funktion existiert: ein Entwurf wurde
    nie gestellt und ist kein Umsatz, eine offene Rechnung ist einer. Wer den Status
    nicht entschlüsselt, zählt Entwürfe mit -- 8'000 CHF im Jahr 2026.
    """
    _leere_spalten_pruefen("bexio_rechnungen", SPALTENVERTRAG["bexio_rechnungen"])
    ergebnis: list[dict] = []
    for z in _zeilen("bexio_rechnungen"):
        tag = z.get("datum")
        if not isinstance(tag, date):
            continue
        ergebnis.append({
            "datum": tag.isoformat(),
            "monat": tag.strftime("%Y-%m"),
            "brutto": float(z.get("brutto") or 0.0),
            "offen": float(z.get("offen") or 0.0),
            "ist_umsatz": bool(z.get("ist_umsatz")),
            "status": str(z.get("status") or ""),
            "kunde": str(z.get("kunde") or ""),
            "kunden_id": z.get("kunden_id"),
        })
    return ergebnis


def umsatz_je_monat() -> dict[str, float]:
    """Fakturierter Umsatz je Monat, brutto -- Entwürfe zählen nicht."""
    ergebnis: dict[str, float] = {}
    for r in rechnungen():
        if not r["ist_umsatz"]:
            continue
        ergebnis[r["monat"]] = ergebnis.get(r["monat"], 0.0) + r["brutto"]
    return ergebnis


def offene_debitoren() -> tuple[float, int]:
    """Summe und Anzahl der offenen Forderungen.

    ``ist_umsatz`` filtert auch hier, und zwar nicht aus Symmetrie: eine
    Entwurfsrechnung hat einen offenen Betrag, weil nie darauf gezahlt wurde. Sie
    ist trotzdem keine Forderung -- niemand ausserhalb des Hauses kennt sie.
    """
    summe, anzahl = 0.0, 0
    for r in rechnungen():
        if r["ist_umsatz"] and r["offen"] > 0.01:
            summe += r["offen"]
            anzahl += 1
    return round(summe, 2), anzahl


def geschaeftsjahre() -> list[dict]:
    """Geschäftsjahre mit Abschlussstand, aufsteigend."""
    zeilen = [
        {
            "jahr": z.get("jahr"),
            "von": z.get("von").isoformat() if isinstance(z.get("von"), date) else None,
            "bis": z.get("bis").isoformat() if isinstance(z.get("bis"), date) else None,
            "ist_abgeschlossen": bool(z.get("ist_abgeschlossen")),
        }
        for z in _zeilen("bexio_geschaeftsjahre")
    ]
    return sorted(zeilen, key=lambda z: z["von"] or "")


def geschaeftsjahr_beginn() -> str:
    """Erster Tag des offenen Geschäftsjahrs.

    Ohne offenes Jahr gilt der 1. Januar des laufenden Kalenderjahres. Die
    Live-Fassung hatte an dieser Stelle ``2025-01-01`` fest verdrahtet -- eine
    Zeitbombe, die den Banksaldo ab 2027 aus dem falschen Jahr aufsummiert hätte.
    """
    for j in reversed(geschaeftsjahre()):
        if not j["ist_abgeschlossen"] and j["von"]:
            return j["von"]
    return f"{date.today().year}-01-01"


# ── Zeiterfassung ────────────────────────────────────────


def zeiteintraege(von: str, bis: str, *, nur_verrechenbar: bool = True) -> list[dict]:
    """Zeiteinträge eines Zeitraums.

    ``betrag`` ist der verrechenbare Betrag **ohne** Mehrwertsteuer -- Satz mal
    Zeit, wie Toggl ihn führt. Der Aufrufer rechnet ihn brutto, wenn er ihn neben
    fakturierte Beträge stellt.

    ``verrechenbar`` steht auch dann in jeder Zeile, wenn danach gefiltert wurde:
    ein Aufrufer mit ``nur_verrechenbar=False`` will die beiden Arten
    unterscheiden, und ohne das Merkmal wäre der Parameter nur eine Möglichkeit,
    unbemerkt das Falsche zu zählen.
    """
    _leere_spalten_pruefen("toggl_zeiteintraege", SPALTENVERTRAG["toggl_zeiteintraege"])
    von_d, bis_d = date.fromisoformat(von[:10]), date.fromisoformat(bis[:10])

    ergebnis: list[dict] = []
    for z in _zeilen("toggl_zeiteintraege"):
        tag = z.get("datum")
        if not isinstance(tag, date) or not (von_d <= tag <= bis_d):
            continue
        if nur_verrechenbar and not z.get("verrechenbar"):
            continue
        ergebnis.append({
            "datum": tag.isoformat(),
            "monat": tag.strftime("%Y-%m"),
            "stunden": float(z.get("stunden") or 0.0),
            "betrag": float(z.get("betrag") or 0.0),
            "stundensatz": float(z.get("stundensatz") or 0.0),
            "verrechenbar": bool(z.get("verrechenbar")),
            "projekt": str(z.get("projekt") or ""),
            "projekt_id": z.get("projekt_id"),
            "kunde": str(z.get("kunde") or ""),
        })
    return ergebnis


def zeitraum_der_zeiterfassung() -> tuple[str, str] | None:
    """Ältester und jüngster erfasster Tag -- die Grenze der Auskunftsfähigkeit.

    Die Zeiterfassung liegt als rollende 24 Monate im Datenraum. Eine Frage, die
    weiter zurückreicht, ist nicht falsch zu beantworten, sondern gar nicht.
    """
    tage = [z.get("datum") for z in _zeilen("toggl_zeiteintraege")]
    echte = sorted(t for t in tage if isinstance(t, date))
    if not echte:
        return None
    return echte[0].isoformat(), echte[-1].isoformat()


def stunden_je_projekt(von: str, bis: str) -> list[dict]:
    """Stunden, Betrag und Effektivsatz je Projekt in einem Zeitraum."""
    gruppen: dict[tuple, dict] = {}
    for e in zeiteintraege(von, bis):
        schluessel = (e["projekt_id"], e["projekt"])
        eintrag = gruppen.setdefault(schluessel, {
            "projekt_id": e["projekt_id"],
            "projekt": e["projekt"],
            "kunde": e["kunde"],
            "stunden": 0.0,
            "betrag": 0.0,
        })
        eintrag["stunden"] += e["stunden"]
        eintrag["betrag"] += e["betrag"]

    ergebnis = []
    for eintrag in gruppen.values():
        stunden = round(eintrag["stunden"], 2)
        betrag = round(eintrag["betrag"], 2)
        ergebnis.append({
            **eintrag,
            "stunden": stunden,
            "betrag": betrag,
            "satz": round(betrag / stunden, 2) if stunden > 0 else 0.0,
        })
    ergebnis.sort(key=lambda z: z["betrag"], reverse=True)
    return ergebnis


def effektive_stundensaetze(monate: int = 12) -> dict[int, float]:
    """Effektivsatz je Projekt (Betrag durch Stunden) über die letzten Monate.

    Die Quelle der Wahrheit für zugesagte Arbeit: was tatsächlich abgerechnet wurde,
    nicht was am Projekt hinterlegt ist.
    """
    heute = date.today()
    von = (heute.replace(day=1) - timedelta(days=31 * monate)).isoformat()
    saetze: dict[int, float] = {}
    for eintrag in stunden_je_projekt(von, heute.isoformat()):
        kennung = eintrag.get("projekt_id")
        if kennung is None or eintrag["stunden"] <= 0 or eintrag["betrag"] <= 0:
            continue
        saetze[int(kennung)] = eintrag["satz"]
    return saetze
