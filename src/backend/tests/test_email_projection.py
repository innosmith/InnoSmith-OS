"""Tests fuer die zwei Abbildungen von «zu dieser Mail gibt es offene Arbeit».

Zwei Eigenschaften traegt dieses Modul, und beide sind unsichtbar, wenn sie brechen:

1. **Die Fahne behaelt eine einzige Bedeutung.** Sie wird gesetzt, wenn Arbeit offen
   ist, und aufgeloest, wenn sie erledigt ist. Bliebe sie nach dem Erledigen stehen,
   wuerde die Outlook-Suche nach markierten Mails ueber Wochen wertlos -- ohne dass
   irgendetwas fehlschlaegt.
2. **TaskPilot verschiebt nur vorwaerts.** Nichts hier darf eine Mail zurueckholen,
   die der Mensch bewusst weggeraeumt hat. Ein System, das das tut, ist unberechenbar,
   und ein Ordner, dem man nicht glaubt, ist wertlos.

Beides sind Regeln, keine Implementierungsdetails. Darum stehen sie als Tests da.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.services import email_projection as ep
from graph_client import GraphClient

IDENTITY = "<AS8P123MB4567.EURP987@AS8P123MB4567.eurprd08.prod.outlook.com>"


class _FakeGraph:
    """Protokolliert, was am Postfach geschehen waere."""

    def __init__(self, *, folder_exists=True, get_fails=False):
        self._folder_exists = folder_exists
        self._get_fails = get_fails
        self.flags: list[tuple[str, bool]] = []
        self.moves: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self.closed = False

    async def get_email(self, message_id):
        if self._get_fails:
            raise RuntimeError("404")
        return {"id": message_id, "internetMessageId": IDENTITY}

    async def set_flag(self, message_id, flagged):
        self.flags.append((message_id, flagged))
        return {"id": message_id}

    async def move_to_folder(self, message_id, folder_name):
        if not self._folder_exists:
            raise ValueError(f"Ordner '{folder_name}' existiert nicht")
        self.moves.append((message_id, folder_name))
        return {"id": "handle-im-tasks-ordner"}

    async def archive_email(self, message_id):
        self.archived.append(message_id)
        return {"id": "handle-im-archiv"}

    async def get_or_create_folder(self, name, parent_folder="inbox"):
        if not self._folder_exists:
            raise ValueError(f"Ordner '{name}' existiert nicht")
        return {"id": "folder-tasks", "displayName": name}

    async def list_emails(self, folder=None, top=20, skip=0, filter_str=None):
        return {"value": self.folder_contents}

    folder_contents: list[dict] = []

    async def close(self):
        self.closed = True


def _task(*, completed=False, handle="handle-posteingang", identity=IDENTITY):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email_message_id=handle,
        internet_message_id=identity,
        is_completed=completed,
    )


@pytest.fixture
def graph(monkeypatch):
    fake = _FakeGraph()
    monkeypatch.setattr(ep, "get_graph_client", lambda: fake)
    monkeypatch.setattr(ep, "sync_message_id", _noop_sync)
    monkeypatch.setattr(ep, "get_tasks_folder_name", _folder_name("Tasks"))
    return fake


async def _noop_sync(db, *, internet_message_id, new_message_id):
    return None


def _folder_name(name):
    async def _get(db):
        return name
    return _get


class TestMarkOpenWork:
    @pytest.mark.asyncio
    async def test_sets_flag_and_moves_to_tasks_folder(self, graph):
        task = _task()
        await ep.mark_open_work(None, task)
        assert graph.flags == [("handle-posteingang", True)]
        assert graph.moves == [("handle-posteingang", "Tasks")]

    @pytest.mark.asyncio
    async def test_flag_comes_before_move(self, graph):
        """Ein Move vergibt ein neues Handle -- danach waere das alte veraltet."""
        order: list[str] = []
        original_flag, original_move = graph.set_flag, graph.move_to_folder

        async def flag(mid, flagged):
            order.append("flag")
            return await original_flag(mid, flagged)

        async def move(mid, folder):
            order.append("move")
            return await original_move(mid, folder)

        graph.set_flag, graph.move_to_folder = flag, move
        await ep.mark_open_work(None, _task())
        assert order == ["flag", "move"]

    @pytest.mark.asyncio
    async def test_new_handle_is_written_to_the_task(self, graph):
        """Ohne Rueckschreiben zeigt der Outlook-Link am Task ins Leere."""
        task = _task()
        await ep.mark_open_work(None, task)
        assert task.email_message_id == "handle-im-tasks-ordner"

    @pytest.mark.asyncio
    async def test_missing_folder_leaves_the_flag_standing(self, monkeypatch):
        """Fehlt der Ordner, traegt die Fahne allein -- kein Halbzustand, keine Ausnahme.

        ``get_or_create_folder`` legt bewusst keine Ordner an: Der Ordner liegt im
        Postfach des Menschen, und der benennt und erstellt ihn selbst.
        """
        fake = _FakeGraph(folder_exists=False)
        monkeypatch.setattr(ep, "get_graph_client", lambda: fake)
        monkeypatch.setattr(ep, "sync_message_id", _noop_sync)
        monkeypatch.setattr(ep, "get_tasks_folder_name", _folder_name("Tasks"))

        task = _task()
        await ep.mark_open_work(None, task)
        assert fake.flags == [("handle-posteingang", True)]
        assert fake.moves == []
        assert task.email_message_id == "handle-posteingang"

    @pytest.mark.asyncio
    async def test_empty_folder_setting_means_flag_only(self, monkeypatch):
        """Leerer Ordnername ist ein zulaessiger Betriebsmodus, kein Fehlerfall."""
        fake = _FakeGraph()
        monkeypatch.setattr(ep, "get_graph_client", lambda: fake)
        monkeypatch.setattr(ep, "sync_message_id", _noop_sync)
        monkeypatch.setattr(ep, "get_tasks_folder_name", _folder_name(""))

        await ep.mark_open_work(None, _task())
        assert fake.flags and fake.moves == []

    @pytest.mark.asyncio
    async def test_stale_handle_is_resolved_over_the_identity(self, monkeypatch):
        """Ein veraltetes Handle ist der Regelfall, sobald Mails wandern."""
        fake = _FakeGraph(get_fails=True)
        monkeypatch.setattr(ep, "get_graph_client", lambda: fake)
        monkeypatch.setattr(ep, "sync_message_id", _noop_sync)
        monkeypatch.setattr(ep, "get_tasks_folder_name", _folder_name("Tasks"))

        async def resolve(client, identity):
            return "handle-frisch" if identity == IDENTITY else None

        monkeypatch.setattr(ep, "resolve_message_id", resolve)

        await ep.mark_open_work(None, _task())
        assert fake.flags == [("handle-frisch", True)]

    @pytest.mark.asyncio
    async def test_task_without_mail_does_nothing(self, graph):
        task = _task(handle=None, identity=None)
        await ep.mark_open_work(None, task)
        assert graph.flags == [] and graph.moves == []


class TestReleaseOpenWork:
    @pytest.mark.asyncio
    async def test_removes_flag_and_archives(self, graph):
        task = _task(completed=True)
        await ep.release_open_work(None, task)
        assert graph.flags == [("handle-posteingang", False)]
        assert graph.archived == ["handle-posteingang"]
        assert task.email_message_id == "handle-im-archiv"

    @pytest.mark.asyncio
    async def test_failing_archive_does_not_raise(self, monkeypatch):
        fake = _FakeGraph()

        async def boom(mid):
            raise RuntimeError("Graph nicht erreichbar")

        fake.archive_email = boom
        monkeypatch.setattr(ep, "get_graph_client", lambda: fake)
        monkeypatch.setattr(ep, "sync_message_id", _noop_sync)
        monkeypatch.setattr(ep, "get_tasks_folder_name", _folder_name("Tasks"))

        task = _task(completed=True)
        await ep.release_open_work(None, task)
        assert task.email_message_id == "handle-posteingang"


class TestReconciler:
    """Der Abgleich darf ausschliesslich vorwaerts wirken."""

    @staticmethod
    def _mail(identity, subject="Betreff"):
        return {"id": f"handle-{identity}", "internetMessageId": identity, "subject": subject}

    @pytest.mark.asyncio
    async def test_completed_task_gets_its_mail_archived(self, graph, monkeypatch):
        """Der eigentliche Reparaturfall: das Aufloesen beim Erledigen war ausgefallen."""
        task = _task(completed=True, handle="handle-A", identity="A")
        graph.folder_contents = [self._mail("A")]
        monkeypatch.setattr(ep, "_task_index", _index({"A": task}))

        repariert = await ep.reconcile_tasks_folder(None)
        assert repariert == 1
        assert graph.archived == ["handle-A"]
        assert graph.flags == [("handle-A", False)]

    @pytest.mark.asyncio
    async def test_open_task_is_left_alone(self, graph, monkeypatch):
        task = _task(completed=False, handle="handle-A", identity="A")
        graph.folder_contents = [self._mail("A")]
        monkeypatch.setattr(ep, "_task_index", _index({"A": task}))

        assert await ep.reconcile_tasks_folder(None) == 0
        assert graph.archived == [] and graph.flags == []

    @pytest.mark.asyncio
    async def test_mail_without_task_stays_where_the_human_put_it(self, graph, monkeypatch):
        """Wer eine Mail selbst in den Ordner zieht, hat eine Absicht.

        Ein System, das sie eine Viertelstunde spaeter wegraeumt, ist genau die
        Unberechenbarkeit, die dieser Ordner vermeiden soll.
        """
        graph.folder_contents = [self._mail("unbekannt")]
        monkeypatch.setattr(ep, "_task_index", _index({}))

        assert await ep.reconcile_tasks_folder(None) == 0
        assert graph.archived == [] and graph.flags == []

    @pytest.mark.asyncio
    async def test_mail_outside_the_folder_is_never_pulled_back(self, graph, monkeypatch):
        """Der Verzicht auf das Zurueckholen ist die Sicherheitsgarantie.

        Die Fahne reist mit der Mail -- der Ordner darf unscharf werden, ohne dass
        Information verloren geht.
        """
        task = _task(completed=False, handle="handle-A", identity="A")
        graph.folder_contents = []
        monkeypatch.setattr(ep, "_task_index", _index({"A": task}))

        assert await ep.reconcile_tasks_folder(None) == 0
        assert graph.moves == [] and graph.flags == []

    @pytest.mark.asyncio
    async def test_missing_folder_skips_the_whole_run(self, monkeypatch):
        fake = _FakeGraph(folder_exists=False)
        monkeypatch.setattr(ep, "get_graph_client", lambda: fake)
        monkeypatch.setattr(ep, "get_tasks_folder_name", _folder_name("Tasks"))

        assert await ep.reconcile_tasks_folder(None) == 0
        assert fake.archived == []


def _index(mapping):
    async def _get(db):
        offen = [i for i, t in mapping.items() if not t.is_completed]
        return mapping, offen
    return _get


class TestSetFlagKeepsReadState:
    """Jeder PATCH kippt ``isRead`` auf true -- dieselbe Falle wie bei Kategorien.

    Ohne Wiederherstellung waere eine ungelesene Mail nach dem Setzen der Fahne
    stillschweigend gelesen und damit aus dem Blick. Genau der Schaden, den der
    Tasks-Ordner verhindern soll.
    """

    class _Client:
        _user_path = "/users/me"
        set_flag = GraphClient.set_flag
        mark_as_unread = GraphClient.mark_as_unread

        def __init__(self, was_read):
            self._was_read = was_read
            self.patches: list[dict] = []

        async def _get(self, path, params=None):
            return {"id": "M1", "isRead": self._was_read}

        async def _patch(self, path, body):
            self.patches.append(body)
            return {"id": "M1"}

    @pytest.mark.asyncio
    async def test_unread_mail_stays_unread(self):
        client = self._Client(was_read=False)
        await client.set_flag("M1", True)
        assert client.patches[0]["flag"]["flagStatus"] == "flagged"
        assert client.patches[-1] == {"isRead": False}

    @pytest.mark.asyncio
    async def test_read_mail_is_not_marked_unread(self):
        client = self._Client(was_read=True)
        await client.set_flag("M1", True)
        assert len(client.patches) == 1

    @pytest.mark.asyncio
    async def test_clearing_the_flag_uses_notflagged(self):
        client = self._Client(was_read=True)
        await client.set_flag("M1", False)
        assert client.patches[0]["flag"]["flagStatus"] == "notFlagged"
