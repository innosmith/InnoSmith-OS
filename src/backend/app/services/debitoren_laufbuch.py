"""Das Laufbuch des Rechnungslaufs — was entschieden und was vollzogen wurde.

## Die Trennlinie

Zwei Arten von Wissen treffen im Monatslauf aufeinander, und sie dürfen nicht
vermischt werden:

**Was ist der Fall** — welche Entwürfe es gibt, wie viele Stunden dahinter
stehen, ob die Positionen stimmen, ob ein Auftrag offen ist. Das steht in Bexio
und Toggl und wird bei jedem Aufruf neu gelesen (``debitoren_lauf``). Eine Kopie
davon hier wäre eine zweite Wahrheit, und sie gewönne genau dann, wenn jemand in
Bexio nachgebessert hat — der Fehler wäre still und sähe aus wie ein
Prüfergebnis.

**Was wurde entschieden und getan** — dass eine Rechnung bewusst zurückgestellt
ist, dass die Dokumente erzeugt sind, dass der Mailentwurf im Postfach liegt,
dass der Versand bestätigt wurde. Davon steht nichts in Bexio, und ein
Seitenwechsel im Browser darf es nicht verlieren. Das ist dieses Modul.

## Warum Zeitstempel statt Zustandsnamen

Naheliegend wäre ein Feld ``zustand`` mit Werten wie ``geprueft``,
``versendet``, ``abgelegt``. Es wäre falsch, weil die Schritte nicht in einer
Reihe liegen: eine zurückgestellte Rechnung kann Dokumente haben, ein
Mailentwurf kann existieren, bevor abgelegt wurde, und «geprüft» ist gar kein
Zustand des Laufs, sondern ein Ergebnis, das sich bei jedem Aufruf ändern kann.

Ein Zeitstempel je Schritt beantwortet **ob** und **wann** mit einer Spalte, und
``NULL`` heisst unmissverständlich «noch nicht». Ein Zustandsfeld müsste
dagegen jede Kombination benennen, und die Liste wächst mit jedem Schritt.

## Die Zeile entsteht beim ersten Vermerk

Ein Lauf legt **nicht** vorsorglich für jede Rechnung eine Zeile an. Die
Rechnungen des Monats kommen live aus Bexio; eine vorsorglich angelegte Zeile
wäre eine Behauptung über einen Bestand, der sich noch ändert. Erst wenn etwas
zu vermerken ist, entsteht die Zeile — bis dahin gilt für jede Rechnung der
Nullzustand, und der ist richtig.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Debitorenlauf, DebitorenlaufRechnung

logger = logging.getLogger("taskpilot.debitoren.laufbuch")

SCHRITTE = ("dokumente_erzeugt_am", "mailentwurf_id", "versendet_am", "abgelegt_am")
"""Die Schritte, die das Laufbuch führt. Reihenfolge nur zur Anzeige."""


def jetzt() -> datetime:
    return datetime.now(timezone.utc)


async def lauf_holen(
    db: AsyncSession, *, jahr: int, monat: int
) -> Debitorenlauf | None:
    """Den Lauf einer Periode lesen, samt Rechnungszeilen. Legt nichts an."""
    ergebnis = await db.execute(
        select(Debitorenlauf)
        .where(Debitorenlauf.jahr == jahr, Debitorenlauf.monat == monat)
        .options(selectinload(Debitorenlauf.rechnungen))
    )
    return ergebnis.scalars().first()


async def lauf_oeffnen(
    db: AsyncSession, *, jahr: int, monat: int, stichtag: date, user_id=None
) -> Debitorenlauf:
    """Den Lauf einer Periode holen oder anlegen.

    Ein zweiter Aufruf legt keinen zweiten Lauf an — die Eindeutigkeit über
    (Jahr, Monat) steht zusätzlich in der Datenbank, damit zwei gleichzeitige
    Aufrufe nicht zwei Läufe erzeugen, die beide halb geführt werden.
    """
    vorhanden = await lauf_holen(db, jahr=jahr, monat=monat)
    if vorhanden is not None:
        return vorhanden

    lauf = Debitorenlauf(jahr=jahr, monat=monat, stichtag=stichtag, user_id=user_id)
    db.add(lauf)
    await db.flush()
    await db.refresh(lauf, ["rechnungen"])
    return lauf


async def zeile(
    db: AsyncSession, lauf: Debitorenlauf, *, rechnung_id: int, nummer: str | None = None
) -> DebitorenlaufRechnung:
    """Die Zeile zu einer Rechnung holen oder anlegen.

    ``nummer`` wird nachgetragen, wenn sie noch fehlt — sie kann beim ersten
    Vermerk unbekannt sein und ist ohnehin nur für Menschen da.
    """
    for r in lauf.rechnungen:
        if r.rechnung_id == rechnung_id:
            if nummer and not r.nummer:
                r.nummer = nummer
            return r

    neu = DebitorenlaufRechnung(lauf_id=lauf.id, rechnung_id=rechnung_id, nummer=nummer)
    db.add(neu)
    await db.flush()
    lauf.rechnungen.append(neu)
    return neu


async def schritt_vermerken(
    db: AsyncSession,
    lauf: Debitorenlauf,
    *,
    rechnung_id: int,
    schritt: str,
    wert: str | None = None,
    nummer: str | None = None,
    erneut: bool = False,
) -> tuple[DebitorenlaufRechnung, bool]:
    """Einen vollzogenen Schritt festhalten. Liefert (Zeile, neu_vermerkt).

    **Ein bereits vermerkter Schritt wird nicht überschrieben**, es sei denn,
    ``erneut`` sagt es ausdrücklich. Das ist der Sinn des Laufbuchs: es hält
    fest, was einmal geschehen ist, damit es nicht ein zweites Mal geschieht.
    Ein zweiter Mailentwurf im Postfach oder eine zweite Kopie im Kundenarchiv
    fällt niemandem auf, bis er sie von Hand findet.

    Der Rückgabewert sagt dem Aufrufer, ob er handeln muss: ``False`` heisst
    «stand schon da, nichts zu tun».
    """
    if schritt not in SCHRITTE:
        raise ValueError(f"Unbekannter Schritt: {schritt!r}")

    z = await zeile(db, lauf, rechnung_id=rechnung_id, nummer=nummer)
    if getattr(z, schritt) is not None and not erneut:
        return z, False

    setattr(z, schritt, wert if schritt == "mailentwurf_id" else jetzt())
    return z, True


async def zuruecklegen(
    db: AsyncSession,
    lauf: Debitorenlauf,
    *,
    rechnung_id: int,
    grund: str,
    nummer: str | None = None,
) -> DebitorenlaufRechnung:
    """Eine Rechnung aus dem Lauf nehmen — die einzige vorgesehene Ausnahme.

    Der Grund ist Pflicht und kein Formularfeld: in drei Wochen ist nicht mehr
    nachvollziehbar, warum eine Rechnung liegen blieb, und ohne Begründung sieht
    eine bewusste Ausnahme genauso aus wie ein Versehen.
    """
    if not grund.strip():
        raise ValueError("Zurückstellen ohne Begründung")
    z = await zeile(db, lauf, rechnung_id=rechnung_id, nummer=nummer)
    z.zurueckgestellt = True
    z.grund = grund.strip()
    return z


async def zurueckholen(
    db: AsyncSession, lauf: Debitorenlauf, *, rechnung_id: int
) -> DebitorenlaufRechnung | None:
    """Eine zurückgestellte Rechnung wieder aufnehmen.

    Der Grund bleibt stehen. Er gehört zur Geschichte der Rechnung, und ihn
    beim Zurückholen zu löschen hiesse, die Spur der Entscheidung zu tilgen.
    """
    for r in lauf.rechnungen:
        if r.rechnung_id == rechnung_id:
            r.zurueckgestellt = False
            return r
    return None


def als_karte(lauf: Debitorenlauf | None) -> dict[int, dict]:
    """Das Laufbuch als Nachschlagewerk nach Bexio-Kennung.

    Damit legt die Ansicht die Vermerke über den live gelesenen Bestand, statt
    beides zu vermischen. Eine Rechnung ohne Zeile fehlt hier schlicht — der
    Nullzustand braucht keinen Eintrag.
    """
    if lauf is None:
        return {}
    return {
        r.rechnung_id: {
            "zurueckgestellt": r.zurueckgestellt,
            "grund": r.grund,
            "dokumente_erzeugt_am": r.dokumente_erzeugt_am,
            "mailentwurf_id": r.mailentwurf_id,
            "versendet_am": r.versendet_am,
            "abgelegt_am": r.abgelegt_am,
        }
        for r in lauf.rechnungen
    }
