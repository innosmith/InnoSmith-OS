"""Tests: Angaben im Entwurf muessen in einer gesehenen Quelle stehen.

Vorfall vom 04.08.2026: Der Schreib-Pass nannte in einer Antwort an den IT-Partner
eines Kunden den A-Record ``app1.challenge.innosmith.cloud -> 51.68.182.145``. Die
IP-Adresse kam in keiner Quelle des Jobs vor -- das Modell hatte den Platzhalter
``[IP deiner Challenge-Box]`` aus einer geloeschten Entwurfs-Quelle mit einer
erfundenen Zahl gefuellt. Der Entwurf ging in die Freigabe mit Confidence 0.9 und
`self_grade` 1.0, weil beides nur die Tool-Nutzung bewertete, nicht den Wahrheits-
gehalt. Waere er versendet worden, haette der Partner bei seinem Kunden einen
DNS-Eintrag auf eine fremde Adresse gesetzt.

Geprueft wird die reine Textseite (``text_style``) und ihre Wirkung auf das
Self-Grading. Die Formulierungsqualitaet selbst gehoert ins Eval, nicht hierher.
"""

from app.services.hermes_worker import _compute_self_grade
from app.services.text_style import (
    factual_tokens,
    placeholder_markers,
    ungrounded_values,
)

# Der echte Entwurf des Vorfalls, gekuerzt auf die pruefbaren Angaben.
_DRAFT_HTML = (
    "<p>Hoi Gabriel</p>"
    "<p>Besten Dank für die Bestätigung. Variante 1 ist auch meine Empfehlung.</p>"
    "<ul><li><strong>CNAME:</strong> _acme-challenge.app1.gsw.ch → "
    "_acme-challenge.app1.challenge.innosmith.cloud</li>"
    "<li><strong>A-Record:</strong> app1.challenge.innosmith.cloud → "
    "51.68.182.145</li></ul>"
    "<p>Die Zertifikate werden alle 90 Tage erneuert.</p>"
    "<p>LG Anthony</p>"
)

# Was der Job wirklich gesehen hat: die gesendete Mail vom 30.07.
_EVIDENCE = [
    "Von dir bräuchte ich pro App einen statischen Eintrag: "
    "_acme-challenge.app1.gsw.ch CNAME app1-gsw.<challenge-zone>. "
    "Die Challenge-Zone kann bei euch liegen oder ich stelle sie bereit "
    "(app1.challenge.innosmith.cloud). Pro App ein A-Record auf 192.168.230.60. "
    "Die Zertifikate laufen aktuell 200, ab März 100 Tage.",
]


class TestFactualTokens:
    def test_finds_ip_and_hostnames(self):
        tokens = factual_tokens(_DRAFT_HTML)
        assert "51.68.182.145" in tokens
        assert "_acme-challenge.app1.gsw.ch" in tokens
        assert "app1.challenge.innosmith.cloud" in tokens

    def test_finds_quantity_with_unit(self):
        assert "90" in factual_tokens("<p>Erneuerung alle 90 Tage.</p>")

    def test_ignores_email_address_domain(self):
        # Eine Signatur-Adresse ist kein Fakt, den der Entwurf belegen muesste.
        assert factual_tokens("<p>Melde dich: anthony@innosmith.ch</p>") == []

    def test_ignores_plain_prose(self):
        assert factual_tokens("<p>Hoi Gabriel, merci für den Entscheid. LG</p>") == []


class TestUngroundedValues:
    def test_flags_invented_ip(self):
        missing = ungrounded_values(_DRAFT_HTML, _EVIDENCE)
        assert "51.68.182.145" in missing

    def test_accepts_values_from_sources(self):
        missing = ungrounded_values(_DRAFT_HTML, _EVIDENCE)
        assert "_acme-challenge.app1.gsw.ch" not in missing
        assert "app1.challenge.innosmith.cloud" not in missing

    def test_flags_number_without_source(self):
        # «alle 90 Tage» stand nirgends -- die Quellen nennen 200 bzw. 100 Tage.
        assert "90" in ungrounded_values(_DRAFT_HTML, _EVIDENCE)

    def test_grounded_draft_has_no_findings(self):
        draft = "<p>Hoi Gabriel</p><p>A-Record auf 192.168.230.60.</p><p>LG</p>"
        assert ungrounded_values(draft, _EVIDENCE) == []

    def test_case_and_separator_insensitive(self):
        draft = "<p>Budget von 1'200 CHF</p>"
        assert ungrounded_values(draft, ["Wir sprachen von 1200 CHF."]) == []

    def test_without_evidence_nothing_is_claimed_wrong(self):
        # Ohne Belege wird nicht geprueft -- sonst waere jeder Entwurf beanstandet.
        assert ungrounded_values(_DRAFT_HTML, []) != []  # rein: alle Tokens fehlen
        # Der Aufrufer im Worker prueft deshalb vorher auf leeres Beweismaterial.


class TestPlaceholderMarkers:
    def test_finds_bracket_placeholder(self):
        marks = placeholder_markers("<p>A-Record: [IP deiner Challenge-Box]</p>")
        assert marks == ["[IP deiner Challenge-Box]"]

    def test_finds_angle_placeholder(self):
        assert placeholder_markers("<p>Ziel: <challenge-zone></p>") == ["<challenge-zone>"]

    def test_clean_draft_has_none(self):
        assert placeholder_markers("<p>Hoi Gabriel</p><p>Merci. LG</p>") == []


class TestSelfGradeCountsGrounding:
    """Tool-Nutzung allein darf nicht mehr 1.0 ergeben."""

    _TOOLS = [
        "mcp_graph_get_thread",
        "mcp_graph_search_sender_history",
        "mcp_taskpilot_get_sender_profile",
        "mcp_graph_search_my_replies",
    ]
    _META = {"conversation_id": "conv-1"}

    def test_full_score_when_grounded(self):
        grade = _compute_self_grade(self._META, {"draft_id": "d1"}, self._TOOLS)
        assert grade["score"] == 1.0
        assert grade["checks"]["values_grounded"] is True

    def test_score_drops_when_ungrounded(self):
        grade = _compute_self_grade(
            self._META,
            {"draft_id": "d1", "draft_quality": "ungrounded"},
            self._TOOLS,
        )
        assert grade["score"] < 1.0
        assert "values_grounded" in grade["missing"]

    def test_no_draft_no_grounding_check(self):
        grade = _compute_self_grade(self._META, {}, self._TOOLS)
        assert "values_grounded" not in grade["checks"]
