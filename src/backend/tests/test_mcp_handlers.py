"""Tests für MCP-Handler: Kontext-Aufbereitung.

Prüft:
- _html_to_text(): Style/Script entfernt, Tags gestrippt, Whitespace normalisiert
- get_thread-Handler: body_text vorhanden (nicht bodyPreview), max 3000 Zeichen, Total-Limit
- search_sender_history-Handler: body_text + from + from_name vorhanden
"""

import json
import pytest

from server import _html_to_text


class TestHtmlToText:
    def test_strips_style_tags(self):
        html = '<html><head><style>body{color:red}</style></head><body>Hello</body></html>'
        assert "color:red" not in _html_to_text(html)
        assert "Hello" in _html_to_text(html)

    def test_strips_script_tags(self):
        html = '<p>Before</p><script>alert("xss")</script><p>After</p>'
        result = _html_to_text(html)
        assert "alert" not in result
        assert "Before" in result
        assert "After" in result

    def test_converts_br_to_newline(self):
        html = 'Line1<br/>Line2<br>Line3'
        result = _html_to_text(html)
        assert "Line1\nLine2\nLine3" in result.replace(" ", "")

    def test_strips_all_html_tags(self):
        html = '<div class="foo"><b>Bold</b> <i>italic</i></div>'
        result = _html_to_text(html)
        assert "<" not in result
        assert "Bold" in result
        assert "italic" in result

    def test_normalizes_whitespace(self):
        html = '<p>  Too   many    spaces  </p>'
        result = _html_to_text(html)
        assert "  " not in result.replace("\n", " ").strip() or result.count("  ") == 0

    def test_empty_input(self):
        assert _html_to_text("") == ""
        assert _html_to_text(None) == ""

    def test_reduces_multiple_newlines(self):
        html = '<p>A</p><p></p><p></p><p></p><p>B</p>'
        result = _html_to_text(html)
        assert "\n\n\n" not in result


class TestGetThreadHandler:
    """Simuliert die get_thread-Logik aus server.py."""

    def _simulate_get_thread(self, msgs, max_per_msg=3000, max_total=15000):
        thread = []
        total_chars = 0
        for msg in msgs:
            if total_chars >= max_total:
                break
            sender = msg.get("from", {}).get("emailAddress", {})
            body_html = msg.get("body", {}).get("content", "")
            body_text = _html_to_text(body_html)[:max_per_msg]
            total_chars += len(body_text)
            thread.append({
                "id": msg.get("id"),
                "from": sender.get("address"),
                "from_name": sender.get("name"),
                "subject": msg.get("subject"),
                "receivedDateTime": msg.get("receivedDateTime"),
                "body_text": body_text,
            })
        return thread

    def test_body_text_present_not_body_preview(self):
        msgs = [{
            "id": "m1",
            "subject": "Test",
            "from": {"emailAddress": {"address": "a@test.ch", "name": "A"}},
            "receivedDateTime": "2026-04-01T10:00:00Z",
            "bodyPreview": "Short preview",
            "body": {"contentType": "html", "content": "<p>Full body content here</p>"},
        }]
        result = self._simulate_get_thread(msgs)
        assert len(result) == 1
        assert "body_text" in result[0]
        assert "bodyPreview" not in result[0]
        assert "Full body content" in result[0]["body_text"]

    def test_body_text_max_3000_chars(self):
        long_body = "<p>" + "A" * 5000 + "</p>"
        msgs = [{
            "id": "m1",
            "subject": "Long",
            "from": {"emailAddress": {"address": "a@test.ch", "name": "A"}},
            "receivedDateTime": "2026-04-01T10:00:00Z",
            "body": {"contentType": "html", "content": long_body},
        }]
        result = self._simulate_get_thread(msgs)
        assert len(result[0]["body_text"]) <= 3000

    def test_total_limit_stops_processing(self):
        msgs = []
        for i in range(10):
            body = "<p>" + "X" * 4000 + "</p>"
            msgs.append({
                "id": f"m{i}",
                "subject": f"Mail {i}",
                "from": {"emailAddress": {"address": "a@test.ch", "name": "A"}},
                "receivedDateTime": f"2026-04-0{min(i+1, 9)}T10:00:00Z",
                "body": {"contentType": "html", "content": body},
            })
        result = self._simulate_get_thread(msgs, max_total=15000)
        total_chars = sum(len(r["body_text"]) for r in result)
        assert total_chars <= 15000 + 3000


