"""Lokaler Datenraum: die Tabellen der Fachsysteme als Dateien auf der Platte.

Warum es ihn gibt: Eine Auswertung über Einzelabrufe eines MCP-Werkzeugs scheitert
zweifach. Sie ist langsam -- jede Runde ist eine LLM-Inferenz, und am 01.09.2026
lief eine Umsatzfrage nach acht Runden ins Zeitlimit. Und sie ist teuer im falschen
Sinn: die Rohdaten wandern durch den Kontext des Modells, womit die Wahl zwischen
lokalem und öffentlichem Modell zur Datenschutzfrage wird.

Der Datenraum dreht das um. Ein Abgleich im Takt schreibt die Tabellen als Parquet
auf die Platte; der Executor hängt das Verzeichnis bei **jedem** Sandbox-Lauf
schreibgeschützt unter ``/daten`` ein. Der Agent beantwortet eine Umsatzfrage dann in
einer einzigen Runde mit DuckDB oder pandas -- und keine einzige Zeile erreicht das
Modell. Damit ist die Modellwahl wieder das, was sie sein soll: eine Frage der
Qualität, nicht des Datenschutzes.

Drei Eigenschaften sind nicht verhandelbar:

* **Atomarer Tausch.** Geschrieben wird nach ``.tmp``, dann ``os.replace``. Ein
  Leser sieht nie eine halb geschriebene Tabelle.
* **Der Katalog trägt den Stand und den letzten Fehler.** Ein stiller Ausfall des
  Abgleichs sähe sonst aus wie «es gibt keine Daten» -- die gefährlichste aller
  Antworten, weil sie plausibel ist.
* **Teilausfall bleibt Teilausfall.** Fällt Toggl aus, bleiben die Bexio-Tabellen
  stehen und behalten ihren alten Stand. Nichts wird geleert, weil eine Quelle
  schweigt.

Wer an die Daten kommt -- und wer nicht:

Der Datenraum enthält den vollständigen Kundenbestand dreier Fachsysteme. Er ist
deshalb an drei Stellen gebunden, und alle drei bestanden schon vorher:

1. **Owner-gebunden.** Jeder Pfad in ``routers/code_execute.py`` verlangt
   ``require_role("owner")``, und ``routers/tasks.py`` verweigert einem Member
   ``assignee='agent'`` bei Anlage wie Änderung. Ein Member kann also weder selbst
   Code ausführen noch einen Agenten dazu bringen.
2. **Ohne Netz.** Die Sandbox läuft mit ``--network none`` und ohne Secrets. Was sie
   liest, kann sie nirgendwohin schicken; ihr einziger Rückkanal ist das Ergebnis,
   das ein Mensch im Chat sieht.
3. **Nur lesend.** ``/daten`` ist read-only eingehängt. Der Datenraum kann aus der
   Sandbox heraus nicht verändert werden.

Die Dateirechte im Wegwerf-Container sind bewusst **nicht** die Schutzgrenze -- der
Sandbox-Benutzer muss lesen können. Die Grenze ist das Heimverzeichnis auf dem Wirt.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.config import get_settings
from app.core.principal import get_owner_settings
from app.database import async_session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "bexio"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "toggl"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "pipedrive"))

logger = logging.getLogger("taskpilot.datenraum")

KATALOG_NAME = "_katalog.json"

# Takt: der volle Abgleich läuft nachts, die Bewegungsdaten stündlich. Der Wert ist
# der Abstand zwischen zwei Prüfungen der Schleife, nicht der Abstand zweier Abgleiche.
PRUEF_INTERVALL_SEKUNDEN = 300

_worker_task: asyncio.Task | None = None
_laeuft = asyncio.Lock()


def datenraum_pfad() -> Path:
    """Verzeichnis des Datenraums (anlegen, falls nicht vorhanden).

    Die Rechte sind bewusst 0755 und nicht 0700: der Sandbox-Container läuft als
    eigener, unprivilegierter Benutzer und muss ``/daten`` lesen können. Die
    Schutzgrenze ist nicht das POSIX-Bit im Wegwerf-Container, sondern das
    Heimverzeichnis auf dem Wirt, die fehlende Netzverbindung der Sandbox und die
    Owner-Bindung des Ausführungspfads.
    """
    cfg = get_settings()
    roh = (cfg.datenraum_dir or "").strip()
    pfad = Path(roh) if roh else Path.home() / ".local" / "share" / "taskpilot" / "datenraum"
    pfad.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(pfad, 0o755)
    except OSError:
        pass
    return pfad


# ── Schreiben ────────────────────────────────────────────


def durchgehend_leere_spalten(tabelle) -> list[str]:
    """Spalten, die in **jeder** Zeile leer sind.

    Der Wächter gegen die teuerste Fehlerart dieses Dienstes: eine Spalte, die es
    gibt, die aber nichts enthält. Genau das passierte mit ``toggl_zeiteintraege``
    -- ``kunde`` und ``datum`` waren in allen 2639 Zeilen leer, weil die Zuordnung
    gegen die falsche Antwortform geschrieben war. Die Tabelle sah vollständig aus,
    und eine Frage nach den Stunden für einen Kunden hätte «keine Zeit erfasst»
    ergeben -- plausibel, ruhig, falsch.

    Ein einzelner Fehlwert ist normal und wird nicht gemeldet. Dass eine Spalte über
    den ganzen Bestand nichts trägt, ist es nie: entweder wird sie falsch befüllt
    oder sie gehört nicht in die Tabelle.

    **Null zählt wie leer.** Der Wächter fing zunächst nur ``NULL`` und übersah
    damit ``bexio_kreditoren.offen_betrag``: Bexios ``pending_amount`` ist bei
    allen 435 Lieferantenrechnungen 0.0, auch bei den drei tatsächlich offenen
    über 4'496 CHF. Fachlich ist das derselbe Fehler -- eine Spalte, die eine
    Auskunft verspricht und keine gibt --, technisch war es einer zu wenig. Die
    Frage «wie viel schulde ich meinen Lieferanten» hätte 0 CHF ergeben.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    leer: list[str] = []
    for spaltenname in tabelle.column_names:
        spalte = tabelle.column(spaltenname)
        if spalte.null_count == tabelle.num_rows:
            leer.append(spaltenname)
        elif pa.types.is_string(spalte.type):
            # Bei Text zählt "" genauso als leer wie NULL.
            if not pc.sum(pc.binary_length(spalte.fill_null(""))).as_py():
                leer.append(spaltenname)
        elif pa.types.is_integer(spalte.type) or pa.types.is_floating(spalte.type):
            # Bei Zahlen zählt 0 wie leer -- aber nur, wenn KEIN Wert abweicht.
            # Eine Wahrheitsspalte ist ausgenommen: «überall false» ist eine
            # Aussage, keine Lücke.
            spanne = pc.max_element_wise(
                pc.abs(pc.min(spalte)), pc.abs(pc.max(spalte))
            ).as_py()
            if spanne == 0:
                leer.append(spaltenname)
    return leer


