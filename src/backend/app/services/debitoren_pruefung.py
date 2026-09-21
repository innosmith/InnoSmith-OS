"""Die Rechnungsprüfung vor dem Versand — jede Regel nennt Feld, Zustand, Erwartung.

Ersetzt ``exportValidierungExcel.py`` und die Prüfung in ``D3_prepareInvoices``.
Beide zusammen hatten einen Konstruktionsfehler, der wichtiger ist als die
einzelnen Befunde: **das Ergebnis war eine Liste von Fehlertexten.** Wer wissen
wollte, ob die Mehrwertsteuer geprüft worden war, musste die Texte nach dem Wort
«MWST» durchsuchen. Daraus folgte dreierlei, alles gemessen am Altbestand:

* Ein einziger Übertragsfehler färbte im Excel **vier fremde Spalten** rot, weil
  die Einfärbung ``any('Stunden' in f or 'Übertrag' in f …)`` fragte und das
  Ergebnis auf die Spalten 4 bis 8 anwendete.
* Eine **Warnung färbte gar nichts**. Ein fehlender Leistungsrapport stand als
  Warnung in der Liste, und die Zeile blieb durchgehend grün.
* **Grün und «nie hingeschaut» sahen gleich aus.** Konnte die IBAN nicht gelesen
  werden, fehlte der Fehler — und damit war die Zelle grün.

Deshalb liefert hier jede Regel einen ``Befund`` mit Feld, Zustand, Erwartung und
Istwert. Nichts wird aus Freitext zurückgeschlossen, und «nicht geprüft» ist ein
eigener Zustand statt der Abwesenheit eines Fehlers.

## Vier Zustände, nicht drei

``RICHTIG`` und ``FALSCH`` sind die geprüften Fälle. Die beiden übrigen trennen,
was im Excel zusammenfiel:

``OFFEN``
    Die Regel gilt, liess sich aber nicht anwenden — die IBAN war nicht lesbar,
    der Leistungsrapport fehlte, die Übertragslesart ist nicht bestätigt. Das
    braucht Aufmerksamkeit.
``ENTFAELLT``
    Die Regel gilt für diese Rechnung nicht — ein Festpreis hat keine
    Stundenprüfung, ein Kunde ohne Referenzpflicht keine Referenz. Das ist in
    Ordnung und darf ruhig aussehen.

Beides ist nicht grün. Der Unterschied ist, ob jemand hinsehen muss.

## Warum die Stundenregeln eine bestätigte Lesart brauchen

``admin_core`` und ``D2_updateDraftRechnungen`` rechneten den Übertrag
verschieden (siehe ``debitoren_uebertrag``). Betroffen war nicht nur der Übertrag
selbst, sondern auch der erwartete Rechnungsbetrag: unterhalb der vereinbarten
Stunden stellt die eine Fassung die Fixstunden in Rechnung, die andere die
geleisteten abzüglich Übertrag. Beide Regeln hängen an derselben Frage.

Dass das nie aufflog, liegt an der Bauweise der alten Prüfung, und das ist der
eigentliche Befund: bei Vertragsart A stufte ``validate_invoice`` jede
Stundendifferenz zu einem *Detail* herab statt zu einem Fehler, und bei
Vertragsart B verglich sie ``fix + zusatz`` mit ``fix + zusatz`` — dieselbe Zahl
auf beiden Seiten. Die Regel, die den Widerspruch hätte finden müssen, konnte für
drei von vier Vertragsarten gar nicht auslösen.

Seit dem 20.09.2026 ist die Lesart bestätigt (``debitoren_uebertrag.BESTAETIGT``).
Der ``Massstab`` verlangt sie trotzdem als Angabe: ein Lauf ohne gesetzte Lesart
meldet die beiden Stundenregeln als ``OFFEN`` statt sie stillschweigend nach
irgendeiner Fassung zu rechnen. Eine Prüfung, die im Zweifel selbst entscheidet,
prüft nicht mehr.

## Was die Rechnung liefert

Die Prüfung liest **keine PDFs**. Ihre Eingabe sind Zahlen aus der Bexio-API und
aus dem Vertragsstammdatum. Der alte Weg — Text aus dem erzeugten PDF mit
regulären Ausdrücken zurücklesen — war die Ursache mehrerer stiller Fehler,
darunter der wichtigste: ``verrechnet_tatsaechlich`` wurde als ``fix + zusatz``
*rekonstruiert* statt gelesen, womit der Vergleich gegen genau diese Summe
tautologisch wurde. Hier ist ``Rechnung.verrechnete_stunden`` die tatsächliche
Summe der Stundenpositionen, und der Vergleich hat wieder einen Inhalt.

## Zuordnung zu den neun alten Regeln

===========================  ==========================  ==============================
alt                          neu                         Änderung
===========================  ==========================  ==============================
1 Stunden-Vergleich          ``stunden``                 nicht mehr tautologisch
2 Übertrag-Berechnung        ``uebertrag``               Lesart muss bestätigt sein
3 MWST                       ``mwst``                    unverändert
4 IBAN-Bank                  ``iban``                    unverändert
5 Referenz                   ``referenz``                Pflicht am Vertrag deklariert
6 Rechnungsdatum             ``rechnungsdatum``          letzter Tag des Leistungsmonats
7 Zusatzstunden ohne Rapport ``leistungsrapport``        unverändert
8 TYP-Erkennung              ``vertragsart``             aus dem Vertrag statt aus dem Text
9 Unicode-Normalisierung     —                           entfällt
—                            ``fixstunden``              neu: Vertrag gegen Rechnung
===========================  ==========================  ==============================

Regel 9 entfällt, weil sie kein fachliches Kriterium war, sondern ein Pflaster:
macOS legt Dateinamen in NFD ab, und der Abgleich von Projektnamen über
Dateinamen scheiterte daran. Mit der Zuordnung über Kennungen statt über Namen
gibt es nichts mehr zu normalisieren.

``fixstunden`` kommt hinzu, weil die vereinbarten Stunden künftig ein deklariertes
Stammdatum sind und nicht mehr per ``(\\d+)h\\s*(?:/monat\\s*)?fix`` aus einem
Positionstext gelesen werden. Der Text wird weiterhin gelesen — aber nur noch zum
Abgleich, und eine Abweichung ist ein Befund statt einer stillen Übernahme.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from app.services import debitoren_uebertrag as ue
from app.services.debitoren_uebertrag import Formel


class Zustand(str, Enum):
    """Wie eine Regel für eine Rechnung ausgegangen ist."""

    RICHTIG = "richtig"
    FALSCH = "falsch"
    OFFEN = "offen"
    ENTFAELLT = "entfaellt"


class Gewicht(str, Enum):
    """Was ein Befund für den Versand bedeutet."""

    FEHLER = "fehler"
    """Blockiert den Versand."""

    WARNUNG = "warnung"
    """Sichtbar, hält aber nicht auf."""

    HINWEIS = "hinweis"
    """Nur zur Kenntnis — kein Urteil."""


class Vertragsart(str, Enum):
    """Wie mit den Stunden eines Monats umgegangen wird."""

    FIX_UEBERTRAGBAR = "fix_uebertragbar"
    FIX_VERFALLEND = "fix_verfallend"
    VARIABEL = "variabel"
    FESTPREIS = "festpreis"


VERTRAGSART_TEXT: dict[Vertragsart, str] = {
    Vertragsart.FIX_UEBERTRAGBAR: "Fixstunden, übertragbar",
    Vertragsart.FIX_VERFALLEND: "Fixstunden, nicht übertragbar",
    Vertragsart.VARIABEL: "Variabel nach Aufwand",
    Vertragsart.FESTPREIS: "Festpreis ohne Stundenbezug",
}


@dataclass(frozen=True)
class Befund:
    """Das Ergebnis genau einer Regel für genau eine Rechnung."""

    regel: str
    titel: str
    feld: str
    """Welche Angabe der Rechnung beurteilt wurde — die Spalte der Ansicht."""
    zustand: Zustand
    gewicht: Gewicht
    begruendung: str
    erwartet: str | None = None
    ist: str | None = None

    @property
    def auffaellig(self) -> bool:
        """Ob dieser Befund Aufmerksamkeit verlangt."""
        return self.zustand in (Zustand.FALSCH, Zustand.OFFEN)


# ── Eingaben ─────────────────────────────────────────────

@dataclass(frozen=True)
class Vertrag:
    """Was vereinbart ist — deklariertes Stammdatum, nicht aus Text gelesen.

    ``fix_stunden`` stand bisher im Positionstext der Bexio-Rechnung und wurde
    per regulärem Ausdruck herausgelesen, in zwei Fassungen, die sich nicht
    einig waren: ``admin_core`` verlangte «Xh fix inklusive», ``D2`` begnügte
    sich mit «Xh/Monat fix», und beide akzeptierten nur ganze Zahlen. Ein
    Vertrag über 7.5 Stunden war damit nicht darstellbar.
    """

    projekt: str
    kunde: str
    mwst_satz: float
    """Der erwartete Satz in Prozent. Ohne Vorgabewert, damit kein Steuersatz
    im Code steht — er kommt aus der Konfiguration."""

    fix_stunden: float | None = None
    uebertragbar: bool = False
    referenz_pflicht: bool = False
    stunden_pruefen: bool = True
    leistungsrapport_erwartet: bool = True

    @property
    def art(self) -> Vertragsart:
        if self.fix_stunden is not None:
            return (
                Vertragsart.FIX_UEBERTRAGBAR if self.uebertragbar
                else Vertragsart.FIX_VERFALLEND
            )
        return Vertragsart.VARIABEL if self.stunden_pruefen else Vertragsart.FESTPREIS


@dataclass(frozen=True)
class Rechnung:
    """Was in der Rechnung steht — aus der Bexio-API, nicht aus dem PDF."""

    nummer: str
    datum: date | None = None
    mwst_satz: float | None = None
    iban: str | None = None
    referenz: str | None = None
    fix_stunden: float | None = None
    """Menge der Fixposition, zum Abgleich mit dem Vertrag."""
    zusatz_stunden: float | None = None
    uebertrag_angabe: float | None = None
    """Die Stundenzahl im Übertragstext der Rechnung."""
    verrechnete_stunden: float | None = None
    """Summe **aller** Stundenpositionen. Gelesen, nicht rekonstruiert."""


@dataclass(frozen=True)
class Periode:
    """Der Leistungsmonat und was in ihm tatsächlich geleistet wurde."""

    jahr: int
    monat: int
    geleistet: float | None = None
    uebertrag_vormonat: float | None = None
    dokumente_erzeugt: bool = False
    """Ob die PDF zu diesem Lauf schon existiert.

    Die Prüfung läuft zweimal: einmal vor dem Erzeugen der Dokumente und einmal
    danach. Zwei Regeln lesen aus der PDF — die Bankverbindung und der
    Leistungsrapport —, und vor dem Erzeugen können beide nichts finden, weil es
    nichts zu finden *gibt*. Ohne diese Unterscheidung trägt im ersten Durchgang
    jede Rechnung zwei Meldungen, die nichts bedeuten; man lernt, über sie
    hinwegzulesen, und übersieht danach die echte.

    Ein eigenes Feld und kein ``None`` am Rapport: die Tatsache gilt für den
    ganzen Lauf, nicht für eine Regel, und die IBAN hätte sie sonst ein zweites
    Mal ausdrücken müssen.
    """
    leistungsrapport_vorhanden: bool = False
    """Nur aussagekräftig, wenn ``dokumente_erzeugt``."""

    @property
    def letzter_tag(self) -> date:
        return date(self.jahr, self.monat, calendar.monthrange(self.jahr, self.monat)[1])


@dataclass(frozen=True)
class Massstab:
    """Was für alle Rechnungen eines Laufs gleich gilt."""

    iban_erwartet: str | None = None
    uebertragsformel: Formel | None = None
    """``None`` heisst: noch nicht entschieden. Siehe ``debitoren_uebertrag``."""
    toleranz_stunden: float = 0.1
    """Wie im Altbestand. Deckt Rundung auf Viertelstunden ab."""


@dataclass
class Ergebnis:
    """Alle Befunde zu einer Rechnung."""

    rechnung: str
    projekt: str
    kunde: str
    vertragsart: Vertragsart
    befunde: list[Befund] = field(default_factory=list)

    @property
    def gesamt(self) -> Gewicht | None:
        """Das schwerste auffällige Gewicht, oder ``None`` wenn alles sauber ist."""
        auffaellig = [b.gewicht for b in self.befunde if b.auffaellig]
        for stufe in (Gewicht.FEHLER, Gewicht.WARNUNG, Gewicht.HINWEIS):
            if stufe in auffaellig:
                return stufe
        return None

    @property
    def versandbereit(self) -> bool:
        return self.gesamt is not Gewicht.FEHLER


# ── Die Regeln ───────────────────────────────────────────

def _zahl(wert: float | None, einheit: str = "h") -> str | None:
    return None if wert is None else f"{wert:g}{einheit}"


def _vertragsart(vertrag: Vertrag) -> Befund:
    """Regel ``vertragsart`` — welche Abrechnungsform gilt (rein informativ)."""
    return Befund(
        regel="vertragsart",
        titel="Vertragsart",
        feld="vertragsart",
        zustand=Zustand.RICHTIG,
        gewicht=Gewicht.HINWEIS,
        begruendung=VERTRAGSART_TEXT[vertrag.art],
        ist=VERTRAGSART_TEXT[vertrag.art],
    )


def _fixstunden(vertrag: Vertrag, rechnung: Rechnung) -> Befund:
    """Regel ``fixstunden`` — stimmt die Rechnung mit dem Vertrag überein.

    Neu. Bisher war der Positionstext die einzige Quelle; wich er vom Vertrag ab,
    rechnete das System mit dem Text weiter und niemand erfuhr davon.
    """
    if vertrag.fix_stunden is None:
        return Befund(
            regel="fixstunden", titel="Fixstunden", feld="fix_stunden",
            zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Kein Vertrag mit Fixstunden.",
        )
    if rechnung.fix_stunden is None:
        return Befund(
            regel="fixstunden", titel="Fixstunden", feld="fix_stunden",
            zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung="Die Rechnung weist keine Fixposition aus.",
            erwartet=_zahl(vertrag.fix_stunden),
        )
    stimmt = abs(rechnung.fix_stunden - vertrag.fix_stunden) < 0.005
    return Befund(
        regel="fixstunden", titel="Fixstunden", feld="fix_stunden",
        zustand=Zustand.RICHTIG if stimmt else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=(
            "Rechnung und Vertrag stimmen überein."
            if stimmt else
            "Die Rechnung weicht vom vereinbarten Umfang ab."
        ),
        erwartet=_zahl(vertrag.fix_stunden),
        ist=_zahl(rechnung.fix_stunden),
    )


def _erwartete_stunden(
    vertrag: Vertrag, periode: Periode, massstab: Massstab
) -> tuple[float | None, str]:
    """Wie viele Stunden nach Vertrag und Lesart in Rechnung gehören.

    Gibt ``(None, Grund)`` zurück, wo sich das nicht bestimmen lässt — und das ist
    bei Fixstunden solange der Fall, wie die Übertragslesart nicht bestätigt ist.
    """
    geleistet = periode.geleistet
    if geleistet is None:
        return None, "Die geleisteten Stunden sind nicht bekannt."

    if vertrag.art is Vertragsart.VARIABEL:
        # Die Herleitung sagt, was in Rechnung **gehört** — nie, was darauf steht.
        # Andernfalls behauptet sie im Fehlerfall das Gegenteil der Differenz, die
        # ihr nachgestellt wird: «7.25h verrechnet. Differenz -7.00h.»
        return geleistet, f"Variable Abrechnung: die {geleistet:g}h geleisteten gehören in Rechnung."

    if massstab.uebertragsformel is None:
        return None, (
            "Die Übertragslesart ist nicht bestätigt. Die beiden Fassungen im "
            "Altbestand stellen unterhalb der vereinbarten Stunden verschiedene "
            "Beträge in Rechnung — die Fixstunden oder die geleisteten abzüglich "
            "Übertrag. Ohne Entscheid ist die Erwartung nicht bestimmbar."
        )

    # Nicht übertragbar heisst: der Vormonat bringt nichts mit. Übertragbar und
    # unbekannt heisst dagegen, dass die Erwartung nicht bestimmbar ist — siehe
    # dieselbe Abgrenzung in ``_uebertrag``.
    if vertrag.art is Vertragsart.FIX_UEBERTRAGBAR:
        if periode.uebertrag_vormonat is None:
            return None, (
                "Der Übertrag aus dem Vormonat ist nicht bekannt; ohne ihn steht "
                "nicht fest, wie viele Stunden dieser Monat abdeckt."
            )
        vormonat = periode.uebertrag_vormonat
    else:
        vormonat = 0.0
    ergebnis = ue.rechnen(
        fix_stunden=vertrag.fix_stunden or 0.0,
        geleistet=geleistet,
        uebertrag_vormonat=vormonat,
        formel=massstab.uebertragsformel,
    )
    if ergebnis.verrechnet_erwartet is None:
        return None, ergebnis.herleitung
    return ergebnis.verrechnet_erwartet, ergebnis.herleitung


def _stunden(
    vertrag: Vertrag, rechnung: Rechnung, periode: Periode, massstab: Massstab
) -> Befund:
    """Regel ``stunden`` — verrechnete gegen erwartete Stunden."""
    kopf = dict(regel="stunden", titel="Verrechnete Stunden", feld="verrechnete_stunden")

    if not vertrag.stunden_pruefen:
        return Befund(
            **kopf, zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Festpreis ohne Stundenbezug.",
        )
    if rechnung.verrechnete_stunden is None:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung="Die Rechnung weist keine Stundenpositionen aus.",
        )

    erwartet, herleitung = _erwartete_stunden(vertrag, periode, massstab)
    if erwartet is None:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.FEHLER,
            begruendung=herleitung,
            ist=_zahl(rechnung.verrechnete_stunden),
        )

    differenz = rechnung.verrechnete_stunden - erwartet
    stimmt = abs(differenz) <= massstab.toleranz_stunden
    return Befund(
        **kopf,
        zustand=Zustand.RICHTIG if stimmt else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=(
            herleitung if stimmt
            else f"{herleitung} Verrechnet sind {rechnung.verrechnete_stunden:g}h, "
                 f"Differenz {differenz:+.2f}h."
        ),
        erwartet=_zahl(erwartet),
        ist=_zahl(rechnung.verrechnete_stunden),
    )


def _uebertrag(
    vertrag: Vertrag, rechnung: Rechnung, periode: Periode, massstab: Massstab
) -> Befund:
    """Regel ``uebertrag`` — die Zahl im Übertragstext gegen die Berechnung."""
    kopf = dict(regel="uebertrag", titel="Übertrag in den Folgemonat", feld="uebertrag_angabe")

    if vertrag.art is not Vertragsart.FIX_UEBERTRAGBAR:
        return Befund(
            **kopf, zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Der Vertrag kennt keinen Übertrag.",
        )
    if periode.geleistet is None:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.FEHLER,
            begruendung="Die geleisteten Stunden sind nicht bekannt.",
            ist=_zahl(rechnung.uebertrag_angabe),
        )
    if periode.uebertrag_vormonat is None:
        # Ein unbekannter Vormonatsübertrag ist nicht null. Wer ihn als null
        # rechnet, bekommt bei einem übertragbaren Vertrag eine plausible
        # falsche Erwartung — und die ist schlimmer als gar keine.
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung=(
                "Der Übertrag aus dem Vormonat ist nicht bekannt; ohne ihn steht "
                "die Kapazität dieses Monats nicht fest."
            ),
            ist=_zahl(rechnung.uebertrag_angabe),
        )
    if massstab.uebertragsformel is None:
        streitig = ue.im_streitfenster(
            fix_stunden=vertrag.fix_stunden or 0.0,
            geleistet=periode.geleistet,
            uebertrag_vormonat=periode.uebertrag_vormonat or 0.0,
        )
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.FEHLER,
            begruendung=(
                "Die Übertragslesart ist nicht bestätigt, und dieser Monat liegt "
                "genau im strittigen Bereich: die eine Fassung trägt den Rest weiter, "
                "die andere lässt ihn verfallen."
                if streitig else
                "Die Übertragslesart ist nicht bestätigt. Für diesen Monat ergäben "
                "allerdings beide Fassungen dasselbe Ergebnis."
            ),
            ist=_zahl(rechnung.uebertrag_angabe),
        )

    ergebnis = ue.rechnen(
        fix_stunden=vertrag.fix_stunden or 0.0,
        geleistet=periode.geleistet,
        uebertrag_vormonat=periode.uebertrag_vormonat or 0.0,
        formel=massstab.uebertragsformel,
    )
    if rechnung.uebertrag_angabe is None:
        if abs(ergebnis.neuer_uebertrag) <= 0.01:
            # Kein Satz und nichts zu übertragen sind dasselbe. Wer «0h werden
            # dem nächsten Monat angerechnet» auf eine Rechnung schreibt, macht
            # sie schlechter. Im Rücklauf gegen den August traf das zwei von
            # sechs Pauschalrechnungen — beide korrekt, beide gemeldet.
            return Befund(
                **kopf, zustand=Zustand.RICHTIG, gewicht=Gewicht.HINWEIS,
                begruendung="Es bleibt nichts übertragen, und die Rechnung sagt nichts.",
                erwartet=_zahl(0.0),
            )
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung=(
                f"Die Rechnung nennt keinen Übertrag, obwohl "
                f"{ergebnis.neuer_uebertrag:g}h in den Folgemonat gehören."
            ),
            erwartet=_zahl(ergebnis.neuer_uebertrag),
        )
    stimmt = abs(rechnung.uebertrag_angabe - ergebnis.neuer_uebertrag) <= 0.01
    return Befund(
        **kopf,
        zustand=Zustand.RICHTIG if stimmt else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=ergebnis.herleitung,
        erwartet=_zahl(ergebnis.neuer_uebertrag),
        ist=_zahl(rechnung.uebertrag_angabe),
    )


def _mwst(vertrag: Vertrag, rechnung: Rechnung) -> Befund:
    """Regel ``mwst`` — der ausgewiesene Satz gegen den vereinbarten."""
    kopf = dict(regel="mwst", titel="Mehrwertsteuer", feld="mwst_satz")
    if rechnung.mwst_satz is None:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung="Die Rechnung weist keinen Steuersatz aus.",
            erwartet=_zahl(vertrag.mwst_satz, "%"),
        )
    stimmt = abs(rechnung.mwst_satz - vertrag.mwst_satz) < 0.01
    return Befund(
        **kopf,
        zustand=Zustand.RICHTIG if stimmt else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=(
            "Satz wie vereinbart." if stimmt
            else "Der Satz weicht von der Vereinbarung ab."
        ),
        erwartet=_zahl(vertrag.mwst_satz, "%"),
        ist=_zahl(rechnung.mwst_satz, "%"),
    )


def _iban(rechnung: Rechnung, periode: Periode, massstab: Massstab) -> Befund:
    """Regel ``iban`` — steht die richtige Bankverbindung auf der Rechnung.

    Verglichen wird nur der Anfang der IBAN, also die Bank. Die Kontonummer
    gehört nicht in eine Prüfkonfiguration.
    """
    kopf = dict(regel="iban", titel="Bankverbindung", feld="iban")
    if not massstab.iban_erwartet:
        return Befund(
            **kopf, zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Keine erwartete Bankverbindung hinterlegt.",
        )
    if not periode.dokumente_erzeugt:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.HINWEIS,
            begruendung="Die IBAN steht in der PDF, und die ist noch nicht erzeugt.",
            erwartet=f"{massstab.iban_erwartet}…",
        )
    if not rechnung.iban:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung="Auf der Rechnung ist keine IBAN feststellbar.",
            erwartet=f"{massstab.iban_erwartet}…",
        )
    entspricht = rechnung.iban.replace(" ", "").startswith(
        massstab.iban_erwartet.replace(" ", "")
    )
    return Befund(
        **kopf,
        zustand=Zustand.RICHTIG if entspricht else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=(
            "Die Bankverbindung stimmt." if entspricht
            else "Die Rechnung nennt eine andere Bank als erwartet."
        ),
        erwartet=f"{massstab.iban_erwartet}…",
        ist=f"{rechnung.iban.replace(' ', '')[:len(massstab.iban_erwartet.replace(' ', ''))]}…",
    )


def _referenz(vertrag: Vertrag, rechnung: Rechnung) -> Befund:
    """Regel ``referenz`` — trägt die Rechnung die vom Kunden verlangte Referenz.

    Bisher hing das an der Liste ``pdf_merge_kunden``: wer eine zusammengeführte
    PDF bekam, brauchte eine Referenz. Das waren zwei Eigenschaften an einem
    Schalter. Da künftig alle Kunden ein zusammengeführtes Dokument erhalten,
    trüge dieser Schalter die Referenzpflicht nicht mehr — sie wird deklariert.
    """
    kopf = dict(regel="referenz", titel="Referenz", feld="referenz")
    if not vertrag.referenz_pflicht:
        return Befund(
            **kopf, zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Dieser Kunde verlangt keine Referenz.",
        )
    vorhanden = bool(rechnung.referenz and rechnung.referenz.strip())
    return Befund(
        **kopf,
        zustand=Zustand.RICHTIG if vorhanden else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=(
            "Referenz vorhanden." if vorhanden
            else "Der Kunde verlangt eine Referenz; die Rechnung trägt keine."
        ),
        ist=rechnung.referenz or None,
    )


def _rechnungsdatum(rechnung: Rechnung, periode: Periode) -> Befund:
    """Regel ``rechnungsdatum`` — letzter Tag des Leistungsmonats.

    Nicht Formsache, sondern Umsatzabgrenzung: der Lauf findet im Folgemonat
    statt, die Leistung gehört aber in den Monat, in dem sie erbracht wurde.
    """
    kopf = dict(regel="rechnungsdatum", titel="Rechnungsdatum", feld="datum")
    erwartet = periode.letzter_tag
    if rechnung.datum is None:
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
            begruendung="Die Rechnung trägt kein lesbares Datum.",
            erwartet=erwartet.strftime("%d.%m.%Y"),
        )
    stimmt = rechnung.datum == erwartet
    return Befund(
        **kopf,
        zustand=Zustand.RICHTIG if stimmt else Zustand.FALSCH,
        gewicht=Gewicht.FEHLER,
        begruendung=(
            "Datiert auf den letzten Tag des Leistungsmonats."
            if stimmt else
            "Ein abweichendes Datum verschiebt den Umsatz in den falschen Monat."
        ),
        erwartet=erwartet.strftime("%d.%m.%Y"),
        ist=rechnung.datum.strftime("%d.%m.%Y"),
    )


def _leistungsrapport(vertrag: Vertrag, rechnung: Rechnung, periode: Periode) -> Befund:
    """Regel ``leistungsrapport`` — liegt der Nachweis bei, wo er nötig ist."""
    kopf = dict(regel="leistungsrapport", titel="Leistungsrapport", feld="leistungsrapport")
    if not vertrag.leistungsrapport_erwartet and not periode.dokumente_erzeugt:
        return Befund(
            **kopf, zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Für dieses Projekt ist kein Rapport vorgesehen.",
        )
    if not periode.dokumente_erzeugt:
        # Nicht «fehlt», sondern «noch nicht da».
        return Befund(
            **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.HINWEIS,
            begruendung="Die Dokumente sind noch nicht erzeugt.",
        )
    if periode.leistungsrapport_vorhanden:
        return Befund(
            **kopf, zustand=Zustand.RICHTIG, gewicht=Gewicht.FEHLER,
            begruendung="Rapport vorhanden.",
        )
    zusatz = rechnung.zusatz_stunden or 0.0
    if zusatz > 0:
        return Befund(
            **kopf, zustand=Zustand.FALSCH, gewicht=Gewicht.FEHLER,
            begruendung=(
                f"{zusatz:g}h Zusatzstunden ohne Nachweis — genau das, wonach der "
                "Kunde zurückfragt."
            ),
        )
    if not vertrag.leistungsrapport_erwartet:
        return Befund(
            **kopf, zustand=Zustand.ENTFAELLT, gewicht=Gewicht.HINWEIS,
            begruendung="Für dieses Projekt ist kein Rapport vorgesehen.",
        )
    return Befund(
        **kopf, zustand=Zustand.OFFEN, gewicht=Gewicht.WARNUNG,
        begruendung="Kein Rapport gefunden, aber auch keine Zusatzstunden verrechnet.",
    )


# ── Zusammenführung ──────────────────────────────────────

def pruefen(
    vertrag: Vertrag,
    rechnung: Rechnung,
    periode: Periode,
    massstab: Massstab,
) -> Ergebnis:
    """Alle Regeln auf eine Rechnung anwenden.

    Reine Funktion ohne Datenbank, ohne Dateien, ohne Netz — damit sie prüfbar
    bleibt und in der Ansicht, im Monatslauf und im Rücklauf gegen abgeschlossene
    Monate dieselbe ist. Zwei Prüfstellen mit unterschiedlichem Urteil über
    dieselbe Rechnung gab es im Altbestand schon; das war einer der Befunde.
    """
    return Ergebnis(
        rechnung=rechnung.nummer,
        projekt=vertrag.projekt,
        kunde=vertrag.kunde,
        vertragsart=vertrag.art,
        befunde=[
            _vertragsart(vertrag),
            _fixstunden(vertrag, rechnung),
            _stunden(vertrag, rechnung, periode, massstab),
            _uebertrag(vertrag, rechnung, periode, massstab),
            _mwst(vertrag, rechnung),
            _iban(rechnung, periode, massstab),
            _referenz(vertrag, rechnung),
            _rechnungsdatum(rechnung, periode),
            _leistungsrapport(vertrag, rechnung, periode),
        ],
    )


REGELN: tuple[str, ...] = (
    "vertragsart",
    "fixstunden",
    "stunden",
    "uebertrag",
    "mwst",
    "iban",
    "referenz",
    "rechnungsdatum",
    "leistungsrapport",
)
"""Die Reihenfolge der Spalten in der Ansicht — und die Prüfliste für den Test,
dass ``pruefen`` zu jeder Regel genau einen Befund liefert."""
