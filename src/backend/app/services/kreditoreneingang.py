"""Der Kreditoreneingang -- was auf eine Entscheidung wartet, und was vorgeschlagen wird.

## Der Eingang ist ein Ort, kein Stichtag

Eine neue Rechnung liegt in ``_OPEN/InnoSmith``; ins Archiv wandert sie erst
**nach** der Freigabe. «Offen» ist deshalb keine Datumsfrage, sondern eine Frage
des Ortes -- liegt die Datei im Eingang, wartet sie. Ein Stichtag wäre
willkürlich gewesen und hätte bei jedem Nachtrag die falsche Antwort gegeben.

Bis zum 22.09.2026 stand ``_OPEN`` im Ordnerausschluss des Moduls. Damit sah die
Extraktion ausschliesslich das Archiv -- also die Ablage *nach* der Freigabe --
und eine ungeprüfte Rechnung war für sie unsichtbar. Gebucht werden muss aber
vor der Ablage.

Der Ausschluss zu lösen genügte nicht. Der Abzug führte ``beleg_datei``, und
darin steht **nur der Dateiname** -- bewusst so, weil ein absoluter Pfad einem
Empfänger etwas verspricht, das er nicht öffnen kann. Nur suchte die Prüfung
hier ``/_OPEN/`` in einer Zeichenkette, die nie einen Schrägstrich enthält: von
1197 Belegen galt keiner als wartend, obwohl 17 im Eingang lagen. Zwei je richtige
Entscheidungen ergaben zusammen einen Eingang, der sich nicht füllen **kann**,
und weil eine leere Liste aussieht wie «nichts zu tun», war das kein Fehler,
sondern eine Ruhe. Seither führt der Abzug ``beleg_ordner`` -- den Ablageort
unter der Archivwurzel, nicht den Maschinenpfad -- und fehlt die Spalte, bricht
der Abgleich mit Meldung ab statt mit einer leeren Liste.

## Warum der Schlüssel und nicht der Name

Der Abzug führt seit dem 22.09.2026 ``lieferant_schluessel`` und ``sha256``.
Beide fehlten, und beide Lücken waren dieselbe Sorte Fehler:

* Ueber ``lieferant`` zu verknüpfen ist Namensabgleich, und der scheitert
  still. Im Datenraum kostete das 227'789 CHF, weil derselbe Kunde in zwei
  Systemen zwei Namen trug.
* Ueber ``beleg_datei`` wiederzuerkennen heisst, den Pfad für die Identität zu
  nehmen. Nach der Ablage ist er ein anderer, und es entsteht eine zweite Zeile.

``lieferant_grund`` sagt dazu, **woher** die Zuordnung stammt. Ein leerer
Schlüssel heisst nicht «kein Lieferant», sondern «nicht eindeutig» -- und nur
dann muss ein Mensch gefragt werden.

## Eine Abweichung ist eine Frage

Die Deklaration in ``docs/kreditorenlieferanten.yaml`` ist eine Erwartung, keine
Regel. Weist ein Lieferant erstmals Schweizer MWST aus, ist die bisherige
Bezugssteuer nicht falsch gewesen, sondern überholt. Darum sammelt der
Vorschlag ``abweichungen`` als Fragen und setzt nichts still um.

Wo die Deklaration bewusst kein Konto nennt, sondern Kandidaten -- Hosttech 6512
gegen 4200, Digitec 6571 gegen 6500 --, bleibt ``sollkonto`` leer. Die Freigabe
ist dann durch die Datenbank gesperrt, bis ein Mensch wählt. Ein geratener
Vorschlag wäre dort Scheingenauigkeit mit Geldfolge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from app.models.models import Kreditorenbeleg
from app.services import kreditorenlieferanten as decl
from app.services import kreditorenregister as reg
from app.services.invoiceinsight_rest import rechnungen_holen
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Wo der Eingang liegt. Als Vorgabe und nicht als Konstante, weil die Wurzel des
# Archivs eine Betriebsangabe ist -- sie steht als Owner-Einstellung darüber.
EINGANG_VORGABE = "_OPEN/InnoSmith"

# Die Klärgründe aus ``lieferant_grund``. Sie stehen hier wörtlich und nicht
# als Negation von «ordner oder absender»: käme im Modul ein neuer Grund dazu,
# wäre er sonst stillschweigend ein Nicht-Klärfall.
KLAERGRUENDE: frozenset[str] = frozenset(
    {"nur_zahlungsanbieter", "mehrdeutig", "unbekannt"}
)


@dataclass
class Vorschlag:
    """Was mit diesem Beleg geschehen soll -- und woher das kommt."""

    sollkonto: str | None = None
    sollkonto_herkunft: str | None = None
    sollkonto_kandidaten: tuple[str, ...] = ()
    steuerbehandlung: str | None = None
    zahlweg: str | None = None
    lieferant_schluessel: str | None = None
    lieferant_bestaetigt: bool = False
    lieferant_kandidaten: tuple[str, ...] = ()
    """Unter wem am Beleg zu wählen ist, wenn der Absender mehrere meint."""
    anzeigename: str | None = None
    leistung: str | None = None
    abweichungen: list[str] = field(default_factory=list)

    @property
    def entscheidbar(self) -> bool:
        """Ob die Freigabe möglich ist, ohne etwas zu raten."""
        return bool(self.sollkonto)


@dataclass
class Befund:
    """Was der Abgleich ergab. Ohne Zählung ist ein stiller Verlust unsichtbar."""

    gesehen: int = 0
    neu: int = 0
    bekannt: int = 0
    ohne_lieferant: int = 0
    ohne_konto: int = 0
    dubletten: int = 0
    """Andere Dateien einer schon erfassten Rechnung. Sie werden bei jedem Lauf
    erneut gemeldet, solange sie im Eingang liegen -- bis jemand sie löscht."""
    schon_im_archiv: list[str] = field(default_factory=list)
    """Im Eingang, aber dieselbe Rechnung liegt schon im Archiv -- zum Löschen."""
    fort: int = 0
    """Warteten bis zu diesem Lauf im Eingang, und die Datei ist in OneDrive nicht mehr da."""
    von_hand_abgelegt: int = 0
    """Warteten im Eingang und liegen jetzt im Archiv, ohne dass TaskPilot sie bewegt hat."""
    nicht_lesbar: list[str] = field(default_factory=list)
    """Liegen im Eingang, aber die Extraktion konnte sie nicht lesen."""
    nicht_auswertbar: dict[str, int] = field(default_factory=dict)
    """Nur gegen eine Schnittstelle ohne Einzelliste: Zählung über das ganze Modul."""
    hinweise: list[str] = field(default_factory=list)


def _im_eingang(ordner: str | None, eingang: str) -> bool:
    """Ob der Beleg im Eingang liegt -- und nicht schon im Archiv.

    Gelesen wird ``beleg_ordner``, der Ablageort unter der Archivwurzel, und
    **nicht** ``beleg_datei``. Bis zum 22.09.2026 stand hier der Dateiname, und
    darin ist nie ein Ordner: von 1197 Belegen galt keiner als wartend, obwohl 17
    im Eingang lagen. Der Eingang war nicht leer, er war unerreichbar -- und weil
    eine leere Liste aussieht wie «nichts zu tun», fiel es nicht als Fehler auf.

    Verglichen wird auf Gliedgrenzen: ``_OPEN/InnoSmith`` trifft sich selbst und
    ``_OPEN/InnoSmith/autodownload``, aber nicht einen Ordner ``_OPEN/InnoSmithXY``.
    """
    if not ordner:
        return False
    rein = ordner.replace("\\", "/").strip("/")
    ziel = eingang.replace("\\", "/").strip("/")
    return rein == ziel or rein.startswith(f"{ziel}/")


def _quelle_von(ordner: str) -> str:
    """Woher der Beleg kam. Der Unterordner sagt es, nichts sonst.

    ``autodownload`` ist das Skript auf dem Mac, alles andere hat ein Mensch
    dorthin gelegt. Der Unterschied trägt: bei einem Autodownload ist ein
    zweiter Bezug derselben Rechnung der Normalfall, bei einer Handablage ein
    Hinweis.

    Geprüft wird das **letzte Glied** des Ablageorts. Die frühere Fassung suchte
    ``/autodownload/`` in einem Pfad mit Dateinamen; im Ablageort steht der Ordner
    am Ende und ohne abschliessenden Schrägstrich, und die Suche fand nie etwas.
    """
    glieder = [t for t in ordner.replace("\\", "/").split("/") if t]
    return "autodownload" if glieder and glieder[-1] == "autodownload" else "ablage_hand"


def _als_datum(wert: Any) -> date | None:
    if isinstance(wert, date):
        return wert
    if not wert:
        return None
    try:
        return date.fromisoformat(str(wert)[:10])
    except ValueError:
        return None


def gewaehlter_schluessel(
    modul: str | None, register: str | None, bestand: decl.Bestand
) -> str | None:
    """Wessen Lieferant gilt: der des Moduls, ausser ein Mensch musste wählen.

    Die Wahl am Registerbeleg gilt nur dort, wo das Modul keine Antwort hat --
    kein Schlüssel oder ein Absender wie «Google», der mehrere meint. Sonst
    gewinnt das Modul, denn dort entscheidet der Ablageordner.
    """
    gewaehlt = bestand.lieferanten.get(register or "")
    if gewaehlt is None or gewaehlt.aufteilen or register == modul:
        return modul
    vom_modul = bestand.lieferanten.get(modul or "")
    if not modul or (vom_modul is not None and register in vom_modul.aufteilen):
        return register
    return modul


def vorschlagen(
    zeile: dict[str, Any], bestand: decl.Bestand, *, gewaehlt: str | None = None
) -> Vorschlag:
    """Baut den Buchungsvorschlag aus Abzug und Deklaration.

    Die Reihenfolge ist die Aussage: erst der Schlüssel, dann die Deklaration,
    dann der Vergleich. Ohne Schlüssel gibt es keinen Vorschlag -- geraten wird
    nicht, gefragt schon. ``gewaehlt`` ist der Lieferant am Registerbeleg; wann
    er zählt, entscheidet ``gewaehlter_schluessel``.
    """
    schluessel = gewaehlter_schluessel(zeile.get("lieferant_schluessel"), gewaehlt, bestand)
    grund = str(zeile.get("lieferant_grund") or "unbekannt")
    v = Vorschlag(lieferant_schluessel=schluessel)
    # Auch nach der Wahl: wer sich vergriffen hat, wählt bis zur Freigabe um.
    absender = bestand.lieferanten.get(zeile.get("lieferant_schluessel") or "")
    if absender is not None and absender.aufteilen:
        v.lieferant_kandidaten = absender.aufteilen

    if not schluessel:
        wer = zeile.get("lieferant") or zeile.get("lieferant_original") or "unbekannt"
        if grund == "nur_zahlungsanbieter":
            v.abweichungen.append(
                f"Auf dem Beleg steht «{wer}» — das ist der Zahlungsabwickler, "
                f"nicht der Lieferant. Wer ist der Kreditor?"
            )
        elif grund == "mehrdeutig":
            v.abweichungen.append(
                f"«{wer}» führt auf mehrere Lieferanten — welcher ist gemeint?"
            )
        else:
            v.abweichungen.append(f"«{wer}» ist kein bekannter Lieferant.")
        return v

    lieferant = bestand.lieferanten.get(schluessel)
    if lieferant is None:
        v.abweichungen.append(
            f"Lieferant «{schluessel}» ist im Modul bekannt, in der Deklaration "
            f"aber nicht — ohne Eintrag gibt es keine Kontoerwartung."
        )
        return v
    if lieferant.aufteilen:
        # Nur hier, wo der Verteiler selbst gilt -- bei einer Rechnung ohne
        # Modulschlüssel, deren Registerbeleg ihn trägt.
        v.lieferant_kandidaten = lieferant.aufteilen
        namen = ", ".join(bestand.lieferanten[s].anzeigename for s in lieferant.aufteilen if s in bestand.lieferanten)
        v.abweichungen.append(
            f"Auf der Rechnung steht «{zeile.get('lieferant') or lieferant.anzeigename}» — "
            f"dahinter stehen mehrere Dienste ({namen}). Welcher ist es?"
        )
        return v

    v.lieferant_bestaetigt = lieferant.bestaetigt
    v.anzeigename = lieferant.anzeigename
    v.leistung = lieferant.leistung
    if not lieferant.leistung:
        v.abweichungen.append(
            f"Für «{lieferant.anzeigename}» hängt die Leistung an der Rechnung — "
            f"sie steht in Buchungstext und Dateiname und ist hier zu erfassen."
        )
    if not lieferant.bestaetigt:
        v.abweichungen.append(
            f"Die Erwartung zu «{schluessel}» ist aus der Historie vorgeschlagen "
            f"und von niemandem geprüft."
        )

    if lieferant.sollkonto:
        v.sollkonto = lieferant.sollkonto
        v.sollkonto_herkunft = "vorschlag"
    elif lieferant.sollkonto_kandidaten:
        # Bewusst kein Vorschlag. Bei Hosttech ist dieselbe Domain einmal
        # eigener Aufwand und einmal weiterverrechnete Leistung -- ein Wert
        # wäre hier nicht unsicher, sondern falsch benannt.
        v.sollkonto_kandidaten = lieferant.sollkonto_kandidaten
        v.abweichungen.append(
            f"Das Konto hängt bei «{schluessel}» an der Rechnung, nicht am "
            f"Lieferanten: {' oder '.join(lieferant.sollkonto_kandidaten)}."
        )
    else:
        v.abweichungen.append(
            f"Für «{schluessel}» ist kein Konto deklariert."
        )

    tag = _als_datum(zeile.get("datum")) or date.today()
    v.steuerbehandlung = lieferant.steuer_am(tag)

    # Die eine Abweichung, die Geld kostet, wenn sie durchrutscht: ein
    # Lieferant, der erstmals Schweizer MWST ausweist. Die Bezugssteuer wäre
    # dann doppelt gerechnet -- einmal als Vorsteuer, einmal gegen 2203.
    mwst = zeile.get("mwst")
    if v.steuerbehandlung == "bezugssteuer" and mwst not in (None, 0, 0.0):
        v.abweichungen.append(
            f"Erwartet war Bezugssteuer, der Beleg weist {mwst} MWST aus — "
            f"weist «{schluessel}» jetzt Schweizer MWST aus?"
        )

    # Zahlweg: nur wenn die Deklaration eindeutig ist. Zwei erlaubte Wege sind
    # keine Auskunft, und aus dem Dokumenttyp abzuleiten war in sbKreditorenBot
    # die Ursache einer Doppelzahlung -- eine per Lastschrift eingezogene
    # Rechnung geriet in den Zahlungslauf.
    if len(lieferant.zahlweg) == 1:
        v.zahlweg = lieferant.zahlweg[0]
        art = str(zeile.get("zahlungsart") or "").upper()
        erwartet = {"karte": "KREDITKARTE", "rechnung": "RECHNUNG"}.get(v.zahlweg)
        if art and erwartet and art != erwartet:
            v.abweichungen.append(
                f"Erwartet war {v.zahlweg}, der Beleg nennt {art.lower()}."
            )

    return v


async def abgleichen(
    db: AsyncSession,
    *,
    basis_url: str,
    token: str,
    eingang: str = EINGANG_VORGABE,
    bestand: decl.Bestand | None = None,
) -> Befund:
    """Nimmt alles auf, was im Eingang liegt, und hängt den Vorschlag daran.

    Idempotent über den Hash: ein zweiter Lauf legt keine zweite Zeile an und
    überschreibt keine getroffene Entscheidung. Nur ein noch unentschiedener
    Vorschlag wird nachgeführt -- sonst verlore eine Korrektur ihre Wirkung,
    sobald der Abgleich das nächste Mal läuft.
    """
    zeilen, abzug = await rechnungen_holen(basis_url, token)
    deklaration = bestand if bestand is not None else decl.laden()
    # Belege, die es gibt und die nicht im Abzug stehen -- Extraktion
    # gescheitert oder noch nicht gelaufen. Ein Beleg, der im Eingang liegt und
    # nirgends erscheint, sieht aus wie keiner. Genannt werden aber nur die im
    # Eingang: am 25.09.2026 meldete die Modulzählung 12, und davon lag keiner
    # hier -- zehn Checklisten im Archiv und zwei Dateien, die es nicht mehr gab.
    befund = Befund()
    einzeln = abzug.get("nicht_auswertbar_einzeln")
    if einzeln is None:
        befund.nicht_auswertbar = dict(abzug.get("nicht_auswertbare_belege") or {})
    else:
        befund.nicht_lesbar = [
            str(b.get("beleg_datei") or b.get("beleg_id"))
            for b in einzeln
            if b.get("datei_vorhanden") and _im_eingang(b.get("beleg_ordner"), eingang)
        ]

    if abzug.get("unvollstaendig"):
        befund.hinweise.append(
            f"Der Abzug war unvollständig ({abzug['unvollstaendig']}) — "
            f"es kann etwas im Eingang liegen, das hier fehlt."
        )

    # Ohne Ablageort ist der Eingang nicht von der Ablage zu unterscheiden, und
    # das Ergebnis wäre eine leere Liste statt einer Meldung. Geprüft wird die
    # **Spalte**, nicht ihr Wert: ein einzelner leerer Ordner ist möglich (Beleg
    # in der Archivwurzel), eine fehlende Spalte ist ein Schnittstellenbruch.
    if zeilen and "beleg_ordner" not in zeilen[0]:
        raise RuntimeError(
            "Der Abzug führt keinen 'beleg_ordner' — damit lässt sich der Eingang "
            "nicht von der Ablage unterscheiden. Läuft eine ältere Fassung der "
            "InvoiceInsight-Schnittstelle?"
        )

    archiv = _archivbestand(zeilen, eingang)

    for zeile in zeilen:
        ordner = zeile.get("beleg_ordner")
        name = str(zeile.get("beleg_datei") or "")
        if not _im_eingang(ordner, eingang):
            continue

        datei_hash = zeile.get("sha256")
        if zeile.get("datei_vorhanden") is False:
            # Stand im Eingang, ist in OneDrive fort. Nicht aufnehmen -- und
            # wer schon wartete, verlässt die Liste mit Begründung. Gezählt
            # wird nur, wer in diesem Lauf geht: das Modul führt die Zeile
            # weiter, und sonst stünde derselbe Hinweis bei jedem Abgleich da,
            # ohne dass es etwas zu tun gäbe.
            if await _aus_der_liste(
                db, datei_hash,
                f"«{name}» ist in OneDrive nicht mehr vorhanden — gelöscht oder "
                f"in einen ausgeschlossenen Ordner verschoben. Nichts zu buchen.",
            ):
                befund.fort += 1
            continue
        befund.gesehen += 1

        if not datei_hash:
            befund.hinweise.append(
                f"«{name}» hat keinen Hash — ohne Identität nicht aufnehmbar."
            )
            continue

        vorher = await reg.nach_hash(db, datei_hash)
        v = vorschlagen(zeile, deklaration, gewaehlt=vorher.lieferant_schluessel if vorher else None)

        # Liegt dieselbe Rechnung schon im Archiv, ist das hier kein Eingang,
        # sondern ein zweiter Bezug. Ohne diesen Wächter stand die alte
        # OpenAI-Rechnung von 2024 als offen da, weil die Nummer nur im
        # Register verglichen wurde -- und dort war die archivierte nie.
        im_archiv = archiv.ort(
            v.lieferant_schluessel, zeile.get("rechnungsnummer"), zeile.get("datum"), datei_hash
        )
        if im_archiv:
            vermerk = (
                f"«{name}» liegt schon im Archiv ({im_archiv}) — ein zweiter "
                f"Bezug derselben Rechnung. Kann gelöscht werden."
            )
            befund.schon_im_archiv.append(vermerk)
            await _aus_der_liste(db, datei_hash, vermerk)
            continue

        aufnahme = await reg.aufnehmen(
            db,
            datei_hash=datei_hash,
            dateiname=name,
            quelle=_quelle_von(str(ordner)),
            graph_pfad=str(PurePosixPath(str(ordner)) / name),
            lieferant_schluessel=v.lieferant_schluessel,
            rechnungsnummer=zeile.get("rechnungsnummer"),
        )
        # Bei einer Kopie zeigt ``beleg`` auf das Original. Dort darf nichts
        # fortgeschrieben werden -- sonst zeigte das Original auf die Datei,
        # die gleich gelöscht wird.
        if aufnahme.abgewiesen:
            befund.dubletten += 1
            befund.hinweise.append(aufnahme.vermerk)
            continue

        if not v.lieferant_schluessel:
            befund.ohne_lieferant += 1
        if not v.sollkonto:
            befund.ohne_konto += 1
        beleg = aufnahme.beleg
        beleg.modul_dokument_id = zeile.get("beleg_id")

        if aufnahme.neu:
            befund.neu += 1
        else:
            befund.bekannt += 1

        _vorschlag_anlegen(beleg, v)

    if not abzug.get("unvollstaendig"):
        await _von_hand_abgelegte(db, zeilen, eingang, befund)

    logger.info(
        "Kreditoreneingang: %d im Eingang, %d neu, %d bekannt, "
        "%d ohne Lieferant, %d ohne Konto, %d Doppel, %d schon im Archiv, "
        "%d fort, %d von Hand abgelegt",
        befund.gesehen, befund.neu, befund.bekannt,
        befund.ohne_lieferant, befund.ohne_konto, befund.dubletten,
        len(befund.schon_im_archiv), befund.fort, befund.von_hand_abgelegt,
    )
    return befund


def _nummer_rein(wert: Any) -> str | None:
    rein = str(wert or "").strip()
    return rein or None


@dataclass
class _Archiv:
    """Was im Archiv liegt: je Rechnungsnummer die Fundstellen, je Hash der Ort."""

    nach_nummer: dict[str, list[tuple[str | None, str, str]]] = field(default_factory=dict)
    """Nummer -> (Lieferant, Rechnungsdatum, Ort)."""
    nach_hash: dict[str, str] = field(default_factory=dict)

    def ort(
        self, schluessel: str | None, nummer: Any, datum: Any, datei_hash: str | None
    ) -> str | None:
        """Dieselbe Nummer genügt nicht -- zwei Lieferanten dürfen sie beide führen.

        Dazu muss der Lieferant stimmen **oder** das Rechnungsdatum. Das zweite
        braucht es, weil im Eingang der Absender zuordnet und im Archiv der
        Ordner: die Workspace-Rechnung vom 30.11.2025 heisst im Autodownload
        «google» und unter Google Workspace «google_workspace».
        """
        rein, tag = _nummer_rein(nummer), str(datum or "")[:10]
        for wer, wann, ort in self.nach_nummer.get(rein or "", []):
            if (schluessel and wer == schluessel) or (tag and wann == tag):
                return ort
        return self.nach_hash.get(datei_hash or "")


def _archivbestand(zeilen: list[dict[str, Any]], eingang: str) -> _Archiv:
    """Nur, was noch da ist -- eine gelöschte Archivdatei macht einen Eingang
    nicht zum Doppel. Ohne Nummer ist die Frage nach der Rechnung nicht
    beantwortbar; dann bleibt nur die nach denselben Bytes."""
    archiv = _Archiv()
    for z in zeilen:
        ordner = z.get("beleg_ordner")
        if not ordner or _im_eingang(ordner, eingang) or z.get("datei_vorhanden") is False:
            continue
        ort = f"{ordner}/{z.get('beleg_datei') or ''}"
        if z.get("sha256"):
            archiv.nach_hash.setdefault(z["sha256"], ort)
        nummer = _nummer_rein(z.get("rechnungsnummer"))
        if nummer:
            archiv.nach_nummer.setdefault(nummer, []).append(
                (z.get("lieferant_schluessel"), str(z.get("datum") or "")[:10], ort)
            )
    return archiv


async def _aus_der_liste(db: AsyncSession, datei_hash: str | None, grund: str) -> bool:
    """Nimmt einen wartenden Beleg mit Begründung aus der Liste -- gelöscht wird nichts.

    Nur, was noch nicht freigegeben ist. Ein freigegebener Beleg ist eine
    Entscheidung, und die hebt kein Abgleich auf. Gibt zurück, ob ein Beleg
    die Liste tatsächlich verlassen hat.
    """
    if not datei_hash:
        return False
    beleg = await reg.nach_hash(db, datei_hash)
    if beleg is None or beleg.freigegeben_am is not None or beleg.zurueckgestellt:
        return False
    beleg.zurueckgestellt = True
    beleg.grund = grund
    logger.info("Kreditoreneingang: aus der Liste -- %s", grund)
    return True


async def _von_hand_abgelegte(
    db: AsyncSession, zeilen: list[dict[str, Any]], eingang: str, befund: Befund
) -> None:
    """Wartende Belege, deren Datei inzwischen im Archiv liegt.

    Das geschieht, wenn eine Rechnung noch auf dem alten Weg gebucht und von
    Hand abgelegt wurde. Ohne diesen Schritt bliebe sie für immer in der
    Warteliste -- als offene Rechnung, die längst bezahlt ist.
    """
    ort = {
        z["beleg_id"]: z for z in zeilen
        if z.get("beleg_id") is not None and z.get("datei_vorhanden") is not False
    }
    for beleg in await reg.offene(db):
        z = ort.get(beleg.modul_dokument_id)
        if z is None or _im_eingang(z.get("beleg_ordner"), eingang):
            continue
        beleg.zurueckgestellt = True
        beleg.grund = (
            f"Von Hand ins Archiv gelegt ({z.get('beleg_ordner')}/{z.get('beleg_datei')}) "
            f"— nicht über TaskPilot gebucht."
        )
        befund.von_hand_abgelegt += 1


def _vorschlag_anlegen(beleg: Kreditorenbeleg, v: Vorschlag) -> None:
    """Schreibt den Vorschlag an den Beleg -- aber nie über eine Entscheidung.

    Der Unterschied hängt an ``sollkonto_herkunft``: ``entscheid`` hat ein
    Mensch gesetzt und bleibt stehen. Ohne diese Unterscheidung machte der
    nächste Abgleich jede Korrektur rückgängig, und zwar ohne Meldung.
    """
    if beleg.sollkonto_herkunft == "entscheid":
        return
    if v.sollkonto:
        beleg.sollkonto = v.sollkonto
        beleg.sollkonto_herkunft = "vorschlag"
    if v.steuerbehandlung:
        beleg.steuerbehandlung = v.steuerbehandlung
    if v.zahlweg:
        beleg.zahlweg = v.zahlweg
    if v.lieferant_schluessel and not beleg.lieferant_schluessel:
        beleg.lieferant_schluessel = v.lieferant_schluessel