# Welche Spalten ein Datum tragen, ist bekannt: die Konnektoren bauen diese Zeilen
# selbst. Deshalb steht es hier deklariert und wird nicht aus dem Spaltennamen
# erschlossen -- eine Spalte «datum» zu erkennen, indem man sie «datum» nennt,
# wäre geraten, und bei ``beginn`` oder ``faellig_am`` liefe das Raten ins Leere.
ZEITSPALTEN: dict[str, tuple[str, ...]] = {
    "bexio_rechnungen": ("datum", "faellig_am", "geaendert_am"),
    "bexio_kontakte": ("geaendert_am",),
    "bexio_kreditoren": ("datum", "faellig_am", "erfasst_am"),
    "bexio_journal": ("datum",),
    "bexio_geschaeftsjahre": ("von", "bis", "abgeschlossen_am"),
    "toggl_zeiteintraege": ("datum", "beginn"),
    "pipedrive_deals": (
        "erstellt_am", "abgeschlossen_am", "gewonnen_am", "verloren_am",
        "erwarteter_abschluss",
    ),
    "invoiceinsight_rechnungen": (
        "datum", "faellig_am", "erneuerung_am", "leistung_von", "leistung_bis",
        "erfasst_am",
    ),
}


def als_zeitspalte(spalte):
    """Eine Textspalte in einen echten Datums- bzw. Zeitstempeltyp überführen.

    Der Anlass ist ein gescheiterter Lauf vom 02.09.2026: ``gewonnen_am`` lag als
    Text im Parquet, und ``EXTRACT(YEAR FROM gewonnen_am)`` beantwortete die Frage
    «wie viel pro Jahr» nicht mit einer Zahl, sondern mit einem Binder-Fehler. Wer
    ein Datum als Zeichenkette ablegt, verlangt bei jeder zeitlichen Frage eine
    Umwandlung von Hand -- und verlagert damit eine Aufgabe, die hier einmal
    richtig zu lösen ist, in jede einzelne Abfrage.

    Der Zieltyp folgt den Werten, nicht dem Namen: ein reines Datum wird ``date``,
    ein Zeitstempel bleibt Zeitstempel. Lässt sich eine Spalte nicht umwandeln,
    bleibt sie Text -- ein unbrauchbarer Wert ist schlimmer als ein unbequemer.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    if not pa.types.is_string(spalte.type):
        return None

    leer = pc.equal(pc.utf8_trim_whitespace(spalte), "")
    bereinigt = pc.if_else(leer, pa.scalar(None, pa.string()), spalte)
    if bereinigt.null_count == len(bereinigt):
        return None

    laenge = pc.max(pc.utf8_length(bereinigt)).as_py() or 0
    kandidaten = (
        [pa.date32()] if laenge <= 10
        else [pa.timestamp("us", tz="UTC"), pa.timestamp("us"), pa.date32()]
    )
    for zieltyp in kandidaten:
        try:
            return pc.cast(bereinigt, zieltyp)
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, ValueError):
            continue
    return None


def zeitspalten_setzen(name: str, tabelle):
    """Die deklarierten Zeitspalten einer Tabelle in echte Typen überführen."""
    for spaltenname in ZEITSPALTEN.get(name, ()):
        if spaltenname not in tabelle.column_names:
            continue
        umgewandelt = als_zeitspalte(tabelle.column(spaltenname))
        if umgewandelt is None:
            logger.warning(
                "Datenraum: Spalte '%s.%s' liess sich nicht als Datum lesen und "
                "bleibt Text -- zeitliche Abfragen darauf scheitern",
                name, spaltenname,
            )
            continue
        tabelle = tabelle.set_column(
            tabelle.column_names.index(spaltenname), spaltenname, umgewandelt
        )
    return tabelle


def tabelle_schreiben(name: str, zeilen: list[dict]) -> dict:
    """Eine Tabelle als Parquet ablegen und ihre Beschreibung zurückgeben.

    Leere Ergebnisse werden **nicht** geschrieben: eine vorhandene Tabelle durch eine
    leere zu ersetzen, wäre der stille Datenverlust, den dieser Dienst verhindern
    soll. Stattdessen bleibt die alte Datei stehen und der Aufrufer meldet den
    leeren Lauf.

    Durchgehend leere Spalten werden geschrieben, aber vermerkt -- siehe
    ``durchgehend_leere_spalten``. Sie zu verschweigen hiesse, dem Agenten eine
    Spalte anzubieten, auf die er sich nicht verlassen kann.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not zeilen:
        raise ValueError(f"Tabelle '{name}' kam leer zurück -- alter Stand bleibt stehen")

    verzeichnis = datenraum_pfad()
    ziel = verzeichnis / f"{name}.parquet"
    vorlaeufig = verzeichnis / f".{name}.parquet.tmp"

    tabelle = zeitspalten_setzen(name, pa.Table.from_pylist(zeilen))
    pq.write_table(tabelle, vorlaeufig, compression="zstd")
    os.replace(vorlaeufig, ziel)
    try:
        os.chmod(ziel, 0o644)
    except OSError:
        pass

    beschreibung = {
        "datei": ziel.name,
        "zeilen": tabelle.num_rows,
        "spalten": {f.name: str(f.type) for f in tabelle.schema},
        "bytes": ziel.stat().st_size,
    }
    leere = durchgehend_leere_spalten(tabelle)
    if leere:
        beschreibung["leere_spalten"] = leere
        logger.warning(
            "Datenraum: Tabelle '%s' hat durchgehend leere Spalten: %s -- "
            "vermutlich eine falsche Feldzuordnung",
            name, ", ".join(leere),
        )
    return beschreibung


