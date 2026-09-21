"""Das Vertragsstammdatum der Debitoren — was je Projekt vereinbart ist.

Liest ``docs/debitorenvertraege.yaml`` und liefert daraus die Eingabe für die
Rechnungsprüfung (``debitoren_pruefung.Vertrag``) sowie die Angaben, die der
Versand braucht: Empfänger, Anrede, Ordnername im Archiv.

## Warum es diese Schicht gibt

Die alte Fassung verteilte dieselbe Frage auf drei Orte. Der Empfänger stand in
``config.yml``, die Referenzpflicht ebenfalls, der Ordnername in einer dritten
Liste — und die **vereinbarten Fixstunden standen nirgends**. Sie wurden zur
Laufzeit aus dem Positionstext der Bexio-Rechnung zurückgelesen, mit zwei
regulären Ausdrücken, die sich nicht einig waren. Damit war der Text die Quelle
der Wahrheit über den Vertrag: wer ihn umformulierte, änderte den Vertrag, und
niemand bekam eine Meldung.

Hier ist die Zahl deklariert. Der Text wird weiterhin gelesen, aber nur noch zum
Abgleich — ``debitoren_pruefung`` meldet eine Abweichung als Befund, statt sie
zu übernehmen.

## Die Wächter

Ein Stammdatum, das schweigend lückenhaft ist, richtet mehr Schaden an als
keines: ein Projekt ohne Vertrag fällt aus der Prüfung heraus und sieht dabei
aus wie ein Projekt ohne Beanstandung. Deshalb prüft ``laden`` beim Einlesen:

1. **Doppelte Schlüssel** — der zweite Eintrag gewänne stillschweigend.
2. **Unbekannte Kundschaft** — ein ``kunde``, den ``kundenschluessel.yaml``
   nicht führt, ergäbe eine tote Verknüpfung, die wie eine Zuordnung aussieht.
3. **Fixstunden ohne Übertragsangabe** — ohne sie ist nicht entscheidbar, ob
   nicht verbrauchte Stunden in den Folgemonat wandern. Der Eintrag wird nicht
   verworfen, aber als unvollständig geführt, und die Prüfung meldet die
   Stundenregeln als offen.
4. **Empfänger fehlt oder ist ein Platzhalter** — die alte Konfiguration trug
   bei einem Projekt ``TODO_EMAIL_EINTRAGEN``. Beim Versand hätte das nichts
   erzeugt; hier ist es ein Befund vor dem Lauf.
5. **Mehrwertsteuersatz ausserhalb 0 bis 100.**

``abgleichen`` ergänzt den fehlenden Blick von der anderen Seite: welche
tatsächlich abgerechneten Projekte haben hier keinen Vertrag. Nur diese Richtung
findet ein neu angelegtes Projekt, von dem die Datei nichts weiss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.debitoren_positionen import Positionsdeutung

logger = logging.getLogger(__name__)

PLATZHALTER = ("todo", "tbd", "xxx", "@example.")
"""Was als Adresse dasteht, ohne eine zu sein. Kleingeschrieben verglichen."""


class Rhythmus(str, Enum):
    """In welchem Takt eine Rechnung zu erwarten ist.

    Deklariert und **nicht** aus den Rechnungen erschlossen. Der Versuch wäre
    naheliegend -- eine Pauschalzeile mit Menge 3 sieht nach einem Quartal aus
    -- und er ginge daneben: bei ImpulsKöniz bedeutet die Drei drei Monate, bei
    den MATRICS-Domains etwas ganz anderes, und in beiden Fällen steht die
    Bedeutung nur im Freitext. Was nur im Freitext steht, ist kein Datum.

    Der Rhythmus entscheidet allein, ob das **Fehlen** einer Rechnung ein Befund
    ist. Er erzeugt keine Rechnung und ändert keinen Betrag.
    """

    MONATLICH = "monatlich"
    QUARTALSWEISE = "quartalsweise"
    JAEHRLICH = "jaehrlich"
    NACH_AUFWAND = "nach_aufwand"
    """Rechnung nur, wenn Leistung erbracht wurde -- AKV-Bot etwa läuft
    sporadisch, weil die Kundschaft wenig Zeit hat. Ein fehlender Monat ist hier
    der Normalfall und kein Mangel."""

    def erwartet_rechnung(self, monat: int | None, *, stunden_vorhanden: bool) -> bool:
        """Ob dieser Leistungsmonat eine eigene Rechnung tragen müsste.

        Ein unbekannter Monat unterdrückt **nichts**: die Quartalsregel könnte
        den Befund sonst wegblenden, ohne dass jemand es merkt. Lieber eine
        Meldung zu viel als eine stillschweigend verschluckte.
        """
        if self is Rhythmus.MONATLICH:
            return True
        if self is Rhythmus.QUARTALSWEISE:
            return monat is None or monat in (3, 6, 9, 12)
        if self is Rhythmus.NACH_AUFWAND:
            return stunden_vorhanden
        return False  # jährlich: monatlich nicht beurteilbar


def _rhythmus_lesen(
    wert: object, schluessel: str, *, rueckfall: "Rhythmus | None" = None
) -> tuple["Rhythmus", str]:
    """Einen Takt aus der Datei lesen. Unbekanntes fällt zurück und wird gemeldet.

    Nicht verworfen, sondern zurückgefallen: ein Vertrag ohne Takt fiele aus der
    Prüfung, und ein Tippfehler im Takt darf nicht die ganze Rechnungsprüfung
    eines Kunden abschalten. Gemeldet wird er trotzdem.
    """
    standard = rueckfall or Rhythmus.MONATLICH
    if wert is None:
        return standard, ""
    try:
        return Rhythmus(str(wert)), ""
    except ValueError:
        erlaubt = ", ".join(r.value for r in Rhythmus)
        return standard, (
            f"Vertrag '{schluessel}': Rhythmus {wert!r} ist unbekannt "
            f"(erlaubt: {erlaubt}) -- es gilt '{standard.value}'"
        )


VERRECHNUNGSARTEN = frozenset({
    "Kunde verrechnet",
    "Fixpreis",
    "Extern rapportiert",
    "keine Verrechnung",
})
"""Die vier Toggl-Tags, die eine Verrechnungsart bezeichnen.

