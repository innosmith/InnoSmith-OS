"""Der Monatssammelbeleg -- dreissig Rechnungen im Monat, eine Buchung.

Cursor rechnet die Nutzung in Schwellen ab: eine Rechnung, sobald rund 100 USD
aufgelaufen sind, im August 2026 fünfundvierzig. Gebucht wird nicht jede
einzeln, sondern ein Beleg je Monat, der sie zusammenfasst; die Rechnungen
selbst werden nur abgelegt. Bis zum 25.09.2026 erzeugte K4 in InnoSmithAdmin
diesen Beleg, seither TaskPilot.

## Ein Beleg je Kalendermonat, nicht je Cursor-Zyklus

K4 rechnete im Zyklus vom 26. bis zum 25., weil die Grundgebühr damals am
26. fällig wurde. Seit dem 26.04.2026 ist sie ein Jahresabo und wird einzeln
gebucht (``Lieferant.sammelt``); der Zyklus hat buchhalterisch keine Bedeutung
mehr. Der Kalendermonat hat dafür zwei: ein Monat hat **genau einen**
BAZG-Monatsmittelkurs, also trägt eine Buchung den richtigen Kurs ohne
Mischkurs -- und die Treuhänderin bucht seit April ohnehin «Usage 2026.MM».
Die Fenster der alten Belege waren dagegen Zufall, je nachdem wann K4 lief:
Juni 08.--18.06., Juli 19.06.--12.07., September 21.08.--20.09.

## Die Werte kommen aus dem Modul

Nicht aus einem eigenen Lesen der PDFs, wie K4 es tat, weil es InvoiceInsight
noch nicht gab. Gemessen am 25.09.2026 an den vier Sammelbelegen Juni bis
September, deren Übersicht K4 aus den PDFs las: 128 von 128 Zeilen stimmen in
Nummer, Datum und Betrag mit dem Modul überein. Eine zweite Leselogik prüfte
nichts, was nicht schon stimmt. Was ohne Lesen prüfbar ist, bleibt: die
Lücke in der fortlaufenden Nummer und die Rechnung, die schon im Archiv liegt.

## Gerechnet wird wie Bexio

USD-Summe mal Kurs, Bezugsteuer 8.1 % auf die USD-Summe, je **einmal**
gerundet -- so rechnet Bexio aus ``BZB81`` die Gegenzeile auf 2203. K4 rundete
jede Zeile einzeln, und im August stand darum 192.25 im Beleg und 192.24 in
Bexio. Die Zeilen zeigen deshalb nur den Betrag in USD.

## Der Nachtrag

Der September-Beleg der Treuhänderin reicht bis Rechnung 04CDDAC1-0202 vom
20.09.2026, gebucht am 25.09. als «Cursor, USA, Usage 2026.09». Was im
September danach kommt, ist ein Nachtrag. Erkannt wird das am Journal, nicht
an einem Datum im Code: steht der Monat schon gebucht, heisst der neue Beleg
«… Nachtrag», und steht auch der schon da, wird nicht gebucht.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from typing import Any
from uuid import UUID

from app.models.models import Kreditorenbeleg
from app.services import kreditorenbuchung as kb
from app.services import kreditorenlieferanten as decl
from app.services import kreditorennorm as norm
from app.services.leistungsrapport import SCHRIFT_FETT, SCHRIFT_NORMAL
from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

SATZ_BEZUGSSTEUER = Decimal("0.081")
"""Der Satz hinter ``BZB81``. Steht hier, weil der Beleg ihn ausweist; Bexio
rechnet mit seinem eigenen."""

_RAPPEN = Decimal("0.01")
_NUMMER = re.compile(r"(.*?)(\d+)")


def _r2(betrag: Decimal) -> Decimal:
    return betrag.quantize(_RAPPEN, rounding=ROUND_HALF_UP)


def _chf(betrag: Decimal) -> str:
    """``5'599.77`` -- die Schreibweise der bisherigen Belege."""
    return f"{betrag:,.2f}".replace(",", "'")


def monat_von(tag: date) -> date:
    return tag.replace(day=1)


