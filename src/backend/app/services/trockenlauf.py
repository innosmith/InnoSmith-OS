"""Trockenlauf: derselbe Weg, nur der letzte Schritt wird angehalten.

Ein Trockenlauf, der **anderen Code** durchläuft als der echte Lauf, beweist
nichts über den echten Lauf. Er beruhigt und prüft nichts — die Stellen, an
denen es schiefgeht, sind gerade die, die er überspringt. Deshalb steht hier
keine zweite, vorsichtige Fassung des Rechnungslaufs, sondern eine **Sperre um
die Fachsystem-Clients**: Bexio lesen, Toggl lesen, Stunden auffalten,
Positionen rechnen, PDF bauen, Zielordner bestimmen — alles läuft echt, mit den
echten Daten. Erst der eine Aufruf, der etwas verändert, wird abgefangen und
notiert.

## Unbekanntes wird abgewiesen, nicht durchgelassen

Die Sperre kennt jede Methode, die der Rechnungslauf benutzt, und teilt sie in
lesend und schreibend. Eine **nicht eingeordnete** Methode wirft
``UnbekannterAufruf``. Das ist die tragende Entscheidung dieses Moduls: eine
Sperre, die Unbekanntes durchreicht, lässt genau die Methode durch, die nach
ihrer Entstehung dazukam — also die, an die niemand mehr dachte. Ein lauter
Fehlschlag im Trockenlauf kostet eine Minute, ein stiller Schreibzugriff eine
Rechnung.

Dass die Einteilung nicht schlicht «POST heisst schreiben» lauten kann, zeigt
Bexio selbst: ``search_invoices``, ``search_contact_by_name`` und
``search_accounts`` sind POST-Aufrufe und lesen.

## Der Schatten

Zwei Leseaufrufe hängen an unterdrückten Schreibaufrufen. ``ablegen`` zählt nach
dem Hochladen nach — im Trockenlauf fände es nichts und meldete einen Fehler,
den es nicht gibt. Und die Sperrklinke (``projektebene_gilt``) liest Ordner, die
derselbe Lauf gerade angelegt hätte.

Darum führt die Sperre einen **Schatten**: was sie vorgibt zu schreiben, sehen
die Leseaufrufe danach. Damit läuft auch das Nachzählen echt durch, inklusive
des Grössenvergleichs — geprüft wird der Weg, nicht bloss die Absicht.

## Was der Trockenlauf nicht kann

Er kann nicht sagen, ob Bexio die Rechnung annimmt. Eine Rechnungsnummer
entsteht erst beim Anlegen, also trägt der Platzhalter sichtbar
``(Trockenlauf)``. Die Zahlungsfrist des Platzhalters ist erfunden; das
**Rechnungsdatum** dagegen wird echt gerechnet, und das ist der Teil, der im
Echtbetrieb Monatsumsätze verschieben würde.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable

logger = logging.getLogger("taskpilot.trockenlauf")

PLATZHALTER = "(Trockenlauf)"

# Frist des Platzhalters. Keine Hausregel, sondern eine Zahl, damit das
# Umdatieren im Trockenlauf überhaupt etwas zu verschieben hat. Im Echtlauf
# kommt sie aus der Zahlungsbedingung des Kontakts.
_FRIST_PLATZHALTER = timedelta(days=30)


class UnbekannterAufruf(RuntimeError):
    """Eine Methode, die weder als lesend noch als schreibend eingeordnet ist."""


@dataclass
class Schreibversuch:
    """Ein unterdrückter Schreibaufruf, so wie er ausgeführt worden wäre."""

    methode: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        teile = [repr(a) for a in self.args]
        teile += [f"{k}={v!r}" for k, v in self.kwargs.items()]
        return f"{self.methode}({', '.join(teile)})"


def _gekuerzt(wert: Any) -> Any:
    """PDF-Rümpfe gehören nicht ins Protokoll — nur ihre Grösse."""
    if isinstance(wert, (bytes, bytearray)):
        return f"<{len(wert)} Bytes>"
    if isinstance(wert, str) and len(wert) > 200:
        return wert[:200] + "…"
    return wert


class Schreibsperre:
    """Hülle um einen Fachsystem-Client, die Schreibaufrufe nur notiert.

    Lesende Aufrufe gehen unverändert an den echten Client. Ein Client ohne
    Sperre ist der Echtlauf; es gibt keinen dritten Zustand.
    """

    #: Methodenname → Bauplan für die vorgetäuschte Antwort. Der Bauplan
    #: bekommt dieselben Argumente wie der echte Aufruf.
    schreibt: dict[str, Callable[..., Any]] = {}
    #: Methoden, die durchgereicht werden.
    liest: frozenset[str] = frozenset()
    #: Lesende Methoden, die den Schatten berücksichtigen müssen.
    schatten: frozenset[str] = frozenset()

    def __init__(self, echt: Any):
        self._echt = echt
        self.vermerke: list[Schreibversuch] = []
        #: Pfad → Grösse in Bytes für vorgetäuschte Uploads, Pfad → None für Ordner.
        self._geschrieben: dict[str, int | None] = {}

    # -- Protokoll ------------------------------------------------------

    @property
    def trocken(self) -> bool:
        return True

    def protokoll(self) -> list[str]:
        return [str(v) for v in self.vermerke]

    def _notieren(self, methode: str, args: tuple, kwargs: dict) -> None:
        versuch = Schreibversuch(
            methode=methode,
            args=tuple(_gekuerzt(a) for a in args),
            kwargs={k: _gekuerzt(v) for k, v in kwargs.items()},
        )
        self.vermerke.append(versuch)
        logger.info("Trockenlauf unterdrückt: %s", versuch)

    # -- Zugriff --------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        if name in self.schreibt:
            bauplan = self.schreibt[name]

            async def gesperrt(*args, **kwargs):
                self._notieren(name, args, kwargs)
                return bauplan(self, *args, **kwargs)

            return gesperrt

        if name in self.schatten:
            return getattr(self, f"_schatten_{name}")

        if name in self.liest:
            return getattr(self._echt, name)

        raise UnbekannterAufruf(
            f"«{name}» ist im Trockenlauf weder als lesend noch als schreibend "
            f"eingeordnet ({type(self._echt).__name__}). Eintragen in "
            f"{type(self).__name__}.liest oder .schreibt — nicht durchreichen."
        )


# ── Bexio ──────────────────────────────────────────────────────────────


def _bexio_rechnung_aus_auftrag(sperre: BexioSperre, order_id: int) -> dict:
    """Was Bexio zurückgäbe: heute datiert, Kennung noch keine.

    Die Datumsfelder sind gesetzt, damit ``rechnungsdatum_setzen`` echt rechnet
    — genau dieser Schritt verschöbe sonst unbemerkt einen Monatsumsatz.
    """
    heute = date.today()
    return {
        "id": 0,
        "document_nr": PLATZHALTER,
        "is_valid_from": heute.isoformat(),
        "is_valid_to": (heute + _FRIST_PLATZHALTER).isoformat(),
    }


def _bexio_rechnung_geaendert(sperre: BexioSperre, invoice_id: int, **felder) -> dict:
    """Die geänderten Felder zurückgeben, damit die Kette weiterläuft."""
    return {"id": invoice_id, **felder}


class BexioSperre(Schreibsperre):
    """Bexio schreibt an sechs Stellen. Alle sechs stehen hier."""

    schreibt = {
        "create_contact": lambda s, *a, **k: {"id": 0},
        "create_invoice_from_order": _bexio_rechnung_aus_auftrag,
        "update_invoice": _bexio_rechnung_geaendert,
        "update_invoice_position": lambda s, *a, **k: {},
        "delete_invoice_position": lambda s, *a, **k: None,
        "issue_invoice": lambda s, *a, **k: True,
    }

    liest = frozenset({
        "test_connection",
        # Kontakte — search_* sind POST-Aufrufe und lesen trotzdem
        "list_contacts", "get_contact",
        "search_contact_by_name", "search_contact_by_email",
        # Aufträge
        "list_orders", "get_order", "alle_auftraege",
        # Rechnungen
        "list_invoices", "search_invoices", "alle_rechnungen",
        "get_invoice", "get_invoice_positions", "get_invoice_pdf",
        # Kreditoren
        "list_bills", "get_bill",
        "alle_lieferantenrechnungen", "alle_lieferantenrechnungen_detailliert",
        # Stammdaten und Journal
        "list_bank_accounts", "get_bank_account",
        "list_accounts", "search_accounts",
        "list_journal", "get_journal", "get_business_years",
        "list_projects", "get_project",
        # Lebenszyklus
        "close", "aclose",
    })


# ── Microsoft Graph ────────────────────────────────────────────────────


def _graph_ordner(sperre: GraphSperre, pfad: str) -> dict:
    for teil in _ordnerkette(pfad):
        sperre._geschrieben.setdefault(teil, None)
    return {"id": PLATZHALTER, "name": pfad.rstrip("/").rsplit("/", 1)[-1]}


def _graph_datei(sperre: GraphSperre, pfad: str, inhalt: bytes, **kwargs) -> dict:
    sperre._geschrieben[pfad.strip("/")] = len(inhalt)
    return {"id": PLATZHALTER, "size": len(inhalt)}


def _ordnerkette(pfad: str) -> list[str]:
    teile = [t for t in pfad.strip("/").split("/") if t]
    return ["/".join(teile[: i + 1]) for i in range(len(teile))]


class GraphSperre(Schreibsperre):
    """Graph schreibt in zwei Welten: Postfach und OneDrive.

    Eingeordnet ist, was der Rechnungslauf braucht, plus die Postfachwege, die
    ein Fehlgriff sonst still ausführen würde — ``send_draft`` steht als
    gesperrt hier, obwohl dieser Prozess ihn nie aufruft.
    """

    schreibt = {
        # Postfach
        "create_draft": lambda s, *a, **k: {"id": PLATZHALTER},
        "update_draft": lambda s, *a, **k: {"id": PLATZHALTER},
        "add_attachment": lambda s, *a, **k: {"id": PLATZHALTER},
        "delete_message": lambda s, *a, **k: None,
        "send_draft": lambda s, *a, **k: None,
        "move_to_folder": lambda s, *a, **k: {"id": PLATZHALTER},
        "archive_email": lambda s, *a, **k: {"id": PLATZHALTER},
        "set_flag": lambda s, *a, **k: {"id": PLATZHALTER},
        "set_categories": lambda s, *a, **k: {"id": PLATZHALTER},
        "mark_as_read": lambda s, *a, **k: None,
        "mark_as_unread": lambda s, *a, **k: None,
        # OneDrive
        "ensure_drive_folder": _graph_ordner,
        "upload_drive_file": _graph_datei,
    }

    liest = frozenset({
        "get_messages", "get_message", "get_attachments", "get_attachment",
        "list_folders", "get_folder", "search_query", "get_search_region",
        "fetch_internet_message_id", "resolve_message_id",
        "get_events", "get_event",
        "close", "aclose",
    })

    schatten = frozenset({"drive_item_by_path", "list_drive_items"})

    async def _schatten_drive_item_by_path(self, pfad: str):
        schluessel = pfad.strip("/")
        if schluessel in self._geschrieben:
            groesse = self._geschrieben[schluessel]
            eintrag: dict[str, Any] = {"name": schluessel.rsplit("/", 1)[-1]}
            if groesse is None:
                eintrag["folder"] = {}
            else:
                eintrag["file"] = {}
                eintrag["size"] = groesse
            return eintrag
        return await self._echt.drive_item_by_path(pfad)

    async def _schatten_list_drive_items(self, pfad: str, top: int = 20):
        schluessel = pfad.strip("/")
        try:
            eintraege = list(await self._echt.list_drive_items(pfad, top=top))
        except Exception:
            # Ein Ordner, den erst dieser Lauf anlegte, existiert noch nicht.
            if not any(p.startswith(schluessel + "/") for p in self._geschrieben):
                raise
            eintraege = []
        bekannt = {e.get("name") for e in eintraege}
        for p, groesse in self._geschrieben.items():
            if p.rsplit("/", 1)[0] != schluessel:
                continue
            name = p.rsplit("/", 1)[-1]
            if name in bekannt:
                continue
            eintraege.append(
                {"name": name, "folder": {}} if groesse is None
                else {"name": name, "file": {}, "size": groesse}
            )
        return eintraege


# ── Toggl ──────────────────────────────────────────────────────────────


def _toggl_tag(sperre: TogglSperre, eintrag_id: int, tag_id: int, *args) -> dict:
    """Was Toggl zurückgäbe: derselbe Eintrag, nun mit dem Tag.

    Der Name kommt aus der Tagtabelle, die die Sperre beim Bauen mitbekommt.
    Ohne ihn liefe die Nachprüfung in ``debitoren_verrechnungsart`` im
    Trockenlauf in einen Fehlschlag, den es nicht gibt — derselbe
    Phantomfehler, gegen den beim Archiv der Schatten steht.

    Was der Trockenlauf hier **nicht** kann: sagen, ob der ``PUT`` mehr
    verändert als das Tag. Darum nennt die Antwort weder Beschreibung noch
    Dauer; die Nachprüfung überspringt, was nicht da ist, statt gegen eine
    erfundene Angabe zu vergleichen und daraus Zuversicht zu schöpfen.
    """
    name = sperre._tagnamen.get(int(tag_id), PLATZHALTER)
    return {"id": eintrag_id, "tags": [name]}


class TogglSperre(Schreibsperre):
    """Toggl schreibt im Rechnungslauf an genau einer Stelle: dem Tag."""

    schreibt = {
        "add_time_entry_tag": _toggl_tag,
    }

    liest = frozenset({
        "test_connection", "me",
        "list_workspaces",
        "list_clients", "get_client", "search_clients",
        "list_projects", "get_project", "search_projects",
        "get_project_with_rate", "get_projects_summary", "get_summary_by_project",
        "list_tags", "list_tasks",
        "search_time_entries", "search_all_time_entries",
        "close", "aclose",
    })

    def __init__(self, echt: Any, *, tags: list[dict] | None = None):
        super().__init__(echt)
        self._tagnamen = {
            t["id"]: t.get("name") or ""
            for t in (tags or [])
            if t.get("id") is not None
        }


# ── Einstellung ────────────────────────────────────────────────────────

EINSTELLUNG = "debitoren_trockenlauf"


def voreinstellung(einstellungen: dict | None) -> bool:
    """Ob die Oberfläche vor dem Schreiben erst zeigen soll. Standard: ja.

    Die Sperre selbst hängt **nicht** an dieser Einstellung — ein Endpunkt
    schreibt nur, wenn der Aufruf ``echt`` ausdrücklich verlangt. Diese
    Einstellung steuert allein, ob die Oberfläche zwei Klicks verlangt oder
    einen. Sonst läge die Sicherung im Browser, und der ist die falsche Stelle
    dafür.
    """
    wert = (einstellungen or {}).get(EINSTELLUNG)
    return True if wert is None else bool(wert)
