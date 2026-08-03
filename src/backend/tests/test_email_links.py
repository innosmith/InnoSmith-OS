"""Tests für app/services/email_links.py.

Die Outlook-URL wurde bis 03.08.2026 im Hermes-Worker gebaut und als roher Text an
die Task-Beschreibung gehängt. Sie liegt jetzt in einem eigenen Modul und wird über
``TaskOut.source_email_web_link`` ausgeliefert -- ein Ort, eine Form, ein Test.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.email_links import outlook_deeplink  # noqa: E402


class TestOutlookDeeplink:
    def test_built_from_message_id(self):
        link = outlook_deeplink("AAMk123")
        assert link == "https://outlook.office.com/mail/deeplink/read/AAMk123"

    def test_none_without_id(self):
        assert outlook_deeplink(None) is None
        assert outlook_deeplink("") is None

    def test_special_characters_are_encoded(self):
        """Graph-IDs enthalten `+`, `/` und `=`; ohne Quoting zerfällt der Pfad."""
        link = outlook_deeplink("AAMk=abc/def+ghi")
        assert link is not None
        assert link.startswith("https://outlook.office.com/mail/deeplink/read/")
        assert "abc/def" not in link
        assert "%3D" in link and "%2F" in link and "%2B" in link
