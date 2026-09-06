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
        # ``_finalize_email_state`` gibt seit dem 24.08.2026 zusaetzlich das nach
        # allen Schritten gueltige Handle zurueck; der Aufrufer schreibt es in die
        # Datenbank. Das Double muss die Form mitbringen, sonst prueft der Test eine
        # Schnittstelle, die es nicht gibt.
        patch.object(
            hw, "_finalize_email_state",
            new=AsyncMock(return_value=hw.FinalizeResult(None, "M1")),
        ),
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


async def _finalize_kwargs_for(content: str, meta: dict | None = None) -> dict:
    """Wie ``_needs_review_for``, liefert aber alle Argumente der Finalisierung."""
    ctx = _patches()
    with ctx[0], ctx[1], ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
        await hw._post_process_triage(
            uuid.uuid4(), content, meta if meta is not None else dict(_META), None, [], None
        )
    finalize.assert_called_once()
    return finalize.call_args.kwargs


class TestKlassenWeissliste:
    """Eine unbekannte ``triage_class`` darf den Job nicht mehr töten.

    Vorfall, fünfmal in zehn Wochen und zuletzt am 04.09.2026: Der Rohwert des
    Modells lief ungeprüft bis in die Datenbank, wo
    ``email_triage_triage_class_check`` zuschlug. Der Zuschlag traf nicht das Feld,
    sondern den ganzen Job -- die Mail blieb ohne Kategorie, ohne Aufgabe und ohne
    Sichtungsmarke liegen, und im Cockpit sah das aus wie ein technischer Ausfall
    ohne Bezug zur Mail.

    Geprüft werden die vier real gemessenen Ursachen, nicht erfundene Beispiele.
    Der Weg dahin war eine Synonymtabelle, die genau drei Umbenennungen kannte und
    alles andere durchliess -- deshalb steht hier eine Weissliste und keine
    Erweiterung der Tabelle: die Menge der Fehlformen ist offen, die Menge der
    gültigen Klassen ist geschlossen.
    """

    @pytest.mark.parametrize(
        "raw,ursache",
        [
            ("f_yi", "Tippfehler in einer gültigen Klasse"),
            ("system", "Label ins Klassenfeld gerutscht"),
            ("newsletter", "Label ins Klassenfeld gerutscht"),
            ("none", "Verweigerung"),
        ],
    )
    @pytest.mark.asyncio
    async def test_measured_invalid_classes_fall_back_to_fyi(self, raw, ursache):
        content = f'{{"triage_class": "{raw}", "label": "System", "confidence": 0.95}}'
        kwargs = await _finalize_kwargs_for(content)
        assert kwargs["triage_class"] == "fyi", ursache
        assert kwargs["needs_review"] is True, ursache

    @pytest.mark.asyncio
    async def test_undecided_class_becomes_a_review_case_not_a_task(self):
        """``task/auto_reply`` ist Unentschiedenheit -- das Backend entscheidet nicht nach.

        Der gemessene Fall (10.07.2026, Kundenmail von sandro.scheidt@be.ch) kam mit
        ``confidence 0.85``, fertigem ``task_title`` und ``needs_review: false``. Die
        naheliegende Rettung wäre, die erste genannte Klasse zu nehmen. Genau das
        wäre falsch: das Modell hat sich nicht entschieden, also entscheidet ein
        Mensch. Ohne die Sichtungsmarke würde daraus eine stumme ``fyi``-Mail, und
        der Kunde wartet auf eine Antwort, die niemand mehr sieht.
        """
        content = (
            '{"triage_class": "task/auto_reply", "label": "Wichtig", '
            '"task_title": "COflow: Umsetzungspunkte", "confidence": 0.85}'
        )
        ctx = _patches()
        with ctx[0], ctx[1] as create_task, ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
            status = await hw._post_process_triage(
                uuid.uuid4(), content, dict(_META), None, [], None
            )
        assert status == "completed"
        create_task.assert_not_called()
        assert finalize.call_args.kwargs["triage_class"] == "fyi"
        assert finalize.call_args.kwargs["needs_review"] is True

    @pytest.mark.asyncio
    async def test_legacy_synonyms_still_translate(self):
        """``board_task`` stammt aus einer älteren Skill-Fassung und meint ``task``.

        Die drei Aliasse sind keine Heuristik, sondern Übersetzungen bekannter
        Umbenennungen (Migration ``c9e2a4b6d8f0``). Sie dürfen die Weissliste
        passieren, ohne eine Sichtungsmarke auszulösen.
        """
        content = (
            '{"triage_class": "board_task", "label": "Wichtig", '
            '"task_title": "Offerte prüfen", "confidence": 0.9}'
        )
        ctx = _patches()
        with ctx[0], ctx[1] as create_task, ctx[2] as finalize, ctx[3], ctx[4], ctx[5], ctx[6]:
            await hw._post_process_triage(uuid.uuid4(), content, dict(_META), None, [], None)
        create_task.assert_called_once()
        assert finalize.call_args.kwargs["triage_class"] == "task"
        assert finalize.call_args.kwargs["needs_review"] is False

    @pytest.mark.asyncio
    async def test_human_class_correction_clears_the_review_mark(self):
        """Hat der Mensch die Klasse gesetzt, ist der Fall gesichtet.

        Sonst trüge eine gerade von Hand korrigierte Mail eine Sichtungsmarke --
        dieselbe Begründung wie bei ``forced_label``.
        """
        meta = dict(_META)
        meta["forced_class"] = "task"
        content = '{"triage_class": "newsletter", "label": "Wichtig", "confidence": 0.95}'
        kwargs = await _finalize_kwargs_for(content, meta)
        assert kwargs["triage_class"] == "task"
        assert kwargs["needs_review"] is False

    @pytest.mark.asyncio
    async def test_valid_class_passes_untouched(self):
        """Gegenprobe: die Weissliste ist kein zweites Pauschal-Gate."""
        content = '{"triage_class": "fyi", "label": "Newsletter", "confidence": 0.95}'
        kwargs = await _finalize_kwargs_for(content)
        assert kwargs["triage_class"] == "fyi"
        assert kwargs["needs_review"] is False


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
    async def test_model_chosen_unklar_is_treated_as_a_rejected_label(self):
        """``Unklar`` vom Modell ist ein Ausweichen und muss zur Sichtung fuehren.

        Vorfall 28.07.-17.08.2026: ``Unklar`` war waehlbar und der Skill empfahl es
        bei Unsicherheit. Gemessen trugen danach 60 Mails dieses Label, davon 51 mit
        ``needs_review = false`` und teils Confidence 0.9 -- der Fall war damit
        weder Aufgabe noch Sichtungsposten und blieb unbearbeitet liegen. Seit der
        Trennung von ``TRIAGE_LABELS`` und ``AGENT_LABELS`` laeuft dieser Fall in
        denselben Pfad wie ein erfundenes Label.
        """
        content = '{"triage_class": "fyi", "label": "Unklar", "confidence": 0.9}'
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
