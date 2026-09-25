"""Die Norm für Buchungstext und Dateiname eines Kreditorenbelegs.

Festgelegt am 25.09.2026 mit der Treuhänderin im Blick: sie muss auf einen Blick
sehen, ob eine Buchung zur Periode und zum Beleg passt. Das Journal führte bis
dahin für denselben Lieferanten vier Schreibweisen -- ``Rapid API Monatsabo``,
`` Rapid API Monatsabo `` mit Rand, ``RapidAPI Abo`` --, und das Archiv
``Monatsabo`` neben ``Monats-Abo`` und ``Abo``. Eine Norm, die niemand
ausrechnet, sondern die eine Funktion erzeugt, hat diese Varianten nicht.

* Buchungstext: ``Name, Leistung JJJJ.MM`` -- ``RapidAPI, Monatsabo 2026.09``
* Dateiname: ``Name Leistung TT.MM.JJJJ[ KK].pdf`` --
  ``RapidAPI Monatsabo 22.09.2026 KK.pdf``

Das Datum ist in beiden das **Rechnungsdatum**. ``KK`` steht genau dann, wenn
mit der Karte bezahlt wurde; so hiess es schon in der Konvention von
``K2_prepareInvoices``, und die Treuhänderin liest daraus das Gegenkonto 2120.
"""

from __future__ import annotations

from datetime import date

# Die Schreibweise ist eine Entscheidung vom 25.09.2026, keine Erschliessung:
# zusammengeschrieben, weil das die häufigste Form im Archiv war.
_SCHREIBWEISE = {"Monats-Abo": "Monatsabo", "Jahres-Abo": "Jahresabo"}

# Was OneDrive in einem Dateinamen nicht annimmt. Ein Schrägstrich legte
# ausserdem still einen Unterordner an.
_VERBOTEN = set('"*:<>?/\\|')


class NormVerletzt(ValueError):
    """Ein Bestandteil fehlt oder taugt nicht für die Norm."""


def leistung_normieren(leistung: str | None) -> str | None:
    """Rand weg, doppelte Leerzeichen weg, Abo-Schreibweise angeglichen."""
    if not leistung:
        return None
    text = " ".join(leistung.split())
    for alt, neu in _SCHREIBWEISE.items():
        text = text.replace(alt, neu)
    return text or None


def _pruefen(name: str | None, leistung: str | None) -> tuple[str, str]:
    name = " ".join((name or "").split())
    leistung = leistung_normieren(leistung)
    if not name:
        raise NormVerletzt("Ohne Lieferantenname gibt es weder Buchungstext noch Dateinamen")
    if not leistung:
        raise NormVerletzt(
            f"Für {name} ist keine Leistung bekannt — sie steht in Buchungstext "
            f"und Dateiname und muss am Beleg erfasst werden"
        )
    zeichen = sorted(_VERBOTEN & set(name + leistung))
    if zeichen:
        raise NormVerletzt(
            f"«{name} {leistung}» enthält {' '.join(zeichen)} — das taugt nicht "
            f"für einen Dateinamen"
        )
    return name, leistung


def buchungstext(name: str | None, leistung: str | None, rechnungsdatum: date) -> str:
    """``RapidAPI, Monatsabo 2026.09``."""
    name, leistung = _pruefen(name, leistung)
    return f"{name}, {leistung} {rechnungsdatum:%Y.%m}"


def dateiname(
    name: str | None, leistung: str | None, rechnungsdatum: date, zahlweg: str | None
) -> str:
    """``RapidAPI Monatsabo 22.09.2026 KK.pdf``."""
    name, leistung = _pruefen(name, leistung)
    kk = " KK" if zahlweg == "karte" else ""
    return f"{name} {leistung} {rechnungsdatum:%d.%m.%Y}{kk}.pdf"


# ── Der Monatssammelbeleg ────────────────────────────────────────────
#
# Festgelegt am 25.09.2026: ein Sammelbeleg je Kalendermonat. Der Text ist die
# Form, in der die Treuhänderin Cursor seit April 2026 bucht; das «USA» nennt,
# woher die Leistung bezogen wird, und damit, warum Bezugssteuer anfällt.

MONATE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


def sammeltext(name: str | None, leistung: str | None, monat: date, *, nachtrag: bool = False) -> str:
    """``Cursor, USA, Usage 2026.09`` -- mit ``Nachtrag``, wenn der Monat schon gebucht ist."""
    name, leistung = _pruefen(name, leistung)
    return f"{name}, USA, {leistung} {monat:%Y.%m}" + (" Nachtrag" if nachtrag else "")


def sammeldateiname(name: str | None, monat: date, zahlweg: str | None, *, nachtrag: bool = False) -> str:
    """``Cursor Sammelbeleg September 2026 KK.pdf`` -- wie die Belege, die die Treuhänderin kennt."""
    name, _ = _pruefen(name, "Sammelbeleg")
    kk = " KK" if zahlweg == "karte" else ""
    zusatz = " Nachtrag" if nachtrag else ""
    return f"{name} Sammelbeleg {MONATE[monat.month - 1]} {monat:%Y}{zusatz}{kk}.pdf"


def nummeriert(dateiname: str, n: int) -> str:
    """Der n-te Beleg desselben Tages: ``… KK.pdf``, ``… KK (2).pdf``, ``… KK (3).pdf``.

    Cursor schickt bis zu sieben Rechnungen am Tag, und die Norm nennt nur das
    Datum. Die Klammer ist die Form, die im Archiv schon steht.
    """
    if n <= 1:
        return dateiname
    stamm, _, endung = dateiname.rpartition(".")
    return f"{stamm} ({n}).{endung}"
