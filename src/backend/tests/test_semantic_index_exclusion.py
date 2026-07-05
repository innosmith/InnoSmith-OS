"""Tests fuer Pfad-Ausschluss und die E-Mail-Pagination des Such-Index.

- ``_path_is_excluded`` / ``_normalize_drive_path``: robuster Praefix-Abgleich
  (``/drive/root:``-Normalisierung, Subordner, Case-Insensitivitaet).
- ``GraphClient.iter_folder_messages``: echte ``@odata.nextLink``-Pagination inkl.
  ``max_total``-Cap -- ohne echten HTTP-Client (gemockte ``_get``/``_get_raw_url``).
- ``GraphClient.iter_all_mail_folders``: rekursive Ordner-Enumeration ohne
  Junk/Geloeschte Elemente.
"""

import pytest

from app.services.semantic_index import (
    _DEFAULT_EXCLUDED_PATHS,
    _normalize_drive_path,
    _path_is_excluded,
)
from graph_client import GraphClient


class TestPathExclusion:
    def test_normalize_strips_drive_root_prefix(self):
        assert _normalize_drive_path("/drive/root:/Shared/Foo") == "/shared/foo"

    def test_normalize_adds_leading_slash_and_lowercases(self):
        assert _normalize_drive_path("Shared/Foo") == "/shared/foo"

    def test_normalize_handles_none(self):
        assert _normalize_drive_path(None) == "/"

    def test_exact_prefix_matches(self):
        excl = ["/Shared/KnowledgeFlow/bundesgericht-steuerrecht"]
        path = "/drive/root:/Shared/KnowledgeFlow/bundesgericht-steuerrecht"
        assert _path_is_excluded(path, excl) is True

    def test_subfolder_matches(self):
        excl = ["/Shared/KnowledgeFlow/bundesgericht-steuerrecht"]
        path = "/drive/root:/Shared/KnowledgeFlow/bundesgericht-steuerrecht/2024/entscheid"
        assert _path_is_excluded(path, excl) is True

    def test_case_insensitive(self):
        excl = ["/shared/knowledgeflow/BUNDESGERICHT-steuerrecht"]
        path = "/drive/root:/Shared/KnowledgeFlow/Bundesgericht-Steuerrecht/x"
        assert _path_is_excluded(path, excl) is True

    def test_unrelated_path_not_excluded(self):
        excl = ["/Shared/KnowledgeFlow/bundesgericht-steuerrecht"]
        assert _path_is_excluded("/drive/root:/Kunden/Merz", excl) is False

    def test_empty_exclusion_list_never_matches(self):
        assert _path_is_excluded("/drive/root:/whatever", []) is False

    def test_defaults_contain_knowledgeflow(self):
        assert any("bundesgericht-steuerrecht" in p for p in _DEFAULT_EXCLUDED_PATHS)


class _FakeGraph:
    """Minimales GraphClient-Double: liefert vorbereitete Seiten ueber die
    echten (ungebundenen) Generator-Methoden aus GraphClient."""

    _user_path = "/users/me"
    _INDEX_SKIP_WELLKNOWN = GraphClient._INDEX_SKIP_WELLKNOWN
    iter_folder_messages = GraphClient.iter_folder_messages
    iter_all_mail_folders = GraphClient.iter_all_mail_folders

    def __init__(self, first_page, next_pages=None):
        self._first_page = first_page
        self._next_pages = list(next_pages or [])
        self.get_calls: list[str] = []
        self.raw_calls: list[str] = []

    async def _get(self, path, params=None):
        self.get_calls.append(path)
        return self._first_page

    async def _get_raw_url(self, url):
        self.raw_calls.append(url)
        return self._next_pages.pop(0)


def _page(ids, next_link=None):
    data = {"value": [{"id": i, "receivedDateTime": "2026-01-01T00:00:00Z"} for i in ids]}
    if next_link:
        data["@odata.nextLink"] = next_link
    return data


@pytest.mark.asyncio
async def test_paginator_follows_nextlink_across_pages():
    fake = _FakeGraph(
        first_page=_page(["a", "b"], next_link="https://graph/page2"),
        next_pages=[_page(["c", "d"], next_link="https://graph/page3"), _page(["e"])],
    )
    got = [m["id"] async for m in fake.iter_folder_messages("F1", page_size=2)]
    assert got == ["a", "b", "c", "d", "e"]
    assert fake.raw_calls == ["https://graph/page2", "https://graph/page3"]


@pytest.mark.asyncio
async def test_paginator_respects_max_total_cap():
    fake = _FakeGraph(
        first_page=_page(["a", "b"], next_link="https://graph/page2"),
        next_pages=[_page(["c", "d"], next_link="https://graph/page3")],
    )
    got = [m["id"] async for m in fake.iter_folder_messages("F1", page_size=2, max_total=3)]
    assert got == ["a", "b", "c"]
    # Nach Erreichen des Caps darf keine weitere Seite mehr geholt werden.
    assert fake.raw_calls == ["https://graph/page2"]


@pytest.mark.asyncio
async def test_paginator_single_page_no_nextlink():
    fake = _FakeGraph(first_page=_page(["only"]))
    got = [m["id"] async for m in fake.iter_folder_messages("F1")]
    assert got == ["only"]
    assert fake.raw_calls == []


class _FakeFolderGraph:
    _user_path = "/users/me"
    _INDEX_SKIP_WELLKNOWN = GraphClient._INDEX_SKIP_WELLKNOWN
    iter_all_mail_folders = GraphClient.iter_all_mail_folders

    def __init__(self, responses):
        # responses: dict endpoint -> page
        self._responses = responses

    async def _get(self, path, params=None):
        return self._responses.get(path, {"value": []})

    async def _get_raw_url(self, url):
        return self._responses.get(url, {"value": []})


@pytest.mark.asyncio
async def test_folder_enumeration_skips_junk_and_deleted_and_recurses():
    root = "/users/me/mailFolders"
    responses = {
        # Well-Known-Aufloesung (per Namen) -> echte IDs, die ausgeschlossen werden.
        "/users/me/mailFolders/junkemail": {"id": "junk"},
        "/users/me/mailFolders/deleteditems": {"id": "deleted"},
        root: {
            "value": [
                {"id": "inbox", "displayName": "Posteingang", "childFolderCount": 1},
                {"id": "junk", "displayName": "Junk", "childFolderCount": 3},
                {"id": "deleted", "displayName": "Geloescht", "childFolderCount": 0},
                {"id": "archiv", "displayName": "ArchivSorted", "childFolderCount": 0},
            ]
        },
        "/users/me/mailFolders/inbox/childFolders": {
            "value": [
                {"id": "sub1", "displayName": "Projekte", "childFolderCount": 0},
            ]
        },
    }
    fake = _FakeFolderGraph(responses)
    folders = await fake.iter_all_mail_folders()
    ids = {f["id"] for f in folders}
    assert ids == {"inbox", "archiv", "sub1"}
    # Junk/Deleted (und deren Kinder) wurden ausgelassen.
    assert "junk" not in ids and "deleted" not in ids