Hier deklariert und nicht aus dem Workspace gelesen, obwohl sie dort stehen.
Der Grund ist die Richtung der Prüfung: gegen den Workspace gehalten wäre jeder
Tag eine gültige Verrechnungsart, auch ein versehentlich angelegter. Diese
Liste ist die Behauptung, welche vier es gibt — der Abgleich mit dem Workspace
prüft sie, statt sie zu ersetzen.
"""


@dataclass(frozen=True)
class Konditionen:
    """Was in einem Zeitraum vereinbart war -- Fixstunden und Takt.

    Beides zusammen, weil es sich zusammen ändert: als ImpulsKöniz im Januar
    2026 auf eine Stunde herunterging, wechselte gleichzeitig der Takt auf
    quartalsweise. Zwei getrennte Zeitachsen für einen Entscheid zu führen hiesse,
    sie auseinanderlaufen zu lassen.
    """

    fix_stunden: float | None = None
    uebertragbar: bool | None = None
    rhythmus: Rhythmus = Rhythmus.MONATLICH


# ── Die Datensätze ───────────────────────────────────────


@dataclass(frozen=True)
class Kunde:
    """Was für alle Projekte einer Kundschaft gilt."""

    schluessel: str
    empfaenger: str | None = None
    referenz_pflicht: bool = False
    ablage: str | None = None
    verrechnungsart: str = ""
    """Das Toggl-Tag für alle Projekte dieser Kundschaft. Leer heisst «Vorgabe»."""
    bestaetigt: bool = False
    hinweis: str = ""

    @property
    def ordner(self) -> str | None:
        """Der Ordnername im Kundenarchiv. ``None`` heisst «nicht abzulegen».

        Früher fiel dieser Wert auf den Schlüssel zurück, und das war ein
        stiller Fehler: der Schlüssel ist eine technische Kennung in ASCII und
        Kleinschreibung, der Ordner trägt den ausgeschriebenen Namen. Gemessen
        am 21.09.2026 hätten elf von 25 aktiven Verträgen einen **neuen** Ordner
        angelegt — «bfh» neben «Berner Fachhochschule», «gsw» neben «GSW
        Treuhand AG». Niemand hätte das bemerkt, denn eine Ablage, die einen
        Ordner anlegt, sieht aus wie eine, die abgelegt hat.

        Darum kein Rückfall: ohne gepflegten Alias wird nicht abgelegt, und der
        fehlende Alias steht als Mangel in der Ansicht.
        """
        return self.ablage or None


@dataclass(frozen=True)
class Vertragsdaten:
    """Ein Eintrag der Datei, angereichert um die Angaben der Kundschaft."""

    schluessel: str
    bezeichnung: str
    kunde: Kunde
    """Die **heutige** Gegenpartei. Für vergangene Monate ``kunde_am`` fragen."""
    mwst: float
    fix_stunden: float | None = None
    """Was **heute** gilt. Für vergangene Monate ``konditionen_am`` fragen."""
    uebertragbar: bool | None = None
    """``None`` heisst «nicht festgelegt» und ist bei gesetzten Fixstunden ein
    Mangel — nicht dasselbe wie ``False`` («verfällt»)."""
    rhythmus: Rhythmus = Rhythmus.MONATLICH
    stunden_pruefen: bool = True
    leistungsrapport: bool = True
    anrede: str = "ich"
    ruhend: bool = False
    erkennung: tuple[str, ...] = ()
    verrechnungsart: str = ""
    """Das Toggl-Tag, das nach bestätigtem Versand gesetzt wird.

    Beim Laden aufgelöst: Vertrag, sonst Kundschaft, sonst Vorgabe. Aufgelöst
    und nicht als Kette gespeichert, weil sonst jede Leseseite die Erbfolge
    nachbauen müsste — und drei Nachbauten sind drei Gelegenheiten, sie
    verschieden zu lesen.
    """
    bestaetigt: bool = False
    hinweis: str = ""

    frueher: tuple[tuple[date, Kunde], ...] = ()
    """Frühere Gegenparteien als (letzter Tag, Kundschaft), aufsteigend sortiert.

    Ein Projekt kann die Gegenpartei wechseln, ohne ein anderes Projekt zu
    werden: ``deinklima`` lief bis Ende 2025 über die Wyss Academy und läuft
    seit 2026 über das Amt für Umwelt und Energie. Für laufende Rechnungen ist
    das belanglos, für die Prüfung abgeschlossener Monate nicht — AUE ist
    referenzpflichtig, die Wyss Academy war es nicht. Ohne diesen Zeitbezug
    meldete der Rücklauf für 2025 eine fehlende Referenz als Fehler, und ein
    Fehlalarm im Rücklauf macht die Messung wertlos, für die er gedacht ist.
    """

    vorher: tuple[tuple[date, Konditionen], ...] = ()
    """Frühere Konditionen als (letzter Tag, Konditionen), aufsteigend sortiert.

    Derselbe Zeitbezug wie bei ``frueher``, für eine andere Eigenschaft. Anlass
    war ImpulsKöniz: 7h im September 2025, 3h im Oktober und November, 1h ab
    Dezember, ab Januar 2026 zusätzlich quartalsweise statt monatlich, und seit
    August 2026 wieder 7h monatlich. Ohne diese Achse prüfte der Rücklauf jeden
    dieser Monate gegen den heutigen Massstab -- und meldete für sieben Monate
    eine Unterdeckung, die keine war.
    """

    @property
    def vollstaendig(self) -> bool:
        """Ob der Vertrag geprüft werden kann, ohne etwas anzunehmen."""
        return not (self.fix_stunden is not None and self.uebertragbar is None)

    def kunde_am(self, tag: date | None = None) -> Kunde:
        """Wer an diesem Tag die Gegenpartei war. Ohne Angabe: die heutige."""
        if tag is None:
            return self.kunde
        for bis, kunde in self.frueher:
            if tag <= bis:
                return kunde
        return self.kunde

    def konditionen_am(self, tag: date | None = None) -> Konditionen:
        """Was an diesem Tag vereinbart war. Ohne Angabe: was heute gilt."""
        heute = Konditionen(
            fix_stunden=self.fix_stunden,
            uebertragbar=self.uebertragbar,
            rhythmus=self.rhythmus,
        )
        if tag is None:
            return heute
        for bis, konditionen in self.vorher:
            if tag <= bis:
                return konditionen
        return heute

    def als_vertrag(self, am: date | None = None):  # noqa: ANN201
        """In die Eingabe der Rechnungsprüfung überführen.

        ``am`` ist der Leistungsmonat und entscheidet über die Gegenpartei —
        und damit über die Referenzpflicht.

        Fehlt bei gesetzten Fixstunden die Übertragsangabe, gehen die Fixstunden
        **nicht** mit. Sonst würde der Vertrag als «Fixstunden, verfallend»
        geprüft — eine Lesart, die niemand erklärt hat, und der Mangel wäre
        hinter einem plausiblen Ergebnis verschwunden.
        """
        from app.services.debitoren_pruefung import Vertrag

        kunde = self.kunde_am(am)
        kond = self.konditionen_am(am)
        vollstaendig = not (kond.fix_stunden is not None and kond.uebertragbar is None)
        return Vertrag(
            projekt=self.bezeichnung,
            kunde=kunde.schluessel,
            mwst_satz=self.mwst,
            fix_stunden=kond.fix_stunden if vollstaendig else None,
            uebertragbar=bool(kond.uebertragbar),
            referenz_pflicht=kunde.referenz_pflicht,
            stunden_pruefen=self.stunden_pruefen,
            leistungsrapport_erwartet=self.leistungsrapport,
        )


def _finden_in(
    vertraege: dict[str, Vertragsdaten], bezeichnung: str
) -> Vertragsdaten | None:
    """Die Suche als Funktion, damit sie auch beim Laden zur Verfügung steht.

    Der Bestand existiert dort noch nicht, die Prüfung «trifft dieser interne
    Projektname einen Vertrag» braucht sie aber schon. Eine zweite Fassung der
    Suche wäre die schlechtere Antwort: sie würde mit dieser auseinanderlaufen,
    und dann prüfte das Laden eine andere Zuordnung als der Lauf.
    """
    gesucht = " ".join(bezeichnung.split()).casefold()
    if not gesucht:
        return None

    treffer: list[tuple[int, str, Vertragsdaten]] = []
    for vertrag in vertraege.values():
        for wort in (vertrag.bezeichnung, *vertrag.erkennung):
            begriff = " ".join(wort.split()).casefold()
            if begriff and begriff in gesucht:
                treffer.append((len(begriff), vertrag.schluessel, vertrag))

    if not treffer:
        return None
    laenge = max(t[0] for t in treffer)
    beste = {t[1]: t[2] for t in treffer if t[0] == laenge}
    if len(beste) > 1:
        logger.warning(
            "Zuordnung mehrdeutig für %r: %s treffen gleich lang",
            bezeichnung, ", ".join(sorted(beste)),
        )
        return None
    return next(iter(beste.values()))


@dataclass
class Bestand:
    """Alle Verträge einer geladenen Datei, mit den Wegen hinein."""

    vertraege: dict[str, Vertragsdaten] = field(default_factory=dict)
    kunden: dict[str, Kunde] = field(default_factory=dict)
    vorgaben: dict[str, Any] = field(default_factory=dict)
    offen: list[dict] = field(default_factory=list)
    interne_projekte: tuple[str, ...] = ()
    """Eigene Toggl-Projekte ohne Kundschaft. Tragen «keine Verrechnung».

    Namentlich deklariert und nicht aus dem Fehlen eines Vertrags erschlossen:
    ein neues Kundenprojekt, das diese Datei noch nicht kennt, bekäme sonst
    still «keine Verrechnung» — verrechenbare Stunden, als unverrechenbar
    abgestempelt, ohne Fehlermeldung.
    """

    def aktive(self) -> list[Vertragsdaten]:
        return [v for v in self.vertraege.values() if not v.ruhend]

    def finden(self, bezeichnung: str) -> Vertragsdaten | None:
        """Den Vertrag zu einem Projekt- oder Rechnungsnamen suchen.

        Gesucht wird als **Teilzeichenkette**, nicht auf Gleichheit: der
        Toggl-Projektname trifft die Bezeichnung genau, der Rechnungstitel in
        Bexio trägt aber den Monat mit («AGG Beratung August 2026»). Aus
        ``recognize_project`` der alten Fassung übernommen, samt der tragenden
        Regel **längster Treffer gewinnt** — sonst entschiede die Reihenfolge im
        Wörterbuch zwischen «Beratung» und «AGG Beratung», und zwar lautlos.

        Ein Unterschied zur alten Fassung: bei gleich langen Treffern auf
        **verschiedene** Verträge wird nichts zurückgegeben. Dort brach ein
        ``sort(reverse=True)`` den Gleichstand alphabetisch — also durch Zufall.
        Eine nicht getroffene Zuordnung fällt in der Ansicht auf, eine falsch
        geratene nicht.

        Der Weg über Stichwörter bleibt ein Behelf. Er verschwindet, sobald die
        Zuordnung über Kennungen läuft: ein Stichwort trifft auch, wo es nicht
        gemeint war, und meldet das nicht.
        """
        return _finden_in(self.vertraege, bezeichnung)


# ── Laden ────────────────────────────────────────────────


def _datei_finden(name: str = "debitorenvertraege.yaml") -> Path:
    """Wo die gepflegte Datei liegt — im Arbeitsbaum wie im Container.

    Die Verzeichniskette aufwärts statt einer festen Tiefe: im Backend-Image
    liegt das Modul unter ``/app/app/services`` und hat dort gar keine fünfte
    Ebene.
    """
    hier = Path(__file__).resolve()
    kandidaten = [eltern / "docs" / name for eltern in hier.parents]
    for pfad in kandidaten:
        if pfad.exists():
            return pfad
    return kandidaten[min(4, len(kandidaten) - 1)]


def _lesen(pfad: Path) -> dict:
    import yaml

    try:
        inhalt = yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("Debitorenvertraege: %s fehlt -- kein Vertrag bekannt", pfad)
        return {}
    except yaml.YAMLError as exc:
        logger.warning("Debitorenvertraege: %s ist nicht lesbar: %s", pfad, exc)
        return {}
    return inhalt if isinstance(inhalt, dict) else {}


def bekannte_kundschaften(pfad: Path | None = None) -> set[str]:
    """Die Schlüssel aus ``kundenschluessel.yaml`` — ohne den Datenraum.

    Bewusst nur die Datei und nicht die Parquet-Tabellen: geprüft wird hier, ob
    ein Vertrag auf eine *deklarierte* Kundschaft zeigt. Ob diese Kundschaft
    auch Kennungen in den Fachsystemen hat, ist die Frage des Kundenschlüssels
    und wird dort beantwortet.
    """
    inhalt = _lesen(pfad or _datei_finden("kundenschluessel.yaml"))
    return {
        e["schluessel"]
        for e in (inhalt.get("kundschaften") or [])
        if isinstance(e, dict) and e.get("schluessel")
    }


def _ist_platzhalter(adresse: str) -> bool:
    klein = adresse.casefold()
    return any(marke in klein for marke in PLATZHALTER) or "@" not in adresse


def laden(
    pfad: Path | None = None,
    *,
    bekannte_kunden: set[str] | None = None,
) -> tuple[Bestand, dict]:
    """Die Datei einlesen und ihre Mängel benennen.

    Zurück kommen der Bestand und ein Befund. Verworfen wird nur, was keine
    brauchbare Zeile ergibt; alles andere wird geführt **und** gemeldet — ein
    Vertrag ohne Empfänger ist zum Prüfen brauchbar und zum Versenden nicht,
    und beides soll sichtbar bleiben.
    """
    inhalt = _lesen(pfad or _datei_finden())
    bekannt = bekannte_kunden if bekannte_kunden is not None else bekannte_kundschaften()

    vorgaben = inhalt.get("vorgaben") or {}
    standardsatz = vorgaben.get("mwst")
    standardanrede = vorgaben.get("anrede") or "ich"
    standardart = (vorgaben.get("verrechnungsart") or "").strip()

    verworfen: list[str] = []
    bemaengelt: list[str] = []

    def art_lesen(eintrag: dict, gegenstand: str, rueckfall: str) -> str:
        """Eine Verrechnungsart lesen und gegen die vier bekannten halten.

        Geprüft wird hier und nicht beim Schreiben: eine unbekannte Art ginge
        sonst als neuer Tagname an Toggl, und Toggl legt einen fehlenden Tag
        **stillschweigend an**. Aus einem Tippfehler würde so ein fünftes Tag,
        das aussieht wie eine Verrechnungsart und keine ist.
        """
        wert = (eintrag.get("verrechnungsart") or "").strip()
        if not wert:
            return rueckfall
        if wert not in VERRECHNUNGSARTEN:
            bemaengelt.append(
                f"{gegenstand}: Verrechnungsart {wert!r} ist unbekannt "
                f"(bekannt: {', '.join(sorted(VERRECHNUNGSARTEN))}) -- "
                f"es gilt {rueckfall!r}"
            )
            return rueckfall
        return wert

    if standardart and standardart not in VERRECHNUNGSARTEN:
        bemaengelt.append(
            f"Vorgabe: Verrechnungsart {standardart!r} ist unbekannt"
        )
        standardart = ""

    kunden: dict[str, Kunde] = {}
    for eintrag in inhalt.get("kunden") or []:
        schluessel = (eintrag or {}).get("schluessel")
        if not schluessel:
            verworfen.append(f"Kundschaft ohne Schluessel: {eintrag!r:.60}")
            continue
        if schluessel in kunden:
            verworfen.append(f"Kundschaft '{schluessel}' steht zweimal")
            continue
        if bekannt and schluessel not in bekannt:
            verworfen.append(
                f"Kundschaft '{schluessel}' gibt es im Kundenschluessel nicht"
            )
            continue
        kunden[schluessel] = Kunde(
            schluessel=schluessel,
            empfaenger=eintrag.get("empfaenger"),
            referenz_pflicht=bool(eintrag.get("referenz_pflicht")),
            ablage=eintrag.get("ablage"),
            verrechnungsart=art_lesen(
                eintrag, f"Kundschaft '{schluessel}'", standardart
            ),
            bestaetigt=bool(eintrag.get("bestaetigt")),
            hinweis=(eintrag.get("hinweis") or "").strip(),
        )

    vertraege: dict[str, Vertragsdaten] = {}
    for eintrag in inhalt.get("vertraege") or []:
        schluessel = (eintrag or {}).get("schluessel")
        bezeichnung = (eintrag or {}).get("bezeichnung")
        if not schluessel or not bezeichnung:
            verworfen.append(f"Vertrag ohne Schluessel oder Bezeichnung: {eintrag!r:.60}")
            continue
        if schluessel in vertraege:
            # Ohne diese Prüfung gewänne der zweite Eintrag, und zwar lautlos.
            verworfen.append(f"Vertrag '{schluessel}' steht zweimal")
            continue
        kunde = kunden.get(eintrag.get("kunde") or "")
        if kunde is None:
            verworfen.append(
                f"Vertrag '{schluessel}': Kundschaft "
                f"'{eintrag.get('kunde')}' ist hier nicht deklariert"
            )
            continue

        satz = eintrag.get("mwst")
        satz = standardsatz if satz is None else satz
        if satz is None or not (0 <= float(satz) <= 100):
            verworfen.append(f"Vertrag '{schluessel}': Mehrwertsteuersatz {satz!r}")
            continue

        fix = eintrag.get("fix_stunden")
        uebertragbar = eintrag.get("uebertragbar")
        if fix is not None and uebertragbar is None:
            # Geführt, aber unvollständig: die Prüfung meldet die Stundenregeln
            # als offen, statt eine der beiden Lesarten anzunehmen.
            bemaengelt.append(
                f"Vertrag '{schluessel}': {fix}h fix, aber 'uebertragbar' fehlt"
            )

        # Frühere Gegenparteien. Ein Wechsel auf eine Kundschaft, die hier nicht
        # deklariert ist, wird verworfen statt stillschweigend auf die heutige
        # zurückzufallen -- sonst prüfte der Rücklauf den falschen Massstab und
        # sähe dabei aus wie ein Treffer.
        frueher: list[tuple[date, Kunde]] = []
        for wechsel in eintrag.get("kundenwechsel") or []:
            frueherer = kunden.get((wechsel or {}).get("kunde") or "")
            bis = (wechsel or {}).get("bis")
            if frueherer is None or not isinstance(bis, date):
                verworfen.append(
                    f"Vertrag '{schluessel}': Kundenwechsel {wechsel!r:.40} "
                    "nennt keine bekannte Kundschaft oder kein Datum"
                )
                continue
            frueher.append((bis, frueherer))
        frueher.sort(key=lambda p: p[0])

        rhythmus, mangel = _rhythmus_lesen(eintrag.get("rhythmus"), schluessel)
        if mangel:
            verworfen.append(mangel)

        # Frühere Konditionen. Was ein Eintrag nicht nennt, gilt wie heute --
        # sonst müsste jede Zeile alles wiederholen, und eine Pflicht zur
        # Wiederholung ist eine Einladung zum Auseinanderlaufen.
        vorher: list[tuple[date, Konditionen]] = []
        for stufe in eintrag.get("konditionen") or []:
            stufe = stufe or {}
            bis_tag = stufe.get("bis")
            if not isinstance(bis_tag, date):
                verworfen.append(
                    f"Vertrag '{schluessel}': Konditionen {stufe!r:.40} nennen kein Datum"
                )
                continue
            takt, taktmangel = _rhythmus_lesen(
                stufe.get("rhythmus"), schluessel, rueckfall=rhythmus
            )
            if taktmangel:
                verworfen.append(taktmangel)
            frueher_fix = stufe.get("fix_stunden", fix)
            frueher_uebertrag = stufe.get("uebertragbar", uebertragbar)
            vorher.append((bis_tag, Konditionen(
                fix_stunden=None if frueher_fix is None else float(frueher_fix),
                uebertragbar=(
                    None if frueher_uebertrag is None else bool(frueher_uebertrag)
                ),
                rhythmus=takt,
            )))
        vorher.sort(key=lambda p: p[0])

        vertrag = Vertragsdaten(
            schluessel=schluessel,
            bezeichnung=bezeichnung,
            kunde=kunde,
            frueher=tuple(frueher),
            mwst=float(satz),
            fix_stunden=None if fix is None else float(fix),
            uebertragbar=None if uebertragbar is None else bool(uebertragbar),
            rhythmus=rhythmus,
            vorher=tuple(vorher),
            stunden_pruefen=eintrag.get("stunden_pruefen", True) is not False,
            leistungsrapport=eintrag.get("leistungsrapport", True) is not False,
            anrede=eintrag.get("anrede") or standardanrede,
            ruhend=bool(eintrag.get("ruhend")),
            erkennung=tuple(eintrag.get("erkennung") or ()),
            verrechnungsart=art_lesen(
                eintrag, f"Vertrag '{schluessel}'", kunde.verrechnungsart
            ),
            bestaetigt=bool(eintrag.get("bestaetigt")),
            hinweis=(eintrag.get("hinweis") or "").strip(),
        )
        vertraege[schluessel] = vertrag

        if not vertrag.ruhend:
            adresse = kunde.empfaenger
            if not adresse:
                bemaengelt.append(
                    f"Vertrag '{schluessel}': Kundschaft '{kunde.schluessel}' "
                    "hat keinen Empfaenger -- Versand nicht moeglich"
                )
            elif _ist_platzhalter(adresse):
                bemaengelt.append(
                    f"Vertrag '{schluessel}': '{adresse}' ist keine Adresse, "
                    "sondern ein Platzhalter"
                )
            if kunde.ordner is None:
                bemaengelt.append(
                    f"Vertrag '{schluessel}': Kundschaft '{kunde.schluessel}' "
                    "hat keinen Ablageordner -- Ablage ins Kundenarchiv gesperrt"
                )

    intern: list[str] = []
    for name in inhalt.get("interne_projekte") or []:
        name = " ".join(str(name or "").split())
        if not name:
            continue
        # Ein interner Name, der auch einen Vertrag trifft, wäre zwei Dinge
        # gleichzeitig: verrechenbar und nicht. Das wird gemeldet und nicht
        # entschieden -- getaggt wird dieses Projekt dann gar nicht.
        if (treffer := _finden_in(vertraege, name)) is not None:
            bemaengelt.append(
                f"Internes Projekt '{name}' trifft auch Vertrag "
                f"'{treffer.schluessel}' -- keine Zuordnung moeglich"
            )
        intern.append(name)

    bestand = Bestand(
        vertraege=vertraege,
        kunden=kunden,
        vorgaben=vorgaben,
        offen=list(inhalt.get("offen") or []),
        interne_projekte=tuple(intern),
    )

    unbestaetigt = [s for s, v in vertraege.items() if not v.bestaetigt]
    unvollstaendig = [s for s, v in vertraege.items() if not v.vollstaendig]
    befund: dict[str, Any] = {
        "vertraege": len(vertraege),
        "aktiv": len(bestand.aktive()),
        "kunden": len(kunden),
        "unbestaetigt": len(unbestaetigt),
    }
    if unvollstaendig:
        befund["ohne_uebertragsangabe"] = sorted(unvollstaendig)
    if verworfen:
        befund["verworfen"] = verworfen
        logger.warning(
            "Debitorenvertraege: %d Eintrag/Eintraege verworfen: %s",
            len(verworfen),
            "; ".join(verworfen[:5]),
        )
    if bemaengelt:
        befund["bemaengelt"] = bemaengelt
    if bestand.offen:
        befund["offene_fragen"] = [
            {"gegenstand": f.get("gegenstand"), "frage": f.get("frage")}
            for f in bestand.offen
        ]
    return bestand, befund


# ── Abgleich gegen den tatsächlichen Bestand ─────────────


def abgleichen(bestand: Bestand, bezeichnungen: list[str]) -> dict:
    """Beide Richtungen zwischen Datei und abgerechneten Projekten.

    Die eine Richtung findet, was in der Datei fehlt — ein neu angelegtes
    Projekt, von dem niemand ihr erzählt hat. Die andere findet, was in der
    Datei steht und nicht mehr vorkommt; das ist kein Fehler, aber es sagt,
    welcher Eintrag ``ruhend`` werden könnte.

    Nur die erste Richtung ist gefährlich: ein Projekt ohne Vertrag wird nicht
    geprüft und sieht deshalb aus wie eines ohne Beanstandung.
    """
    ohne_vertrag = sorted({n for n in bezeichnungen if bestand.finden(n) is None})
    getroffen = {
        v.schluessel
        for n in bezeichnungen
        if (v := bestand.finden(n)) is not None
    }
    ohne_rechnung = sorted(
        v.schluessel for v in bestand.aktive() if v.schluessel not in getroffen
    )

    ergebnis: dict[str, Any] = {
        "geprueft": len(bezeichnungen),
        "zugeordnet": len(bezeichnungen) - len(ohne_vertrag),
    }
    if ohne_vertrag:
        ergebnis["ohne_vertrag"] = ohne_vertrag
    if ohne_rechnung:
        ergebnis["ohne_rechnung"] = ohne_rechnung
    return ergebnis


# ── Fixstunden herleiten ─────────────────────────────────


def vorschlagen(
    bestand: Bestand, deutungen: dict[str, Positionsdeutung]
) -> list[dict]:
    """Aus den gelesenen Positionen einen Vorschlag je Vertrag bilden.

    Die vereinbarten Stunden stehen heute nur im Positionstext. Sie von dort zu
    übernehmen wäre genau der alte Fehler; sie **vorzuschlagen** und bestätigen
    zu lassen ist der Weg, den auch der Kundenschlüssel geht: eine Maschine darf
    hinzufügen, nie ändern.

    Deshalb drei Ausgänge und keine stille Übernahme:

    ``neu``
        Die Datei sagt nichts, der Text schon. Zur Bestätigung vorgelegt.
    ``abweichend``
        Beide sagen etwas, und es ist nicht dasselbe. Das ist ein Befund und
        kein Vorschlag — hier wird nichts überschrieben.
    ``bestaetigt``
        Beide sagen dasselbe. Nichts zu tun, aber wert, gezählt zu werden.

    Die Übertragbarkeit lässt sich nicht aus einer Zahl ableiten, nur aus dem
    Vorhandensein einer Übertragszeile — und die fehlt im ersten Monat einer
    Vereinbarung auch dann, wenn übertragen wird. Der Vorschlag nennt sie
    deshalb als Beobachtung und nicht als Wert.
    """
    vorschlaege: list[dict] = []
    for schluessel, deutung in sorted(deutungen.items()):
        vertrag = bestand.vertraege.get(schluessel)
        if vertrag is None:
            continue
        gelesen = deutung.fix_stunden
        if gelesen is None:
            continue

        eintrag: dict[str, Any] = {
            "vertrag": schluessel,
            "bezeichnung": vertrag.bezeichnung,
            "fix_stunden_laut_rechnung": gelesen,
            "uebertragszeile_vorhanden": deutung.uebertrag_angabe is not None,
        }
        if vertrag.fix_stunden is None:
            eintrag["art"] = "neu"
            eintrag["frage"] = (
                f"Sind für «{vertrag.bezeichnung}» {gelesen:g} Stunden pro Monat "
                "vereinbart, und verfallen nicht verbrauchte Stunden?"
            )
        elif abs(vertrag.fix_stunden - gelesen) > 1e-9:
            eintrag["art"] = "abweichend"
            eintrag["fix_stunden_laut_vertrag"] = vertrag.fix_stunden
            eintrag["frage"] = (
                f"«{vertrag.bezeichnung}»: der Vertrag nennt "
                f"{vertrag.fix_stunden:g}h, die Rechnung {gelesen:g}h. "
                "Welche Angabe stimmt?"
            )
        else:
            eintrag["art"] = "bestaetigt"
        vorschlaege.append(eintrag)
    return vorschlaege