def monatsletzter(monat: date) -> date:
    return (monat.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def monatsname(monat: date) -> str:
    return f"{norm.MONATE[monat.month - 1]} {monat:%Y}"


def _datum(wert: Any) -> date | None:
    return kb._datum(wert)


def _betrag(wert: Any) -> Decimal | None:
    if wert is None:
        return None
    try:
        return Decimal(str(wert))
    except ArithmeticError:
        return None


# ── Wer wartet, und in welchem Monat ─────────────────────────────────


def wartet(beleg: Kreditorenbeleg, zeile: dict[str, Any], lieferant: decl.Lieferant | None) -> bool:
    """Eine offene Nutzungsrechnung, die auf den Sammelbeleg ihres Monats wartet.

    Zurückgestellte bleiben in der Einzelliste -- sie tragen einen Grund, und
    der soll sichtbar bleiben. Ein freigegebener Beleg ist schon entschieden.
    """
    return (
        lieferant is not None
        and beleg.belegart == "rechnung"
        and not beleg.zurueckgestellt
        and beleg.freigegeben_am is None
        and beleg.sammelbeleg_id is None
        and _datum(zeile.get("datum")) is not None
        and lieferant.sammelt(zeile.get("abrechnungszyklus"), zeile.get("dokumenttyp"))
    )


def nach_monat(
    eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]],
) -> dict[date, list[tuple[Kreditorenbeleg, dict[str, Any]]]]:
    """Nach dem Monat des Rechnungsdatums, nicht des Eingangs."""
    gruppen: dict[date, list[tuple[Kreditorenbeleg, dict[str, Any]]]] = {}
    for beleg, zeile in eintraege:
        tag = _datum(zeile.get("datum"))
        if tag is not None:
            gruppen.setdefault(monat_von(tag), []).append((beleg, zeile))
    return dict(sorted(gruppen.items()))


# ── Die Prüfung ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    beleg_id: UUID
    modul_id: int | None
    dateiname: str
    nummer: str | None
    datum: date | None
    betrag: Decimal | None
    waehrung: str | None


@dataclass
class Sammelpruefung:
    lieferant: decl.Lieferant
    monat: date
    positionen: list[Position] = field(default_factory=list)
    kurs: Decimal | None = None
    nachtrag: bool = False
    luecken: list[str] = field(default_factory=list)
    vollstaendig: bool = False
    vollstaendig_grund: str = ""
    vollstaendig_bestaetigt: bool = False
    verstoesse: list[str] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    plan: kb.Buchungsplan | None = None
    ablageziel: str | None = None

    @property
    def bereit(self) -> bool:
        """Nichts verstösst -- ob der Monat vollständig ist, steht separat."""
        return self.plan is not None and not self.verstoesse

    @property
    def buchbar(self) -> bool:
        return self.bereit and (self.vollstaendig or self.vollstaendig_bestaetigt)

    @property
    def rechnungsnummer(self) -> str:
        """Der Wächter im Register: ein Beleg je Monat, ein Nachtrag je Monat."""
        return f"Sammelbeleg {self.monat:%Y.%m}" + (" Nachtrag" if self.nachtrag else "")

    @property
    def waehrung(self) -> str | None:
        arten = {p.waehrung for p in self.positionen}
        return next(iter(arten)) if len(arten) == 1 else None

    @property
    def betrag(self) -> Decimal:
        return sum((p.betrag or Decimal(0) for p in self.positionen), Decimal(0))

    @property
    def bezugsteuer(self) -> Decimal:
        return _r2(self.betrag * SATZ_BEZUGSSTEUER)

    @property
    def betrag_chf(self) -> Decimal | None:
        return _r2(self.betrag * self.kurs) if self.kurs is not None else None

    @property
    def bezugsteuer_chf(self) -> Decimal | None:
        return _r2(self.bezugsteuer * self.kurs) if self.kurs is not None else None


def _zerlegen(nummer: str | None) -> tuple[str, int, int] | None:
    """``04CDDAC1-0203`` → (``04CDDAC1-``, 203, 4). Ohne Ziffern am Ende keine Folge."""
    treffer = _NUMMER.fullmatch((nummer or "").strip())
    if not treffer:
        return None
    return treffer.group(1), int(treffer.group(2)), len(treffer.group(2))


