"""Der Kreditoreneingang -- die Warteliste und drei Handlungen.

Warum ein eigener Router und nicht ein paar Routen mehr in ``creditors.py``:
jener Router **fragt** InvoiceInsight aus und schreibt nichts in TaskPilot. Hier
entstehen Zustandsänderungen, die einen Zahlungsvorgang auslösen. Die Trennung
ist die Erinnerung daran, dass diese Routen anders zu behandeln sind.

Die Adresse bleibt ``/api/creditors/...``, weil die Oberfläche einen Bereich
kennt und nicht zwei.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

from app.auth.deps import require_role
from app.database import get_db
from app.models import User
from app.models.models import Kreditorenbeleg
from app.routers.creditors import _rest_zugang
from app.schemas.kreditoreneingang import (
    Buchungsbericht,
    Buchungspruefung,
    EingangsBeleg,
    Freigabe,
    LieferantBestaetigen,
    LieferantKandidat,
    LieferantWahl,
    SammelFreigabe,
    SammelMonat,
    SammelPosition,
    Sammelvorschau,
    Warteliste,
    Zuruecklegen,
)
from app.services import bazg_kurse
from app.services import kreditorenbuchung as kb
from app.services import kreditorenlieferanten as decl
from app.services import kreditoreneingang as eing
from app.services import kreditorennorm as norm
from app.services import kreditorenregister as reg
from app.services import sammelbeleg as sb
from app.services.fachsysteme import bexio_zugang
from app.services.graph import get_graph_client
from app.services.invoiceinsight_rest import (
    SchnittstellenFehler,
    belegdatei_holen,
    rechnungen_holen,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/creditors", tags=["creditors"])


def _eingangsordner(user: User) -> str:
    """Wo der Eingang liegt. Betriebsangabe, keine Konstante."""
    return (user.settings or {}).get("kreditoren_eingang") or eing.EINGANG_VORGABE


@router.post("/eingang/abgleichen", response_model=Warteliste)
async def eingang_abgleichen(
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Nimmt auf, was im Eingang liegt, und liefert die Warteliste zurück.

    Ein eigener Aufruf und nicht heimlich im GET: der Abgleich schreibt. Eine
    Leseroute, die nebenbei Zeilen anlegt, ist nicht wiederholbar ohne Folgen --
    und ausgerechnet der Browser wiederholt sie gern.
    """
    url, token = _rest_zugang(user)
    try:
        befund = await eing.abgleichen(
            db, basis_url=url, token=token, eingang=_eingangsordner(user)
        )
    except SchnittstellenFehler as fehler:
        raise HTTPException(status_code=fehler.status, detail=fehler.meldung) from fehler
    except RuntimeError as fehler:
        raise HTTPException(status_code=503, detail=str(fehler)) from fehler
    await db.commit()

    liste = await _warteliste(db, user, url, token)
    liste.dubletten = befund.dubletten
    liste.schon_im_archiv = befund.schon_im_archiv
    liste.fort = befund.fort
    liste.von_hand_abgelegt = befund.von_hand_abgelegt
    liste.hinweise.extend(befund.hinweise)
    if befund.fort:
        liste.hinweise.append(
            f"{befund.fort} Beleg(e) im Eingang sind in OneDrive nicht mehr vorhanden "
            f"und stehen deshalb nicht in der Liste."
        )
    if befund.von_hand_abgelegt:
        liste.hinweise.append(
            f"{befund.von_hand_abgelegt} wartende(r) Beleg(e) liegen inzwischen von Hand "
            f"im Archiv und wurden mit Vermerk aus der Liste genommen."
        )
    if befund.nicht_lesbar:
        liste.hinweise.append(
            f"{len(befund.nicht_lesbar)} Beleg(e) im Eingang konnten nicht gelesen "
            f"werden und stehen deshalb nicht in der Liste: "
            f"{', '.join(befund.nicht_lesbar)}."
        )
    for status_, anzahl in befund.nicht_auswertbar.items():
        liste.hinweise.append(
            f"{anzahl} Beleg(e) mit Status «{status_}» sind nicht gelesen und "
            f"stehen deshalb in keiner Liste."
        )
    return liste


