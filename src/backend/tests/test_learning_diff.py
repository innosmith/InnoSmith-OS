"""Tests fuer die reine Diff-Logik des Self-Learning-Service.

Prueft die signal-relevanten Hilfsfunktionen ohne DB/LLM:
- html_to_text: HTML -> Plaintext
- strip_quoted_history: zitierten Original-Thread abschneiden
- compute_draft_diff: "saubere" Freigabe vs. echter Stil-Edit erkennen
- normalize_rejection_reason: Ablehnungsgrund gegen die Weissliste
"""

import re
from pathlib import Path

import pytest

from app.services.learning import (
    REJECTION_REASONS,
    compute_draft_diff,
    extract_salutation_signature,
    html_to_text,
    normalize_rejection_reason,
    strip_quoted_history,
)


class TestExtractSalutationSignature:
    def test_informal_du_greeting_and_closing(self):
        body = "Hallo Peter,\n\nDanke dir für die Rückmeldung. Ich melde mich.\n\nLG Anthony"
        out = extract_salutation_signature(body)
        assert out["greeting"] == "Hallo Peter"
        assert out["register"] == "du"
        assert out["closing"].lower().startswith("lg")

    def test_formal_sie_greeting(self):
        body = (
            "Sehr geehrter Herr Müller,\n\nBesten Dank für Ihre Anfrage.\n\n"
            "Freundliche Grüsse\nAnthony Smith"
        )
        out = extract_salutation_signature(body)
        assert out["greeting"].startswith("Sehr geehrter Herr Müller")
        assert out["register"] == "sie"
        assert "grüsse" in out["closing"].lower()

    def test_no_signals_returns_empty(self):
        assert extract_salutation_signature("") == {}
        assert "greeting" not in extract_salutation_signature("Text ohne Anrede.")


class TestHtmlToText:
    def test_strips_tags_and_entities(self):
        html = "<p>Hallo&nbsp;Welt</p><br><div>Gr&uuml;sse</div>"
        out = html_to_text(html)
        assert "Hallo Welt" in out
        assert "<" not in out and ">" not in out

    def test_empty(self):
        assert html_to_text(None) == ""
        assert html_to_text("") == ""


class TestStripQuotedHistory:
    def test_cuts_at_von_marker(self):
        body = "Mein neuer Text.\nVon: chef@firma.ch\nGesendet: gestern\nAlter Inhalt"
        out = strip_quoted_history(body)
        assert out == "Mein neuer Text."

    def test_cuts_at_underscore_separator(self):
        body = "Antwort hier.\n______________\nOriginalnachricht"
        out = strip_quoted_history(body)
        assert out == "Antwort hier."

    def test_no_marker_keeps_all(self):
        body = "Nur ein kurzer Text ohne Zitat."
        assert strip_quoted_history(body) == body


class TestComputeDraftDiff:
    def test_identical_is_clean(self):
        html = "<p>Danke fuer Ihre Nachricht. Ich melde mich morgen.</p>"
        ergebnis = compute_draft_diff(html, html)
        assert ergebnis.is_clean is True
        assert ergebnis.diff_text == ""

    def test_whitespace_only_change_is_clean(self):
        a = "<p>Danke   fuer  Ihre Nachricht.</p>"
        b = "<p>Danke fuer Ihre Nachricht.</p>"
        assert compute_draft_diff(a, b).is_clean is True

    def test_real_edit_detected(self):
        a = "<p>Danke fuer Ihre Nachricht. Ich melde mich morgen.</p>"
        b = "<p>Besten Dank fuer Ihre Mail. Ich melde mich naechste Woche.</p>"
        ergebnis = compute_draft_diff(a, b)
        assert ergebnis.is_clean is False
        assert ergebnis.diff_text  # nicht leer

    def test_only_quoted_history_differs_is_clean(self):
        # Gleicher neuer Text, aber unterschiedlich zitierter Verlauf -> sauber.
        a = "<p>Passt, danke!</p><p>Von: a@x.ch</p><p>Alte Mail A</p>"
        b = "<p>Passt, danke!</p><p>Von: a@x.ch</p><p>Komplett andere alte Mail B</p>"
        assert compute_draft_diff(a, b).is_clean is True