def luecken(positionen: list[Position], bekannte: Iterable[str]) -> list[str]:
    """Nummern, die zwischen der letzten bekannten und der höchsten fehlen.

    ``bekannte`` sind alle Nummern des Lieferanten im Modul, ob im Eingang
    oder im Archiv. Was dort fehlt, wurde nie bezogen -- das ist die Lücke.
    Auf dem Beleg vermerkt, wie bei K4. Nur der eigene Monat zählt: eine
    übersprungene Nummer hält nie einen späteren an.
    """
    teile = [t for t in (_zerlegen(p.nummer) for p in positionen) if t]
    if not teile:
        return []
    praefix, _, breite = teile[0]
    eigene = [n for p, n, _ in teile if p == praefix]
    bekannt = {n for p, n, _ in (t for t in map(_zerlegen, bekannte) if t) if p == praefix}
    vorher = [n for n in bekannt if n < min(eigene)]
    beginn = max(vorher) + 1 if vorher else min(eigene)
    return [
        f"{praefix}{n:0{breite}d}"
        for n in range(beginn, max(eigene) + 1)
        if n not in bekannt and n not in eigene
    ]


def vollstaendigkeit(
    positionen: list[Position], bekannte: dict[str, date | None], letzter: date
) -> tuple[bool, str]:
    """Ob die Nummernfolge belegt, dass im Monat keine Rechnung mehr fehlt.

    Cursor nummeriert fortlaufend und datiert mit der Nummer steigend,
    gemessen am 25.09.2026 an 200 Rechnungen (es fehlen nur 0004 und 0007
    aus 2024). Ist die Folge im Monat lückenlos und die nächste Nummer
    schon bekannt und nach dem Monatsletzten datiert, kann keine mehr
    nachkommen. Sonst belegt es erst der Download nach Monatsende.
    """
    teile = [t for t in (_zerlegen(p.nummer) for p in positionen) if t]
    if not teile:
        return False, "Die Rechnungsnummern bilden keine Folge — die Vollständigkeit ist nicht belegbar."
    praefix, _, breite = teile[0]
    hoechste = max(n for p, n, _ in teile if p == praefix)
    folge = {
        t[1]: d for nummer, d in bekannte.items() if (t := _zerlegen(nummer)) and t[0] == praefix
    }

    def name(n: int) -> str:
        return f"{praefix}{n:0{breite}d}"

    fehlen = luecken(positionen, bekannte)
    if fehlen:
        return False, (
            f"Lücke in der Nummernfolge: {', '.join(fehlen)} — weder im Eingang noch im Archiv. "
            f"Nie heruntergeladen, oder gibt es die Nummer nicht?"
        )
    nachfolger = next((n for n in sorted(folge) if n > hoechste and (folge[n] or date.min) > letzter), None)
    if nachfolger is None:
        return False, (
            f"Nach {name(hoechste)} ist noch keine Rechnung aus dem Folgemonat bekannt — "
            f"ob im Monat noch eine fehlt, belegt erst der Download nach Monatsende."
        )
    dazwischen = [name(n) for n in range(hoechste + 1, nachfolger) if n not in folge]
    if dazwischen:
        return False, (
            f"Zwischen {name(hoechste)} und {name(nachfolger)} fehlt {', '.join(dazwischen)} — "
            f"sie könnte noch in diesen Monat gehören."
        )
    return True, (
        f"Lückenlos bis {name(hoechste)}; die nächste Rechnung {name(nachfolger)} "
        f"ist vom {folge[nachfolger]:%d.%m.%Y}."
    )


def positionen(eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]]) -> list[Position]:
    """Registerzeile und Modulzeile zu einer Zeile des Belegs, nach Datum und Nummer."""
    return sorted(
        (
            Position(
                beleg_id=b.id,
                modul_id=b.modul_dokument_id,
                dateiname=b.dateiname,
                nummer=(b.rechnungsnummer or z.get("rechnungsnummer") or None),
                datum=_datum(z.get("datum")),
                betrag=_betrag(z.get("betrag")),
                waehrung=str(z.get("waehrung") or "").upper() or None,
            )
            for b, z in eintraege
        ),
        key=lambda x: (x.datum or date.min, x.nummer or ""),
    )


