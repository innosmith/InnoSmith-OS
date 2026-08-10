"""Golden-Set-Regressionstest fuer die Triage-Entscheidungslogik (_post_process_triage).

Dies ist das Offline-Regressionsnetz gegen das "Wochen-Pendeln": es haelt das
fail-closed-Verhalten fest und haette die Regression aus Commit c061b17
(unverwertbarer Output -> Auto-Task) gefangen. Kein LLM/keine echte DB -- die
externen Effekte (Task-Erstellung, Outlook-Finalisierung, Episode, Notify) sind
gemockt; geprueft wird die *Entscheidung* (welche Klasse, Task ja/nein, Status).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.hermes_worker as hw


class _FakeDB:
    def __init__(self, job=None):
        self._job = job
        self.commit = AsyncMock()

    async def execute(self, *args, **kwargs):
        res = MagicMock()
        res.scalar_one_or_none.return_value = self._job
        return res


class _FakeSession:
    def __init__(self, job=None):
        self._db = _FakeDB(job)

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


def _session_factory(job=None):
    return lambda: _FakeSession(job)


_META = {
    "email_message_id": "M1",
    "subject": "Testbetreff",
    "from_address": "kunde@example.ch",
    "from_name": "Kundin",
    "conversation_id": "",
}


def _patches(job=None):
    """Standard-Mocks: DB-Session + alle externen Seiteneffekte."""
    return [
        patch.object(hw, "async_session", _session_factory(job)),
        patch.object(hw, "_create_email_task", new=AsyncMock(return_value=None)),
        patch.object(hw, "_finalize_email_state", new=AsyncMock()),
        patch.object(hw, "record_episode", new=AsyncMock()),
        patch.object(hw, "notify_agent_awaiting_approval", new=AsyncMock()),
        patch.object(hw, "_snapshot_agent_draft", new=AsyncMock(return_value=None)),
        patch.object(
            hw, "_ensure_draft_in_thread",
            new=AsyncMock(side_effect=lambda d, m, s: (d, s)),
        ),
    ]


@pytest.mark.asyncio
async def test_no_json_block_is_fail_closed_no_task():
    """Kein verwertbarer JSON-Block -> fyi/needs-review, NIE ein Auto-Task (c061b17)."""
    content = "Ich habe die Mail gelesen, aber keinen JSON-Block ausgegeben."
    ctx = _patches()
    with ctx[0], ctx[1] as create_task, ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
        status = await hw._post_process_triage(uuid.uuid4(), content, dict(_META), None, [], None)
    assert status == "completed"
    create_task.assert_not_called()
    finalize.assert_called_once()


@pytest.mark.asyncio
async def test_task_json_creates_task():
    """Sauberes task-JSON -> Task wird erstellt."""
    content = 'Entscheid: {"triage_class": "task", "label": "Wichtig", "task_title": "Offerte prüfen"}'
    ctx = _patches()
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6]:
        status = await hw._post_process_triage(uuid.uuid4(), content, dict(_META), None, [], None)
    assert status == "completed"
    create_task.assert_called_once()


@pytest.mark.asyncio
async def test_auto_reply_without_draft_downgrades_to_fyi_no_task():
    """auto_reply ohne echten Entwurf -> fyi (kein Task), fail-closed."""
    content = '{"triage_class": "auto_reply", "label": "Wichtig"}'
    ctx = _patches()
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6]:
        status = await hw._post_process_triage(uuid.uuid4(), content, dict(_META), None, [], None)
    # fyi -> kein Task, Status completed (nicht awaiting_approval)
    assert status == "completed"
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_auto_reply_with_draft_awaits_approval():
    """auto_reply MIT echtem Entwurf -> awaiting_approval, kein Task.

    Der Entwurf stammt hier aus dem Schreib-Pass. Ein Entwurf, den bereits der
    Klassifikations-Lauf mitbringt, wird dagegen verworfen -- siehe
    ``test_two_pass_draft.py``.
    """
    content = '{"triage_class": "auto_reply", "label": "Wichtig"}'
    job = SimpleNamespace(metadata_json={})
    ctx = _patches(job=job)
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], \
            patch.object(hw, "_generate_reply_draft", new=AsyncMock(return_value="DRAFT-1")):
        status = await hw._post_process_triage(
            uuid.uuid4(), content, dict(_META),
            None, ["search_sender_history", "get_sender_profile", "search_my_replies"], None,
        )
    assert status == "awaiting_approval"
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_forced_class_task_overrides_auto_reply_with_draft():
    """forced_class=task hat Vorrang: trotz Entwurf wird ein Task erstellt (kein Auto-Switch)."""
    content = '{"triage_class": "auto_reply", "label": "Wichtig", "task_title": "Manuell entscheiden"}'
    meta = dict(_META)
    meta["forced_class"] = "task"
    ctx = _patches()
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6]:
        status = await hw._post_process_triage(uuid.uuid4(), content, meta, "DRAFT-9", [], None)
    assert status == "completed"
    create_task.assert_called_once()


async def _needs_review_for(content: str) -> bool:
    """Fuehrt das Post-Processing aus und liefert das an Outlook gemeldete needs_review."""
    ctx = _patches()
    with ctx[0], ctx[1], ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
        await hw._post_process_triage(uuid.uuid4(), content, dict(_META), None, [], None)
    finalize.assert_called_once()
    return finalize.call_args.kwargs["needs_review"]


class TestUnsicherheitBremstDenMove:
    """``needs_review`` ist die einzige Bremse vor einem Move -- sie muss alles tragen.

    Vorfall vom 10.08.2026: Eine Kundenrueckfrage von justin.springer@swissbankers.ch
    wurde als ``label='System'`` + ``fyi`` eingeordnet und waere damit nach
    ``Inbox/System`` verschwunden. Das Modell hatte ``"confidence": "high"``
    geliefert -- ein String. ``float("high")`` wirft ValueError, der Handler setzte
    still ``confidence = None``, und das Gate darunter prueft
    ``confidence is not None and confidence < schwelle``. Der unsicherste Lauf des
    Tages galt damit als der sicherste.

    Zweiter Teil desselben Vorfalls: An ``_finalize_email_state`` ging nur
    ``label_invalid``, waehrend die DB nur die Confidence-Haelfte speicherte. Beide
    Haelften kannten einander nicht, deshalb stand eine Mail mit verworfenem Label
    (``Unklar``) mit ``needs_review: false`` in der Datenbank.

    Aufgefallen ist beides erst, als die Bedingung ``inferenceClassification ==
    'other'`` aus ``move_target`` entfiel -- bis dahin verdeckte sie den Fehler, weil
    sie 79 % aller Mails ohnehin blockierte.
    """

    @pytest.mark.asyncio
    async def test_non_numeric_confidence_blocks_move(self):
        """Der Springer-Fall: ``"high"`` ist keine Zahl und darf nicht als sicher gelten."""
        content = '{"triage_class": "fyi", "label": "System", "confidence": "high"}'
        assert await _needs_review_for(content) is True

    @pytest.mark.asyncio
    async def test_missing_confidence_blocks_move(self):
        """Fehlende Selbsteinschaetzung ist Unsicherheit, nicht Sicherheit.

        ``confidence`` ist laut Skill Pflichtfeld; sein Fehlen ist ein Vertragsbruch
        des Modells. Preis dafuer sind gemessene 14 von 177 Mails (30 Tage), die
        liegen bleiben -- genau die Laeufe, in denen unsauber gearbeitet wurde.
        """
        content = '{"triage_class": "fyi", "label": "Newsletter"}'
        assert await _needs_review_for(content) is True

    @pytest.mark.asyncio
    async def test_low_confidence_blocks_move(self):
        content = '{"triage_class": "fyi", "label": "System", "confidence": 0.3}'
        assert await _needs_review_for(content) is True

    @pytest.mark.asyncio
    async def test_rejected_label_blocks_move(self):
        """Ein erfundenes Label bremst weiterhin -- auch bei hoher Confidence."""
        content = '{"triage_class": "fyi", "label": "System-Info", "confidence": 0.99}'
        assert await _needs_review_for(content) is True

    @pytest.mark.asyncio
    async def test_confident_classification_allows_move(self):
        """Die Gegenprobe: ein sauberer Lauf wird nicht gebremst.

        Ohne diese Zusicherung waere die Bremse ein zweites Pauschal-Gate -- genau
        der Fehler, den ``inferenceClassification == 'other'`` gemacht hat.
        """
        content = '{"triage_class": "fyi", "label": "Newsletter", "confidence": 0.95}'
        assert await _needs_review_for(content) is False


class TestBeraterKorrekturSchlaegtModell:
    """Eine Label-Korrektur aus dem Cockpit muss den Korrektur-Job ueberdauern.

    Der Reclassify-Endpunkt setzt die Kategorie zuerst selbst
    (``_apply_label_correction``) und reiht danach einen Job mit ``forced_class``
    ein. Dieser Job lief bisher ohne Kenntnis der Label-Korrektur durch
    ``_finalize_email_state`` und schrieb die Kategorie mit dem frisch geratenen
    LLM-Label zurueck -- die Korrektur hielt Sekunden.

    Konkret: Die Kundenmail von Justin Springer sollte am 10.08.2026 von ``System``
    auf ``Wichtig`` + ``task`` korrigiert werden. Ohne ``forced_label`` haette der
    Korrektur-Job in Outlook wieder ``System`` gesetzt.
    """

    @pytest.mark.asyncio
    async def test_forced_label_overrides_model_label(self):
        content = '{"triage_class": "task", "label": "System", "task_title": "Antwort an Justin", "confidence": 0.9}'
        meta = dict(_META)
        meta["forced_label"] = "Wichtig"
        ctx = _patches()
        with ctx[0], ctx[1], ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
            await hw._post_process_triage(uuid.uuid4(), content, meta, None, [], None)
        assert finalize.call_args.args[1] == "Wichtig"

    @pytest.mark.asyncio
    async def test_forced_label_clears_needs_review(self):
        """Ein von Hand korrigierter Fall ist gesichtet -- auch ohne Confidence."""
        content = '{"triage_class": "task", "label": "System", "task_title": "X"}'
        meta = dict(_META)
        meta["forced_label"] = "Wichtig"
        ctx = _patches()
        with ctx[0], ctx[1], ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
            await hw._post_process_triage(uuid.uuid4(), content, meta, None, [], None)
        assert finalize.call_args.kwargs["needs_review"] is False

    @pytest.mark.asyncio
    async def test_invalid_forced_label_is_ignored(self):
        """Nur Labels aus dem Vokabular erzwingen etwas -- keine Hintertuer."""
        content = '{"triage_class": "task", "label": "Wichtig", "task_title": "X", "confidence": 0.9}'
        meta = dict(_META)
        meta["forced_label"] = "Kundenkram"
        ctx = _patches()
        with ctx[0], ctx[1], ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
            await hw._post_process_triage(uuid.uuid4(), content, meta, None, [], None)
        assert finalize.call_args.args[1] == "Wichtig"
