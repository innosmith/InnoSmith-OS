"""Das Kreditorenregister -- Aufnahme, Zustand und Ablage eines Belegs.

## Warum der Hash und nicht der Pfad

Am 21.09.2026 an der Datenbank des InvoiceInsight-Moduls gemessen: sie erkennt
ein Dokument an ``file_path``, speichert ``sha256_hash`` und benutzt ihn **nie**
zum Wiedererkennen. Vier Rechnungen sind dadurch heute doppelt erfasst, und
jede ist ein echter Ablagefehler, den der Hash sofort verraet:

* dieselbe Datei unter ``Google Cloud EMEA … 31.01.2026`` **und** ``Google
  Workspace Abo 31.01.2026`` -- eine Rechnung, zwei Lieferanten,
* ``Cursor Usage 02.09.2026`` und ``… 20.09.2026`` -- Zahlendreher im Datum,
* ``Google Cloud 30.06.2026 KK`` und ``…-gx10-7c32`` -- zweimal archiviert,
* ``Cursor Usage10.05.2026`` und ``Cursor Usage 10.05.2026``.

Verschiebt TaskPilot eine Datei vom Eingang ins Archiv, aendert sich ihr Pfad
und ihr Graph-Handle. Waere der Pfad die Identitaet, entstuende bei jeder Ablage
eine zweite Zeile -- und jede Auswertung zaehlte doppelt, ohne Fehlermeldung.
Dieselbe Lehre wie bei ``internet_message_id`` gegen ``email_message_id`` im
Mailpfad: **die Identitaet ist der Inhalt, der Ort ist ein Handle**, und ein
eigener Move schreibt das neue Handle zurueck.

## Eine Wahrheit, zwei Projektionen

Die Wahrheit ist die Zeile mit ``freigegeben_am IS NULL``. Die Projektion ist
die Datei im Eingangsordner. Liegt nichts im Eingang, ist nichts offen. Und es
gilt dieselbe tragende Regel wie im Postfach: **vorwaerts nur, Richtung Archiv
-- rueckwaerts nur der Mensch.**
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.models.models import Kreditorenbeleg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

QUELLEN: frozenset[str] = frozenset(
    {"autodownload", "ablage_hand", "postfach", "upload", "erzeugt"}
)
"""``erzeugt``: von TaskPilot selbst hergestellt -- der Monatssammelbeleg."""
BELEGARTEN: frozenset[str] = frozenset({"rechnung", "spese", "sammelbeleg"})


def hash_von(inhalt: bytes) -> str:
    """Die Identitaet eines Belegs: SHA-256 ueber den Dateiinhalt."""
    return hashlib.sha256(inhalt).hexdigest()


@dataclass
class Aufnahme:
    """Was die Aufnahme ergab -- und ob sie etwas Neues war."""

    beleg: Kreditorenbeleg
    neu: bool
    vermerk: str
    abgewiesen: bool = False
    """Eine andere Datei derselben Rechnung. ``beleg`` ist dann das Original,
    und am Original darf der Aufrufer nichts fortschreiben."""

    @property
    def doppelt(self) -> bool:
        return not self.neu


def _nummer(wert: str | None) -> str | None:
    """Die Rechnungsnummer ohne Rand. Leer ist keine Nummer."""
    rein = (wert or "").strip()
    return rein or None


async def _original(
    db: AsyncSession, lieferant: str | None, nummer: str | None, ausser: UUID | None = None
) -> Kreditorenbeleg | None:
    """Der Beleg, der diese Rechnung schon trägt -- sofern Lieferant und Nummer
    beide bekannt sind. Fehlt eines, ist die Frage nicht beantwortbar."""
    if not lieferant or not nummer:
        return None
    abfrage = select(Kreditorenbeleg).where(
        Kreditorenbeleg.lieferant_schluessel == lieferant,
        Kreditorenbeleg.rechnungsnummer == nummer,
    )
    if ausser is not None:
        abfrage = abfrage.where(Kreditorenbeleg.id != ausser)
    return (await db.execute(abfrage)).scalar_one_or_none()


def _doppel_vermerk(dateiname: str, nummer: str, original: Kreditorenbeleg) -> str:
    # Welche Datei zuerst gesehen wurde, ist Zufall -- im Eingang war es die mit
    # «(2)» im Namen. Deshalb keine Empfehlung, welche gelöscht werden soll.
    return (
        f"«{dateiname}» ist Rechnung {nummer} — dieselbe wie «{original.dateiname}», "
        f"als andere Datei. Nur eine wird gebucht; eine der beiden kann weg."
    )


async def aufnehmen(
    db: AsyncSession,
    *,
    datei_hash: str,
    dateiname: str,
    quelle: str,
    graph_item_id: str | None = None,
    graph_pfad: str | None = None,
    belegart: str = "rechnung",
    lieferant_schluessel: str | None = None,
    rechnungsnummer: str | None = None,
) -> Aufnahme:
    """Nimmt einen Beleg auf -- oder erkennt ihn wieder.

    Ist der Hash bekannt, entsteht **keine** zweite Zeile. Stattdessen wird der
    Ort nachgefuehrt, denn ein bekannter Beleg an einem neuen Pfad ist derselbe
    Beleg, der bewegt wurde. Genau hier entstehen sonst die Doppelzaehlungen.

    Eine Ausnahme macht die Regel nicht weicher, sondern schaerfer: liegt der
    Beleg schon im Archiv (``abgelegt_am`` gesetzt) und taucht erneut im Eingang
    auf, ist das **nicht** eine Bewegung, sondern ein zweiter Download. Dann
    bleibt der Archivort stehen und der Fund wird gemeldet -- sonst zeigte das
    Register auf den Eingang, wo die Datei bald nicht mehr liegt.

    Der Hash erkennt dieselbe **Datei**, nicht dieselbe **Rechnung**. Cursor
    liefert für dieselbe Nummer bei jedem Abruf andere Bytes; am 02.09.2026
    lagen neun Dateien für vier Rechnungen im Archiv. Deshalb prüft
    ``rechnungsnummer`` mit dem Lieferanten als zweiter Wächter: eine andere
    Datei derselben Rechnung wird gemeldet und nicht aufgenommen
    (``abgewiesen``). Stand die Kopie schon vor dem Wächter im Register, wird
    sie mit Begründung zurückgestellt statt gelöscht -- zurückholen darf der
    Mensch.
    """
    if quelle not in QUELLEN:
        raise ValueError(f"Unbekannte Quelle {quelle!r}; erlaubt: {sorted(QUELLEN)}")
    if belegart not in BELEGARTEN:
        raise ValueError(f"Unbekannte Belegart {belegart!r}; erlaubt: {sorted(BELEGARTEN)}")

    bekannt = (
        await db.execute(
            select(Kreditorenbeleg).where(Kreditorenbeleg.datei_hash == datei_hash)
        )
    ).scalar_one_or_none()

    nummer = _nummer(rechnungsnummer)

    if bekannt is not None:
        # Ergänzt wird nur, was fehlt -- und erst nach der Prüfung, sonst
        # scheiterte der Schreibvorgang an der Eindeutigkeit in der Datenbank.
        wer = bekannt.lieferant_schluessel or lieferant_schluessel
        nr = bekannt.rechnungsnummer or nummer
        if (wer, nr) != (bekannt.lieferant_schluessel, bekannt.rechnungsnummer):
            original = await _original(db, wer, nr, ausser=bekannt.id)
            if original is not None:
                vermerk = _doppel_vermerk(dateiname, nr or "", original)
                if bekannt.freigegeben_am is None and not bekannt.zurueckgestellt:
                    bekannt.zurueckgestellt = True
                    bekannt.grund = vermerk
                logger.info("Kreditorenregister: %s", vermerk)
                return Aufnahme(bekannt, neu=False, vermerk=vermerk, abgewiesen=True)
            bekannt.lieferant_schluessel = wer
            bekannt.rechnungsnummer = nr

        if bekannt.abgelegt_am is not None:
            vermerk = (
                f"«{dateiname}» liegt seit {bekannt.abgelegt_am:%d.%m.%Y} im Archiv "
                f"unter {bekannt.archiv_pfad} — derselbe Inhalt, erneut bezogen. "
                f"Nichts aufgenommen, nichts überschrieben."
            )
            logger.info("Kreditorenregister: %s", vermerk)
            return Aufnahme(bekannt, neu=False, vermerk=vermerk)

        alt = bekannt.graph_pfad
        bekannt.graph_pfad = graph_pfad or bekannt.graph_pfad
        bekannt.graph_item_id = graph_item_id or bekannt.graph_item_id
        vermerk = (
            f"«{dateiname}» ist bereits im Register"
            + (f", Ort nachgeführt: {alt} → {graph_pfad}" if graph_pfad and graph_pfad != alt else "")
        )
        logger.info("Kreditorenregister: %s", vermerk)
        return Aufnahme(bekannt, neu=False, vermerk=vermerk)

    original = await _original(db, lieferant_schluessel, nummer)
    if original is not None:
        vermerk = _doppel_vermerk(dateiname, nummer or "", original)
        logger.info("Kreditorenregister: %s", vermerk)
        return Aufnahme(original, neu=False, vermerk=vermerk, abgewiesen=True)

    beleg = Kreditorenbeleg(
        datei_hash=datei_hash,
        dateiname=dateiname,
        quelle=quelle,
        belegart=belegart,
        graph_item_id=graph_item_id,
        graph_pfad=graph_pfad,
        lieferant_schluessel=lieferant_schluessel,
        rechnungsnummer=nummer,
    )
    db.add(beleg)
    await db.flush()
    return Aufnahme(beleg, neu=True, vermerk=f"«{dateiname}» neu aufgenommen")


async def nach_hash(db: AsyncSession, datei_hash: str) -> Kreditorenbeleg | None:
    return (
        await db.execute(select(Kreditorenbeleg).where(Kreditorenbeleg.datei_hash == datei_hash))
    ).scalar_one_or_none()


async def offene(db: AsyncSession, *, mit_zurueckgestellten: bool = False) -> list[Kreditorenbeleg]:
    """Die Warteliste: aufgenommen, noch nicht freigegeben.

    Keine Periode, kein Monatslauf. Rechnungen kommen laufend herein, und ein
    Lauf, der auf den Monatswechsel wartet, liesse einen Beleg vom 3. bis zum
    30. liegen, ohne dass das irgendeinen Zweck hätte.
    """
    abfrage = select(Kreditorenbeleg).where(Kreditorenbeleg.freigegeben_am.is_(None))
    if not mit_zurueckgestellten:
        abfrage = abfrage.where(Kreditorenbeleg.zurueckgestellt.is_(False))
    return list(
        (await db.execute(abfrage.order_by(Kreditorenbeleg.eingang_am))).scalars().all()
    )


async def zu_buchen(db: AsyncSession) -> list[Kreditorenbeleg]:
    """Freigegeben, aber noch nicht gebucht -- oder gebucht und noch nicht abgelegt.

    Beides gehört in dieselbe Liste: eine Buchung ohne Ablage ist nicht
    fertig, und wer sie nicht sieht, findet die Rechnung Wochen später im
    Eingang und hält sie für neu.
    """
    abfrage = select(Kreditorenbeleg).where(
        Kreditorenbeleg.freigegeben_am.is_not(None),
        Kreditorenbeleg.zurueckgestellt.is_(False),
        (Kreditorenbeleg.gebucht_am.is_(None)) | (Kreditorenbeleg.abgelegt_am.is_(None)),
    )
    return list(
        (await db.execute(abfrage.order_by(Kreditorenbeleg.freigegeben_am))).scalars().all()
    )


async def zuruecklegen(
    db: AsyncSession, beleg: Kreditorenbeleg, grund: str
) -> Kreditorenbeleg:
    """Nimmt einen Beleg aus der Warteliste -- mit Begruendung.

    Ohne Grund ist eine Ausnahme in drei Wochen nicht mehr nachvollziehbar, und
    der Beleg sieht dann aus wie vergessen statt wie entschieden.
    """
    if not grund.strip():
        raise ValueError("Zurücklegen ohne Grund: in drei Wochen nicht nachvollziehbar")
    if beleg.freigegeben_am is not None:
        raise ValueError(
            "Der Beleg ist schon freigegeben — zurückholen darf nur der Mensch "
            "von Hand, nicht dieser Weg"
        )
    beleg.zurueckgestellt = True
    beleg.grund = grund.strip()
    return beleg


async def freigeben(
    db: AsyncSession, beleg: Kreditorenbeleg, *, durch: UUID | None
) -> Kreditorenbeleg:
    """Haelt die menschliche Freigabe fest -- einmal und nicht wieder.

    Der Zeitstempel ist die Sperre gegen den zweiten Durchlauf: was freigegeben
    ist, wird gebucht, und zweimal gebucht ist zweimal bezahlt.
    """
    if beleg.freigegeben_am is not None:
        raise ValueError(
            f"«{beleg.dateiname}» ist seit {beleg.freigegeben_am:%d.%m.%Y %H:%M} "
            f"freigegeben — eine zweite Freigabe würde eine zweite Buchung erzeugen"
        )
    if beleg.zurueckgestellt:
        raise ValueError(
            f"«{beleg.dateiname}» ist zurückgestellt ({beleg.grund}) — "
            f"erst zurückholen, dann freigeben"
        )
    if not beleg.sollkonto:
        # Dieselbe Bedingung steht als Prüfregel in der Datenbank. Doppelt, weil
        # die Datenbank nur «Constraint verletzt» sagen kann -- und die Freigabe
        # ist die Stelle, an der ein Mensch eine lesbare Auskunft braucht.
        raise ValueError(
            f"«{beleg.dateiname}» hat kein Sollkonto — freigeben heisst buchen, "
            f"und ohne Konto hätte die Buchung kein Ziel"
        )
    beleg.freigegeben_am = datetime.now(UTC)
    beleg.freigegeben_von = durch
    return beleg


async def ablage_vermerken(
    db: AsyncSession, beleg: Kreditorenbeleg, *, archiv_pfad: str, graph_item_id: str | None
) -> Kreditorenbeleg:
    """Schreibt den Archivort fest und führt das Handle nach.

    Nach dem Verschieben ist das alte ``graph_item_id`` ungültig. Wer es
    stehen liesse, hätte ein Handle, das auf nichts zeigt -- und eine spätere
    Änderung liefe ins Leere, ohne zu scheitern.
    """
    if beleg.freigegeben_am is None:
        raise ValueError(
            f"«{beleg.dateiname}» ist nicht freigegeben — es wird nur abgelegt, "
            f"was ein Mensch bestätigt hat"
        )
    beleg.archiv_pfad = archiv_pfad
    beleg.graph_pfad = archiv_pfad
    if graph_item_id:
        beleg.graph_item_id = graph_item_id
    beleg.abgelegt_am = datetime.now(UTC)
    return beleg
