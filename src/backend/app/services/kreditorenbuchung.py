"""Normprüfung und Buchung eines Kreditorenbelegs in Bexio.

## Warum vor jeder Buchung geprüft wird

Die Treuhänderin soll auf einen Blick sehen, dass Periode, Konto und Kurs der
Norm entsprechen. Gemessen am Rapid-API-Bestand 2026 (neun Buchungen) gab es
genau die Fehler, die eine Prüfung findet und ein Mensch übersieht:

* April und Juni als USD-Rechnung in **CHF zum Kurs 1.0** gebucht,
* die Juni-Rechnung am **03.07.** statt am 22.06. -- im falschen Monat,
* vier Schreibweisen desselben Buchungstexts.

Deshalb bucht TaskPilot nur, was die Prüfung besteht, und bei einem Verstoss
gar nicht. Ein Verstoss ist keine Warnung zum Wegklicken: er hält die Buchung
an, und die Maske sagt, warum.

## Das Buchungsmuster -- gemessen, nicht angenommen

Aus den bestehenden Buchungen gelesen (Rapid API 22.08.2026, Buchung 1388):
eine ``manual_single_entry``, Soll das Aufwandskonto, Haben 2120 bei Karte,
``tax_id`` BZB81 mit ``tax_account_id`` gleich dem Sollkonto. Die Gegenzeile
auf 2203 erzeugt Bexio selbst. Rechnungen mit Schweizer MWST (OpenAI,
Anthropic, Google, Microsoft) tragen **keinen** Steuercode: das Konto rechnet
mit Saldosteuersatz ab, ohne Vorsteuerabzug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.models.models import Kreditorenbeleg
from app.services import kreditorenlieferanten as decl
from app.services import kreditorennorm as norm
from app.services import kreditorenregister as reg
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

HABEN_KARTE = "2120"
KONTO_BEZUGSSTEUER = "2203"
CODE_BEZUGSSTEUER = "BZB81"


@dataclass(frozen=True)
class Buchungsplan:
    """Was nach Bexio geht -- vollständig, bevor irgendetwas geschickt wird."""

    datum: date
    sollkonto: str
    habenkonto: str
    betrag: Decimal
    waehrung: str
    kurs: Decimal
    steuercode: str | None
    text: str
    referenz: str
    dateiname: str

    @property
    def betrag_chf(self) -> Decimal:
        return (self.betrag * self.kurs).quantize(Decimal("0.01"))


@dataclass
class Pruefung:
    plan: Buchungsplan | None = None
    verstoesse: list[str] = field(default_factory=list)
    """Halten die Buchung an."""
    hinweise: list[str] = field(default_factory=list)
    """Für die Treuhänderin sichtbar, halten nichts an."""

    @property
    def buchbar(self) -> bool:
        return self.plan is not None and not self.verstoesse


@dataclass(frozen=True)
class Bexiostand:
    """Was aus Bexio für die Prüfung gebraucht wird, einmal gelesen."""

    konto_id: dict[str, int]
    steuer_id: dict[str, int]
    waehrung_id: dict[str, int]
    journal: list[dict[str, Any]]
    """Das Journal vom Vormonat bis zum Ende des Rechnungsmonats."""

    def konto_nr(self, kennung: int | None) -> str | None:
        return next((nr for nr, k in self.konto_id.items() if k == kennung), None)


def _datum(wert: Any) -> date | None:
    if isinstance(wert, date):
        return wert
    try:
        return date.fromisoformat(str(wert)[:10]) if wert else None
    except ValueError:
        return None


def _monat(tag: date) -> str:
    return f"{tag:%m.%Y}"


def pruefen(
    beleg: Kreditorenbeleg,
    zeile: dict[str, Any],
    lieferant: decl.Lieferant | None,
    bexio: Bexiostand,
    *,
    kurs: Decimal | None,
    kurs_fehler: str | None = None,
    heute: date | None = None,
) -> Pruefung:
    """Die Normprüfung. Rein: kein Netz, keine Datenbank -- darum testbar.

    Gesammelt wird **alles**, nicht beim ersten Fehler abgebrochen: wer drei
    Mängel hat, soll sie in einem Durchgang sehen und nicht in dreien.
    """
    p = Pruefung()
    v, h = p.verstoesse, p.hinweise
    heute = heute or date.today()

    # ── Zustand ──────────────────────────────────────────────────────
    if beleg.freigegeben_am is None:
        v.append("Der Beleg ist noch nicht freigegeben.")
    if beleg.gebucht_am is not None:
        v.append(f"Schon gebucht am {beleg.gebucht_am:%d.%m.%Y} ({beleg.bexio_referenz}).")
    if lieferant is None:
        v.append("Ohne Lieferant keine Buchung — erst zuordnen.")
        return p
    if lieferant.aufteilen:
        v.append(
            f"«{lieferant.anzeigename}» steht für mehrere Dienste — erst wählen, welcher es ist."
        )
        return p
    if lieferant.sammelt(zeile.get("abrechnungszyklus"), zeile.get("dokumenttyp")):
        v.append(
            f"{lieferant.anzeigename} wird über den Sammelbeleg des Monats gebucht — "
            f"diese Rechnung wird nur abgelegt."
        )
    if str(zeile.get("dokumenttyp") or "").upper() == "GUTSCHRIFT":
        v.append("Eine Gutschrift — über diesen Weg wird nur Aufwand gebucht, keine Minderung.")

    # ── Datum = Rechnungsdatum ───────────────────────────────────────
    tag = _datum(zeile.get("datum"))
    if tag is None:
        v.append("Das Rechnungsdatum ist nicht gelesen — gebucht wird auf das Rechnungsdatum.")
    elif tag > heute:
        v.append(f"Das Rechnungsdatum {tag:%d.%m.%Y} liegt in der Zukunft.")

    # ── Betrag und Währung ───────────────────────────────────────────
    try:
        betrag = Decimal(str(zeile.get("betrag")))
    except (ArithmeticError, ValueError):
        betrag = Decimal(0)
    if betrag <= 0:
        v.append("Kein Betrag gelesen — eine Buchung über null ist keine.")
    waehrung = str(zeile.get("waehrung") or "").upper()
    if waehrung not in bexio.waehrung_id:
        v.append(f"Die Währung «{waehrung or '–'}» kennt Bexio nicht.")
    if kurs is None:
        v.append(kurs_fehler or f"Kein BAZG-Monatsmittelkurs für {waehrung}.")

    # ── Zahlweg und Gegenkonto ───────────────────────────────────────
    if beleg.zahlweg != "karte":
        v.append(
            "Über diesen Weg wird nur gebucht, was mit der Karte bezahlt ist "
            f"(Haben {HABEN_KARTE}). Zahlweg hier: {beleg.zahlweg or 'unbekannt'}."
        )

    # ── Konto gemäss Deklaration ─────────────────────────────────────
    konto = beleg.sollkonto
    if not konto:
        v.append("Kein Sollkonto.")
    elif konto not in bexio.konto_id:
        v.append(f"Das Konto {konto} gibt es im Bexio-Kontenplan nicht.")
    elif beleg.sollkonto_herkunft == "vorschlag":
        erwartet = {lieferant.sollkonto, *lieferant.sollkonto_kandidaten} - {None}
        if konto not in erwartet:
            v.append(
                f"Das vorgeschlagene Konto {konto} passt nicht mehr zur Deklaration "
                f"({', '.join(sorted(erwartet)) or 'keines'}) — bitte neu entscheiden."
            )
    elif lieferant.sollkonto and konto != lieferant.sollkonto:
        h.append(f"Konto {konto} von Hand entschieden, üblich ist {lieferant.sollkonto}.")

    # ── Steuer ───────────────────────────────────────────────────────
    steuercode: str | None = None
    behandlung = beleg.steuerbehandlung
    if behandlung == "bezugssteuer":
        steuercode = CODE_BEZUGSSTEUER
        if steuercode not in bexio.steuer_id:
            v.append(f"Den Steuercode {steuercode} gibt es in Bexio nicht.")
        if zeile.get("mwst") not in (None, 0, 0.0):
            v.append(
                "Die Rechnung weist Schweizer MWST aus, deklariert ist Bezugssteuer — "
                "gebucht würde die Steuer doppelt."
            )
    elif behandlung not in ("inland_mwst", "ohne_mwst"):
        v.append("Die Steuerbehandlung ist unbekannt — ohne sie kein Steuercode.")

    # ── Buchungstext und Dateiname ───────────────────────────────────
    leistung = beleg.leistung or lieferant.leistung
    text = datei = None
    if tag is not None:
        try:
            text = norm.buchungstext(lieferant.anzeigename, leistung, tag)
            datei = norm.dateiname(lieferant.anzeigename, leistung, tag, beleg.zahlweg)
        except norm.NormVerletzt as fehler:
            v.append(str(fehler))

    if not beleg.rechnungsnummer:
        h.append("Keine Rechnungsnummer gelesen — die Referenz in Bexio bleibt leer.")

    # ── Journal: Doppel und Periode ──────────────────────────────────
    if tag is not None and konto in bexio.konto_id:
        _journal_pruefen(p, lieferant, bexio, tag, konto, betrag, waehrung, text)

    if not v and tag and konto and text and datei and kurs is not None:
        p.plan = Buchungsplan(
            datum=tag,
            sollkonto=konto,
            habenkonto=HABEN_KARTE,
            betrag=betrag,
            waehrung=waehrung,
            kurs=kurs,
            steuercode=steuercode,
            text=text,
            referenz=beleg.rechnungsnummer or "",
            dateiname=datei,
        )
    return p


def _journal_pruefen(
    p: Pruefung,
    lieferant: decl.Lieferant,
    bexio: Bexiostand,
    tag: date,
    konto: str,
    betrag: Decimal,
    waehrung: str,
    text: str | None,
) -> None:
    """Zwei Fragen an das Journal: gibt es diese Buchung schon, und gibt es in
    dieser Periode schon eine?

    Die Gegenzeile auf 2203 zählt nicht mit: sie gehört zur Buchung, die sie
    erzeugt hat, und würde jede Bezugssteuer-Buchung doppelt zählen.
    """
    soll_id = bexio.konto_id[konto]
    bezug_id = bexio.konto_id.get(KONTO_BEZUGSSTEUER)
    waehrung_id = bexio.waehrung_id.get(waehrung)
    hauptzeilen = [z for z in bexio.journal if z.get("credit_account_id") != bezug_id]

    for z in hauptzeilen:
        if (
            _datum(z.get("date")) == tag
            and z.get("debit_account_id") == soll_id
            and z.get("currency_id") == waehrung_id
            and Decimal(str(z.get("amount") or 0)) == betrag
        ):
            p.verstoesse.append(
                f"Schon im Journal: {tag:%d.%m.%Y}, {konto}, {betrag} {waehrung} "
                f"«{(z.get('description') or '').strip()}» (Zeile {z.get('id')})."
            )

    eigene = sorted(
        (z for z in hauptzeilen if lieferant.bucht_als(z.get("description"))),
        key=lambda z: str(z.get("date")),
    )
    im_monat = [z for z in eigene if (_datum(z.get("date")) or date.min).strftime("%m.%Y") == _monat(tag)]
    # Gleicher Betrag oder gleicher Text im selben Monat hält an -- so wäre die
    # Juni-Rechnung aufgefallen, die am 03.07. neben der Juli-Rechnung stand.
    # Eine andere Leistung desselben Lieferanten ist dagegen richtig: OpenAI
    # verrechnet Abo und API-Verbrauch im selben Monat.
    for z in im_monat:
        beschreibung = (z.get("description") or "").strip()
        wann = f"{_datum(z.get('date')):%d.%m.%Y}"
        gleich = Decimal(str(z.get("amount") or 0)) == betrag and z.get("currency_id") == waehrung_id
        if gleich or (text is not None and beschreibung == text):
            p.verstoesse.append(
                f"Im {_monat(tag)} ist {lieferant.anzeigename} schon gebucht: {wann} "
                f"«{beschreibung}» — eine Buchung je Periode."
            )
        else:
            p.hinweise.append(
                f"Im {_monat(tag)} steht für {lieferant.anzeigename} schon {wann} «{beschreibung}» "
                f"— eine andere Leistung?"
            )
    if lieferant.rhythmus == "monatlich":
        vormonat = tag.replace(day=1) - timedelta(days=1)
        if not any((_datum(z.get("date")) or date.min).strftime("%m.%Y") == _monat(vormonat) for z in eigene):
            p.hinweise.append(
                f"Im {_monat(vormonat)} ist für {lieferant.anzeigename} nichts gebucht — "
                f"eine Lücke in der Periode, oder die Rechnung liegt noch nicht vor."
            )


# ── Beschaffung ──────────────────────────────────────────────────────


async def bexio_lesen(bexio_client: Any, tag: date | None) -> Bexiostand:
    """Kontenplan, Steuercodes, Währungen und das Journal um den Rechnungsmonat.

    Live aus Bexio und nicht aus dem Datenraum: der Datenraum ist bis zu einem
    Tag alt, und eine Buchung der Treuhänderin von heute Morgen muss die
    Doppelprüfung sehen.
    """
    konten = await bexio_client.list_accounts(limit=2000)
    steuern = await bexio_client.list_taxes()
    waehrungen = await bexio_client.list_currencies()
    journal: list[dict] = []
    if tag is not None:
        von = (tag.replace(day=1) - timedelta(days=1)).replace(day=1)
        bis = (tag.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        journal = await bexio_client.get_journal(von.isoformat(), bis.isoformat())
    return Bexiostand(
        konto_id={str(k.get("account_no")): int(k["id"]) for k in konten if k.get("id") is not None},
        steuer_id={str(s.get("code")): int(s["id"]) for s in steuern if s.get("is_active", True)},
        waehrung_id={str(w.get("name")).upper(): int(w["id"]) for w in waehrungen},
        journal=journal,
    )


def nutzlast(plan: Buchungsplan, bexio: Bexiostand) -> dict[str, Any]:
    """Der Rumpf für ``POST /3.0/accounting/manual_entries`` -- im gemessenen Muster."""
    soll_id = bexio.konto_id[plan.sollkonto]
    zeile: dict[str, Any] = {
        "debit_account_id": soll_id,
        "credit_account_id": bexio.konto_id[plan.habenkonto],
        "description": plan.text,
        "amount": float(plan.betrag),
        "currency_id": bexio.waehrung_id[plan.waehrung],
        "currency_factor": float(plan.kurs),
    }
    if plan.steuercode:
        zeile["tax_id"] = bexio.steuer_id[plan.steuercode]
        zeile["tax_account_id"] = soll_id
    return {
        "type": "manual_single_entry",
        "date": plan.datum.isoformat(),
        "reference_nr": plan.referenz,
        "entries": [zeile],
    }


@dataclass
class Buchungsergebnis:
    buchung_id: int
    zeile_id: int | None
    beleg_angehaengt: bool
    journal_zeilen: int
    meldungen: list[str] = field(default_factory=list)


async def buchen(
    beleg: Kreditorenbeleg,
    plan: Buchungsplan,
    bexio: Bexiostand,
    bexio_client: Any,
    datei: bytes | None,
) -> Buchungsergebnis:
    """Bucht, hängt den Beleg an und zählt im Journal nach.

    Der Aufrufer hält die Zeile gesperrt und schreibt ``gebucht_am`` in
    derselben Transaktion. Scheitert das Anhängen, bleibt die Buchung stehen --
    sie ist richtig, nur der Beleg fehlt, und das steht in den Meldungen.
    """
    antwort = await bexio_client.create_manual_entry(nutzlast(plan, bexio))
    # Ab hier steht die Buchung in Bexio. Nichts darf mehr so scheitern, dass
    # das Register es nicht erfährt -- sonst bucht der nächste Klick ein zweites Mal.
    buchung_id = int(antwort["id"])
    beleg.gebucht_am = datetime.now(UTC)
    beleg.bexio_referenz = f"manual_entry:{buchung_id}"
    logger.info(
        "Kreditorenbeleg gebucht: %s → Bexio %s (%s %s zu %s, %s)",
        beleg.dateiname, buchung_id, plan.betrag, plan.waehrung, plan.kurs, plan.text,
    )
    zeilen_antwort = antwort.get("entries") or [{}]
    zeile_id = zeilen_antwort[0].get("id")

    ergebnis = Buchungsergebnis(buchung_id, zeile_id, False, 0)
    if zeile_id is None:
        ergebnis.meldungen.append("Gebucht, aber Bexio nannte keine Zeile — der Beleg hängt nicht an.")
    elif datei:
        try:
            await bexio_client.attach_manual_entry_file(buchung_id, zeile_id, plan.dateiname, datei)
            ergebnis.beleg_angehaengt = True
        except Exception as fehler:  # noqa: BLE001 -- die Buchung steht, gemeldet wird
            ergebnis.meldungen.append(f"Gebucht, aber der Beleg hängt nicht an: {fehler}")
    else:
        ergebnis.meldungen.append("Gebucht, aber die Belegdatei war nicht abrufbar.")

    # Nachzählen: bei Bezugssteuer zwei Zeilen (Betrag und 2203), sonst eine.
    erwartet = 2 if plan.steuercode else 1
    try:
        journal = await bexio_client.get_journal(plan.datum.isoformat(), plan.datum.isoformat())
    except Exception as fehler:  # noqa: BLE001 -- die Buchung steht, gemeldet wird
        ergebnis.meldungen.append(f"Gebucht, aber das Journal war zum Nachzählen nicht lesbar: {fehler}")
        return ergebnis
    zeilen = [z for z in journal if (z.get("description") or "").strip() == plan.text]
    ergebnis.journal_zeilen = len(zeilen)
    if len(zeilen) != erwartet:
        ergebnis.meldungen.append(
            f"Im Journal stehen {len(zeilen)} Zeilen «{plan.text}», erwartet {erwartet} — bitte in Bexio ansehen."
        )
    return ergebnis


# ── Ablage ───────────────────────────────────────────────────────────

ARCHIV_WURZEL = "Finanzen/Kreditoren"
"""Die Wurzel in OneDrive; ``graph_pfad`` und ``archiv_pfad`` sind relativ dazu."""


class AblageFehlt(RuntimeError):
    """Die Ablage ist nicht geschehen -- die Buchung bleibt davon unberührt."""


def ablageziel(lieferant: decl.Lieferant, dateiname: str, tag: date) -> str:
    """``RapidAPI/2026/RapidAPI Monatsabo 22.09.2026 KK.pdf`` -- Jahr des Rechnungsdatums."""
    return f"{lieferant.ordner}/{tag:%Y}/{dateiname}"


async def vor_der_buchung_pruefen(
    beleg: Kreditorenbeleg, groesse: int, ziel: str, graph: Any, *, wurzel: str = ARCHIV_WURZEL
) -> None:
    """Liegt die Rechnung noch dort, mit denselben Bytes -- und ist das Ziel frei?

    Gefragt wird OneDrive selbst, nicht der Spiegel des Moduls: der kann um
    einen Takt zurückliegen, und eine Buchung auf eine inzwischen gelöschte
    Rechnung ist nicht mehr zurückzunehmen, ohne dass die Treuhänderin es
    merkt. Scheitert die Prüfung, wird nicht gebucht -- sonst stünde eine
    Buchung da, deren Ablage absehbar scheitert.
    """
    if not beleg.graph_pfad:
        raise AblageFehlt("Im Register steht kein Ort der Datei. Nicht gebucht.")
    quelle = await graph.drive_item_by_path(f"{wurzel}/{beleg.graph_pfad}")
    if quelle is None:
        raise AblageFehlt(
            f"«{beleg.graph_pfad}» liegt in OneDrive nicht mehr im Eingang — "
            f"gelöscht oder verschoben? Nicht gebucht."
        )
    if quelle.get("size") is not None and int(quelle["size"]) != groesse:
        raise AblageFehlt(
            f"«{beleg.graph_pfad}» hat in OneDrive {int(quelle['size'])} Bytes, gelesen wurden "
            f"{groesse} — die Datei wurde ersetzt. Nicht gebucht, bitte neu abgleichen."
        )
    if await graph.drive_item_by_path(f"{wurzel}/{ziel}") is not None:
        raise AblageFehlt(f"Am Ziel liegt schon «{ziel}» — nicht gebucht, bitte ansehen.")


async def ablegen(
    db: AsyncSession, beleg: Kreditorenbeleg, ziel: str, graph: Any, *, wurzel: str = ARCHIV_WURZEL
) -> str:
    """Verschiebt die Rechnung aus dem Eingang ins Archiv, unter ihrem Normnamen.

    Erst **nach** der Buchung: Bexio hat den Beleg dann schon, und eine Ablage,
    die scheitert, lässt eine richtige Buchung zurück statt einer Datei im
    Archiv, zu der es keine Buchung gibt. Eine gleichnamige Datei am Ziel wird
    nicht ersetzt -- sie ist ein Befund.
    """
    if not beleg.graph_pfad:
        raise AblageFehlt("Im Register steht kein Ort der Datei.")
    quelle = await graph.drive_item_by_path(f"{wurzel}/{beleg.graph_pfad}")
    if quelle is None:
        raise AblageFehlt(f"«{beleg.graph_pfad}» liegt nicht mehr im Eingang — von Hand verschoben?")
    if await graph.drive_item_by_path(f"{wurzel}/{ziel}") is not None:
        raise AblageFehlt(f"Am Ziel liegt schon «{ziel}» — nichts überschrieben, bitte ansehen.")

    ordner_pfad, name = ziel.rsplit("/", 1)
    ordner = await graph.ensure_drive_folder(f"{wurzel}/{ordner_pfad}")
    neu = await graph.move_drive_item(quelle["id"], ordner["id"], name)

    await reg.ablage_vermerken(db, beleg, archiv_pfad=ziel, graph_item_id=neu.get("id") or quelle["id"])
    logger.info("Kreditorenbeleg abgelegt: %s → %s", beleg.dateiname, ziel)
    return ziel