@router.get("/eingang", response_model=Warteliste)
async def eingang(
    mit_zurueckgestellten: bool = Query(default=False),
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die Warteliste, ohne etwas zu verändern."""
    url, token = _rest_zugang(user)
    return await _warteliste(
        db, user, url, token, mit_zurueckgestellten=mit_zurueckgestellten
    )


async def _warteliste(
    db: AsyncSession,
    user: User,
    url: str,
    token: str,
    *,
    mit_zurueckgestellten: bool = False,
) -> Warteliste:
    """Register und Modulbestand zusammenführen.

    Verknüpft wird über ``modul_dokument_id``, nicht über den Dateinamen. Zwei
    Cursor-Rechnungen desselben Tages heissen gleich; der Name ist kein
    Schlüssel.
    """
    belege = await reg.offene(db, mit_zurueckgestellten=mit_zurueckgestellten)
    zu_buchen = await reg.zu_buchen(db)
    zurueckgestellt = sum(1 for b in belege if b.zurueckgestellt)

    hinweise: list[str] = []
    je_kennung: dict[int, dict[str, Any]] = {}
    if belege or zu_buchen:
        try:
            zeilen, _ = await rechnungen_holen(url, token)
            je_kennung = {
                int(z["beleg_id"]): z for z in zeilen if z.get("beleg_id") is not None
            }
        except (RuntimeError, SchnittstellenFehler) as fehler:
            # Die Liste bleibt brauchbar: die Registerangaben stehen in TaskPilot,
            # nur Betrag und Produkt fehlen. Stillschweigend leere Beträge zu
            # zeigen wäre schlimmer als die Meldung.
            hinweise.append(
                f"Beträge und Produkte fehlen — InvoiceInsight antwortete nicht: {fehler}"
            )
            logger.warning("Warteliste ohne Modulangaben: %s", fehler)

    deklaration = decl.laden()

    def zeile_von(b: Kreditorenbeleg) -> dict[str, Any]:
        return je_kennung.get(b.modul_dokument_id or -1) or {}

    sammelnd = [
        (b, zeile_von(b)) for b in belege
        if sb.wartet(b, zeile_von(b), deklaration.lieferanten.get(b.lieferant_schluessel or ""))
    ]
    im_sammelbeleg = {b.id for b, _ in sammelnd}
    zeilen_aus = [
        _eingangsbeleg(b, zeile_von(b), deklaration) for b in belege if b.id not in im_sammelbeleg
    ]
    # Eine Einzelrechnung eines Sammelbelegs ist mit der Ablage fertig --
    # «nicht gebucht» ist dort der richtige Endzustand, keine Lücke.
    zu_buchen = [b for b in zu_buchen if not (b.abgelegt_am and b.sammelbeleg_id)]

    return Warteliste(
        belege=zeilen_aus,
        sammelbeleg_wartet=_je_monat(sammelnd, deklaration),
        zu_buchen=[
            _eingangsbeleg(b, je_kennung.get(b.modul_dokument_id or -1) or {}, deklaration)
            for b in zu_buchen
        ],
        offen=sum(1 for b in zeilen_aus if not b.zurueckgestellt),
        ohne_konto=sum(1 for b in zeilen_aus if not b.sollkonto),
        zurueckgestellt=zurueckgestellt,
        stand=max((b.eingang_am for b in belege if b.eingang_am), default=None),
        hinweise=hinweise,
    )


def _je_monat(
    eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]],
    deklaration: decl.Bestand,
    *,
    heute: date | None = None,
) -> list[SammelMonat]:
    heute = heute or date.today()
    je_lieferant: dict[str, list[tuple[Kreditorenbeleg, dict[str, Any]]]] = {}
    for b, z in eintraege:
        je_lieferant.setdefault(b.lieferant_schluessel or "", []).append((b, z))

    ergebnis = []
    for schluessel in sorted(je_lieferant):
        for monat, gruppe in sb.nach_monat(je_lieferant[schluessel]).items():
            zeilen = [z for _, z in gruppe]
            waehrungen = {z.get("waehrung") for z in zeilen}
            vollstaendig = all(z.get("betrag") is not None for z in zeilen) and len(waehrungen) == 1
            ergebnis.append(
                SammelMonat(
                    lieferant_schluessel=schluessel,
                    anzeigename=deklaration.lieferanten[schluessel].anzeigename,
                    jahr=monat.year,
                    monat=monat.month,
                    bezeichnung=sb.monatsname(monat),
                    anzahl=len(zeilen),
                    betrag=round(sum(float(z["betrag"]) for z in zeilen), 2) if vollstaendig else None,
                    waehrung=next(iter(waehrungen)) if vollstaendig else None,
                    abgeschlossen=heute > sb.monatsletzter(monat),
                )
            )
    return ergebnis


def _eingangsbeleg(
    beleg: Kreditorenbeleg, zeile: dict[str, Any], deklaration: decl.Bestand
) -> EingangsBeleg:
    """Registerzeile und Modulzeile zu einer Zeile der Maske."""
    v = (
        eing.vorschlagen(zeile, deklaration, gewaehlt=beleg.lieferant_schluessel)
        if zeile
        else eing.Vorschlag()
    )
    if not zeile and beleg.belegart != "sammelbeleg":
        v.abweichungen.append(
            "Dieser Beleg ist registriert, aber noch nicht gelesen — "
            "die Extraktion hat ihn nicht erfasst."
        )
    lieferant = deklaration.lieferanten.get(beleg.lieferant_schluessel or "")
    return EingangsBeleg(
        id=beleg.id,
        dateiname=beleg.dateiname,
        quelle=beleg.quelle,
        eingang_am=beleg.eingang_am,
        beleg_id=beleg.modul_dokument_id,
        lieferant_schluessel=beleg.lieferant_schluessel,
        lieferant=zeile.get("lieferant"),
        rechnungsnummer=beleg.rechnungsnummer or zeile.get("rechnungsnummer"),
        lieferant_bestaetigt=v.lieferant_bestaetigt,
        lieferant_kandidaten=[
            LieferantKandidat(schluessel=s, anzeigename=deklaration.lieferanten[s].anzeigename)
            for s in v.lieferant_kandidaten
            if s in deklaration.lieferanten
        ],
        betrag=zeile.get("betrag"),
        betrag_chf=zeile.get("betrag_chf"),
        waehrung=zeile.get("waehrung"),
        datum=zeile.get("datum"),
        produkt=zeile.get("produkt"),
        sollkonto=beleg.sollkonto,
        sollkonto_herkunft=beleg.sollkonto_herkunft,
        sollkonto_kandidaten=list(v.sollkonto_kandidaten),
        steuerbehandlung=beleg.steuerbehandlung,
        zahlweg=beleg.zahlweg,
        **_norm_vorschau(beleg, v, zeile),
        freigegeben_am=beleg.freigegeben_am,
        gebucht_am=beleg.gebucht_am,
        bexio_referenz=beleg.bexio_referenz,
        abgelegt_am=beleg.abgelegt_am,
        archiv_pfad=beleg.archiv_pfad,
        nur_ablegen=beleg.sammelbeleg_id is not None
        or bool(
            zeile
            and lieferant
            and lieferant.sammelt(zeile.get("abrechnungszyklus"), zeile.get("dokumenttyp"))
        ),
        zurueckgestellt=beleg.zurueckgestellt,
        grund=beleg.grund,
        abweichungen=v.abweichungen,
    )


def _norm_vorschau(
    beleg: Kreditorenbeleg, v: eing.Vorschlag, zeile: dict[str, Any]
) -> dict[str, Any]:
    """Buchungstext und Zieldateiname, wie sie beim Buchen entstünden.

    Vor der Freigabe gezeigt und nicht erst danach: wer freigibt, soll sehen,
    was in Bexio und im Archiv stehen wird. Taugt ein Bestandteil nicht, steht
    der Grund unter den Abweichungen -- die Vorschau ist leer statt falsch.
    """
    leistung = beleg.leistung or v.leistung
    herkunft = "beleg" if beleg.leistung else ("lieferant" if v.leistung else None)
    datum = eing._als_datum(zeile.get("datum"))
    ergebnis: dict[str, Any] = {"leistung": leistung, "leistung_herkunft": herkunft}
    if not (v.anzeigename and leistung and datum):
        return ergebnis
    try:
        ergebnis["buchungstext"] = norm.buchungstext(v.anzeigename, leistung, datum)
        ergebnis["dateiname_ziel"] = norm.dateiname(
            v.anzeigename, leistung, datum, beleg.zahlweg
        )
    except norm.NormVerletzt as fehler:
        v.abweichungen.append(str(fehler))
    return ergebnis


async def _beleg_holen(db: AsyncSession, beleg_id: UUID) -> Kreditorenbeleg:
    beleg = await db.get(Kreditorenbeleg, beleg_id)
    if beleg is None:
        raise HTTPException(status_code=404, detail="Beleg nicht im Register")
    return beleg


async def _gesperrt_holen(db: AsyncSession, beleg_id: UUID) -> Kreditorenbeleg:
    """Die Zeile gesperrt und frisch gelesen -- ein zweiter Klick wartet auf den ersten."""
    beleg = (
        await db.execute(
            select(Kreditorenbeleg)
            .where(Kreditorenbeleg.id == beleg_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if beleg is None:
        raise HTTPException(status_code=404, detail="Beleg nicht im Register")
    return beleg


@router.post("/eingang/{beleg_id}/freigeben", response_model=Buchungsbericht)
async def freigeben(
    beleg_id: UUID,
    angaben: Freigabe,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Freigeben heisst buchen und ablegen -- in einem Schritt.

    Die Normprüfung läuft **vor** dem Festschreiben der Freigabe: verstösst
    der Beleg, bleibt er unverändert in der Liste und die Antwort nennt den
    Grund. Scheitert erst die Buchung oder die Ablage, steht die Freigabe --
    der Beleg erscheint unter «Nachholen», und nichts wird zweimal gebucht.

    Ein mitgeschicktes Konto gilt als **Entscheidung** und übersteht jeden
    weiteren Abgleich. Ohne diese Unterscheidung machte der nächste Lauf jede
    Korrektur rückgängig, ohne Meldung.
    """
    beleg = await _gesperrt_holen(db, beleg_id)

    if angaben.sollkonto:
        beleg.sollkonto = angaben.sollkonto
        beleg.sollkonto_herkunft = "entscheid"
    if angaben.steuerbehandlung:
        beleg.steuerbehandlung = angaben.steuerbehandlung
    if angaben.zahlweg:
        beleg.zahlweg = angaben.zahlweg
    if angaben.leistung:
        beleg.leistung = norm.leistung_normieren(angaben.leistung)

    # Ohne Leistung gäbe es weder Buchungstext noch Archivnamen -- die Freigabe
    # sähe erledigt aus und scheiterte erst beim Buchen.
    lieferant = decl.laden().lieferanten.get(beleg.lieferant_schluessel or "")
    if lieferant and lieferant.aufteilen:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"«{lieferant.anzeigename}» steht für mehrere Dienste — erst am Beleg wählen, welcher es ist.",
        )
    if not (beleg.leistung or (lieferant and lieferant.leistung)):
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "Die Leistung fehlt — sie steht in Buchungstext und Dateiname. "
                "Bitte am Beleg erfassen, etwa «Domain beispiel.ch»."
            ),
        )

    try:
        await reg.freigeben(db, beleg, durch=user.id)
    except ValueError as fehler:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(fehler)) from fehler

    # Eine Nutzungsrechnung eines Sammel-Lieferanten hält hier an: die Prüfung
    # meldet, dass sie mit dem Sammelbeleg ihres Monats freigegeben wird.
    try:
        pruefung = await _pruefen(user, beleg)
    except HTTPException:
        await db.rollback()
        raise
    if not pruefung[0].buchbar or pruefung[0].plan is None:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Nicht freigegeben: " + " · ".join(pruefung[0].verstoesse)
        )
    await db.commit()

    logger.info(
        "Kreditorenbeleg freigegeben: %s auf %s (%s) durch %s",
        beleg.dateiname, beleg.sollkonto, beleg.sollkonto_herkunft, user.email,
    )
    meldungen = _lieferant_mitbestaetigen(lieferant, beleg, user)

    beleg = await _gesperrt_holen(db, beleg_id)
    try:
        bericht = await _buchen_und_ablegen(db, user, beleg, *pruefung)
    except _NichtGebucht as fehler:
        bericht = Buchungsbericht(gebucht=False, meldungen=[fehler.meldung])
    bericht.freigegeben = True
    bericht.meldungen.extend(meldungen)
    return bericht


