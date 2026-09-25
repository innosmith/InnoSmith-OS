"""Was der Eingang nach draussen zeigt.

Die Warteliste liefert **pro Beleg** alles, was zur Entscheidung nötig ist --
Vorschlag, Herkunft und Abweichungen. Sonst müsste die Maske je Zeile
nachfragen, und bei 65 Belegen wären das 65 Abrufe für eine Liste.

Was sie **nicht** liefert, sind die gelesenen Einzelwerte. Die holt die
Korrekturmaske über ``/api/creditors/beleg/{beleg_id}`` -- eine Liste, die alle
Felder aller Belege trägt, ist keine Liste mehr.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class LieferantKandidat(BaseModel):
    schluessel: str
    anzeigename: str


class EingangsBeleg(BaseModel):
    """Ein Beleg in der Warteliste, samt Buchungsvorschlag."""

    id: UUID
    dateiname: str
    quelle: str
    eingang_am: datetime | None = None
    beleg_id: int | None = Field(
        default=None,
        description=(
            "Kennung im InvoiceInsight-Modul. Der Weg zu den gelesenen Werten "
            "und zur Originaldatei. Fehlt sie, ist der Beleg registriert, aber "
            "noch nicht gelesen."
        ),
    )

    lieferant_schluessel: str | None = None
    lieferant: str | None = None
    rechnungsnummer: str | None = None
    lieferant_bestaetigt: bool = Field(
        default=False,
        description=(
            "Ob die Erwartung zu diesem Lieferanten schon jemand geprüft hat. "
            "'false' heisst «aus der Historie vorgeschlagen» -- am 22.09.2026 "
            "trifft das auf alle 149 Einträge zu."
        ),
    )
    lieferant_kandidaten: list[LieferantKandidat] = Field(
        default_factory=list,
        description=(
            "Wenn der Absender mehrere Lieferanten meint -- «Google» für "
            "Workspace, Cloud, One, YouTube und Gemini. Dann ist am Beleg zu "
            "wählen, bevor es ein Konto gibt."
        ),
    )
    betrag: float | None = None
    betrag_chf: float | None = None
    waehrung: str | None = None
    datum: str | None = None
    produkt: str | None = None

    sollkonto: str | None = None
    sollkonto_herkunft: str | None = Field(
        default=None,
        description=(
            "'vorschlag' aus der Deklaration oder 'entscheid' vom Menschen. Nur "
            "eine Entscheidung darf die Deklaration fortschreiben, und nur bei "
            "einem Vorschlag ist eine Abweichung eine Frage statt eines Fehlers."
        ),
    )
    sollkonto_kandidaten: list[str] = Field(
        default_factory=list,
        description=(
            "Wenn das Konto an der Rechnung hängt und nicht am Lieferanten. Bei "
            "Hosttech 6512 gegen 4200. Dann gibt es bewusst keinen Vorschlag."
        ),
    )
    steuerbehandlung: str | None = None
    zahlweg: str | None = None

    leistung: str | None = None
    leistung_herkunft: str | None = Field(
        default=None,
        description="'lieferant' aus der Deklaration oder 'beleg', am Beleg entschieden.",
    )
    buchungstext: str | None = Field(
        default=None,
        description=(
            "So erscheint die Buchung in Bexio -- 'RapidAPI, Monatsabo 2026.09'. "
            "Fehlt er, fehlt ein Bestandteil; warum, steht in 'abweichungen'."
        ),
    )
    dateiname_ziel: str | None = Field(
        default=None,
        description="So heisst die Datei im Archiv -- 'RapidAPI Monatsabo 22.09.2026 KK.pdf'.",
    )

    freigegeben_am: datetime | None = None
    gebucht_am: datetime | None = None
    bexio_referenz: str | None = None
    abgelegt_am: datetime | None = None
    archiv_pfad: str | None = None
    nur_ablegen: bool = Field(
        default=False,
        description="Wird über den Sammelbeleg gebucht -- diese Rechnung wird nur abgelegt.",
    )

    zurueckgestellt: bool = False
    grund: str | None = None
    abweichungen: list[str] = Field(
        default_factory=list,
        description=(
            "Fragen, nicht Fehler. Eine Erwartung, die nicht zutrifft, wird von "
            "einer Antwort fortgeschrieben -- sie wird nicht überschrieben."
        ),
    )

    @property
    def entscheidbar(self) -> bool:
        return bool(self.sollkonto)


class SammelMonat(BaseModel):
    """Nutzungsrechnungen, die nicht einzeln entschieden werden -- sie warten
    auf den Sammelbeleg ihres Kalendermonats."""

    lieferant_schluessel: str
    anzeigename: str
    jahr: int
    monat: int
    bezeichnung: str = Field(description="«September 2026».")
    anzahl: int
    betrag: float | None = Field(
        default=None, description="Summe in der Rechnungswährung; fehlt, wenn eine nicht gelesen ist."
    )
    waehrung: str | None = None
    abgeschlossen: bool = Field(
        description="Der Monat ist vorbei. Vorher gibt es weder alle Rechnungen noch den BAZG-Kurs."
    )


class SammelPosition(BaseModel):
    beleg_id: UUID
    dateiname: str
    nummer: str | None = None
    datum: date | None = None
    betrag: float | None = None


class Sammelvorschau(BaseModel):
    """Was der Sammelbeleg eines Monats bucht, und ob er darf -- ohne zu schreiben.

    Die Beträge stehen, wie Bexio sie rechnet: Total mal Kurs und Bezugsteuer
    auf das Total, je einmal gerundet.
    """

    lieferant_schluessel: str
    anzeigename: str
    jahr: int
    monat: int
    bezeichnung: str
    nachtrag: bool = Field(description="Der Monat ist schon gebucht; das hier kam danach.")
    positionen: list[SammelPosition] = Field(default_factory=list)
    waehrung: str | None = None
    betrag: float | None = None
    bezugsteuer: float | None = None
    kurs: float | None = Field(default=None, description="BAZG-Monatsmittel, CHF je Einheit.")
    betrag_chf: float | None = None
    bezugsteuer_chf: float | None = None
    buchbar: bool
    bereit: bool = Field(
        description=(
            "Nichts verstösst. Ohne Vollständigkeitsbeleg braucht die Freigabe "
            "trotzdem die Bestätigung, dass der Download nach Monatsende lief."
        )
    )
    vollstaendig: bool = Field(
        description=(
            "Die Nummern sind lückenlos, und die nächste Rechnung ist aus dem "
            "Folgemonat bekannt -- dann kann im Monat keine mehr fehlen."
        )
    )
    vollstaendig_grund: str
    verstoesse: list[str] = Field(default_factory=list)
    hinweise: list[str] = Field(default_factory=list)
    luecken: list[str] = Field(default_factory=list)
    datum: date | None = None
    sollkonto: str | None = None
    habenkonto: str | None = None
    steuercode: str | None = None
    buchungstext: str | None = None
    referenz: str | None = None
    ablageziel: str | None = None


class SammelFreigabe(BaseModel):
    """Die Freigabe eines Sammelbelegs."""

    vollstaendig_bestaetigt: bool = Field(
        default=False,
        description=(
            "Der Download nach Monatsende ist gelaufen und synchronisiert. Nur "
            "nötig, wo die Nummernfolge die Vollständigkeit nicht selbst belegt."
        ),
    )


class Warteliste(BaseModel):
    """Die Warteliste samt dem, was beim Abgleich auffiel."""

    belege: list[EingangsBeleg]
    sammelbeleg_wartet: list[SammelMonat] = Field(
        default_factory=list,
        description=(
            "Nutzungsrechnungen, je Kalendermonat gezählt. Sie stehen nicht in "
            "'belege': ihre Buchung ist der Sammelbeleg, und 30 Freigaben im "
            "Monat für nichts als die Ablage wären Arbeit ohne Entscheidung."
        ),
    )
    zu_buchen: list[EingangsBeleg] = Field(
        default_factory=list,
        description=(
            "Freigegeben, aber noch nicht gebucht oder noch nicht abgelegt. "
            "Eine Buchung ohne Ablage ist nicht fertig."
        ),
    )
    offen: int = Field(description="Was auf eine Entscheidung wartet.")
    ohne_konto: int = Field(
        description="Davon ohne Kontovorschlag -- diese lassen sich nicht freigeben."
    )
    zurueckgestellt: int = 0
    dubletten: int = Field(
        default=0,
        description=(
            "Beim letzten Abgleich: andere Dateien einer schon erfassten Rechnung "
            "(gleicher Lieferant, gleiche Nummer). Nicht aufgenommen; welche, "
            "steht in 'hinweise'."
        ),
    )
    schon_im_archiv: list[str] = Field(
        default_factory=list,
        description=(
            "Beim letzten Abgleich: Dateien im Eingang, deren Rechnung schon im "
            "Archiv liegt. Nicht aufgenommen -- sie können gelöscht werden."
        ),
    )
    fort: int = Field(
        default=0,
        description="Beim letzten Abgleich: Belege im Eingang, deren Datei in OneDrive fort ist.",
    )
    von_hand_abgelegt: int = Field(
        default=0,
        description=(
            "Beim letzten Abgleich: wartende Belege, die inzwischen von Hand im "
            "Archiv liegen -- noch auf dem alten Weg gebucht."
        ),
    )
    stand: datetime | None = None
    hinweise: list[str] = Field(
        default_factory=list,
        description=(
            "Was am Rand fehlt. Ein Beleg, der im Eingang liegt und nirgends "
            "erscheint, sieht ohne diese Zeilen aus wie keiner."
        ),
    )


class Freigabe(BaseModel):
    """Die Freigabe eines Belegs -- mit dem Konto, auf das gebucht wird."""

    sollkonto: str | None = Field(
        default=None,
        description=(
            "Nur nötig, wenn das Konto noch offen ist oder vom Vorschlag "
            "abweicht. Wird es gesetzt, gilt es als Entscheidung und übersteht "
            "jeden weiteren Abgleich."
        ),
    )
    steuerbehandlung: str | None = None
    zahlweg: str | None = None
    leistung: str | None = Field(
        default=None,
        description=(
            "Nur nötig, wenn der Lieferant keine Vorgabe trägt oder diese "
            "Rechnung etwas anderes verrechnet -- 'API Usage' statt 'Monatsabo'."
        ),
    )


class LieferantWahl(BaseModel):
    """Welcher Lieferant hinter diesem Beleg steht, wo das Modul es nicht weiss."""

    schluessel: str = Field(min_length=1, max_length=80)


class Buchungspruefung(BaseModel):
    """Die Normprüfung eines freigegebenen Belegs -- live gegen Bexio und BAZG.

    Die Werte des Plans stehen nur, wenn nichts verstösst: ein halber Plan
    sähe buchbar aus.
    """

    buchbar: bool
    verstoesse: list[str] = Field(
        default_factory=list, description="Halten die Buchung an."
    )
    hinweise: list[str] = Field(
        default_factory=list, description="Für die Treuhänderin sichtbar, halten nichts an."
    )
    datum: date | None = None
    sollkonto: str | None = None
    habenkonto: str | None = None
    betrag: float | None = None
    waehrung: str | None = None
    kurs: float | None = Field(default=None, description="BAZG-Monatsmittel, CHF je Einheit.")
    betrag_chf: float | None = None
    steuercode: str | None = None
    buchungstext: str | None = None
    referenz: str | None = None
    ablageziel: str | None = Field(
        default=None, description="Relativ zu Finanzen/Kreditoren -- 'RapidAPI/2026/…pdf'."
    )


class Buchungsbericht(BaseModel):
    """Was beim Freigeben, Buchen und Ablegen geschehen ist."""

    freigegeben: bool = False
    gebucht: bool
    bexio_referenz: str | None = None
    beleg_angehaengt: bool = False
    journal_zeilen: int = 0
    abgelegt: str | None = None
    meldungen: list[str] = Field(
        default_factory=list,
        description="Was nicht ganz geklappt hat -- die Buchung steht trotzdem.",
    )


class LieferantBestaetigen(BaseModel):
    """Die Erwartung zu einem Lieferanten prüfen und festschreiben."""

    sollkonto: str | None = Field(
        default=None,
        description=(
            "Nur nötig, wenn noch keines deklariert ist oder das bisherige "
            "falsch war. Ein genanntes Konto weicht den Vorschlag aus und löst "
            "eine Kandidatenliste auf -- ein Mensch darf hier entscheiden, die "
            "Maschine nicht."
        ),
    )


class Zuruecklegen(BaseModel):
    """Ein Beleg aus der Warteliste nehmen -- nie ohne Grund.

    Ohne Begründung ist eine Ausnahme in drei Wochen nicht mehr
    nachvollziehbar, und der Beleg sieht aus wie vergessen statt wie
    entschieden.
    """

    grund: str = Field(min_length=3)
