"""Tests fuer die Platzhalter-Erkennung und die Instruktions-Konsistenz.

Hintergrund (Vorfall vom 03.08.2026): Ein Antwort-Entwurf ging als fertiger
Vorschlag in die Freigabe, der nur aus «Liebe Grüsse» und «LG Anthony» bestand.
Ursache war ein Widerspruch zwischen Skill und Prompt -- der Skill verlangte einen
Entwurf im Klassifikations-Lauf, der Prompt verbot ihn.

Diese Datei deckt beide Seiten ab:
- ``has_content_between_greeting_and_closing``: erkennt Platzhalter strukturell,
  validiert an echten Entwuerfen aus dem Bestand (kein Fehlalarm).
- Konsistenz zwischen Skill-Text und ``two_pass_draft``: der Skill darf im
  Zwei-Pass-Betrieb keine Entwurfs-Erstellung mehr verlangen.
"""

from pathlib import Path

import pytest

from app.services.learning import has_content_between_greeting_and_closing as has_content


# ── Platzhalter-Erkennung ────────────────────────────────────────────────────

# Der reale Platzhalter aus Job a71b75c4 (Mail «Details Zertifikate», 03.08.2026).
PLACEHOLDER_HTML = (
    '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
    '</head><body style="font-family:Arial,sans-serif; font-size:10pt">'
    "<p>Liebe Grüsse</p><p>LG Anthony</p></body></html>"
)

# Der kuerzeste KORREKTE Entwurf aus dem Bestand (27.07.2026, 205 Zeichen) -- er
# ist kuerzer als manch fehlerhafter und darf niemals als Platzhalter gelten.
SHORTEST_VALID_HTML = (
    "<html><body><p>Hallo Justin</p>"
    "<p>10:15 passt – ich bin um 10:15 online.</p>"
    "<p>LG und bis morgen, Anthony</p></body></html>"
)


def test_placeholder_without_content_is_detected():
    assert has_content(PLACEHOLDER_HTML) is False


def test_shortest_valid_draft_is_not_flagged():
    """Laengenbasierte Pruefungen scheitern hier: 205 gegen 196 Zeichen."""
    assert has_content(SHORTEST_VALID_HTML) is True


@pytest.mark.parametrize(
    "html",
    [
        # Formeller Entwurf mit nachgestellter Namenszeile.
        "<p>Sehr geehrte Frau Muster</p><p>Besten Dank für Ihre Anfrage.</p>"
        "<p>Freundliche Grüsse</p><p>Anthony Smith</p>",
        # Ohne Anrede, nur Inhalt und Schluss.
        "<p>Passt so, ich melde mich morgen.</p><p>LG Anthony</p>",
        # Ohne Schlussformel.
        "<p>Hallo Mike</p><p>Die Zertifikate sind angekommen, danke.</p>",
        # Mehrere Inhaltsabsaetze.
        "<p>Hoi Gabriel</p><p>Erster Punkt.</p><p>Zweiter Punkt.</p><p>LG Anthony</p>",
        # Gruss-Wort im Inhalt darf nicht zu Fehlalarm fuehren.
        "<p>Hallo Team</p><p>Bitte richte Grüsse an Nicole aus.</p><p>LG Anthony</p>",
    ],
)
def test_real_drafts_are_never_flagged(html):
    assert has_content(html) is True


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<p></p>",
        "<p>Hallo Mike</p><p>LG Anthony</p>",
        "<p>Guten Morgen Simone</p><p>Freundliche Grüsse</p><p>Anthony Smith</p>",
    ],
)
def test_content_free_drafts_are_flagged(html):
    assert has_content(html) is False


# ── Konsistenz Skill <-> Code ────────────────────────────────────────────────

SKILL_DIR = Path.home() / ".hermes" / "skills" / "email-triage"


def _skill_texts() -> list[tuple[str, str]]:
    """SKILL.md und alle Referenzen des Triage-Skills, falls ausgerollt."""
    if not SKILL_DIR.is_dir():
        return []
    files = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]
    return [(f.name, f.read_text(encoding="utf-8")) for f in files if f.is_file()]


def test_triage_skill_does_not_instruct_drafting():
    """Der Triage-Skill darf im Zwei-Pass-Betrieb keinen Entwurf mehr verlangen.

    Genau dieser Widerspruch -- Skill verlangt ``create_draft``, Prompt verbietet
    es -- hat den Platzhalter-Entwurf verursacht. Keine bestehende Teststelle
    konnte ihn sehen, weil Skill und Code nirgends gemeinsam geprueft wurden.

    Erwaehnungen sind erlaubt (etwa «create_draft hast du nicht»); verboten ist
    die Aufforderung, es aufzurufen.
    """
    from app.config import get_settings

    if not get_settings().two_pass_draft:
        pytest.skip("Einpass-Betrieb: der Triage-Skill darf dort drafte")

    offenders: list[str] = []
    for name, text in _skill_texts():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "create_draft(" in line:
                offenders.append(f"{name}:{lineno}: {line.strip()[:100]}")
    assert not offenders, (
        "Der Triage-Skill fordert zum Erstellen eines Entwurfs auf, obwohl der "
        "Schreib-Pass dafuer zustaendig ist:\n" + "\n".join(offenders)
    )


def test_triage_skill_states_that_it_does_not_draft():
    """Positivprobe: der Skill sagt ausdruecklich, dass er nicht draftet."""
    texts = dict(_skill_texts())
    if "SKILL.md" not in texts:
        pytest.skip("Triage-Skill nicht ausgerollt")
    assert "keine Antwort-Entwürfe" in texts["SKILL.md"] or (
        "keinen Draft erstellen" in texts["SKILL.md"]
    )
