"""Tests: unfertige Entwuerfe duerfen nie als Quelle gelten.

Vorfall vom 04.08.2026 (Job ``9e0ad2dd``, Thread «Client-Zugang auf GX10 Server»):
Der Indexer erfasste den Ordner ``Entwuerfe`` mit und loeschte nie, wenn eine
Nachricht verschwand. Im Index lag deshalb noch ein Agenten-Entwurf vom 30.07.,
den der Berater langst geloescht hatte -- Graph antwortete auf dessen ID mit 404.
Die Kontext-Recherche lieferte ihn als belegte Quelle; er enthielt den Platzhalter
«[IP deiner Challenge-Box]», und der Schreib-Pass fuellte ihn mit einer frei
erfundenen IP-Adresse. Unentdeckt blieb es, weil `self_grade` nur Tool-Aufrufe
zaehlt und alle Pflichtquellen geladen waren (Score 1.0).

Abgesichert wird hier die strukturelle Seite: Ordner-Ausschluss, Guard im
Indexer, Draft-Filter in den Sammelabfragen des Graph-Clients.
"""

import pytest

from app.services.semantic_index import _index_email
from graph_client import GraphClient


class _FakeFolderGraph:
    _user_path = "/users/me"
    _INDEX_SKIP_WELLKNOWN = GraphClient._INDEX_SKIP_WELLKNOWN
    iter_all_mail_folders = GraphClient.iter_all_mail_folders

    def __init__(self, responses):
        self._responses = responses

    async def _get(self, path, params=None):
        return self._responses.get(path, {"value": []})

    async def _get_raw_url(self, url):
        return self._responses.get(url, {"value": []})


@pytest.mark.asyncio
async def test_folder_enumeration_skips_drafts():
    """Der Entwuerfe-Ordner gehoert nicht in den Such-Index."""
    root = "/users/me/mailFolders"
    responses = {
        "/users/me/mailFolders/junkemail": {"id": "junk"},
        "/users/me/mailFolders/deleteditems": {"id": "deleted"},
        "/users/me/mailFolders/drafts": {"id": "drafts-id"},
        root: {
            "value": [
                {"id": "inbox", "displayName": "Posteingang", "childFolderCount": 0},
                {"id": "drafts-id", "displayName": "Entwürfe", "childFolderCount": 0},
                {"id": "sent", "displayName": "Gesendete Elemente", "childFolderCount": 0},
            ]
        },
    }
    folders = await _FakeFolderGraph(responses).iter_all_mail_folders()
    ids = {f["id"] for f in folders}
    assert ids == {"inbox", "sent"}


def test_wellknown_skiplist_contains_drafts():
    assert "drafts" in GraphClient._INDEX_SKIP_WELLKNOWN


class _FakeMessageGraph:
    """Liefert eine feste Nachricht fuer ``get_email``."""

    def __init__(self, msg):
        self._msg = msg
        self.calls = 0

    async def get_email(self, message_id):
        self.calls += 1
        return self._msg


class _FakeDb:
    """Zaehlt Schreibversuche -- ein Entwurf darf keinen einzigen ausloesen."""

    def __init__(self):
        self.executions = 0

    async def execute(self, *args, **kwargs):
        self.executions += 1
        raise AssertionError("Ein Entwurf darf nicht indexiert werden")


@pytest.mark.asyncio
async def test_index_email_skips_draft(monkeypatch):
    """Zweite Verteidigungslinie: auch eine einzelne Entwurfs-ID wird nicht indexiert."""
    async def _no_existing(db, source_type, source_id, user_id):
        return None

    monkeypatch.setattr(
        "app.services.semantic_index._existing_modified", _no_existing
    )
    client = _FakeMessageGraph({
        "id": "draft-1",
        "isDraft": True,
        "subject": "RE: Client-Zugang auf GX10 Server",
        "body": {"content": "CNAME: _acme-challenge.app1.gsw.ch"},
        "receivedDateTime": "2026-07-30T07:53:08Z",
    })
    written = await _index_email(_FakeDb(), client, "draft-1", "user-1")
    assert written == 0
    assert client.calls == 1


class _FakeCollectionGraph:
    """GraphClient-Double fuer die Sammelabfragen (ohne HTTP)."""

    _user_path = "/users/me"
    _drop_drafts = staticmethod(GraphClient._drop_drafts)
    get_conversation_messages = GraphClient.get_conversation_messages
    search_emails = GraphClient.search_emails
    search_sender_emails = GraphClient.search_sender_emails

    def __init__(self, page):
        self._page = page
        self.params: list[dict] = []

    async def _get(self, path, params=None):
        self.params.append(params or {})
        return self._page


_THREAD_PAGE = {
    "value": [
        {
            "id": "sent-1",
            "isDraft": False,
            "from": {"emailAddress": {"address": "anthony@innosmith.ch"}},
            "receivedDateTime": "2026-07-30T09:55:40Z",
            "body": {"content": "Sag mir, welchen Weg ihr bevorzugt."},
        },
        {
            "id": "draft-1",
            "isDraft": True,
            "receivedDateTime": "2026-07-30T07:53:08Z",
            "body": {"content": "A-Record: [IP deiner Challenge-Box]"},
        },
    ]
}


@pytest.mark.asyncio
async def test_thread_excludes_drafts():
    """Ein nie gesendeter Entwurf ist kein Teil des Threads."""
    fake = _FakeCollectionGraph(_THREAD_PAGE)
    msgs = await fake.get_conversation_messages("conv-1")
    assert [m["id"] for m in msgs] == ["sent-1"]
    # Ohne isDraft im $select kaeme der Entwurf unerkannt durch.
    assert "isDraft" in fake.params[0]["$select"]


@pytest.mark.asyncio
async def test_search_excludes_drafts():
    fake = _FakeCollectionGraph(_THREAD_PAGE)
    msgs = await fake.search_emails("GX10")
    assert [m["id"] for m in msgs] == ["sent-1"]


@pytest.mark.asyncio
async def test_sender_history_excludes_drafts():
    fake = _FakeCollectionGraph(_THREAD_PAGE)
    msgs = await fake.search_sender_emails("gabriel.brunner@umb.ch")
    assert [m["id"] for m in msgs] == ["sent-1"]