def nachbauen(
    lieferant: decl.Lieferant,
    monat: date,
    eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]],
    *,
    kurs: Decimal,
    nachtrag: bool,
    bekannte: Iterable[str] | None = None,
) -> Sammelpruefung:
    """Den gebuchten Beleg für die nachgeholte Ablage noch einmal herstellen.

    Ohne Prüfung: gebucht ist er schon, und das Journal meldete ihn jetzt als
    gebucht -- die Prüfung hielte ihn für einen Nachtrag seiner selbst.
    """
    p = Sammelpruefung(
        lieferant=lieferant, monat=monat, kurs=kurs, nachtrag=nachtrag, positionen=positionen(eintraege)
    )
    p.luecken = luecken(p.positionen, bekannte or set())
    return p


def pruefen(
    lieferant: decl.Lieferant,
    monat: date,
    eintraege: list[tuple[Kreditorenbeleg, dict[str, Any]]],
    bexio: kb.Bexiostand,
    *,
    kurs: Decimal | None,
    kurs_fehler: str | None = None,
    im_archiv: dict[str, str] | None = None,
    bekannte: dict[str, date | None] | None = None,
    heute: date | None = None,
    vollstaendig_bestaetigt: bool = False,
) -> Sammelpruefung:
    """Die Normprüfung des Monatsbelegs. Rein: kein Netz, keine Datenbank.

    ``im_archiv`` führt je Rechnungsnummer den Archivort, wo das Modul sie
    ausserhalb des Eingangs kennt. Liegt eine wartende Rechnung dort schon,
    ist sie sehr wahrscheinlich gebucht -- am 25.09.2026 stand 04CDDAC1-0047
    vom 01.05. als Kopie im Autodownload und offen im Register.

    ``bekannte`` führt jede Nummer des Lieferanten im Modul mit ihrem Datum,
    im Eingang wie im Archiv: daran misst sich die Vollständigkeit. Wo sie
    nicht belegt ist, bucht erst ``vollstaendig_bestaetigt``.
    """
    heute = heute or date.today()
    letzter = monatsletzter(monat)
    p = Sammelpruefung(
        lieferant=lieferant, monat=monat, kurs=kurs, positionen=positionen(eintraege),
        vollstaendig_bestaetigt=vollstaendig_bestaetigt,
    )
    v, h = p.verstoesse, p.hinweise

    # ── Zeitpunkt ────────────────────────────────────────────────────
    if heute <= letzter:
        v.append(
            f"Der {monatsname(monat)} läuft noch — gebucht wird nach dem Monatsende, "
            f"wenn alle Rechnungen da sind und das BAZG den Monatsmittelkurs nennt."
        )
    if not p.positionen:
        v.append(f"Für {monatsname(monat)} wartet keine Rechnung.")
        return p

    # ── Die Rechnungen ───────────────────────────────────────────────
    for pos in p.positionen:
        if not pos.nummer:
            v.append(f"«{pos.dateiname}»: keine Rechnungsnummer gelesen.")
        if pos.betrag is None or pos.betrag <= 0:
            v.append(f"«{pos.dateiname}»: kein Betrag gelesen.")
        if pos.datum and monat_von(pos.datum) != monat:
            v.append(f"«{pos.dateiname}» ist vom {pos.datum:%d.%m.%Y} und gehört nicht in den {monatsname(monat)}.")
    nummern = [pos.nummer for pos in p.positionen if pos.nummer]
    doppelt = sorted({n for n in nummern if nummern.count(n) > 1})
    if doppelt:
        v.append(f"Doppelt im Beleg: {', '.join(doppelt)}.")
    for nummer in nummern:
        if im_archiv and nummer in im_archiv:
            v.append(
                f"Rechnung {nummer} liegt schon im Archiv ({im_archiv[nummer]}) — "
                f"wohl schon gebucht. Die Kopie im Eingang kann weg."
            )
    haeufigkeit = Counter(pos.waehrung or "–" for pos in p.positionen)
    if len(haeufigkeit) > 1:
        ueblich = haeufigkeit.most_common(1)[0][0]
        for pos in p.positionen:
            if (pos.waehrung or "–") != ueblich:
                v.append(
                    f"«{pos.dateiname}» lautet auf {pos.waehrung or 'keine Währung'}, "
                    f"der Monat auf {ueblich} — gehört diese Datei in den Sammelbeleg?"
                )
    elif p.waehrung not in bexio.waehrung_id:
        v.append(f"Die Währung «{p.waehrung or '–'}» kennt Bexio nicht.")
    if kurs is None and heute > letzter:
        v.append(kurs_fehler or f"Kein BAZG-Monatsmittelkurs für {p.waehrung} im {monatsname(monat)}.")

    p.luecken = luecken(p.positionen, bekannte or {})
    p.vollstaendig, p.vollstaendig_grund = vollstaendigkeit(p.positionen, bekannte or {}, letzter)
    if not p.vollstaendig and vollstaendig_bestaetigt:
        h.append(f"Vollständigkeit von Hand bestätigt: {p.vollstaendig_grund}")

    # ── Konto, Zahlweg, Steuer aus der Deklaration ───────────────────
    konto = lieferant.sollkonto
    if not konto:
        v.append(f"Für {lieferant.anzeigename} ist kein Sollkonto deklariert.")
    elif konto not in bexio.konto_id:
        v.append(f"Das Konto {konto} gibt es im Bexio-Kontenplan nicht.")
    if "karte" not in lieferant.zahlweg:
        v.append(f"Über diesen Weg wird nur gebucht, was mit der Karte bezahlt ist (Haben {kb.HABEN_KARTE}).")
    steuercode: str | None = None
    behandlung = lieferant.steuer_am(letzter)
    if behandlung == "bezugssteuer":
        steuercode = kb.CODE_BEZUGSSTEUER
        if steuercode not in bexio.steuer_id:
            v.append(f"Den Steuercode {steuercode} gibt es in Bexio nicht.")
    elif behandlung not in ("inland_mwst", "ohne_mwst"):
        v.append("Die Steuerbehandlung ist unbekannt — ohne sie kein Steuercode.")

    # ── Text, Nachtrag, Journal ──────────────────────────────────────
    text = datei = None
    try:
        regulaer = norm.sammeltext(lieferant.anzeigename, lieferant.leistung, monat)
        bezug_id = bexio.konto_id.get(kb.KONTO_BEZUGSSTEUER)
        im_monat = [
            z for z in bexio.journal
            if z.get("credit_account_id") != bezug_id
            and monat <= (_datum(z.get("date")) or date.min) <= letzter
        ]
        texte = {(z.get("description") or "").strip(): z for z in im_monat}
        # Das Journal führt keine Referenz, wohl aber den Betrag. Steht der
        # Monat mit genau diesem Betrag schon da, ist es diese Buchung -- etwa
        # wenn Bexio gebucht hat und das Register es nicht mehr erfuhr. Ohne
        # diese Sperre hielte der nächste Klick sie für einen Nachtrag.
        for t in (regulaer, norm.sammeltext(lieferant.anzeigename, lieferant.leistung, monat, nachtrag=True)):
            z = texte.get(t)
            if z and _betrag(z.get("amount")) == p.betrag:
                v.append(
                    f"«{t}» steht mit {_chf(p.betrag)} {p.waehrung or ''} schon im Journal "
                    f"({_datum(z.get('date')):%d.%m.%Y}) — dieser Beleg ist gebucht."
                )
        if regulaer in texte:
            p.nachtrag = True
            z = texte[regulaer]
            h.append(
                f"Der {monatsname(monat)} ist schon gebucht ({_datum(z.get('date')):%d.%m.%Y}, "
                f"«{regulaer}») — dieser Beleg ist der Nachtrag mit dem, was seither kam."
            )
        text = norm.sammeltext(lieferant.anzeigename, lieferant.leistung, monat, nachtrag=p.nachtrag)
        if p.nachtrag and text in texte:
            v.append(f"Auch «{text}» steht schon im Journal — ein zweiter Nachtrag wird nicht gebucht.")
        datei = norm.sammeldateiname(lieferant.anzeigename, monat, "karte", nachtrag=p.nachtrag)
    except norm.NormVerletzt as fehler:
        v.append(str(fehler))

    if datei:
        p.ablageziel = f"{lieferant.ordner}/{letzter:%Y}/Sammelbelege/{datei}"
    if not v and konto and text and datei and kurs is not None and p.waehrung:
        erste, letzte = nummern[0], nummern[-1]
        p.plan = kb.Buchungsplan(
            datum=letzter,
            sollkonto=konto,
            habenkonto=kb.HABEN_KARTE,
            betrag=p.betrag,
            waehrung=p.waehrung,
            kurs=kurs,
            steuercode=steuercode,
            text=text,
            referenz=erste if erste == letzte else f"{erste} bis {letzte}",
            dateiname=datei,
        )
    return p


