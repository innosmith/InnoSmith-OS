"""Kreditorenlieferanten -- was wir bei einer eingehenden Rechnung erwarten.

Die Deklaration steht in ``docs/kreditorenlieferanten.yaml`` und beantwortet
vier Fragen je Lieferant: in welchen Archivordner der Beleg gehoert, auf welches
Konto er laeuft, ob Bezugssteuer anfaellt und in welchem Takt etwas kommt.

## Warum eine Erwartung und keine Regel

Am 21.09.2026 gemessen: bei **87 %** der Kartenbuchungen 2026 hat der Lieferant
historisch immer dasselbe Sollkonto. Dort traegt ein Vorschlag. Bei Hosttech und
Metanet traegt er nicht -- dieselbe Domain ist einmal eigener Aufwand (6512) und
einmal weiterverrechnete Leistung (4200), und was zutrifft, weiss nur der
Mensch. Solche Eintraege fuehren ``sollkonto_kandidaten`` statt ``sollkonto``.
Ein einzelner erwarteter Wert waere dort Scheingenauigkeit.

## Die Steuerbehandlung steht auf der Rechnung, nicht im Journal

Erst war sie aus dem Journal erschlossen: keine Gegenzeile auf 2203 also
``unbekannt``. Das war zu wenig. «Nicht gebucht» und «faellt nicht an» sind
zwei Aussagen, und aus dem Fehlen einer Buchung folgt die zweite nicht -- wer
sie trotzdem als Erwartung hinschreibt, laesst den Abgleich genau den Fehler
absegnen, den er finden soll.

Entschieden wird deshalb an den Rechnungen, und das ist entscheidbar: das
Modul liest ``vat_amount`` zu 1'133 Belegen. Am 21.09.2026 gemessen, und die
Gegenprobe traegt das Verfahren -- Cursor 0 von 68 Rechnungen mit Schweizer
MWST, Render 0 von 9, Toggl 0 von 8, waehrend OpenAI 28 von 28 und Anthropic
4 von 4 sie ausweisen. Beide sind in der Schweiz registriert und verrechnen
8.1 %; **es faellt keine Bezugssteuer an, die Buchung ist richtig.** Dass das
Feld gelesen und nicht gerechnet ist, zeigt Microsoft mit 7.7 **und** 8.1,
also dem Satzwechsel per 01.01.2024.

Die ``supplier_uid`` traegt hier ausdruecklich **nichts**. Das Modell erfindet
plausible Nummern, wenn es keine lesen kann (``CHE-123.456.789`` bei der
Steuerverwaltung), und fuer die Frage braucht es sie ohnehin nicht: ob MWST
ausgewiesen ist, steht im Betrag.

Vier Werte, weil drei zu wenig waren:

- ``bezugssteuer`` -- auslaendischer Lieferant, keine Schweizer MWST
- ``inland_mwst`` -- MWST ausgewiesen, Vorsteuer abziehbar
- ``ohne_mwst`` -- inlaendisch und von der Steuer befreit. Die Ausgleichskasse
  weist auf 77 von 77 Rechnungen keine MWST aus, VZ auf 17 von 17. Das ist
  kein Widerspruch und keine Bezugssteuer, sondern Art. 21 MWSTG. Ohne diesen
  Wert waeren 4 steuerbefreite Stellen als offene Frage gelandet.
- ``unbekannt`` -- die Rechnungen widersprechen sich oder es gibt keine. Dann
  steht die Frage im Wortlaut unter ``offen``, nicht als Etikett am Eintrag.

Und der Unterschied, der die Fragenliste von 15 auf das Echte verkuerzt:
``vat_amount = 0`` ist eine gelesene Null, ``vat_amount = NULL`` ist
Schweigen. Nur die Null ist ein Beweis. Google Cloud hat drei Belege ohne
gelesenen Betrag -- daraus «keine MWST» zu schliessen, waere dieselbe
Verwechslung wie oben, eine Stufe tiefer.

## Zwei Waechter

1. **Tote Zuordnung** -- eine Journalschreibweise ohne Treffer sieht aus wie eine
   Zuordnung und traegt keine. Wird beim Erzeugen geprueft und gemeldet.
2. **Fehlende Deklaration** -- ein aktiver Lieferant ohne Kontoerwartung faellt
   im Vorschlagsverfahren still durch. ``unvollstaendig()`` zaehlt sie, aber nur
   die aktiven: eine Warnliste ueber 83 stille Lieferanten waere Rauschen und
   wuerde nicht gelesen.

Geschrieben, nicht editiert: eine Maschine darf **hinzufuegen, nie aendern oder
entfernen**. ``bestaetigt: false`` heisst «aus der Historie vorgeschlagen, von
niemandem geprueft».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

def _datei_finden(name: str = "kreditorenlieferanten.yaml") -> Path:
    """Wo die gepflegte Datei liegt -- im Arbeitsbaum wie im Container.

    Die Verzeichniskette aufwaerts statt einer festen Tiefe: im Backend-Image
    liegt das Modul unter ``/app/app/services`` und hat dort gar keine fuenfte
    Ebene. Dieselbe Loesung wie in ``debitorenvertraege``.
    """
    hier = Path(__file__).resolve()
    kandidaten = [eltern / "docs" / name for eltern in hier.parents]
    for pfad in kandidaten:
        if pfad.exists():
            return pfad
    return kandidaten[min(4, len(kandidaten) - 1)]

Steuerbehandlung = Literal["bezugssteuer", "inland_mwst", "ohne_mwst", "unbekannt"]
Zahlweg = Literal["karte", "rechnung", "bank_direkt"]
Rhythmus = Literal["monatlich", "unregelmaessig", "jaehrlich", "unbekannt"]

# Bexio stellt jeder Buchung aus einer Lieferantenrechnung diesen Text voran.
LIEFERANTENRECHNUNG = "(Lieferantenrechnung erstellt)"

ZYKLEN: frozenset[str] = frozenset(
    {"MONTHLY", "YEARLY", "QUARTERLY", "SEMI_ANNUALLY", "ONE_TIME", "USAGE_BASED", "UNKNOWN"}
)
"""Die Werte, die das Modul in ``abrechnungszyklus`` schreibt (``VALID_CYCLES`` im Kern)."""

BEHANDLUNGEN: frozenset[str] = frozenset(
    {"bezugssteuer", "inland_mwst", "ohne_mwst", "unbekannt"}
)
ZAHLWEGE: frozenset[str] = frozenset({"karte", "rechnung", "bank_direkt"})
RHYTHMEN: frozenset[str] = frozenset(
    {"monatlich", "unregelmaessig", "jaehrlich", "unbekannt"}
)
BUCHUNGSARTEN: frozenset[str] = frozenset({"einzeln", "sammelbeleg"})


@dataclass(frozen=True)
class Steuerzeile:
    """Eine Steuerbehandlung ab einem Zeitpunkt.

    Mehrere Zeilen, weil sich das Verhalten eines Lieferanten aendern kann: wer
    heute keine Schweizer MWST ausweist, kann es naechstes Jahr tun. Die alte
    Zeile wird dann nicht ueberschrieben, sondern von einer neuen mit spaeterem
    ``ab`` abgeloest -- die frueheren Buchungen bleiben damit erklaerbar.
    """

    ab: date | None
    behandlung: Steuerbehandlung


@dataclass(frozen=True)
class Lieferant:
    schluessel: str
    ordner: str
    nebenordner: tuple[str, ...] = ()
    sollkonto: str | None = None
    sollkonto_kandidaten: tuple[str, ...] = ()
    regel: str | None = None
    zahlweg: tuple[Zahlweg, ...] = ()
    steuer: tuple[Steuerzeile, ...] = ()
    rhythmus: Rhythmus = "unbekannt"
    aktiv: bool = False
    bestaetigt: bool = False
    belege_bis: int | None = None
    leistung: str | None = None
    """Was üblicherweise verrechnet wird -- «Monatsabo», «Usage». Steht im
    Buchungstext und im Dateinamen. Fehlt sie, hängt die Leistung an der
    Rechnung (Hosttech: jede Domain einzeln) und wird dort erfasst."""
    name: str | None = None
    """Nur, wo der Ordnername in Buchung und Dateiname nicht taugt."""
    schreibweisen: tuple[str, ...] = ()
    """Womit eine Buchung dieses Lieferanten im Journal beginnt -- ``Rapid API``,
    ``Open AI``, ``Antrophic``. Die Altlast aus der Zeit, als von Hand gebucht
    wurde. Was TaskPilot bucht, beginnt mit ``{anzeigename},`` und braucht
    keinen Eintrag."""
    buchung: str = "einzeln"
    """``sammelbeleg``: die Rechnungen werden nur abgelegt, gebucht wird der
    Monatsbeleg, der sie zusammenfasst (Cursor, 30 Rechnungen im Monat)."""
    einzeln_bei_zyklus: tuple[str, ...] = ()
    """Bei ``sammelbeleg``: die Abrechnungszyklen, die trotzdem einzeln gebucht
    werden -- bei Cursor Jahres- und Monatsabo, wie die Treuhänderin es hielt."""
    aufteilen: tuple[str, ...] = ()
    """Ein Absender, hinter dem mehrere Lieferanten stehen. Auf allen
    Google-Rechnungen steht «Google», ob Workspace, Cloud oder YouTube --
    der Dienst ist am Absender nicht ablesbar. Ein solcher Eintrag wird nie
    gebucht; er nennt die Lieferanten, unter denen am Beleg zu wählen ist."""

    @property
    def anzeigename(self) -> str:
        """Der Name vorne in Buchungstext und Dateiname."""
        return self.name or self.ordner

    def bucht_als(self, beschreibung: str | None) -> bool:
        """Ob eine Journalzeile zu diesem Lieferanten gehört.

        Über den Anfang und nicht über Enthaltensein: ``Domain`` steht bei
        Metanet am Anfang und bei Hosttech mitten im Text.
        """
        text = (beschreibung or "").strip()
        if text.startswith(LIEFERANTENRECHNUNG):
            text = text[len(LIEFERANTENRECHNUNG):].strip()
        return text.startswith(f"{self.anzeigename},") or any(
            text.startswith(s) for s in self.schreibweisen
        )

    def sammelt(self, abrechnungszyklus: str | None, dokumenttyp: str | None = None) -> bool:
        """Ob eine Rechnung in den Monatssammelbeleg gehört statt einzeln gebucht zu werden.

        Alles ausser den deklarierten Zyklen (``einzeln_bei_zyklus``) und
        Gutschriften. Das Cursor-Jahresabo vom 26.04.2026 über 1'920 USD und
        das Monatsabo von Januar bis März 2026 hat die Treuhänderin einzeln
        gebucht. Eine Gutschrift mindert, und der Sammelbeleg addiert nur.

        Ausgeschlossen wird, statt eingeschlossen: was das Modul als
        Nachbelastung liest (0139, 55.12 USD, ``ONE_TIME``), ist trotzdem
        Nutzung und stand im alten Sammelbeleg. Liest das Modul einen Zyklus
        falsch, landet die Rechnung sichtbar auf dem anderen Weg -- in der
        Einzelliste oder in der Positionsliste des Sammelbelegs.
        """
        return (
            self.buchung == "sammelbeleg"
            and abrechnungszyklus not in self.einzeln_bei_zyklus
            and str(dokumenttyp or "").upper() != "GUTSCHRIFT"
        )

    @property
    def entscheid_je_rechnung(self) -> bool:
        """Ob das Konto nicht vorhersagbar ist und gefragt werden muss."""
        return self.sollkonto is None and len(self.sollkonto_kandidaten) > 1

    @property
    def deklariert(self) -> bool:
        """Ob genug dasteht, um einen Buchungsvorschlag zu bauen."""
        return bool(self.sollkonto or self.sollkonto_kandidaten)

    def steuer_am(self, tag: date) -> Steuerbehandlung:
        """Die Behandlung, die an diesem Tag galt.

        Zeilen ohne ``ab`` gelten von Anfang an. Gibt es mehrere, gewinnt die
        juengste, deren ``ab`` nicht in der Zukunft liegt.
        """
        passend = [z for z in self.steuer if z.ab is None or z.ab <= tag]
        if not passend:
            return "unbekannt"
        return max(passend, key=lambda z: z.ab or date.min).behandlung


@dataclass
class Bestand:
    """Alle Lieferanten samt dem, was beim Laden auffiel."""

    lieferanten: dict[str, Lieferant] = field(default_factory=dict)
    offen: tuple[str, ...] = ()
    version: int = 0
    stand: date | None = None
    maengel: tuple[str, ...] = ()

    def nach_ordner(self, ordner: str) -> Lieferant | None:
        """Findet den Lieferanten zu einem Archivordnernamen.

        Woertlich, nicht aehnlich: entweder der Name steht so in der Datei, oder
        der Ordner gilt als nicht zugeordnet.
        """
        return next(
            (l for l in self.lieferanten.values() if l.ordner == ordner), None
        )

    def unvollstaendig(self) -> list[Lieferant]:
        """Aktive Lieferanten ohne Kontoerwartung.

        Nur die aktiven, weil eine Meldung ueber 83 stille Lieferanten aus
        Rauschen bestuende und damit wie gar keine wirkte. Ein Verteiler wie
        ``google`` fehlt hier mit Absicht: er hat kein Konto, weil er keines
        haben darf.
        """
        return sorted(
            (
                l for l in self.lieferanten.values()
                if l.aktiv and not l.deklariert and not l.aufteilen
            ),
            key=lambda l: l.schluessel,
        )

    def unbestaetigt(self) -> list[Lieferant]:
        """Aktive Lieferanten, deren Vorschlag noch niemand geprueft hat."""
        return sorted(
            (l for l in self.lieferanten.values() if l.aktiv and not l.bestaetigt),
            key=lambda l: l.schluessel,
        )


def _als_datum(wert: object) -> date | None:
    if wert is None:
        return None
    if isinstance(wert, date):
        return wert
    return date.fromisoformat(str(wert))


def _steuer_lesen(roh: object, schluessel: str, maengel: list[str]) -> tuple[Steuerzeile, ...]:
    if not isinstance(roh, list):
        return ()
    zeilen: list[Steuerzeile] = []
    for eintrag in roh:
        if not isinstance(eintrag, dict):
            continue
        behandlung = str(eintrag.get("behandlung", "unbekannt"))
        if behandlung not in BEHANDLUNGEN:
            maengel.append(
                f"{schluessel}: unbekannte Steuerbehandlung {behandlung!r} — "
                f"erlaubt sind {sorted(BEHANDLUNGEN)}"
            )
            continue
        zeilen.append(Steuerzeile(_als_datum(eintrag.get("ab")), behandlung))  # type: ignore[arg-type]
    return tuple(zeilen)


def laden(pfad: Path | None = None) -> Bestand:
    """Liest die Deklaration und prueft sie gegen die erlaubten Werte.

    Ein unbekannter Wert wird **verworfen und gemeldet**, nicht stillschweigend
    uebernommen -- sonst entstuende eine Erwartung, gegen die nie etwas passt.
    """
    ziel = pfad or _datei_finden()
    try:
        daten = yaml.safe_load(ziel.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("Kreditorenlieferanten: %s fehlt — kein Lieferant bekannt", ziel)
        return Bestand()
    except yaml.YAMLError as exc:
        logger.warning("Kreditorenlieferanten: %s ist nicht lesbar: %s", ziel, exc)
        return Bestand()
    if not isinstance(daten, dict):
        return Bestand()
    maengel: list[str] = []
    lieferanten: dict[str, Lieferant] = {}

    for roh in daten.get("lieferanten") or []:
        schluessel = str(roh.get("schluessel") or "").strip()
        ordner = str(roh.get("ordner") or "").strip()
        if not schluessel or not ordner:
            maengel.append(f"Eintrag ohne Schlüssel oder Ordner: {roh!r}")
            continue
        if schluessel in lieferanten:
            maengel.append(f"{schluessel}: doppelter Schlüssel — der zweite wird verworfen")
            continue

        wege = roh.get("zahlweg")
        wege = [wege] if isinstance(wege, str) else list(wege or [])
        unerlaubt = [w for w in wege if w not in ZAHLWEGE]
        if unerlaubt:
            maengel.append(f"{schluessel}: unbekannter Zahlweg {unerlaubt}")
            wege = [w for w in wege if w in ZAHLWEGE]

        rhythmus = str(roh.get("rhythmus") or "unbekannt")
        if rhythmus not in RHYTHMEN:
            maengel.append(f"{schluessel}: unbekannter Rhythmus {rhythmus!r}")
            rhythmus = "unbekannt"

        buchung = str(roh.get("buchung") or "einzeln")
        if buchung not in BUCHUNGSARTEN:
            maengel.append(f"{schluessel}: unbekannte Buchungsart {buchung!r} — gilt als einzeln")
            buchung = "einzeln"

        einzeln = [str(z).strip().upper() for z in (roh.get("einzeln_bei_zyklus") or [])]
        unbekannt = [z for z in einzeln if z not in ZYKLEN]
        if unbekannt:
            maengel.append(
                f"{schluessel}: einzeln_bei_zyklus nennt {unbekannt} — "
                f"das Modul kennt nur {sorted(ZYKLEN)}"
            )
            einzeln = [z for z in einzeln if z in ZYKLEN]
        if einzeln and buchung != "sammelbeleg":
            maengel.append(f"{schluessel}: einzeln_bei_zyklus wirkt nur bei buchung: sammelbeleg")

        konto = roh.get("sollkonto")
        kandidaten = tuple(str(k) for k in (roh.get("sollkonto_kandidaten") or []))
        if konto and kandidaten:
            maengel.append(
                f"{schluessel}: sollkonto und sollkonto_kandidaten zugleich — "
                f"das eine behauptet Eindeutigkeit, das andere widerspricht ihr"
            )

        lieferanten[schluessel] = Lieferant(
            schluessel=schluessel,
            ordner=ordner,
            nebenordner=tuple(roh.get("nebenordner") or []),
            sollkonto=str(konto) if konto else None,
            sollkonto_kandidaten=kandidaten,
            regel=roh.get("regel"),
            zahlweg=tuple(wege),  # type: ignore[arg-type]
            steuer=_steuer_lesen(roh.get("steuer"), schluessel, maengel),
            rhythmus=rhythmus,  # type: ignore[arg-type]
            aktiv=bool(roh.get("aktiv")),
            bestaetigt=bool(roh.get("bestaetigt")),
            belege_bis=roh.get("belege_bis"),
            leistung=str(roh["leistung"]).strip() or None if roh.get("leistung") else None,
            name=str(roh["name"]).strip() or None if roh.get("name") else None,
            schreibweisen=tuple(
                str(s) for s in (roh.get("schreibweisen") or []) if str(s).strip()
            ),
            buchung=buchung,
            einzeln_bei_zyklus=tuple(einzeln),
            aufteilen=tuple(str(s) for s in (roh.get("aufteilen") or []) if str(s).strip()),
        )

    for l in lieferanten.values():
        tot = [s for s in l.aufteilen if s not in lieferanten or lieferanten[s].aufteilen]
        if tot:
            maengel.append(
                f"{l.schluessel}: aufteilen nennt {tot}, die es nicht als buchbaren "
                f"Lieferanten gibt — die Wahl am Beleg hätte kein Ziel"
            )

    doppelte = _doppelte_ordner(lieferanten)
    maengel.extend(
        f"Ordner {o!r} ist mehreren Lieferanten zugeordnet: {ks} — "
        f"die Ablage wüsste nicht, welcher gemeint ist"
        for o, ks in doppelte.items()
    )

    for hinweis in maengel:
        logger.warning("kreditorenlieferanten: %s", hinweis)

    return Bestand(
        lieferanten=lieferanten,
        offen=tuple(daten.get("offen") or []),
        version=int(daten.get("version") or 0),
        stand=_als_datum(daten.get("stand")),
        maengel=tuple(maengel),
    )


def _doppelte_ordner(lieferanten: dict[str, Lieferant]) -> dict[str, list[str]]:
    nach: dict[str, list[str]] = {}
    for l in lieferanten.values():
        nach.setdefault(l.ordner, []).append(l.schluessel)
    return {o: ks for o, ks in nach.items() if len(ks) > 1}


# ── Bestaetigen ──────────────────────────────────────────────────────


def _block_finden(zeilen: list[str], schluessel: str) -> tuple[int, int]:
    """Anfang und Ende des Eintrags. ``ValueError``, wenn es ihn nicht gibt."""
    anfang = next(
        (
            i
            for i, z in enumerate(zeilen)
            if z.strip() == f"- schluessel: {schluessel}" and z.startswith("  - ")
        ),
        None,
    )
    if anfang is None:
        raise ValueError(f"Lieferant {schluessel!r} steht nicht in der Deklaration")
    ende = next(
        (
            i
            for i in range(anfang + 1, len(zeilen))
            if zeilen[i].startswith("  - schluessel:")
            or (zeilen[i] and not zeilen[i].startswith((" ", "#")))
        ),
        len(zeilen),
    )
    return anfang, ende


def bestaetigen(
    schluessel: str,
    *,
    sollkonto: str | None = None,
    durch: str = "",
    pfad: Path | None = None,
) -> Lieferant:
    """Setzt ``bestaetigt: true`` -- und, wenn genannt, das entschiedene Konto.

    **Zeilengenau geaendert, nicht neu ausgegeben.** ``yaml.safe_dump`` wuerde
    jeden Kommentar wegwerfen, und die Kommentare sind hier die Beweislage:
    ``sollkonto: "6570"   # einstimmig, 48 Buchungen`` sagt, woher die Erwartung
    kommt. Ohne sie bliebe eine Zahl ohne Herkunft, und die naechste Pruefung
    muesste die Messung wiederholen. Ausserdem bleibt der Diff bei einer Zeile,
    und genau das ist der Zweck dieser Datei.

    Ein Mensch **darf** hier aendern -- die Regel «nur hinzufuegen» bindet die
    Maschine, nicht ihn. Deshalb weicht ein entschiedenes Konto den bisherigen
    Vorschlag aus, und eine Kandidatenliste wird auskommentiert statt geloescht:
    stehen beide, widerspricht die Datei sich selbst, und ``laden()`` meldet das
    zu Recht als Mangel.
    """
    ziel = pfad or _datei_finden()
    zeilen = ziel.read_text(encoding="utf-8").splitlines()
    anfang, ende = _block_finden(zeilen, schluessel)
    block = zeilen[anfang:ende]
    heute = date.today().isoformat()
    wer = f" durch {durch}" if durch else ""

    if sollkonto:
        neu = f'    sollkonto: "{sollkonto}"   # entschieden {heute}{wer}'
        for i, z in enumerate(block):
            if z.strip().startswith("sollkonto:"):
                block[i] = neu
                break
        else:
            block.insert(1, neu)
        for i, z in enumerate(block):
            if z.strip().startswith("sollkonto_kandidaten:"):
                # Auskommentiert, nicht entfernt: die Kandidaten waren die
                # gemessene Lage, und dass hier entschieden wurde, ist nur mit
                # ihnen verstaendlich.
                block[i] = f"  # {z.strip()}   # entschieden {heute}{wer}"

    for i, z in enumerate(block):
        if z.strip().startswith("bestaetigt:"):
            block[i] = "    bestaetigt: true"
            break
    else:
        block.append("    bestaetigt: true")

    _atomar_schreiben(ziel, zeilen[:anfang] + block + zeilen[ende:])

    bestand = laden(ziel)
    eintrag = bestand.lieferanten.get(schluessel)
    if eintrag is None:
        raise ValueError(
            f"{schluessel!r} war nach dem Schreiben nicht mehr lesbar — "
            f"die Datei wurde nicht verändert"
        )
    logger.info(
        "Kreditorenlieferant bestätigt: %s, Konto %s%s",
        schluessel, eintrag.sollkonto or "offen", wer,
    )
    return eintrag


ERGAENZBAR: frozenset[str] = frozenset({"leistung", "name", "schreibweisen", "buchung"})


def ergaenzen(
    werte: dict[str, str] | dict[str, list[str]],
    *,
    feld: str,
    kommentare: dict[str, str] | None = None,
    pfad: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Trägt ein Feld bei mehreren Lieferanten ein -- nur, wo es noch fehlt.

    Die Maschine darf hinzufügen, nie ändern: steht das Feld schon, bleibt es,
    auch wenn der neue Wert anders lautet. Es könnte eine Entscheidung sein.
    Liefert ``(eingetragen, übergangen)``; ein unbekannter Schlüssel ist ein
    ``ValueError`` und die Datei bleibt unverändert.
    """
    if feld not in ERGAENZBAR:
        raise ValueError(f"{feld!r} ist nicht ergänzbar — erlaubt sind {sorted(ERGAENZBAR)}")
    ziel = pfad or _datei_finden()
    zeilen = ziel.read_text(encoding="utf-8").splitlines()
    kommentare = kommentare or {}
    eingetragen: list[str] = []
    uebergangen: list[str] = []

    for schluessel, wert in werte.items():
        anfang, ende = _block_finden(zeilen, schluessel)
        if any(z.strip().startswith(f"{feld}:") for z in zeilen[anfang:ende]):
            uebergangen.append(schluessel)
            continue
        # Unmittelbar unter dem Ordner, damit Leistung und Name beim Lesen
        # neben dem stehen, woraus der Dateiname entsteht.
        nach = next(
            (i for i in range(anfang, ende) if zeilen[i].strip().startswith("ordner:")),
            anfang,
        )
        notiz = f"   # {kommentare[schluessel]}" if kommentare.get(schluessel) else ""
        zeilen.insert(nach + 1, f"    {feld}: {_yaml_text(wert)}{notiz}")
        eingetragen.append(schluessel)

    if eingetragen:
        _atomar_schreiben(ziel, zeilen)
        bestand = laden(ziel)
        fehlt = [s for s in eingetragen if not getattr(bestand.lieferanten.get(s), feld, None)]
        if fehlt:
            raise ValueError(f"Nach dem Schreiben nicht lesbar: {fehlt}")
    return eingetragen, uebergangen


def _yaml_text(wert: str | list[str]) -> str:
    """Als JSON geschrieben, das auch YAML ist: «.ai Jahresabo» und «2026» bleiben
    Text, und eine Liste steht auf einer Zeile."""
    import json

    return json.dumps(wert, ensure_ascii=False)


def _atomar_schreiben(ziel: Path, zeilen: list[str]) -> None:
    """Die Datei ersetzen, damit ein Leser nie eine halbe Datei sieht.

    Der Abgleich laeuft nebenher und liest dieselbe Datei; ein Schreiben in
    Teilen liesse ihn eine Deklaration ohne die halbe Lieferantenliste sehen --
    und das saehe aus wie ein geloeschter Bestand, nicht wie ein Zwischenstand.
    """
    import os

    vorlaeufig = ziel.with_suffix(".yaml.neu")
    vorlaeufig.write_text("\n".join(zeilen).rstrip() + "\n", encoding="utf-8")
    os.replace(vorlaeufig, ziel)