def _lieferant_mitbestaetigen(
    lieferant: decl.Lieferant | None, beleg: Kreditorenbeleg, user: User
) -> list[str]:
    """Die erste Freigabe bestätigt die Erwartung zum Lieferanten.

    Das Konto wird nur übernommen, wo die Deklaration keines kennt. Eine
    Kandidatenliste bleibt stehen -- dass ein Beleg auf 4200 ging, heisst nicht,
    dass der nächste nicht auf 6512 gehört -- und ein abweichendes Einzelkonto
    ist eine Entscheidung für diesen Beleg, nicht für den Lieferanten.
    """
    if lieferant is None or lieferant.bestaetigt:
        return []
    ohne_konto = not lieferant.sollkonto and not lieferant.sollkonto_kandidaten
    try:
        decl.bestaetigen(
            lieferant.schluessel,
            sollkonto=beleg.sollkonto if ohne_konto else None,
            durch=user.email,
        )
    except (ValueError, OSError) as fehler:
        logger.warning("Lieferant %s nicht bestätigt: %s", lieferant.schluessel, fehler)
        return [f"Lieferant nicht als bestätigt vermerkt: {fehler}"]
    return []


async def _modulzeile(user: User, beleg: Kreditorenbeleg) -> dict[str, Any]:
    """Die gelesenen Werte aus dem Modul, in derselben Projektion wie die Warteliste."""
    if beleg.modul_dokument_id is None:
        raise HTTPException(status_code=409, detail="Der Beleg ist noch nicht gelesen.")
    url, token = _rest_zugang(user)
    try:
        zeilen, _ = await rechnungen_holen(url, token)
    except SchnittstellenFehler as fehler:
        raise HTTPException(status_code=fehler.status, detail=fehler.meldung) from fehler
    except RuntimeError as fehler:
        raise HTTPException(status_code=503, detail=str(fehler)) from fehler
    zeile = next((z for z in zeilen if z.get("beleg_id") == beleg.modul_dokument_id), None)
    if zeile is None:
        raise HTTPException(
            status_code=409,
            detail=f"InvoiceInsight führt Beleg {beleg.modul_dokument_id} nicht mehr.",
        )
    return zeile


async def _pruefen(user: User, beleg: Kreditorenbeleg) -> tuple[kb.Pruefung, Any, kb.Bexiostand]:
    """Die Normprüfung mit allem, was sie live braucht."""
    zeile = await _modulzeile(user, beleg)
    lieferant = decl.laden().lieferanten.get(beleg.lieferant_schluessel or "")
    tag = kb._datum(zeile.get("datum"))
    client = bexio_zugang(user)
    try:
        stand = await kb.bexio_lesen(client, tag)
    except Exception as fehler:  # noqa: BLE001 -- ohne Bexio keine Prüfung, gemeldet wird
        raise HTTPException(status_code=503, detail=f"Bexio nicht lesbar: {fehler}") from fehler

    kurs = kurs_fehler = None
    waehrung = str(zeile.get("waehrung") or "")
    if tag and waehrung:
        try:
            kurs = await bazg_kurse.monatsmittel(waehrung, tag)
        except bazg_kurse.KursFehlt as fehler:
            kurs_fehler = str(fehler)
    return kb.pruefen(beleg, zeile, lieferant, stand, kurs=kurs, kurs_fehler=kurs_fehler), client, stand


def _pruefung_aus(p: kb.Pruefung, lieferant: decl.Lieferant | None) -> Buchungspruefung:
    plan = p.plan
    return Buchungspruefung(
        buchbar=p.buchbar,
        verstoesse=p.verstoesse,
        hinweise=p.hinweise,
        **(
            {
                "datum": plan.datum,
                "sollkonto": plan.sollkonto,
                "habenkonto": plan.habenkonto,
                "betrag": float(plan.betrag),
                "waehrung": plan.waehrung,
                "kurs": float(plan.kurs),
                "betrag_chf": float(plan.betrag_chf),
                "steuercode": plan.steuercode,
                "buchungstext": plan.text,
                "referenz": plan.referenz,
                "ablageziel": kb.ablageziel(lieferant, plan.dateiname, plan.datum) if lieferant else None,
            }
            if plan
            else {}
        ),
    )