# ── Das Dokument ─────────────────────────────────────────────────────


class _Seite(FPDF):
    def __init__(self) -> None:
        super().__init__()
        for pfad in (SCHRIFT_NORMAL, SCHRIFT_FETT):
            if not pfad.exists():
                raise FileNotFoundError(f"Schrift für den Sammelbeleg fehlt: {pfad}")
        self.add_font("Inter", "", str(SCHRIFT_NORMAL))
        self.add_font("Inter", "B", str(SCHRIFT_FETT))
        self.set_margins(left=15, top=15, right=15)
        self.set_auto_page_break(auto=True, margin=18)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Inter", "", 7)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4, f"Übersicht, Seite {self.page_no()} von {{nb}}", align="C")


def uebersicht(p: Sammelpruefung, *, aussteller: str | None = None, erstellt: datetime | None = None) -> bytes:
    """Die Übersichtsseite: jede Rechnung mit Datum, Nummer, Betrag -- und die Summen, wie gebucht."""
    if p.kurs is None or p.waehrung is None:
        raise ValueError("Ohne Kurs und einheitliche Währung gibt es keinen Beleg")
    w = p.waehrung
    erstellt = erstellt or datetime.now(UTC)
    daten = [pos.datum for pos in p.positionen if pos.datum]

    s = _Seite()
    s.alias_nb_pages()
    s.add_page()
    s.set_text_color(51, 51, 51)
    s.set_font("Inter", "B", 14)
    titel = f"{p.lieferant.anzeigename} – Sammelbeleg {monatsname(p.monat)}" + (" – Nachtrag" if p.nachtrag else "")
    s.cell(0, 7, titel, new_x="LMARGIN", new_y="NEXT")
    s.set_font("Inter", "", 8)
    s.set_text_color(90, 90, 90)
    kopf = f"Rechnungen vom {min(daten):%d.%m.%Y} bis {max(daten):%d.%m.%Y}  |  {len(p.positionen)} Rechnungen"
    s.cell(0, 4, kopf + (f"  |  {aussteller}" if aussteller else ""), new_x="LMARGIN", new_y="NEXT")
    s.cell(
        0, 4,
        f"Umrechnung zum Monatsmittelkurs des BAZG für {monatsname(p.monat)}: "
        f"1 {w} = {p.kurs} CHF (backend-rates.bazg.admin.ch)",
        new_x="LMARGIN", new_y="NEXT",
    )
    s.ln(4)

    spalten = ((30, "Datum", "L"), (60, "Rechnungsnummer", "L"), (35, f"Betrag {w}", "R"))
    s.set_fill_color(90, 90, 90)
    s.set_text_color(255, 255, 255)
    s.set_font("Inter", "B", 8)
    for breite, titel_, ausrichtung in spalten:
        s.cell(breite, 6, titel_, align=ausrichtung, fill=True)
    s.ln(6)
    s.set_text_color(51, 51, 51)
    s.set_font("Inter", "", 8)
    for i, pos in enumerate(p.positionen):
        s.set_fill_color(245, 245, 245)
        werte = (f"{pos.datum:%d.%m.%Y}" if pos.datum else "–", pos.nummer or "–", _chf(pos.betrag or Decimal(0)))
        for (breite, _, ausrichtung), wert in zip(spalten, werte, strict=True):
            s.cell(breite, 5, wert, align=ausrichtung, fill=i % 2 == 1)
        s.ln(5)

    s.ln(2)
    s.set_draw_color(51, 51, 51)
    s.set_line_width(0.3)
    s.line(15, s.get_y(), 15 + sum(b for b, _, _ in spalten), s.get_y())
    s.ln(2)
    summen = (
        (f"Total {len(p.positionen)} Rechnungen", f"{_chf(p.betrag)} {w}", True),
        (f"Bezugsteuer {SATZ_BEZUGSSTEUER * 100:.1f} % auf das Total", f"{_chf(p.bezugsteuer)} {w}", False),
        (f"Aufwand in CHF zum Kurs {p.kurs}", f"{_chf(p.betrag_chf)} CHF", True),
        (f"Bezugsteuer in CHF zum Kurs {p.kurs}", f"{_chf(p.bezugsteuer_chf)} CHF", False),
    )
    for text, wert, fett in summen:
        s.set_font("Inter", "B" if fett else "", 8)
        s.cell(90, 5, text)
        s.cell(35, 5, wert, align="R")
        s.ln(5)

    s.ln(5)
    s.set_font("Inter", "", 7.5)
    s.set_text_color(90, 90, 90)
    if p.nachtrag:
        s.multi_cell(
            0, 3.6,
            f"Nachtrag: der {monatsname(p.monat)} ist bereits gebucht. Dieser Beleg enthält "
            f"nur die Rechnungen, die danach eingegangen sind.",
        )
        s.ln(1)
    if p.luecken:
        s.set_font("Inter", "B", 7.5)
        s.cell(0, 4, "Hinweis: Lücke in der Nummernfolge", new_x="LMARGIN", new_y="NEXT")
        s.set_font("Inter", "", 7.5)
        s.multi_cell(0, 3.6, "Nicht vorhanden: " + ", ".join(p.luecken))
        s.ln(1)
    s.multi_cell(
        0, 3.6,
        f"Bezugsteuer {SATZ_BEZUGSSTEUER * 100:.1f} % auf Dienstleistungen aus dem Ausland "
        f"(Art. 45 MWSTG). Betrag und Steuer je einmal auf das Total gerundet, wie in der Buchung. "
        f"Erstellt am {erstellt:%d.%m.%Y} durch TaskPilot. Es folgt die erste Seite jeder Rechnung.",
    )
    return bytes(s.output())


