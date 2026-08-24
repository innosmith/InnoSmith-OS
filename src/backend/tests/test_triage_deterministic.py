"""Tests für die deterministische Regel-Engine (apply_deterministic_rules).

Kein LLM/keine echte DB: DB-Session und Graph-Client sind gemockt. Geprüft wird
die *Entscheidung* (greift eine Regel?) und die ausgeführte Aktion (Kategorie +
Move), analog zur Meeting-Override.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.triage import apply_deterministic_rules


class _FakeDB:
    def __init__(self):
        self.add = MagicMock()
        self.flush = AsyncMock()
        self.statements: list = []
        self.execute = AsyncMock(side_effect=self._record)
        # ``sync_message_id`` prueft vor dem Update auf email_triage, ob das neue
        # Handle schon belegt ist. ``None`` heisst frei.
        self.scalar = AsyncMock(return_value=None)

    async def _record(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return MagicMock()

    def updated_tables(self) -> set[str]:
        return {
            s.table.name
            for s in self.statements
            if hasattr(s, "table") and hasattr(s, "is_update") and s.is_update
        }


def _rule(conditions, action, rule_text="Testregel"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        rule_type="deterministic",
        status="active",
        rule_text=rule_text,
        match_conditions=conditions,
        action=action,
        priority=100,
        applied_count=0,
    )


def _email(address="kunde@example.ch", subject="Newsletter"):
    return {
        "id": "MID-1",
        "internetMessageId": "<abc@example.ch>",
        "from": {"emailAddress": {"address": address, "name": "Kundin"}},
        "subject": subject,
        "receivedDateTime": "2026-06-24T10:00:00Z",
        "inferenceClassification": "focused",
        "conversationId": "C1",
    }


@pytest.mark.asyncio
async def test_matching_rule_applies_action():
    db = _FakeDB()
    client = MagicMock()
    client.set_categories = AsyncMock()
    client.move_to_folder = AsyncMock(return_value={"id": "MID-2"})
    rule = _rule(
        [{"field": "domain", "op": "equals", "value": "example.ch"}],
        {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
    )

    handled = await apply_deterministic_rules(db, client, _email(), [rule])

    assert handled is True
    db.add.assert_called_once()
    client.set_categories.assert_awaited_once_with("MID-1", ["Newsletter"])
    client.move_to_folder.assert_awaited_once_with("MID-1", "Newsletter")
    # applied_count-Update wird abgesetzt.
    db.execute.assert_awaited()


@pytest.mark.asyncio
async def test_move_writes_the_new_handle_back():
    """Nach dem Move muss das neue Graph-Handle in der Datenbank landen.

    Der Move aendert die Graph-ID. Ohne Rueckschreiben zeigten ``tasks`` und
    ``email_triage`` weiter auf das Handle des Posteingangs -- Outlook-Links liefen
    ins Leere und die Triage erkannte die Mail beim naechsten Lauf nicht wieder.
    """
    db = _FakeDB()
    client = MagicMock()
    client.set_categories = AsyncMock()
    client.move_to_folder = AsyncMock(return_value={"id": "MID-2"})
    rule = _rule(
        [{"field": "domain", "op": "equals", "value": "example.ch"}],
        {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
    )

    await apply_deterministic_rules(db, client, _email(), [rule])

    assert {"tasks", "email_triage"} <= db.updated_tables()


@pytest.mark.asyncio
async def test_no_writeback_without_identity():
    """Ohne internetMessageId gibt es keinen Anker -- dann wird nichts geraten."""
    db = _FakeDB()
    client = MagicMock()
    client.set_categories = AsyncMock()
    client.move_to_folder = AsyncMock(return_value={"id": "MID-2"})
    email = _email()
    del email["internetMessageId"]
    rule = _rule(
        [{"field": "domain", "op": "equals", "value": "example.ch"}],
        {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
    )

    await apply_deterministic_rules(db, client, email, [rule])

    assert "tasks" not in db.updated_tables()


@pytest.mark.asyncio
async def test_non_matching_rule_does_not_apply():
    db = _FakeDB()
    client = MagicMock()
    client.set_categories = AsyncMock()
    client.move_to_folder = AsyncMock()
    rule = _rule(
        [{"field": "domain", "op": "equals", "value": "andere.ch"}],
        {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
    )

    handled = await apply_deterministic_rules(db, client, _email(), [rule])

    assert handled is False
    db.add.assert_not_called()
    client.set_categories.assert_not_called()
    client.move_to_folder.assert_not_called()


@pytest.mark.asyncio
async def test_first_matching_rule_wins():
    db = _FakeDB()
    client = MagicMock()
    client.set_categories = AsyncMock()
    client.move_to_folder = AsyncMock()
    miss = _rule(
        [{"field": "subject", "op": "contains", "value": "rechnung"}],
        {"triage_class": "fyi", "category": "Finanzen", "folder": "Finanzen"},
    )
    hit = _rule(
        [{"field": "subject", "op": "contains", "value": "newsletter"}],
        {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
    )

    handled = await apply_deterministic_rules(db, client, _email(), [miss, hit])

    assert handled is True
    client.set_categories.assert_awaited_once_with("MID-1", ["Newsletter"])


@pytest.mark.asyncio
async def test_no_rules_returns_false():
    db = _FakeDB()
    client = MagicMock()
    handled = await apply_deterministic_rules(db, client, _email(), [])
    assert handled is False