class TestSearchSenderHistoryHandler:
    """Simuliert die search_sender_history-Logik aus server.py."""

    def _simulate_search_sender_history(self, msgs):
        history = []
        for msg in msgs:
            sender = msg.get("from", {}).get("emailAddress", {})
            body_html = msg.get("body", {}).get("content", "")
            body_text = _html_to_text(body_html)[:1500]
            history.append({
                "id": msg.get("id"),
                "from": sender.get("address"),
                "from_name": sender.get("name"),
                "subject": msg.get("subject"),
                "receivedDateTime": msg.get("receivedDateTime"),
                "body_text": body_text,
                "conversationId": msg.get("conversationId"),
            })
        return history

    def test_has_required_fields(self):
        msgs = [{
            "id": "m1",
            "subject": "Test",
            "from": {"emailAddress": {"address": "sender@test.ch", "name": "Sender Name"}},
            "receivedDateTime": "2026-04-01T10:00:00Z",
            "body": {"contentType": "html", "content": "<p>Content</p>"},
            "conversationId": "conv-1",
        }]
        result = self._simulate_search_sender_history(msgs)
        assert len(result) == 1
        entry = result[0]
        assert "body_text" in entry
        assert "from" in entry
        assert "from_name" in entry
        assert entry["from"] == "sender@test.ch"
        assert entry["from_name"] == "Sender Name"
        assert "Content" in entry["body_text"]


class TestToolVisibility:
    """Sichtbarkeit von ``create_draft`` je Betriebsart.

    Die Trennung ist die strukturelle Absicherung des Zwei-Pass-Entwurfs: im
    Zwei-Pass-Betrieb darf der Triage-Agent (Registrierung ``graph``, Modus
    ``safe``) das Tool nicht sehen, sonst kann er wieder einen Platzhalter-Entwurf
    erzeugen, der den echten Schreib-Pass verdraengt (Vorfall vom 03.08.2026).
    In beiden Betriebsarten muessen ``safe`` und ``admin`` disjunkt bleiben.
    """

    @staticmethod
    def _names(monkeypatch, mode: str, triage_draft: str) -> set[str]:
        import server

        monkeypatch.setenv("GRAPH_TOOL_MODE", mode)
        monkeypatch.setenv("GRAPH_TRIAGE_DRAFT", triage_draft)
        return {t.name for t in server._visible_tools()}

    def test_two_pass_hides_create_draft_from_triage(self, monkeypatch):
        assert "create_draft" not in self._names(monkeypatch, "safe", "0")
        assert "create_draft" in self._names(monkeypatch, "admin", "0")

    def test_single_pass_keeps_create_draft_in_triage(self, monkeypatch):
        assert "create_draft" in self._names(monkeypatch, "safe", "1")
        assert "create_draft" not in self._names(monkeypatch, "admin", "1")

    @pytest.mark.parametrize("triage_draft", ["0", "1"])
    def test_safe_and_admin_stay_disjoint(self, monkeypatch, triage_draft):
        safe = self._names(monkeypatch, "safe", triage_draft)
        admin = self._names(monkeypatch, "admin", triage_draft)
        assert not (safe & admin)

    def test_mutating_tools_never_reach_triage(self, monkeypatch):
        """Unabhaengig vom Entwurfs-Flag: der Triage-Agent mutiert nie Outlook."""
        for triage_draft in ("0", "1"):
            safe = self._names(monkeypatch, "safe", triage_draft)
            assert not (safe & {"send_email", "set_email_categories",
                                "move_email_to_folder", "mark_as_read"})