def katalog_lesen() -> dict:
    """Katalog laden; fehlt oder bricht er, gilt ein leerer Katalog."""
    pfad = datenraum_pfad() / KATALOG_NAME
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tabellen": {}, "quellen": {}}


def katalog_schreiben(katalog: dict) -> None:
    """Katalog atomar ersetzen."""
    verzeichnis = datenraum_pfad()
    ziel = verzeichnis / KATALOG_NAME
    vorlaeufig = verzeichnis / f".{KATALOG_NAME}.tmp"
    vorlaeufig.write_text(json.dumps(katalog, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(vorlaeufig, ziel)
    try:
        os.chmod(ziel, 0o644)
    except OSError:
        pass


# ── Quellen ──────────────────────────────────────────────


@dataclass
class Quelle:
    """Eine Datenquelle: was sie liefert und wie oft sie gefragt wird."""

    lader: Callable[[dict], Awaitable[tuple[dict[str, list[dict]], dict]]]
    beschreibung: str
    stuendlich: bool = False


async def _lade_bexio(settings: dict) -> tuple[dict[str, list[dict]], dict]:
    """Die Buchhaltung als Ganzes: Debitoren, Kreditoren, Journal und Stammdaten.

    Fuenf Tabellen, weil vier Fragen gestellt werden und keine einzelne Tabelle
    mehr als eine davon beantwortet:

    * «Was haben wir eingenommen» -- ``bexio_rechnungen``
    * «Was hat uns etwas gekostet» -- ``bexio_journal``, und **nur** dort. Ueber
      den Kreditorenweg liefen 2025 bloss 88'177 von 401'459 CHF Aufwand.
    * «Was ist offen, wann faellig» -- ``bexio_kreditoren``
    * «Was bedeutet Konto 227, ist 2026 schon abgeschlossen» -- die Stammdaten

    Das Journal braucht den Kontenplan, um Kennungen aufzuloesen, und die
    Geschaeftsjahre, um seine Abrufe zu begrenzen. Deshalb werden beide vor ihm
    geladen und ihr Ergebnis weitergereicht statt zweimal geholt.
    """
    from bexio_client import BexioClient, BexioConfig
    from journal import journal_laden
    from kreditoren import lieferantenrechnungen_laden
    from rechnungen import kontakte_laden, rechnungen_laden
    from stammdaten import geschaeftsjahre_laden, kontenplan_laden

    token = settings.get("bexio_api_token") or get_settings().bexio_api_token
    if not token:
        raise RuntimeError("Bexio-Token nicht konfiguriert")

    client = BexioClient(BexioConfig(api_token=token))
    kontenzeilen, konten = await kontenplan_laden(client)
    bestand = await rechnungen_laden(client)
    kontakte = await kontakte_laden(client)
    kreditoren = await lieferantenrechnungen_laden(client, konten)
    jahre = await geschaeftsjahre_laden(client)
    journal = await journal_laden(client, konten, jahre)

    hinweise: dict = {"waehrungen": bestand.waehrungen}
    if bestand.unbekannte_status:
        hinweise["unbekannte_status"] = bestand.unbekannte_status
    if bestand.kunden_ohne_namen:
        hinweise["kunden_ohne_namen"] = bestand.kunden_ohne_namen
    if bestand.ohne_mwst:
        hinweise["rechnungen_ohne_mwst"] = bestand.ohne_mwst

    # Der Kreditorenbefund gehört in den Katalog, nicht ins Protokoll: eine
    # unvollständige Tabelle, die aussieht wie eine vollständige, ist genau der
    # stille Fehler, den dieser Dienst verhindern soll.
    hinweise["kreditoren_gemeldet"] = kreditoren.gemeldet
    hinweise["kreditoren_waehrungen"] = kreditoren.waehrungen
    if not kreditoren.vollstaendig:
        hinweise["kreditoren_unvollstaendig"] = (
            f"{len(kreditoren.zeilen)} von {kreditoren.gemeldet} Zeilen"
        )
    if kreditoren.ohne_detail:
        hinweise["kreditoren_ohne_detail"] = len(kreditoren.ohne_detail)
    if kreditoren.unbekannte_status:
        hinweise["kreditoren_unbekannte_status"] = kreditoren.unbekannte_status
    if kreditoren.ohne_umrechnung:
        hinweise["kreditoren_ohne_umrechnung"] = kreditoren.ohne_umrechnung

    # Beim Journal ist die Vollstaendigkeit die einzige Eigenschaft, die zaehlt:
    # ein abgeschnittener Jahrgang sieht aus wie ein sparsames Jahr.
    hinweise["journal_jahre"] = sorted(j for j in journal.jahre if j)
    if not journal.blaettern_geprueft:
        hinweise["journal_blaettern"] = "ungeprüft (zu wenige Buchungen für die Probe)"
    elif journal.blaettern_wirkungslos:
        hinweise["journal_blaettern"] = "offset wirkt NICHT -- Jahrgänge am Limit sind unvollständig"
    if journal.jahre_am_limit:
        hinweise["journal_jahre_am_limit"] = journal.jahre_am_limit
    if journal.ohne_konto:
        hinweise["journal_ohne_konto"] = journal.ohne_konto
    if journal.unbekannte_herkunft:
        hinweise["journal_unbekannte_herkunft"] = journal.unbekannte_herkunft

    offene_jahre = [j["jahr"] for j in jahre if not j["ist_abgeschlossen"]]
    if offene_jahre:
        hinweise["offene_geschaeftsjahre"] = offene_jahre

    return (
        {
            "bexio_rechnungen": bestand.zeilen,
            "bexio_kontakte": kontakte,
            "bexio_kreditoren": kreditoren.zeilen,
            "bexio_journal": journal.zeilen,
            "bexio_konten": kontenzeilen,
            "bexio_geschaeftsjahre": jahre,
        },
        hinweise,
    )


async def _lade_toggl(settings: dict) -> tuple[dict[str, list[dict]], dict]:
    """Zeiteinträge und Projekte aus Toggl (rollende 24 Monate).

    Die Reports-API v3 antwortet **gruppiert**, nicht als flache Liste. Eine Zeile
    trägt ``project_id``, ``description``, ``billable`` und darunter ein Feld
    ``time_entries`` mit den eigentlichen Buchungen (``id``, ``start``, ``seconds``).

    Zwei Felder, die man auf der obersten Ebene erwartet, gibt es dort nicht:
    ``start`` und ``client_id``. Der erste Entwurf las beide dort -- mit dem Ergebnis,
    dass **alle 2639 Zeiteinträge** ohne Datum und ohne Kunde in den Datenraum gingen.
    Die Tabelle war vollzählig, jede Zeitfrage wäre trotzdem falsch beantwortet
    worden: nach Stunden für einen Kunden gefragt, hätte sie null ergeben.

    Deshalb wird hier aufgefaltet -- eine Zeile je tatsächlicher Buchung -- und der
    Kunde über das Projekt aufgelöst, wo er in Toggl hängt.
    """
    from toggl_client import TogglClient, TogglConfig

    cfg = get_settings()
    token = settings.get("toggl_api_token") or cfg.toggl_api_token
    workspace = int(settings.get("toggl_workspace_id") or cfg.toggl_workspace_id or 0)
    if not token or not workspace:
        raise RuntimeError("Toggl-Token oder Workspace nicht konfiguriert")

    client = TogglClient(TogglConfig(api_token=token, workspace_id=workspace))
    heute = date.today()
    von = (heute - timedelta(days=730)).isoformat()

    projekte = await client.list_projects(active="both")
    projektnamen = {p.get("id"): p.get("name") or "" for p in projekte}
    kunden = {c.get("id"): c.get("name") or "" for c in await client.list_clients(status="both")}
    # Der Kunde hängt am Projekt, nicht am Zeiteintrag -- siehe unten.
    projekt_kunde = {p.get("id"): p.get("client_id") for p in projekte}

    eintraege: list[dict] = []
    ohne_projekt = 0
    for gruppe in await client.search_all_time_entries(workspace, von, heute.isoformat()):
        projekt_id = gruppe.get("project_id")
        if projekt_id is None:
            ohne_projekt += 1
        kunden_id = projekt_kunde.get(projekt_id)

        satz_rappen = gruppe.get("hourly_rate_in_cents") or 0
        untereintraege = gruppe.get("time_entries") or []
        sekunden_gesamt = sum(u.get("seconds") or 0 for u in untereintraege)
        betrag_rappen = gruppe.get("billable_amount_in_cents") or 0

        for u in untereintraege:
            sekunden = u.get("seconds") or 0
            # Der Betrag gilt für die Gruppe. Ihn nach Sekunden aufzuteilen ist keine
            # Schätzung, sondern die Umkehrung seiner Entstehung (Satz mal Zeit).
            anteil = (sekunden / sekunden_gesamt) if sekunden_gesamt else 0
            eintraege.append({
                "eintrag_id": u.get("id"),
                "datum": (u.get("start") or "")[:10] or None,
                "beginn": u.get("start") or None,
                "projekt_id": projekt_id,
                "projekt": projektnamen.get(projekt_id, ""),
                "kunden_id": kunden_id,
                "kunde": kunden.get(kunden_id, ""),
                "person": gruppe.get("username") or "",
                "beschreibung": gruppe.get("description") or "",
                "stunden": round(sekunden / 3600, 4),
                "verrechenbar": bool(gruppe.get("billable")),
                "stundensatz": round(satz_rappen / 100, 2),
                "betrag": round(betrag_rappen * anteil / 100, 2),
                "waehrung": gruppe.get("currency") or "",
            })

    projektzeilen = [{
        "projekt_id": p.get("id"),
        "projekt": p.get("name") or "",
        "kunden_id": p.get("client_id"),
        "kunde": kunden.get(p.get("client_id"), ""),
        "aktiv": bool(p.get("active")),
        "verrechenbar": bool(p.get("billable")),
    } for p in projekte]

    # Gezählt wird der unaufgelöste Name, nicht die fehlende Kennung: ein Projekt mit
    # Kundennummer, zu der es keinen Namen gibt, sieht in der Tabelle genauso aus wie
    # eines ganz ohne Kunden -- und ist doch ein Mangel statt einer Tatsache.
    hinweise: dict = {
        "zeitraum": f"{von} bis {heute.isoformat()}",
        "eintraege_ohne_kundennamen": sum(1 for e in eintraege if not e["kunde"]),
    }
    if ohne_projekt:
        hinweise["gruppen_ohne_projekt"] = ohne_projekt

    return {"toggl_zeiteintraege": eintraege, "toggl_projekte": projektzeilen}, hinweise


async def _lade_pipedrive(settings: dict) -> tuple[dict[str, list[dict]], dict]:
    """Deals, Personen und Organisationen aus Pipedrive."""
    from pipedrive_client import PipedriveClient, PipedriveConfig

    token = settings.get("pipedrive_api_token") or get_settings().pipedrive_api_token
    if not token:
        raise RuntimeError("Pipedrive-Token nicht konfiguriert")

    client = PipedriveClient(PipedriveConfig(api_token=token))
    organisationen = await client.list_all("/organizations")
    personen = await client.list_all("/persons")
    orgnamen = {o.get("id"): o.get("name") or "" for o in organisationen}
    personennamen = {p.get("id"): p.get("name") or "" for p in personen}

    # Trichter und Phase kommen als Zahl. Undekodiert wären sie so nutzlos wie eine
    # Währungskennung: der Agent könnte nicht nach «Angebot» filtern, sondern nur
    # nach «5» -- und müsste raten, was das heisst.
    trichter = {t.get("id"): t.get("name") or "" for t in await client.list_all("/pipelines")}
    phasen = {s.get("id"): s.get("name") or "" for s in await client.list_all("/stages")}

    deals = []
    geloescht = 0
    for status in ("open", "won", "lost"):
        for d in await client.list_all("/deals", {"status": status}):
            # Gelöschte Deals liefert Pipedrive mit aus. Sie zählen nirgends mit --
            # aktuell sind es null, aber ein Filter, den man erst nach dem ersten
            # falschen Abschlussbericht einbaut, ist einer zu spät.
            if d.get("is_deleted"):
                geloescht += 1
                continue
            deals.append({
                "deal_id": d.get("id"),
                "titel": d.get("title") or "",
                "status": status,
                "wert": float(d.get("value") or 0),
                "waehrung": d.get("currency") or "",
                "organisation_id": d.get("org_id"),
                "organisation": orgnamen.get(d.get("org_id"), ""),
                "person_id": d.get("person_id"),
                "person": personennamen.get(d.get("person_id"), ""),
                "trichter": trichter.get(d.get("pipeline_id"), ""),
                "phase": phasen.get(d.get("stage_id"), ""),
                "wahrscheinlichkeit": d.get("probability"),
                "erstellt_am": d.get("add_time"),
                "abgeschlossen_am": d.get("close_time"),
                "gewonnen_am": d.get("won_time"),
                "verloren_am": d.get("lost_time"),
                "erwarteter_abschluss": d.get("expected_close_date"),
                "verlustgrund": d.get("lost_reason") or "",
                "archiviert": bool(d.get("is_archived")),
            })

    personenzeilen = [{
        "person_id": p.get("id"),
        "name": p.get("name") or "",
        "organisation_id": p.get("org_id"),
        "organisation": orgnamen.get(p.get("org_id"), ""),
    } for p in personen]

    orgzeilen = [{
        "organisation_id": o.get("id"),
        "name": o.get("name") or "",
        "adresse": (o.get("address") or {}).get("value") if isinstance(o.get("address"), dict) else (o.get("address") or ""),
    } for o in organisationen]

    hinweise: dict = {
        "deals_ohne_organisation": sum(1 for d in deals if not d["organisation_id"]),
    }
    if geloescht:
        hinweise["geloeschte_deals_uebersprungen"] = geloescht

    return (
        {
            "pipedrive_deals": deals,
            "pipedrive_personen": personenzeilen,
            "pipedrive_organisationen": orgzeilen,
        },
        hinweise,
    )


async def _lade_invoiceinsight(settings: dict) -> tuple[dict[str, list[dict]], dict]:
    """Kreditorenrechnungen aus InvoiceInsight -- die Belegebene zur Buchhaltung.

    Es ist **derselbe Gegenstand** wie in Bexio, nicht ein anderer. Der Abgleich vom
    03.09.2026 zeigt das Beleg für Beleg: von 16 T+R-Rechnungen in Bexio stehen 15
    hier mit identischem Datum und identischem Betrag, bei bexio AG 7 von 8 über
    acht Jahre. Wer daraus zwei getrennte Welten macht, vergleicht nichts mehr.

    Was InvoiceInsight hinzufügt, ist der Detailgrad: Produkt, Kategorie,
    Abrechnungszyklus, Erneuerungsdatum, Mehrwertsteuer -- und bei rund einem
    Fünftel der Belege die maschinell aus dem Einzahlungsschein gelesenen QR-Daten.
    Eine Buchhaltung hält je Beleg im Wesentlichen einen Betrag fest.

    Wo die Bestände auseinanderlaufen, gibt es genau drei Gründe, und keiner davon
    ist ein Rechenfehler: hier fehlende Belege (Ausgleichskasse 2022: Bexio 13,
    hier 8), der Versatz zwischen Rechnungs- und Buchungsdatum, der denselben Beleg
    über den Jahreswechsel in zwei Jahre legt, und Sammelbuchungen (Cursor 2026:
    129 Einzelrechnungen hier gegen 31 Buchungsvorgänge dort).
    """
    from app.services.invoiceinsight_client import InvoiceInsightClient

    cfg = get_settings()
    schluessel = settings.get("invoiceinsight_api_key") or cfg.invoiceinsight_api_key
    url = settings.get("invoiceinsight_url") or cfg.invoiceinsight_url
    if not schluessel or not url:
        raise RuntimeError("InvoiceInsight nicht konfiguriert")

    zeilen, befund = await InvoiceInsightClient(url, schluessel).export_alle_rechnungen()
    return {"invoiceinsight_rechnungen": zeilen}, befund


QUELLEN: dict[str, Quelle] = {
    "bexio": Quelle(
        lader=_lade_bexio,
        beschreibung=(
            "Die Buchhaltung: Buchungsjournal (vollständiger Aufwand), Debitoren, "
            "Kreditoren, Kontenplan und Geschäftsjahre"
        ),
        stuendlich=True,
    ),
    "invoiceinsight": Quelle(
        lader=_lade_invoiceinsight,
        beschreibung="Kreditorenrechnungen mit Produkt, Kategorie und Abrechnungszyklus",
        stuendlich=False,
    ),
    "toggl": Quelle(
        lader=_lade_toggl,
        beschreibung="Zeiteinträge und Projekte der Zeiterfassung",
        stuendlich=True,
    ),
    "pipedrive": Quelle(
        lader=_lade_pipedrive,
        beschreibung="Deals, Personen und Organisationen aus dem CRM",
        stuendlich=False,
    ),
}


# ── Abgleich ─────────────────────────────────────────────


async def quelle_abgleichen(name: str, settings: dict, katalog: dict) -> dict:
    """Eine Quelle abgleichen und den Katalog fortschreiben (in-place).

    Ein Fehler wird protokolliert und im Katalog vermerkt, nicht geworfen: eine
    Quelle darf die anderen nicht mitreissen.
    """
    quelle = QUELLEN[name]
    beginn = time.monotonic()
    jetzt = datetime.now(timezone.utc).isoformat()

    try:
        tabellen, hinweise = await quelle.lader(settings)
    except Exception as exc:  # noqa: BLE001 -- Teilausfall ist eingeplant
        logger.warning("Datenraum: Quelle '%s' fehlgeschlagen: %s", name, exc)
        eintrag = katalog.setdefault("quellen", {}).setdefault(name, {})
        eintrag["letzter_fehler"] = f"{type(exc).__name__}: {exc}"
        eintrag["letzter_versuch"] = jetzt
        return eintrag

    geschrieben, leer = {}, []
    for tabellenname, zeilen in tabellen.items():
        try:
            beschreibung = tabelle_schreiben(tabellenname, zeilen)
        except ValueError as exc:
            logger.warning("Datenraum: %s", exc)
            leer.append(tabellenname)
            continue
        beschreibung.update({"quelle": name, "stand": jetzt})
        katalog.setdefault("tabellen", {})[tabellenname] = beschreibung
        geschrieben[tabellenname] = beschreibung["zeilen"]

    eintrag = katalog.setdefault("quellen", {}).setdefault(name, {})
    eintrag.update({
        "beschreibung": quelle.beschreibung,
        "stand": jetzt,
        "letzter_versuch": jetzt,
        "letzter_fehler": None,
        "dauer_sekunden": round(time.monotonic() - beginn, 2),
        "tabellen": geschrieben,
    })
    if hinweise:
        eintrag["hinweise"] = hinweise
    if leer:
        eintrag["leer_geblieben"] = leer

    logger.info(
        "Datenraum: '%s' abgeglichen in %.1fs -- %s",
        name, eintrag["dauer_sekunden"],
        ", ".join(f"{t}: {z}" for t, z in geschrieben.items()) or "nichts geschrieben",
    )
    return eintrag


def _zeilen_lesen(name: str, spalten: tuple[str, ...]) -> list[dict]:
    """Wenige Spalten einer bestehenden Tabelle zurücklesen.

    Der Kundenschlüssel muss den **Gesamtstand** sehen, nicht die eben abgeglichene
    Quelle: er verbindet Bexio, Toggl und Pipedrive, und die drei werden getrennt
    aufgefrischt. Gelesen wird deshalb aus den Dateien und nicht aus dem Lauf.
    """
    import pyarrow.parquet as pq

    pfad = datenraum_pfad() / f"{name}.parquet"
    if not pfad.exists():
        return []
    try:
        vorhanden = set(pq.read_schema(pfad).names)
        return pq.read_table(
            pfad, columns=[s for s in spalten if s in vorhanden]
        ).to_pylist()
    except Exception as exc:  # noqa: BLE001 -- eine kaputte Datei darf nicht alles reissen
        logger.warning("Datenraum: '%s' nicht lesbar: %s", name, exc)
        return []


async def _kundschaften_vorschlagen() -> None:
    """Neue Kundschaften zuordnen lassen, bevor die Tabelle geschrieben wird.

    Läuft vor ``kundenschluessel_schreiben``, damit ein angenommener Vorschlag
    noch in denselben Abgleich einfliesst statt erst eine Stunde später zu wirken.

    Best-effort und bewusst still: ein nicht erreichbares Modell ist kein Grund,
    einen Abgleich scheitern zu lassen. Der Schlüssel bleibt dann so gut, wie er
    schon war -- die Lücke steht ohnehin im Katalog.
    """
    from app.services import kundenschluessel as ks

    try:
        await ks.vorschlagen()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Datenraum: Kundschaften nicht vorschlagbar: %s", exc)


def kundenschluessel_schreiben(katalog: dict) -> None:
    """Die gepflegte Kundenzuordnung als Tabelle ablegen und ihre Lücken melden.

    Läuft nach jedem Abgleich, weil sich der Bestand der drei Systeme geändert
    haben kann. Scheitert sie, bleibt der alte Stand stehen und der Rest des
    Abgleichs gilt trotzdem -- eine fehlende Zuordnung ist ärgerlich, ein
    verlorener Abgleich wäre schlimmer.
    """
    from app.services import kundenschluessel as ks

    try:
        gebraucht: dict[str, set[str]] = {}
        for tabelle, kennung, anzeige in ks.SYSTEME.values():
            gebraucht.setdefault(tabelle, set()).update((kennung, anzeige))
        for tabelle, kennung, bedingung in ks.RELEVANZ.values():
            spalten = gebraucht.setdefault(tabelle, set())
            spalten.add(kennung)
            if bedingung is not None:
                spalten.add(bedingung[0])
        tabellen = {
            name: _zeilen_lesen(name, tuple(sorted(spalten)))
            for name, spalten in gebraucht.items()
        }
        zeilen, befund = ks.aufbauen(tabellen)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Datenraum: Kundenschlüssel nicht aufbaubar: %s", exc)
        return

    eintrag = katalog.setdefault("quellen", {}).setdefault("kundenschluessel", {})
    eintrag.update({
        "beschreibung": (
            "Gepflegte Zuordnung: welche Kennungen in Bexio, Toggl und Pipedrive "
            "dieselbe Kundschaft meinen (docs/kundenschluessel.yaml)"
        ),
        "stand": datetime.now(timezone.utc).isoformat(),
        "letzter_fehler": None,
        "hinweise": befund,
    })

    if not zeilen:
        logger.info("Datenraum: Kundenschlüssel ist leer -- keine Tabelle geschrieben")
        return
    try:
        beschreibung = tabelle_schreiben("kundenschluessel", zeilen)
    except ValueError as exc:
        logger.warning("Datenraum: %s", exc)
        return
    beschreibung.update({"quelle": "kundenschluessel", "stand": eintrag["stand"]})
    katalog.setdefault("tabellen", {})["kundenschluessel"] = beschreibung
    eintrag["tabellen"] = {"kundenschluessel": beschreibung["zeilen"]}


@contextmanager
def _dateisperre():
    """Prozessübergreifende Sperre für den Abgleich.

    Nötig, weil zwei Prozesse schreiben können: der Worker im Backend im Takt und
    der MCP-Subprozess auf Zuruf des Agenten. ``os.replace`` verhindert zwar halbe
    Dateien, nicht aber, dass ein Katalog den anderen überholt.
    """
    sperre = datenraum_pfad() / ".abgleich.lock"
    with open(sperre, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


async def abgleichen(quellen: list[str] | None = None, *, vorschlaege: bool = True) -> dict:
    """Die genannten Quellen abgleichen (Default: alle) und den Katalog neu schreiben.

    ``vorschlaege=False`` überspringt die Zuordnungsvorschläge. Der Aufruf über das
    Werkzeug ``datenraum_auffrischen`` setzt das, und zwar aus einem gemessenen
    Grund: die Vorschläge kosten acht Modellaufrufe auf demselben lokalen Modell,
    das in diesem Moment der fragende Agent belegt. Am 03.09.2026 hat genau das
    einen Chat-Lauf ins Zeitlimit von 600 Sekunden getrieben -- der Agent wartete
    minutenlang auf seinen eigenen Auffrischungsaufruf, während dieser um dieselbe
    GPU kämpfte. Im Hintergrundtakt ist die Arbeit richtig, im Werkzeugpfad nicht.
    """
    async with _laeuft:
        namen = [n for n in (quellen or list(QUELLEN)) if n in QUELLEN]
        if not namen:
            return katalog_lesen()

        try:
            async with async_session() as db:
                settings = await get_owner_settings(db)
        except Exception:  # noqa: BLE001
            logger.warning("Datenraum: Owner-Einstellungen nicht lesbar, nutze .env")
            settings = {}

        with _dateisperre():
            katalog = katalog_lesen()
            for name in namen:
                await quelle_abgleichen(name, settings, katalog)

            if vorschlaege:
                await _kundschaften_vorschlagen()
            kundenschluessel_schreiben(katalog)
            katalog["stand"] = datetime.now(timezone.utc).isoformat()
            katalog["verzeichnis_in_der_sandbox"] = "/daten"
            katalog_schreiben(katalog)
            # Nur nach einem vollstaendigen Lauf: bei einem Teilabgleich stuenden die
            # Tabellen der uebersprungenen Quellen zu Unrecht als verwaist da.
            if set(namen) == set(QUELLEN):
                verwaiste_tabellen_entfernen()
        return katalog


# ── Worker ───────────────────────────────────────────────


async def _worker_loop() -> None:
    """Prüft im Takt, welche Quellen fällig sind.

    Stündliche Quellen laufen bei jeder vollen Stunde, alle Quellen einmal nachts.
    Der erste Durchlauf startet verzögert, damit er nicht mit dem Hochfahren
    konkurriert.
    """
    cfg = get_settings()
    await asyncio.sleep(90)
    logger.info(
        "Datenraum-Worker gestartet (Prüfung alle %ds, Vollabgleich um %02d Uhr)",
        PRUEF_INTERVALL_SEKUNDEN, cfg.datenraum_full_hour,
    )

    if not katalog_lesen().get("tabellen"):
        logger.info("Datenraum ist leer -- einmaliger Erstabgleich")
        await abgleichen()

    letzte_stunde: int | None = None
    letzter_volltag: date | None = None

    while True:
        try:
            jetzt = datetime.now()
            if letzter_volltag != jetzt.date() and jetzt.hour == cfg.datenraum_full_hour:
                letzter_volltag = jetzt.date()
                letzte_stunde = jetzt.hour
                await abgleichen()
            elif letzte_stunde != jetzt.hour:
                letzte_stunde = jetzt.hour
                faellig = [n for n, q in QUELLEN.items() if q.stuendlich]
                if faellig:
                    await abgleichen(faellig)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- der Worker darf nie sterben
            logger.exception("Datenraum-Worker: Durchlauf fehlgeschlagen")
        await asyncio.sleep(PRUEF_INTERVALL_SEKUNDEN)


async def start_datenraum_worker() -> None:
    global _worker_task
    if not get_settings().datenraum_enabled:
        logger.info("Datenraum deaktiviert (TP_DATENRAUM_ENABLED=false)")
        return
    _worker_task = asyncio.create_task(_worker_loop())


async def stop_datenraum_worker() -> None:
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _worker_task = None


# ── Aufräumen ────────────────────────────────────────────


def verwaiste_tabellen_entfernen() -> list[str]:
    """Parquet-Dateien löschen, die keine Quelle mehr beansprucht.

    Damit verschwinden Kundendaten, sobald eine Quelle abgeschaltet wird -- sonst
    bliebe der letzte Abzug unbefristet liegen.
    """
    katalog = katalog_lesen()
    bekannt = {f"{name}.parquet" for name in katalog.get("tabellen", {})}
    entfernt = []
    for datei in datenraum_pfad().glob("*.parquet"):
        if datei.name not in bekannt:
            datei.unlink(missing_ok=True)
            entfernt.append(datei.name)
    if entfernt:
        logger.info("Datenraum: verwaiste Tabellen entfernt: %s", entfernt)
    return entfernt


def datenraum_leeren() -> None:
    """Den gesamten Datenraum löschen (Auskunfts- und Löschbegehren, Neuaufbau)."""
    verzeichnis = datenraum_pfad()
    shutil.rmtree(verzeichnis, ignore_errors=True)
    logger.warning("Datenraum vollständig geleert: %s", verzeichnis)