@router.get("/eingang/{beleg_id}/pruefung", response_model=Buchungspruefung)
async def buchung_pruefen(
    beleg_id: UUID,
    sollkonto: str | None = Query(default=None, max_length=10),
    leistung: str | None = Query(default=None, max_length=120),
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Was gebucht würde, und ob es darf -- ohne etwas zu schreiben.

    Vor der Freigabe ist es eine **Vorschau**: Konto und Leistung, die in der
    Maske stehen, gelten für diese eine Prüfung, und die Freigabe gilt als
    gegeben. So zeigt die Maske vor dem Klick, was der Klick bucht -- und der
    Klick prüft danach dasselbe noch einmal, diesmal festgeschrieben.
    """
    beleg = await _beleg_holen(db, beleg_id)
    lieferant = decl.laden().lieferanten.get(beleg.lieferant_schluessel or "")
    vorschau = beleg.freigegeben_am is None
    if vorschau:
        if sollkonto:
            beleg.sollkonto = sollkonto
        if leistung and leistung.strip():
            beleg.leistung = norm.leistung_normieren(leistung)
        beleg.freigegeben_am = datetime.now(UTC)
    try:
        p, _, _ = await _pruefen(user, beleg)
    finally:
        if vorschau:
            await db.rollback()
    return _pruefung_aus(p, lieferant)


@router.post("/eingang/{beleg_id}/buchen", response_model=Buchungsbericht)
async def buchen(
    beleg_id: UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die Buchung nachholen, wenn sie bei der Freigabe gescheitert ist.

    Die Zeile bleibt bis zum Festschreiben gesperrt, und geprüft wird **nach**
    dem Sperren: ein zweiter Klick wartet auf den ersten und findet dann
    «schon gebucht» statt einer zweiten Buchung.
    """
    beleg = await _gesperrt_holen(db, beleg_id)
    p, client, stand = await _pruefen(user, beleg)
    try:
        return await _buchen_und_ablegen(db, user, beleg, p, client, stand)
    except _NichtGebucht as fehler:
        raise HTTPException(status_code=fehler.status, detail=fehler.meldung) from fehler


class _NichtGebucht(Exception):
    """Nichts in Bexio geschrieben -- der Beleg steht, wie er vorher stand."""

    def __init__(self, status: int, meldung: str):
        super().__init__(meldung)
        self.status = status
        self.meldung = meldung


async def _buchen_und_ablegen(
    db: AsyncSession,
    user: User,
    beleg: Kreditorenbeleg,
    p: kb.Pruefung,
    client: Any,
    stand: kb.Bexiostand,
) -> Buchungsbericht:
    """Bucht die gesperrte Zeile, hängt den Beleg an und legt die Rechnung ab.

    Vor der Buchung wird OneDrive gefragt, ob die Datei noch dort liegt, mit
    denselben Bytes, und ob das Ziel frei ist. Ohne diese Frage buchte die
    Freigabe eine Rechnung, die jemand in der Zwischenzeit gelöscht hat --
    und die Ablage scheiterte erst hinterher.
    """
    if not p.buchbar or p.plan is None:
        await db.rollback()
        raise _NichtGebucht(409, "Nicht gebucht: " + " · ".join(p.verstoesse))
    lieferant = decl.laden().lieferanten[beleg.lieferant_schluessel]
    ziel = kb.ablageziel(lieferant, p.plan.dateiname, p.plan.datum)

    url, token = _rest_zugang(user)
    try:
        datei, _, _ = await belegdatei_holen(url, token, beleg.modul_dokument_id)
    except (SchnittstellenFehler, RuntimeError) as fehler:
        # Ohne Datei wird nicht gebucht: eine Buchung ohne Beleg müsste die
        # Treuhänderin nachfordern, und das ist genau der Weg, der wegfallen soll.
        await db.rollback()
        raise _NichtGebucht(503, f"Belegdatei nicht abrufbar: {fehler}") from fehler

    graph = get_graph_client()
    if graph is None:
        await db.rollback()
        raise _NichtGebucht(503, "Nicht gebucht: Graph ist nicht eingerichtet, die Ablage wäre unmöglich.")
    try:
        await kb.vor_der_buchung_pruefen(beleg, len(datei), ziel, graph)
    except kb.AblageFehlt as fehler:
        await db.rollback()
        raise _NichtGebucht(409, str(fehler)) from fehler
    except Exception as fehler:  # noqa: BLE001 -- OneDrive nicht erreichbar, nicht gebucht
        await db.rollback()
        raise _NichtGebucht(503, f"OneDrive nicht erreichbar, nicht gebucht: {fehler}") from fehler

    try:
        ergebnis = await kb.buchen(beleg, p.plan, stand, client, datei)
    except Exception as fehler:  # noqa: BLE001 -- nicht gebucht, der Grund geht zurück
        await db.rollback()
        logger.warning("Buchung abgelehnt für %s: %s", beleg.dateiname, fehler)
        raise _NichtGebucht(502, f"Bexio hat nicht gebucht: {fehler}") from fehler
    await db.commit()

    bericht = Buchungsbericht(
        gebucht=True,
        bexio_referenz=beleg.bexio_referenz,
        beleg_angehaengt=ergebnis.beleg_angehaengt,
        journal_zeilen=ergebnis.journal_zeilen,
        meldungen=list(ergebnis.meldungen),
    )
    bericht.abgelegt, meldung = await _ablegen(db, beleg, ziel)
    if meldung:
        bericht.meldungen.append(meldung)
    return bericht


async def _ablegen(db: AsyncSession, beleg: Kreditorenbeleg, ziel: str) -> tuple[str | None, str | None]:
    """Ablage mit eigenem Festschreiben. Scheitert sie, bleibt alles davor stehen."""
    graph = get_graph_client()
    if graph is None:
        return None, "Nicht abgelegt: Graph ist nicht eingerichtet."
    try:
        abgelegt = await kb.ablegen(db, beleg, ziel, graph)
    except Exception as fehler:  # noqa: BLE001 -- die Buchung steht, gemeldet wird
        await db.rollback()
        logger.warning("Ablage gescheitert für %s: %s", beleg.dateiname, fehler)
        return None, f"Nicht abgelegt: {fehler}"
    await db.commit()
    return abgelegt, None


@router.post("/eingang/{beleg_id}/ablegen", response_model=Buchungsbericht)
async def ablegen(
    beleg_id: UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die Ablage nachholen -- einer Buchung, eines Sammelbelegs oder einer seiner Rechnungen."""
    beleg = await _beleg_holen(db, beleg_id)
    lieferant = decl.laden().lieferanten.get(beleg.lieferant_schluessel or "")
    if lieferant is None:
        raise HTTPException(status_code=409, detail="Ohne Lieferant kein Archivordner.")
    if beleg.abgelegt_am is not None:
        raise HTTPException(status_code=409, detail=f"Schon abgelegt: {beleg.archiv_pfad}")
    if beleg.belegart == "sammelbeleg":
        return await _sammelbeleg_nachholen(db, user, beleg, lieferant)

    if beleg.sammelbeleg_id is not None:
        haupt = await db.get(Kreditorenbeleg, beleg.sammelbeleg_id)
        if haupt is None or haupt.gebucht_am is None:
            raise HTTPException(status_code=409, detail="Der Sammelbeleg dieser Rechnung ist nicht gebucht.")
    elif beleg.gebucht_am is None:
        raise HTTPException(
            status_code=409,
            detail="Erst buchen, dann ablegen — sonst liegt eine Rechnung im Archiv, zu der es keine Buchung gibt.",
        )

    zeile = await _modulzeile(user, beleg)
    tag = kb._datum(zeile.get("datum"))
    if tag is None:
        raise HTTPException(status_code=409, detail="Ohne Rechnungsdatum kein Archivname.")
    try:
        name = norm.dateiname(
            lieferant.anzeigename, beleg.leistung or lieferant.leistung, tag, beleg.zahlweg
        )
    except norm.NormVerletzt as fehler:
        raise HTTPException(status_code=409, detail=str(fehler)) from fehler

    ziel = kb.ablageziel(lieferant, name, tag)
    if beleg.sammelbeleg_id is not None:
        graph = get_graph_client()
        if graph is None:
            raise HTTPException(status_code=503, detail="Graph ist nicht eingerichtet.")
        ziel = await _freies_ziel(graph, ziel, set())
    abgelegt, meldung = await _ablegen(db, beleg, ziel)
    if abgelegt is None:
        raise HTTPException(status_code=502, detail=meldung)
    return Buchungsbericht(gebucht=beleg.gebucht_am is not None, bexio_referenz=beleg.bexio_referenz, abgelegt=abgelegt)


# ── Der Monatssammelbeleg ────────────────────────────────────────────


@dataclass
class _Sammellage:
    """Alles, was Vorschau, Dokument und Freigabe eines Monats brauchen -- einmal gelesen."""

    lieferant: decl.Lieferant
    pruefung: sb.Sammelpruefung
    eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]]
    client: Any
    stand: kb.Bexiostand
    url: str
    token: str


def _sammellieferant(schluessel: str) -> decl.Lieferant:
    lieferant = decl.laden().lieferanten.get(schluessel)
    if lieferant is None:
        raise HTTPException(status_code=404, detail=f"Lieferant «{schluessel}» ist nicht deklariert.")
    if lieferant.buchung != "sammelbeleg":
        raise HTTPException(
            status_code=409, detail=f"{lieferant.anzeigename} wird einzeln gebucht, nicht über einen Sammelbeleg."
        )
    return lieferant


def _erster_des_monats(jahr: int, monat: int) -> date:
    try:
        return date(jahr, monat, 1)
    except ValueError as fehler:
        raise HTTPException(status_code=422, detail=f"{monat}.{jahr} ist kein Monat.") from fehler


async def _modulbestand(user: User) -> tuple[str, str, list[dict[str, Any]]]:
    url, token = _rest_zugang(user)
    try:
        zeilen, _ = await rechnungen_holen(url, token)
    except SchnittstellenFehler as fehler:
        raise HTTPException(status_code=fehler.status, detail=fehler.meldung) from fehler
    except RuntimeError as fehler:
        raise HTTPException(status_code=503, detail=str(fehler)) from fehler
    return url, token, zeilen


async def _sammellage(
    db: AsyncSession,
    user: User,
    schluessel: str,
    jahr: int,
    monat: int,
    *,
    sperren: bool = False,
    vollstaendig_bestaetigt: bool = False,
) -> _Sammellage:
    """Die wartenden Rechnungen des Monats, geprüft gegen Bexio, BAZG und das Archiv.

    ``sperren`` hält die Zeilen bis zum Festschreiben: ein zweiter Klick auf
    «Freigeben» wartet auf den ersten und findet danach nichts mehr, was wartet.
    """
    lieferant = _sammellieferant(schluessel)
    erster = _erster_des_monats(jahr, monat)
    letzter = sb.monatsletzter(erster)

    abfrage = select(Kreditorenbeleg).where(
        Kreditorenbeleg.lieferant_schluessel == schluessel,
        Kreditorenbeleg.belegart == "rechnung",
        Kreditorenbeleg.freigegeben_am.is_(None),
        Kreditorenbeleg.zurueckgestellt.is_(False),
        Kreditorenbeleg.sammelbeleg_id.is_(None),
    )
    if sperren:
        abfrage = abfrage.with_for_update().execution_options(populate_existing=True)
    belege = list((await db.execute(abfrage)).scalars().all())

    url, token, zeilen = await _modulbestand(user)
    je_kennung = {int(z["beleg_id"]): z for z in zeilen if z.get("beleg_id") is not None}
    eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]] = []
    ungelesen = 0
    for b in belege:
        z = je_kennung.get(b.modul_dokument_id or -1)
        if not z:
            ungelesen += 1
        elif sb.wartet(b, z, lieferant) and sb.monat_von(kb._datum(z.get("datum"))) == erster:
            eintraege.append((b, z))

    # Wo das Modul eine Nummer ausserhalb des Eingangs kennt, liegt sie im Archiv.
    eingang = _eingangsordner(user)
    eigene = [z for z in zeilen if z.get("lieferant_schluessel") == schluessel and z.get("rechnungsnummer")]
    im_archiv = {
        str(z["rechnungsnummer"]).strip(): f"{z.get('beleg_ordner')}/{z.get('beleg_datei')}"
        for z in eigene
        if not eing._im_eingang(z.get("beleg_ordner"), eingang)
    }
    bekannte = {str(z["rechnungsnummer"]).strip(): kb._datum(z.get("datum")) for z in eigene}

    client = bexio_zugang(user)
    try:
        stand = await kb.bexio_lesen(client, letzter)
    except Exception as fehler:  # noqa: BLE001 -- ohne Bexio keine Prüfung, gemeldet wird
        raise HTTPException(status_code=503, detail=f"Bexio nicht lesbar: {fehler}") from fehler

    kurs = kurs_fehler = None
    waehrung = next((str(z["waehrung"]) for _, z in eintraege if z.get("waehrung")), "")
    if waehrung and date.today() > letzter:
        try:
            kurs = await bazg_kurse.monatsmittel(waehrung, letzter)
        except bazg_kurse.KursFehlt as fehler:
            kurs_fehler = str(fehler)

    p = sb.pruefen(
        lieferant, erster, eintraege, stand,
        kurs=kurs, kurs_fehler=kurs_fehler, im_archiv=im_archiv, bekannte=bekannte,
        vollstaendig_bestaetigt=vollstaendig_bestaetigt,
    )
    if ungelesen:
        p.verstoesse.append(
            f"{ungelesen} Beleg(e) von {lieferant.anzeigename} sind noch nicht gelesen — ob sie "
            f"in den {sb.monatsname(erster)} gehören, steht erst danach fest."
        )
    return _Sammellage(lieferant, p, eintraege, client, stand, url, token)


def _vorschau_aus(p: sb.Sammelpruefung) -> Sammelvorschau:
    plan = p.plan
    mit_betrag = bool(p.positionen) and p.waehrung is not None
    return Sammelvorschau(
        lieferant_schluessel=p.lieferant.schluessel,
        anzeigename=p.lieferant.anzeigename,
        jahr=p.monat.year,
        monat=p.monat.month,
        bezeichnung=sb.monatsname(p.monat),
        nachtrag=p.nachtrag,
        positionen=[
            SammelPosition(
                beleg_id=pos.beleg_id,
                dateiname=pos.dateiname,
                nummer=pos.nummer,
                datum=pos.datum,
                betrag=float(pos.betrag) if pos.betrag is not None else None,
            )
            for pos in p.positionen
        ],
        waehrung=p.waehrung,
        betrag=float(p.betrag) if mit_betrag else None,
        bezugsteuer=float(p.bezugsteuer) if mit_betrag else None,
        kurs=float(p.kurs) if p.kurs is not None else None,
        betrag_chf=float(p.betrag_chf) if mit_betrag and p.betrag_chf is not None else None,
        bezugsteuer_chf=float(p.bezugsteuer_chf) if mit_betrag and p.bezugsteuer_chf is not None else None,
        buchbar=p.buchbar,
        bereit=p.bereit,
        vollstaendig=p.vollstaendig,
        vollstaendig_grund=p.vollstaendig_grund,
        verstoesse=p.verstoesse,
        hinweise=p.hinweise,
        luecken=p.luecken,
        ablageziel=p.ablageziel,
        **(
            {
                "datum": plan.datum,
                "sollkonto": plan.sollkonto,
                "habenkonto": plan.habenkonto,
                "steuercode": plan.steuercode,
                "buchungstext": plan.text,
                "referenz": plan.referenz,
            }
            if plan
            else {}
        ),
    )


def _aussteller(eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]]) -> str | None:
    """Wer die Rechnungen stellt -- «Anysphere, Inc.», wie es auf ihnen steht."""
    return next((str(z["lieferant_original"]) for _, z in eintraege if z.get("lieferant_original")), None)


async def _sammel_dateien(url: str, token: str, p: sb.Sammelpruefung) -> dict[UUID, bytes]:
    dateien: dict[UUID, bytes] = {}
    for pos in p.positionen:
        if pos.modul_id is None:
            raise HTTPException(status_code=409, detail=f"«{pos.dateiname}» ist nicht gelesen.")
        try:
            dateien[pos.beleg_id], _, _ = await belegdatei_holen(url, token, pos.modul_id)
        except (SchnittstellenFehler, RuntimeError) as fehler:
            raise HTTPException(
                status_code=503, detail=f"Rechnung «{pos.dateiname}» nicht abrufbar: {fehler}"
            ) from fehler
    return dateien


async def _freies_ziel(graph: Any, ziel: str, vergeben: set[str]) -> str:
    """Der erste freie Name nach der Norm: ``… KK.pdf``, dann ``… KK (2).pdf`` usw.

    Cursor schickt bis zu sieben Rechnungen am Tag. Belegt ist, was schon im
    Archiv liegt oder in diesem Lauf schon vergeben wurde -- überschrieben wird nie.
    """
    for n in range(1, 50):
        kandidat = norm.nummeriert(ziel, n)
        if kandidat in vergeben:
            continue
        if await graph.drive_item_by_path(f"{kb.ARCHIV_WURZEL}/{kandidat}") is None:
            return kandidat
    raise kb.AblageFehlt(f"Kein freier Name für «{ziel}» — bitte im Archiv ansehen.")


async def _einzelziele(graph: Any, p: sb.Sammelpruefung) -> dict[UUID, str]:
    vergeben: set[str] = set()
    ziele: dict[UUID, str] = {}
    for pos in p.positionen:
        name = norm.dateiname(p.lieferant.anzeigename, p.lieferant.leistung, pos.datum, "karte")
        ziel = await _freies_ziel(graph, kb.ablageziel(p.lieferant, name, pos.datum), vergeben)
        vergeben.add(ziel)
        ziele[pos.beleg_id] = ziel
    return ziele


async def _sammel_hochladen(
    db: AsyncSession, beleg: Kreditorenbeleg, ziel: str, inhalt: bytes, graph: Any
) -> tuple[str | None, str | None]:
    """Den Sammelbeleg ins Archiv schreiben. Er lag nie im Eingang -- er entsteht hier."""
    try:
        await graph.ensure_drive_folder(f"{kb.ARCHIV_WURZEL}/{ziel.rsplit('/', 1)[0]}")
        neu = await graph.upload_drive_file(f"{kb.ARCHIV_WURZEL}/{ziel}", inhalt)
        await reg.ablage_vermerken(db, beleg, archiv_pfad=ziel, graph_item_id=(neu or {}).get("id"))
    except Exception as fehler:  # noqa: BLE001 -- die Buchung steht, gemeldet wird
        await db.rollback()
        logger.warning("Sammelbeleg nicht abgelegt (%s): %s", ziel, fehler)
        return None, f"Sammelbeleg gebucht, aber nicht abgelegt: {fehler}"
    await db.commit()
    return ziel, None


@router.get("/sammelbeleg/{schluessel}/{jahr}/{monat}", response_model=Sammelvorschau)
async def sammelbeleg_vorschau(
    schluessel: str,
    jahr: int,
    monat: int,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Was der Sammelbeleg des Monats bucht und ob er darf -- ohne etwas zu schreiben."""
    return _vorschau_aus((await _sammellage(db, user, schluessel, jahr, monat)).pruefung)


@router.get("/sammelbeleg/{schluessel}/{jahr}/{monat}/pdf")
async def sammelbeleg_pdf(
    schluessel: str,
    jahr: int,
    monat: int,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Der Beleg, wie er an die Buchung angehängt würde -- zum Ansehen vor der Freigabe."""
    lage = await _sammellage(db, user, schluessel, jahr, monat)
    p = lage.pruefung
    if p.kurs is None or p.waehrung is None or not p.positionen:
        raise HTTPException(status_code=409, detail="Noch kein Beleg: " + " · ".join(p.verstoesse))
    dateien = await _sammel_dateien(lage.url, lage.token, p)
    try:
        inhalt = sb.erzeugen(p, dateien, aussteller=_aussteller(lage.eintraege))
    except Exception as fehler:  # noqa: BLE001 -- eine unlesbare Rechnung, gemeldet wird
        raise HTTPException(status_code=422, detail=f"Der Beleg liess sich nicht herstellen: {fehler}") from fehler
    name = p.plan.dateiname if p.plan else f"Sammelbeleg {p.monat:%Y-%m}.pdf"
    return Response(
        content=inhalt,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}"},
    )


@router.post("/sammelbeleg/{schluessel}/{jahr}/{monat}/freigeben", response_model=Buchungsbericht)
async def sammelbeleg_freigeben(
    schluessel: str,
    jahr: int,
    monat: int,
    angaben: SammelFreigabe | None = None,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Freigeben heisst buchen und ablegen -- für den ganzen Monat in einem Schritt.

    Belegt die Nummernfolge nicht, dass der Monat vollständig ist, bucht erst
    die Bestätigung, dass der Download nach Monatsende gelaufen ist.

    Die Reihenfolge ist die der Einzelfreigabe. Erst prüfen, auch ob jede
    Rechnung noch mit denselben Bytes im Eingang liegt und ob die Ziele frei
    sind; dann buchen, mit dem Beleg angehängt; erst danach ablegen. Scheitert
    die Buchung, ist nichts geschrieben -- ein zweiter Versuch bucht nicht
    doppelt, weil die Prüfung den Text im Journal findet. Scheitert erst die
    Ablage, steht die Buchung, und was fehlt, erscheint unter «Nachholen».
    """
    lage = await _sammellage(
        db, user, schluessel, jahr, monat, sperren=True,
        vollstaendig_bestaetigt=bool(angaben and angaben.vollstaendig_bestaetigt),
    )
    p = lage.pruefung
    if not p.buchbar or p.plan is None or p.ablageziel is None:
        await db.rollback()
        gruende = p.verstoesse or [f"Nicht als vollständig belegt. {p.vollstaendig_grund}"]
        raise HTTPException(status_code=409, detail="Nicht freigegeben: " + " · ".join(gruende))

    graph = get_graph_client()
    if graph is None:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Nicht gebucht: Graph ist nicht eingerichtet, die Ablage wäre unmöglich.")
    je_beleg = {b.id: b for b, _ in lage.eintraege}
    try:
        dateien = await _sammel_dateien(lage.url, lage.token, p)
        ziele = await _einzelziele(graph, p)
        for pos in p.positionen:
            await kb.vor_der_buchung_pruefen(
                je_beleg[pos.beleg_id], len(dateien[pos.beleg_id]), ziele[pos.beleg_id], graph
            )
        if await graph.drive_item_by_path(f"{kb.ARCHIV_WURZEL}/{p.ablageziel}") is not None:
            raise kb.AblageFehlt(f"Am Ziel liegt schon «{p.ablageziel}» — nicht gebucht, bitte ansehen.")
        inhalt = sb.erzeugen(p, dateien, aussteller=_aussteller(lage.eintraege))
    except HTTPException:
        await db.rollback()
        raise
    except kb.AblageFehlt as fehler:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(fehler)) from fehler
    except Exception as fehler:  # noqa: BLE001 -- OneDrive oder eine Rechnung, nicht gebucht
        await db.rollback()
        raise HTTPException(status_code=503, detail=f"Nicht gebucht: {fehler}") from fehler

    auf = await reg.aufnehmen(
        db,
        datei_hash=reg.hash_von(inhalt),
        dateiname=p.plan.dateiname,
        quelle="erzeugt",
        belegart="sammelbeleg",
        lieferant_schluessel=schluessel,
        rechnungsnummer=p.rechnungsnummer,
    )
    if not auf.neu:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Für {sb.monatsname(p.monat)} steht schon «{auf.beleg.dateiname}» im Register — nicht ein zweites Mal.",
        )
    s = auf.beleg
    s.sollkonto = p.plan.sollkonto
    s.sollkonto_herkunft = "vorschlag"
    s.steuerbehandlung = lage.lieferant.steuer_am(p.plan.datum)
    s.zahlweg = "karte"
    await reg.freigeben(db, s, durch=user.id)
    for b, _ in lage.eintraege:
        b.sammelbeleg_id = s.id
        b.sollkonto = b.sollkonto or s.sollkonto
        b.sollkonto_herkunft = b.sollkonto_herkunft or "vorschlag"
        b.freigegeben_am = s.freigegeben_am
        b.freigegeben_von = user.id
    await db.flush()

    try:
        ergebnis = await kb.buchen(s, p.plan, lage.stand, lage.client, inhalt)
    except Exception as fehler:  # noqa: BLE001 -- nicht gebucht, der Grund geht zurück
        await db.rollback()
        logger.warning("Sammelbeleg %s nicht gebucht: %s", p.rechnungsnummer, fehler)
        raise HTTPException(status_code=502, detail=f"Bexio hat nicht gebucht: {fehler}") from fehler
    await db.commit()
    logger.info(
        "Sammelbeleg gebucht: %s, %d Rechnungen, %s %s durch %s",
        p.plan.text, len(p.positionen), p.plan.betrag, p.plan.waehrung, user.email,
    )

    bericht = Buchungsbericht(
        freigegeben=True,
        gebucht=True,
        bexio_referenz=s.bexio_referenz,
        beleg_angehaengt=ergebnis.beleg_angehaengt,
        journal_zeilen=ergebnis.journal_zeilen,
        meldungen=list(ergebnis.meldungen),
    )
    sammel_id = s.id
    bericht.abgelegt, meldung = await _sammel_hochladen(db, s, p.ablageziel, inhalt, graph)
    if meldung:
        bericht.meldungen.append(meldung)

    # Je Rechnung neu geholt: eine gescheiterte Ablage rollt zurück und lässt
    # die geladenen Zeilen verfallen.
    abgelegt = 0
    for pos in p.positionen:
        beleg = await db.get(Kreditorenbeleg, pos.beleg_id)
        ort, meldung = await _ablegen(db, beleg, ziele[pos.beleg_id])
        if ort:
            abgelegt += 1
        elif meldung:
            bericht.meldungen.append(f"«{pos.dateiname}»: {meldung}")
    if abgelegt < len(p.positionen):
        bericht.meldungen.append(
            f"{abgelegt} von {len(p.positionen)} Rechnungen abgelegt — der Rest steht unter «Nachholen»."
        )
    logger.info("Sammelbeleg %s: %d von %d Rechnungen abgelegt", sammel_id, abgelegt, len(p.positionen))
    return bericht


_SAMMELNUMMER = re.compile(r"Sammelbeleg (\d{4})\.(\d{2})( Nachtrag)?")


async def _sammelbeleg_nachholen(
    db: AsyncSession, user: User, beleg: Kreditorenbeleg, lieferant: decl.Lieferant
) -> Buchungsbericht:
    """Den gebuchten Sammelbeleg nachträglich ablegen -- aus seinen Rechnungen neu hergestellt.

    Die Bytes der Buchung liegen in Bexio; hier entsteht derselbe Beleg aus
    denselben Rechnungen zum selben, abgeschlossenen Monatskurs.
    """
    if beleg.gebucht_am is None:
        raise HTTPException(status_code=409, detail="Der Sammelbeleg ist nicht gebucht.")
    treffer = _SAMMELNUMMER.fullmatch(beleg.rechnungsnummer or "")
    if not treffer:
        raise HTTPException(status_code=409, detail=f"«{beleg.rechnungsnummer}» nennt keinen Monat.")
    erster = date(int(treffer.group(1)), int(treffer.group(2)), 1)

    einzelne = list(
        (await db.execute(select(Kreditorenbeleg).where(Kreditorenbeleg.sammelbeleg_id == beleg.id))).scalars().all()
    )
    url, token, zeilen = await _modulbestand(user)
    je_kennung = {int(z["beleg_id"]): z for z in zeilen if z.get("beleg_id") is not None}
    fehlt = [b.dateiname for b in einzelne if (b.modul_dokument_id or -1) not in je_kennung]
    if fehlt:
        raise HTTPException(status_code=409, detail=f"Nicht mehr im Modul: {', '.join(fehlt)}")
    eintraege = [(b, je_kennung[b.modul_dokument_id]) for b in einzelne]
    waehrung = next((str(z["waehrung"]) for _, z in eintraege if z.get("waehrung")), "USD")
    try:
        kurs = await bazg_kurse.monatsmittel(waehrung, sb.monatsletzter(erster))
    except bazg_kurse.KursFehlt as fehler:
        raise HTTPException(status_code=503, detail=str(fehler)) from fehler
    bekannte = {
        str(z["rechnungsnummer"]).strip() for z in zeilen
        if z.get("lieferant_schluessel") == lieferant.schluessel and z.get("rechnungsnummer")
    }
    p = sb.nachbauen(lieferant, erster, eintraege, kurs=kurs, nachtrag=bool(treffer.group(3)), bekannte=bekannte)
    try:
        inhalt = sb.erzeugen(p, await _sammel_dateien(url, token, p), aussteller=_aussteller(eintraege))
    except HTTPException:
        raise
    except Exception as fehler:  # noqa: BLE001 -- eine unlesbare Rechnung, gemeldet wird
        raise HTTPException(status_code=422, detail=f"Der Beleg liess sich nicht herstellen: {fehler}") from fehler

    graph = get_graph_client()
    if graph is None:
        raise HTTPException(status_code=503, detail="Graph ist nicht eingerichtet.")
    ziel = f"{lieferant.ordner}/{erster:%Y}/Sammelbelege/{beleg.dateiname}"
    abgelegt, meldung = await _sammel_hochladen(db, beleg, ziel, inhalt, graph)
    if abgelegt is None:
        raise HTTPException(status_code=502, detail=meldung)
    return Buchungsbericht(gebucht=True, bexio_referenz=beleg.bexio_referenz, abgelegt=abgelegt)


@router.post("/eingang/{beleg_id}/lieferant", response_model=EingangsBeleg)
async def lieferant_waehlen(
    beleg_id: UUID,
    angaben: LieferantWahl,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Wählt den Lieferanten, wo der Absender mehrere meint oder keinen.

    Danach entsteht der Vorschlag neu -- Konto, Steuer und Zahlweg des
    gewählten Lieferanten. Ein Konto, das ein Mensch schon entschieden hat,
    bleibt stehen. Wo das Modul den Lieferanten über den Ordner kennt, gilt
    der Ordner; dann ist die Wahl ein Widerspruch und wird abgelehnt.
    """
    deklaration = decl.laden()
    lieferant = deklaration.lieferanten.get(angaben.schluessel)
    if lieferant is None:
        raise HTTPException(status_code=404, detail=f"Lieferant «{angaben.schluessel}» ist nicht deklariert.")
    if lieferant.aufteilen:
        raise HTTPException(
            status_code=409,
            detail=f"«{lieferant.anzeigename}» steht selbst für mehrere Dienste — bitte einen davon wählen.",
        )

    beleg = await _gesperrt_holen(db, beleg_id)
    if beleg.freigegeben_am is not None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Schon freigegeben — der Lieferant steht fest.")
    try:
        zeile = await _modulzeile(user, beleg)
    except HTTPException:
        await db.rollback()
        raise

    v = eing.vorschlagen(zeile, deklaration, gewaehlt=lieferant.schluessel)
    if v.lieferant_schluessel != lieferant.schluessel:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"InvoiceInsight ordnet diesen Beleg «{v.lieferant_schluessel}» zu — "
                f"dort entscheidet der Ablageordner."
            ),
        )

    beleg.lieferant_schluessel = lieferant.schluessel
    if beleg.sollkonto_herkunft != "entscheid":
        beleg.sollkonto = beleg.sollkonto_herkunft = None
        beleg.steuerbehandlung = beleg.zahlweg = None
    eing._vorschlag_anlegen(beleg, v)
    await db.commit()
    logger.info(
        "Kreditorenbeleg %s: Lieferant %s gewählt durch %s",
        beleg.dateiname, lieferant.schluessel, user.email,
    )
    return _eingangsbeleg(beleg, zeile, deklaration)


@router.post("/eingang/{beleg_id}/zuruecklegen", response_model=EingangsBeleg)
async def zuruecklegen(
    beleg_id: UUID,
    angaben: Zuruecklegen,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Aus der Warteliste nehmen -- mit Begründung, sonst 422."""
    beleg = await _beleg_holen(db, beleg_id)
    try:
        await reg.zuruecklegen(db, beleg, angaben.grund)
    except ValueError as fehler:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(fehler)) from fehler
    await db.commit()

    return EingangsBeleg(
        id=beleg.id,
        dateiname=beleg.dateiname,
        quelle=beleg.quelle,
        eingang_am=beleg.eingang_am,
        beleg_id=beleg.modul_dokument_id,
        lieferant_schluessel=beleg.lieferant_schluessel,
        sollkonto=beleg.sollkonto,
        sollkonto_herkunft=beleg.sollkonto_herkunft,
        zurueckgestellt=beleg.zurueckgestellt,
        grund=beleg.grund,
    )


@router.patch("/lieferant/{schluessel}")
async def lieferant_bestaetigen(
    schluessel: str,
    angaben: LieferantBestaetigen,
    user: User = Depends(require_role("owner")),
):
    """Die Erwartung zu einem Lieferanten bestätigen -- und das Konto festlegen.

    Schreibt in ``docs/kreditorenlieferanten.yaml`` zurück. Zeilengenau, damit
    die Belegkommentare stehen bleiben: ``# einstimmig, 48 Buchungen`` ist die
    Herleitung, und ohne sie müsste die nächste Prüfung die Messung wiederholen.

    Damit die Bestätigung einen Neubau übersteht, muss ``docs/`` **schreibbar**
    eingehängt sein. Liegt sie nur im Abbild, ist jede Bestätigung nach dem
    nächsten ``make prod`` weg -- und das fällt erst auf, wenn wieder alle 149
    Einträge unbestätigt sind.
    """
    try:
        eintrag = decl.bestaetigen(
            schluessel, sollkonto=angaben.sollkonto, durch=user.email
        )
    except ValueError as fehler:
        raise HTTPException(status_code=404, detail=str(fehler)) from fehler
    except OSError as fehler:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Die Deklaration ist nicht schreibbar ({fehler}) — ist docs/ "
                f"schreibbar eingehängt?"
            ),
        ) from fehler

    return {
        "schluessel": eintrag.schluessel,
        "ordner": eintrag.ordner,
        "sollkonto": eintrag.sollkonto,
        "sollkonto_kandidaten": list(eintrag.sollkonto_kandidaten),
        "bestaetigt": eintrag.bestaetigt,
    }


