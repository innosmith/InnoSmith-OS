"""Tests fuer die Trennung von E-Mail-Identitaet und Graph-Handle.

Hintergrund des Vorfalls, den diese Tests absichern: Die Graph-``id`` einer Nachricht
wechselt bei jedem Ordnerwechsel. TaskPilot speicherte nur diese ID und schrieb sie
nach eigenen Moves nie zurueck. Sobald ein Task bestaetigt und die Quell-Mail
archiviert war, zeigte ``tasks.email_message_id`` auf ein Handle, das es nicht mehr
gab -- der Outlook-Link im Task-Detail fuehrte ins Leere, und der Triage-Dedupe
konnte dieselbe Mail nach einem Move nicht wiedererkennen.

Ohne diesen Kontext liest sich ``internet_message_id`` wie ein redundantes zweites
Feld, und der naechste Umbau wirft es weg.
"""

import pytest

from app.services.email_identity import (
    fetch_internet_message_id,
    resolve_message_id,
)
from app.services.email_links import outlook_deeplink
from graph_client import GraphClient


class _FakeGraph:
    """Minimales Double auf der echten (ungebundenen) Client-Methode.

    So testet der Fall die tatsaechliche Filter- und Auswertungslogik von
    ``find_by_internet_message_id`` und nicht eine Nachbildung davon.
    """

    _user_path = "/users/me"
    _drop_drafts = staticmethod(GraphClient._drop_drafts)
    find_by_internet_message_id = GraphClient.find_by_internet_message_id

    def __init__(self, response=None, raise_on_get=False):
        self._response = response or {"value": []}
        self._raise = raise_on_get
        self.params: list[dict] = []

    async def _get(self, path, params=None):
        if self._raise:
            raise RuntimeError("Graph nicht erreichbar")
        self.params.append(params or {})
        return self._response


IDENTITY = "<AS8P123MB4567.EURP987@AS8P123MB4567.eurprd08.prod.outlook.com>"


class TestFindByInternetMessageId:
    @pytest.mark.asyncio
    async def test_filters_on_internet_message_id(self):
        fake = _FakeGraph({"value": [{"id": "handle-neu", "isDraft": False}]})
        msg = await fake.find_by_internet_message_id(IDENTITY)
        assert msg["id"] == "handle-neu"
        assert fake.params[0]["$filter"] == f"internetMessageId eq '{IDENTITY}'"

    @pytest.mark.asyncio
    async def test_skips_drafts(self):
        """Ein Entwurf ist keine Fundstelle -- er wurde nie gesendet."""
        fake = _FakeGraph(
            {"value": [{"id": "entwurf", "isDraft": True}, {"id": "echt", "isDraft": False}]}
        )
        msg = await fake.find_by_internet_message_id(IDENTITY)
        assert msg["id"] == "echt"

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_found(self):
        fake = _FakeGraph({"value": []})
        assert await fake.find_by_internet_message_id(IDENTITY) is None

    @pytest.mark.asyncio
    async def test_empty_identity_makes_no_request(self):
        fake = _FakeGraph()
        assert await fake.find_by_internet_message_id("") is None
        assert fake.params == []

    @pytest.mark.asyncio
    async def test_escapes_single_quotes_in_filter(self):
        """Ein Apostroph im Wert wuerde den OData-Filter sonst zerreissen (400)."""
        fake = _FakeGraph({"value": []})
        await fake.find_by_internet_message_id("<o'brien@example.com>")
        assert fake.params[0]["$filter"] == "internetMessageId eq '<o''brien@example.com>'"


class TestResolveMessageId:
    @pytest.mark.asyncio
    async def test_returns_current_handle_after_move(self):
        """Der Kern: nach einem Move fuehrt die Identitaet auf das neue Handle."""
        fake = _FakeGraph({"value": [{"id": "handle-im-archiv", "isDraft": False}]})
        assert await resolve_message_id(fake, IDENTITY) == "handle-im-archiv"

    @pytest.mark.asyncio
    async def test_graph_failure_yields_none_instead_of_raising(self):
        """Best-effort: ein Ausfall darf keinen Request und keinen Job sprengen."""
        fake = _FakeGraph(raise_on_get=True)
        assert await resolve_message_id(fake, IDENTITY) is None

    @pytest.mark.asyncio
    async def test_missing_identity_yields_none(self):
        assert await resolve_message_id(_FakeGraph(), None) is None


class _FakeMailClient:
    def __init__(self, payload=None, raise_on_get=False):
        self._payload = payload or {}
        self._raise = raise_on_get

    async def get_email(self, message_id):
        if self._raise:
            raise RuntimeError("404")
        return self._payload


class TestFetchInternetMessageId:
    @pytest.mark.asyncio
    async def test_reads_identity_for_known_handle(self):
        client = _FakeMailClient({"id": "handle", "internetMessageId": IDENTITY})
        assert await fetch_internet_message_id(client, "handle") == IDENTITY

    @pytest.mark.asyncio
    async def test_absent_field_yields_none(self):
        client = _FakeMailClient({"id": "handle"})
        assert await fetch_internet_message_id(client, "handle") is None

    @pytest.mark.asyncio
    async def test_dead_handle_yields_none(self):
        client = _FakeMailClient(raise_on_get=True)
        assert await fetch_internet_message_id(client, "handle") is None


class TestSelectCarriesIdentity:
    """Ohne ``internetMessageId`` im ``$select`` traegt keine Zeile eine Identitaet.

    Diese Zusicherung ist die Voraussetzung fuer alles andere: Triage-Dedupe und
    Task-Erstellung lesen den Wert aus der Listenantwort. Faellt er aus dem
    ``$select``, bleiben alle neuen Zeilen still ohne Identitaet -- ein Fehler, den
    kein Verhalten sofort zeigt.
    """

    def test_list_emails_selects_identity(self):
        import inspect

        src = inspect.getsource(GraphClient.list_emails)
        assert "internetMessageId" in src

    def test_get_email_selects_identity(self):
        import inspect

        src = inspect.getsource(GraphClient.get_email)
        assert "internetMessageId" in src


class TestDeeplinkFollowsHandle:
    """Der Deeplink haengt am Handle -- deshalb muss das Handle nachgefuehrt werden.

    Dieser Test haelt fest, *warum* ``sync_message_id`` existiert: zwei Handles
    derselben Mail ergeben zwei verschiedene URLs, und nur die aktuelle traegt.
    """

    def test_different_handles_yield_different_urls(self):
        vorher = outlook_deeplink("handle-im-posteingang")
        nachher = outlook_deeplink("handle-im-archiv")
        assert vorher != nachher
        assert nachher is not None
