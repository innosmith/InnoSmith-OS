"""Der Nachweis, dass die Portierung dasselbe Dokument erzeugt wie die Vorlage.

Der Leistungsrapport geht an Kundschaft und liegt dort seit Jahren im Ordner.
«Wortgetreu portiert» ist deshalb eine Behauptung, die belegt gehört — und der
einzige Beleg, der zählt, ist ein Vergleich beider Ausgaben bei gleicher
Eingabe.

Verglichen wird auf zwei Ebenen, weil eine allein nicht reicht: der **Text**
(er sähe eine verschobene Spalte nicht) und die **Zeichenbefehle mit ihren
Koordinaten** (sie sehen ein fehlendes Wort nicht).

Die Vorlage wird nicht importiert — sie zieht pandas, requests, dotenv und den
``path_manager`` mit. Stattdessen wird die Layoutklasse aus der Quelldatei
herausgelöst und in einem eigenen Namensraum ausgeführt. Das ist genau der
Code, der heute die Rapporte erzeugt.

Fehlt die Arbeitskopie der Vorlage, wird übersprungen statt zu scheitern: der
Beleg ist dann nicht erbracht, aber das ist kein Fehler der Portierung.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from app.services import leistungsrapport as neu

VORLAGE = Path("/tmp/InnoSmithAdmin-readonly/D1_createLeistungsrapporte.py")
ALT_ASSETS = Path("/tmp/InnoSmithAdmin-readonly/assets")

pytestmark = pytest.mark.skipif(
    not VORLAGE.exists(),
    reason="Arbeitskopie der Vorlage nicht vorhanden — Vergleich nicht durchführbar",
)


@pytest.fixture(scope="module")
def alt_erzeugen():
    """Die Layoutklasse der Vorlage, aus der Datei herausgelöst."""
    quelle = VORLAGE.read_text(encoding="utf-8")
    anfang = quelle.index("class LeistungsrapportPDF(FPDF):")
    ende = quelle.index("# Projekt-Konsistenzprüfung")
    ausschnitt = quelle[anfang:ende]

    raum: dict = {}
    exec("from pathlib import Path\nfrom fpdf import FPDF\n", raum)  # noqa: S102
    raum["ASSETS_DIR"] = ALT_ASSETS
    exec(ausschnitt, raum)  # noqa: S102
    return raum["generate_leistungsrapport_pdf"]


EINTRAEGE = [
    (1, "Konzeptarbeit und Abstimmung", 4.25, 7),
    (5, "Workshop mit dem Team zur Priorisierung der Massnahmen", 3.5, 8),
    (12, "Prüfung der Grössenordnung – Begleitung", 1.75, 7),
    (19, "Dokumentation", 2.0, None),
]


@pytest.fixture(scope="module")
def beide(alt_erzeugen, tmp_path_factory) -> tuple[bytes, bytes]:
    rapport = neu.aufbereiten(
        [
            {"datum": date(2026, 8, t), "beschreibung": b,
             "dauer_stunden": h, "task_id": tid}
            for t, b, h, tid in EINTRAEGE
        ],
        jahr=2026, monat=8, bereiche={7: "Analyse", 8: "Durchführung"},
    )

    ziel = tmp_path_factory.mktemp("rapport") / "alt.pdf"
    assert alt_erzeugen(
        {
            "formatted_date": "August 2026",
            "employee_name": None,
            "entries": [
                {"date": z.datum, "description": z.taetigkeit, "hours": z.stunden}
                for z in rapport.zeilen
            ],
            "duration_sum": f"{rapport.summe:.2f}",
            "has_task_breakdown": True,
            "task_totals": [{"label": n, "hours": h} for n, h in rapport.nach_bereich],
            "has_user_breakdown": False,
            "user_totals": None,
        },
        ziel,
        ALT_ASSETS / "InnoSmith 1200px.png",
    ), "Die Vorlage selbst konnte kein PDF erzeugen"

    return ziel.read_bytes(), neu.erzeugen(rapport)


def _seitentexte(roh: bytes) -> list[str]:
    from pypdf import PdfReader

    return [s.extract_text() for s in PdfReader(BytesIO(roh)).pages]


def _anweisungen(roh: bytes) -> list[str]:
    """Der vollständige Anweisungsstrom je Seite.

    Hier steht jede Koordinate, jede Graustufe, jede Linienstärke und jeder
    Schriftwechsel. Ein Ausschnitt davon wäre schwächer und liefe Gefahr, leer
    zu sein und trotzdem zu bestehen.
    """
    from pypdf import PdfReader

    return [
        seite.get_contents().get_data().decode("latin-1", "replace")
        for seite in PdfReader(BytesIO(roh)).pages
    ]


def test_gleiche_seitenzahl(beide):
    alt, neu_bytes = beide
    assert len(_seitentexte(alt)) == len(_seitentexte(neu_bytes))


def test_gleicher_text(beide):
    """Ein fehlendes Wort, eine andere Rundung, ein verlorener Umlaut."""
    alt, neu_bytes = beide
    assert _seitentexte(neu_bytes) == _seitentexte(alt)


def test_gleiche_anweisungen(beide):
    """Jede Linie, jede Fläche und jede Textmarke an derselben Stelle.

    Der Textvergleich allein sähe eine um zwei Millimeter verschobene Spalte
    nicht — auf einem Kundendokument fällt genau das auf.
    """
    alt, neu_bytes = beide
    erwartet = _anweisungen(alt)
    assert erwartet and any(erwartet), "Leerer Vergleich wäre ein blinder Test"
    assert _anweisungen(neu_bytes) == erwartet


def test_gleich_grosse_datei(beide):
    """Der gröbste und zugleich schärfste Hinweis: weicht die Grösse ab, ist
    irgendwo eine Schrift, ein Bild oder ein Objekt anders eingebettet."""
    alt, neu_bytes = beide
    assert len(neu_bytes) == len(alt)