@router.get("/kontenplan")
async def kontenplan(user: User = Depends(require_role("owner"))):
    """Die Aufwandskonten, auf die gebucht werden kann.

    Warum es diesen Endpunkt braucht: bis hierhin liess sich ein Konto nur
    übernehmen, nicht wählen. Bei 48 der 149 Lieferanten trägt die Deklaration
    einen Vorschlag, bei 15 eine Kandidatenliste -- bei **86** gar nichts. Für
    diese 86 war «Freigeben» gesperrt und es gab kein Eingabefeld. Eine Maske mit
    einem gesperrten Knopf und keinem Weg daran vorbei ist keine Maske.

    Gelesen wird aus dem Datenraum und nicht live aus Bexio: die Antwort gehört in
    eine Auswahlliste, die bei jedem Belegwechsel steht, und ein Kontenplan ändert
    sich nicht im Minutentakt. Fällt der Datenraum aus, ist die Liste leer statt
    falsch -- und die Meldung sagt, warum.
    """
    from app.services import datenraum_lesen as raum

    try:
        konten = raum.aufwandskonten()
    except raum.DatenraumUnbrauchbar as fehler:
        raise HTTPException(
            status_code=503,
            detail=f"Kontenplan nicht lesbar: {fehler}",
        ) from fehler

    return {"konten": konten, **raum.stand()}


