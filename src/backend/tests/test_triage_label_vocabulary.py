"""Konsistenz zwischen Label-Vokabular im Code und im ausgerollten Triage-Skill.

Hintergrund (Vorfall 28.07.-17.08.2026): ``Unklar`` stand im Agenten-Vokabular, und
``SKILL.md`` empfahl es ausdruecklich -- «Bist du beim Label unsicher, wähle `Unklar`
-- das bleibt sichtbar». Das Modell folgte der Empfehlung mit hoher Confidence: 20 %
aller kategorisierten Mails trugen ``Unklar`` (vorher 0 %), praktisch keine davon mit
``needs_review``. Weil der Fall damit weder Aufgabe noch Sichtungsmarke erzeugte, lag
er unsichtbar in der Inbox -- betroffen waren echte Kundenthreads. Parallel wurden
``Offerten/Verträge``, ``Networking/Leads`` und ``Signale`` drei Wochen lang nie
vergeben: das Modell wich aus, statt sich zu entscheiden.

Kein Unit-Test konnte das sehen, weil das Vokabular an drei Orten gepflegt wird
(``triage_labels.py``, ``SKILL.md``, ``references/triage-rules.md``) und jede Seite
fuer sich stimmig war. Dieser Test liest sie gemeinsam -- Regel 2 aus
``.cursor/rules/agent-instruktionen.mdc``.

Skips statt Fehlschlaege, wenn der Skill lokal nicht ausgerollt ist.
"""

import re
from pathlib import Path

import pytest

from app.services.triage_labels import AGENT_LABELS, FALLBACK_LABEL

SKILL_DIR = Path.home() / ".hermes" / "skills" / "email-triage"


def _skill_texts() -> dict[str, str]:
    if not SKILL_DIR.is_dir():
        return {}
    files = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]
    return {f.name: f.read_text(encoding="utf-8") for f in files if f.is_file()}


def test_skill_does_not_offer_the_fallback_label():
    """Der Skill darf ``Unklar`` nicht als Wahl anbieten.

    Erlaubt sind Erwaehnungen, die es als Backend-Urteil erklaeren ("wird
    verworfen", "keines deiner Labels"). Verboten ist die Aufforderung, es zu
    waehlen -- genau die Formulierung, die den Vorfall ausgeloest hat.
    """
    texts = _skill_texts()
    if not texts:
        pytest.skip("Triage-Skill nicht ausgerollt")

    # Aufforderungen wie "wähle Unklar", "nutze Unklar", "setze Unklar",
    # "Label Unklar vergeben" -- Verb und Label in derselben Zeile.
    verbs = r"(wähle|waehle|nutze|verwende|setze|vergib|nimm|gib)"
    pattern = re.compile(rf"{verbs}\b[^.\n]{{0,40}}{FALLBACK_LABEL}", re.IGNORECASE)

    offenders: list[str] = []
    for name, text in texts.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{name}:{lineno}: {line.strip()[:110]}")
    assert not offenders, (
        f"Der Skill fordert zur Wahl von '{FALLBACK_LABEL}' auf, obwohl das Label "
        "dem Agenten nicht offensteht (AGENT_LABELS). Unsicherheit gehoert in "
        "'confidence':\n" + "\n".join(offenders)
    )


def test_skill_json_schema_lists_exactly_the_agent_labels():
    """Der Pflicht-JSON-Block im Skill muss ``AGENT_LABELS`` spiegeln.

    Driftet die Liste, liefert das Modell ein Label, das das Backend verwirft --
    und jede solche Mail wird zur Sichtung markiert, statt klassifiziert zu werden.
    """
    texts = _skill_texts()
    if "triage-rules.md" not in texts:
        pytest.skip("Triage-Referenzen nicht ausgerollt")

    block = re.search(
        r'"label":\s*"<([^>]+)>"', texts["triage-rules.md"]
    )
    assert block, "Label-Zeile im Pflicht-JSON-Block nicht gefunden"
    assert tuple(block.group(1).split("|")) == AGENT_LABELS


def test_finanzen_is_decided_by_topic_not_by_sender_domain():
    """Die Domain-Klausel fuer ``Finanzen`` darf nicht zurueckkommen.

    Bis zum 30.08.2026 stand in Stufe 2: «ODER der Absender gehoert zur
    Treuhand-Domain ``t-r.ch`` (JEDE Person dort)». Gemessen erzeugte die Klausel mehr
    falsche als richtige Treffer, weil T+R gleichzeitig Kunde ist: «Anfrage fuer
    Unterstuetzung im DMS Projekt», «Beschaffung und Setup eigene KI-Hardware» und der
    TaxFlash-Newsletter landeten als Finanzmails in der Inbox, waehrend echte
    Lieferantenrechnungen von ``invoice@metanet.ch`` als ``System`` aus der Inbox
    verschwanden.

    Die Herkunft entscheidet nur dort, wo sie das Thema garantiert -- bei
    Funktionspostfaechern wie ``Rechnungswesen@t-r.ch``, und das ist eine
    deterministische Regel in ``learned_rules``, kein Satz im Prompt.
    """
    texts = _skill_texts()
    if "triage-rules.md" not in texts:
        pytest.skip("Triage-Referenzen nicht ausgerollt")
    text = texts["triage-rules.md"]

    # Die Klausel selbst: "ODER der Absender gehört zur ... Domain".
    klausel = re.compile(
        r"ODER\s+der\s+Absender\s+geh(ö|oe)rt\s+zur[^.\n]{0,40}Domain", re.IGNORECASE
    )
    treffer = [
        f"Zeile {i}: {line.strip()[:110]}"
        for i, line in enumerate(text.splitlines(), start=1)
        if klausel.search(line)
    ]
    assert not treffer, (
        "Finanzen haengt wieder an einer Absender-Domain:\n" + "\n".join(treffer)
    )
    assert "am Inhalt, nie am Absender" in text, (
        "Stufe 2 muss den Grundsatz ausdruecklich nennen -- eine geloeschte Klausel "
        "ohne Begruendung kommt zurueck."
    )


def test_skill_move_rules_name_the_backend_correspondence_check():
    """Der Skill muss den Korrespondenz-Nachweis des Backends erwaehnen.

    Sonst optimiert das Modell weiter auf eine Wirkung, die es nicht mehr hat, und
    ein falsches ``System`` wird zur stillen Fehlkategorie statt zum Fehlmove.
    """
    texts = _skill_texts()
    if "triage-rules.md" not in texts:
        pytest.skip("Triage-Referenzen nicht ausgerollt")
    text = texts["triage-rules.md"]
    assert "Korrespondenz-Nachweis" in text
    assert "je selbst geschrieben" in text