class TestAenderungsmass:
    """Das Mass muss Politur von Neufassung trennen -- sonst sagt es nichts.

    Das binaere ``is_clean`` verbarg den wichtigsten Befund ueber die Entwuerfe.
    An allen 29 echten Bearbeitungen nachgerechnet (06.09.2026) liegt keine
    Wolke um die Mitte vor, sondern eine Zweiteilung: vier Faelle unter 0.15
    und vierzehn ab 0.80. Diese Tests halten fest, dass die beiden Enden auch
    an konstruierten Beispielen auseinanderfallen.
    """

    def test_unchanged_draft_measures_zero(self):
        html = "<p>Besten Dank, ich schaue es mir bis Freitag an.</p>"
        assert compute_draft_diff(html, html).change_ratio == 0.0

    def test_single_word_swap_stays_small(self):
        """Eine ausgetauschte Anrede ist Politur und darf nicht wie Neufassung aussehen."""
        a = "<p>Guten Tag Herr Meier, besten Dank fuer Ihre Nachricht vom Montag.</p>"
        b = "<p>Hallo Peter, besten Dank fuer Ihre Nachricht vom Montag.</p>"
        assert 0 < compute_draft_diff(a, b).change_ratio < 0.4

    def test_full_rewrite_measures_high(self):
        a = "<p>Besten Dank fuer Ihre Nachricht. Ich melde mich naechste Woche.</p>"
        b = "<p>Die Offerte liegt bei. Position drei haben wir gekuerzt, weil "
        b += "der Aufwand tiefer ausfaellt als angenommen.</p>"
        assert compute_draft_diff(a, b).change_ratio > 0.8

    def test_reflow_alone_does_not_count_as_change(self):
        """Outlook setzt den Zeilenumbruch beim Senden neu -- das ist keine Korrektur.

        Der Grund, auf Woertern statt auf Zeilen oder Zeichen zu messen: sonst
        traegt jede versendete Mail ein Aenderungsmass, das niemand verursacht hat.
        """
        a = "<p>Besten Dank fuer Ihre Nachricht.<br>Ich melde mich morgen.</p>"
        b = "<p>Besten Dank fuer Ihre Nachricht. Ich melde mich morgen.</p>"
        assert compute_draft_diff(a, b).change_ratio == 0.0

    def test_measure_is_bounded(self):
        """Die Datenbank prueft ``BETWEEN 0 AND 1`` -- hier faellt es frueher auf."""
        a = "<p>Kurz.</p>"
        b = "<p>" + " ".join(f"Wort{i}" for i in range(200)) + "</p>"
        assert 0.0 <= compute_draft_diff(a, b).change_ratio <= 1.0


class TestAblehnungsgrund:
    """Der Grund ist eine geschlossene Menge -- und er darf fehlen duerfen."""

    @pytest.mark.parametrize("grund", REJECTION_REASONS)
    def test_known_reasons_pass(self, grund):
        assert normalize_rejection_reason(grund) == grund

    def test_missing_reason_is_allowed(self):
        """«Ohne Grund» ist eine gueltige Antwort, kein Fehler.

        Waere der Grund Pflicht, waere die schnellste Antwort die erste in der
        Liste -- und die Messreihe truege eine Mehrheit, die niemand gemeint hat.
        """
        assert normalize_rejection_reason(None) is None
        assert normalize_rejection_reason("") is None

    def test_invented_reason_is_dropped_not_stored(self):
        """Lieber keine Angabe als eine Kategorie, die spaeter als Befund erscheint."""
        assert normalize_rejection_reason("zu_lang") is None
        assert normalize_rejection_reason({"grund": "nicht_noetig"}) is None

    def test_frontend_offers_exactly_these_reasons(self):
        """Beide Seiten muessen dieselben Kennungen kennen.

        Sonst schickt die Oberflaeche einen Grund, den das Backend still
        verwirft -- der Klick fuehlt sich an wie erfasst und ist es nicht.
        Dieselbe Gattung Graben wie zwischen Prompt und Skill.
        """
        quelle = (
            Path(__file__).resolve().parents[2]
            / "frontend/src/components/agent/Ablehnungsgrund.tsx"
        )
        if not quelle.is_file():
            pytest.skip("Frontend-Baustein nicht vorhanden")
        block = re.search(
            r"ABLEHNUNGSGRUENDE = \[(.*?)\] as const", quelle.read_text("utf-8"), re.S
        )
        assert block, "ABLEHNUNGSGRUENDE nicht gefunden -- Baustein umbenannt?"
        assert tuple(re.findall(r"'([a-z_]+)'", block.group(1))) == REJECTION_REASONS