async def _aus_onedrive(db: AsyncSession, modul_id: int, grund: str) -> tuple[bytes, str, str]:
    """Der Rückfall, wenn das Modul schweigt: die Datei über den Registerpfad aus OneDrive.

    Langsamer als der Spiegel des Moduls, aber wer eine Rechnung prüft, soll
    sie sehen -- auch wenn der Extraktionsdienst gerade neu startet.
    """
    beleg = (
        await db.execute(select(Kreditorenbeleg).where(Kreditorenbeleg.modul_dokument_id == modul_id))
    ).scalar_one_or_none()
    pfad = beleg and (beleg.archiv_pfad or beleg.graph_pfad)
    graph = get_graph_client()
    if not pfad or graph is None:
        raise HTTPException(status_code=503, detail=f"Modul nicht erreichbar ({grund}), kein Rückfall möglich.")
    try:
        eintrag = await graph.drive_item_by_path(f"{kb.ARCHIV_WURZEL}/{pfad}")
        if eintrag is None:
            raise HTTPException(status_code=404, detail=f"«{pfad}» liegt nicht mehr in OneDrive.")
        inhalt = await graph.download_drive_item(eintrag["id"])
    except HTTPException:
        raise
    except Exception as fehler:  # noqa: BLE001 -- beide Wege zu, gemeldet wird
        raise HTTPException(
            status_code=503, detail=f"Modul ({grund}) und OneDrive ({fehler}) nicht erreichbar."
        ) from fehler
    logger.info("Belegdatei %s aus OneDrive statt aus dem Modul: %s", modul_id, grund)
    return inhalt, "application/pdf", pfad.rsplit("/", 1)[-1]


@router.get("/beleg/{beleg_id}/datei")
async def belegdatei(
    beleg_id: int,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Die Originaldatei, durchgereicht vom Modul.

    ``beleg_id`` ist die Modulkennung, nicht die Registerkennung -- die Datei
    kennt nur das Modul. Durchgereicht statt aus dem Dateisystem gelesen: das
    Backend läuft im Container und sieht den Archivpfad des Hosts nicht.
    """
    url, token = _rest_zugang(user)
    try:
        inhalt, typ, name = await belegdatei_holen(url, token, beleg_id)
    except SchnittstellenFehler as fehler:
        if fehler.status < 500:
            raise HTTPException(status_code=fehler.status, detail=fehler.meldung) from fehler
        inhalt, typ, name = await _aus_onedrive(db, beleg_id, fehler.meldung)
    except RuntimeError as fehler:
        inhalt, typ, name = await _aus_onedrive(db, beleg_id, str(fehler))

    return Response(
        content=inhalt,
        media_type=typ,
        headers={
            # ``inline``, damit der Browser das PDF neben den Feldern anzeigt
            # statt es herunterzuladen. Wer prüfen soll, muss sehen.
            "Content-Disposition": f'inline; filename="{name}"',
        },
    )
