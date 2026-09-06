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


def _client(sent=None):
    """Graph-Client-Double. ``sent`` ist das Ergebnis der Suche in Gesendetem.

    Die leere Liste heisst "Anthony hat dieser Adresse nie geschrieben" und gibt
    damit einen Move frei. Ohne dieses Double liefe ``_is_known_correspondent`` in
    seinen ``except``-Zweig, meldete "nicht prüfbar" und der Move bliebe aus --
    fail-closed und richtig, aber es wäre nicht der Fall, den der Test prüfen will.
    """
    client = MagicMock()
    client.set_categories = AsyncMock()
    client.move_to_folder = AsyncMock(return_value={"id": "MID-2"})
    client.search_my_replies_to = AsyncMock(return_value=sent if sent is not None else [])
    return client


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
    client = _client()
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
    client = _client()
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
    client = _client()
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
    client = _client()
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
    client = _client()
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


class TestRegelpfadRaeumtNichtBlind:
    """Der Regelpfad muss vor einem Move denselben Nachweis führen wie der Modellpfad.

    Bis zum 06.09.2026 lief ``_execute_deterministic_action`` an ``move_target()``
    vorbei und verschob direkt. Damit fehlte ihm die Sicherung, die im Modellpfad
    im August eingebaut wurde, nachdem 49 Mails namentlicher Absender aus dem
    Posteingang geräumt worden waren -- darunter «Projekt NITL -- Bitte um
    Rückmeldung».

    Gemessen betraf die Lücke damals keine aktive Regel: von neun Regeln hatten
    nur zwei einen Zielordner, und beide nannten den Absender wörtlich. Die
    Sicherung ist deshalb kein Rückbau, sondern ein Geländer für die Regel, die
    jemand künftig anlegt -- etwa ``domain equals t-r.ch``, was die gesamte
    Treuhänder-Korrespondenz wegräumen würde.
    """

    @pytest.mark.asyncio
    async def test_named_sender_moves_without_asking(self):
        """``sender equals`` ist eine Menschenentscheidung -- kein Nachweis nötig.

        Sonst kostete jede Regel einen Graph-Abruf, dessen Antwort bei einer reinen
        Versandadresse immer dieselbe ist.
        """
        db = _FakeDB()
        client = _client()
        rule = _rule(
            [{"field": "sender", "op": "equals", "value": "wordpress@innosmith.ch"}],
            {"triage_class": "fyi", "category": "System", "folder": "System"},
        )

        await apply_deterministic_rules(
            db, client, _email(address="wordpress@innosmith.ch"), [rule]
        )

        client.move_to_folder.assert_awaited_once_with("MID-1", "System")
        client.search_my_replies_to.assert_not_called()

    @pytest.mark.asyncio
    async def test_broad_rule_spares_own_correspondence(self):
        """Eine Domain-Regel darf keine Adresse wegräumen, der Anthony selbst schrieb."""
        db = _FakeDB()
        client = _client(sent=[{"id": "SENT-1"}])
        rule = _rule(
            [{"field": "domain", "op": "equals", "value": "t-r.ch"}],
            {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
        )

        handled = await apply_deterministic_rules(
            db, client, _email(address="dominique.chuard@t-r.ch"), [rule]
        )

        # Die Regel greift und kategorisiert -- nur verschoben wird nicht.
        assert handled is True
        client.set_categories.assert_awaited_once_with("MID-1", ["Newsletter"])
        client.move_to_folder.assert_not_called()

    @pytest.mark.asyncio
    async def test_unprovable_correspondence_blocks_the_move(self):
        """Ein Ausfall der Graph-Suche darf keinen Move freigeben.

        Dieselbe Richtung wie in ``move_target``, wo ``None`` als "ist ein Kontakt"
        zählt: Nichtwissen ist kein Freibrief.
        """
        db = _FakeDB()
        client = _client()
        client.search_my_replies_to = AsyncMock(side_effect=RuntimeError("Graph down"))
        rule = _rule(
            [{"field": "subject", "op": "contains", "value": "newsletter"}],
            {"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"},
        )

        await apply_deterministic_rules(db, client, _email(), [rule])

        client.move_to_folder.assert_not_called()

    @pytest.mark.asyncio
    async def test_rule_does_not_claim_measured_confidence(self):
        """Eine Regel ist eine Setzung, keine Messung.

        ``confidence=1.0`` stand im Cockpit neben echten Modellwerten und sah damit
        aus wie das sicherste Urteil des Systems.
        """
        db = _FakeDB()
        client = _client()
        rule = _rule(
            [{"field": "sender", "op": "equals", "value": "kunde@example.ch"}],
            {"triage_class": "fyi", "category": "Finanzen"},
        )

        await apply_deterministic_rules(db, client, _email(), [rule])

        record = db.add.call_args.args[0]
        assert record.confidence is None
        assert record.suggested_action["deterministic_override"] == str(rule.id)

    @pytest.mark.asyncio
    async def test_invalid_action_class_is_fail_closed(self):
        """Eine Regel mit unerlaubter Klasse tötet nicht den CHECK-Constraint.

        ``auto_reply`` ist auf diesem Pfad sinnlos -- hier schreibt niemand einen
        Entwurf --, und ein Fantasiewert würde in der Datenbank denselben
        ``email_triage_triage_class_check`` reissen wie eine erfundene Modellklasse.
        """
        db = _FakeDB()
        client = _client()
        rule = _rule(
            [{"field": "sender", "op": "equals", "value": "kunde@example.ch"}],
            {"triage_class": "auto_reply", "category": "Wichtig"},
        )

        await apply_deterministic_rules(db, client, _email(), [rule])

        assert db.add.call_args.args[0].triage_class == "fyi"