def zusammenfuegen(deckblatt: bytes, rechnungen: list[bytes]) -> bytes:
    """Übersicht und dahinter die erste Seite jeder Rechnung.

    Nur die erste: die zweite führt bei Cursor bloss die Positionsliste weiter,
    und so hielt es auch K4.
    """
    schreiber = PdfWriter()
    for seite in PdfReader(BytesIO(deckblatt)).pages:
        schreiber.add_page(seite)
    for inhalt in rechnungen:
        seiten = PdfReader(BytesIO(inhalt)).pages
        if len(seiten):
            schreiber.add_page(seiten[0])
    puffer = BytesIO()
    schreiber.write(puffer)
    return puffer.getvalue()


def erzeugen(
    p: Sammelpruefung, dateien: dict[UUID, bytes], *, aussteller: str | None = None
) -> bytes:
    """Der ganze Beleg. Fehlt eine Rechnung, fehlt der Beleg -- nicht nur eine Seite."""
    fehlen = [pos.dateiname for pos in p.positionen if pos.beleg_id not in dateien]
    if fehlen:
        raise ValueError(f"Rechnungen fehlen für den Beleg: {', '.join(fehlen)}")
    return zusammenfuegen(
        uebersicht(p, aussteller=aussteller),
        [dateien[pos.beleg_id] for pos in p.positionen],
    )
