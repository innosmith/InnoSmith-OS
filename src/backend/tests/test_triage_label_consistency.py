"""Konsistenz zwischen Move-Politik im Code und Label-Anweisungen im Skill.

Vorfall vom 10.08.2026: Eine Kundenrueckfrage von justin.springer@swissbankers.ch
wurde als ``label='System'`` + ``triage_class='fyi'`` eingeordnet. Das Modell hatte
die Klasse zuerst bestimmt (ein offener Task zum Thread machte ``fyi`` richtig) und
dann das Label aus der Aufzaehlung in Abschnitt 3 des Skills gezogen: «**fyi**: kein
Handlungsbedarf (Newsletter, System, Junk, reine CC-Info)». Der Rueckschluss von der
Klasse auf das Label war damit im Skill angelegt.

Solange ``move_target`` zusaetzlich ``inferenceClassification == 'other'`` verlangte,
blieb der Fehler folgenlos -- das Gate blockierte 79 % aller Mails ohnehin. Ohne diese
Bremse verschwindet eine so eingeordnete Kundenmail nach ``Inbox/System``. Deshalb
gehoert die Trennung von Label und Klasse ab jetzt geprueft und nicht nur formuliert.

Der Skill liegt ausserhalb des Repos (``~/.hermes/skills/``) und wird in Prod per
Volume gemountet. Er kann lokal fehlen -- dann wird uebersprungen, nie fehlgeschlagen.
"""

from pathlib import Path

import pytest

SKILL_DIR = Path.home() / ".hermes" / "skills" / "email-triage"


def _skill_texts() -> dict[str, str]:
    """SKILL.md und alle Referenzen des Triage-Skills, falls ausgerollt."""
    if not SKILL_DIR.is_dir():
        return {}
    files = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]
    return {f.name: f.read_text(encoding="utf-8") for f in files if f.is_file()}


def _require_skill() -> dict[str, str]:
    texts = _skill_texts()
    if "SKILL.md" not in texts:
        pytest.skip("Triage-Skill nicht ausgerollt")
    return texts


def test_fyi_is_not_defined_as_a_label_list():
    """``fyi`` darf nicht mehr als Aufzaehlung von Labels erklaert werden.

    Die verbotene Form ist eine Definition von ``fyi``, die im selben Satz
    System/Newsletter/Junk als Klammer-Aufzaehlung nachschiebt. Das Modell liest sie
    als Kandidatenliste fuer das Label.
    """
    import re

    verboten = re.compile(
        r"\*\*fyi\*\*:[^\n]*\((?=[^\n)]*Newsletter)(?=[^\n)]*System)(?=[^\n)]*Junk)"
    )
    offenders = [
        f"{name}:{lineno}: {line.strip()[:110]}"
        for name, text in _require_skill().items()
        for lineno, line in enumerate(text.splitlines(), start=1)
        if verboten.search(line)
    ]
    assert not offenders, (
        "Der Skill definiert 'fyi' wieder ueber eine Label-Aufzaehlung. Genau dieser "
        "Rueckschluss hat eine Kundenmail als 'System' einsortiert:\n"
        + "\n".join(offenders)
    )


def test_skill_separates_label_from_class():
    """Positivprobe: der Skill sagt ausdruecklich, dass es zwei getrennte Fragen sind."""
    rules = _require_skill().get("triage-rules.md")
    if rules is None:
        pytest.skip("triage-rules.md nicht ausgerollt")
    assert "zwei getrennte Fragen" in rules


def test_skill_rules_out_system_for_human_senders():
    """Der Umkehrschluss zu Stufe 3 muss im Skill stehen, nicht nur gemeint sein.

    Bedingung 1 von Stufe 3 (maschineller Absender) ist zwingend. Sie war nur
    positiv formuliert, weshalb ein fehlender Handlungsbedarf genuegte, um bei
    ``System`` zu landen.
    """
    rules = _require_skill().get("triage-rules.md")
    if rules is None:
        pytest.skip("triage-rules.md nicht ausgerollt")
    assert 'ist sie NIE "System"' in rules


def test_skill_move_table_matches_code():
    """Der Skill darf keine Move-Bedingung nennen, die das Backend nicht mehr prueft.

    Bis August 2026 stand dort «nur wenn `fyi` und Outlook selbst die Mail als
    «Other» einordnet». Eine Anweisung, die auf eine entfernte Codebedingung
    verweist, ist schlimmer als keine: sie erzeugt ein falsches Weltmodell im
    Modell -- so kam es auch zum erfundenen JSON-Feld ``"move"``.
    """
    from app.services.triage_labels import LABEL_FOLDERS

    rules = _require_skill().get("triage-rules.md")
    if rules is None:
        pytest.skip("triage-rules.md nicht ausgerollt")
    assert "«Other»" not in rules
    for label in LABEL_FOLDERS:
        assert label in rules, f"Move-faehiges Label {label} fehlt im Skill"
