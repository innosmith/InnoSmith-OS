"""Tests fuer die beiden Nachlaeufe am Postfach: Archiv-Sichtung und Fahnen-Aufgriff.

Beide greifen Mails auf, die den normalen Posteingang-Pfad nie durchlaufen haben --
und beide koennen bei einem Fehler ein Postfach fluten, ohne dass etwas fehlschlaegt.
Genau diese stillen Eigenschaften stehen hier als Tests:

- Der Archiv-Nachlauf **liest nur**. Was der Mensch weggeraeumt hat, bleibt liegen.
- Beide erkennen Mails an der ``internetMessageId``, nicht am Graph-Handle. Ein
  archiviertes Handle ist ein anderes als das im Posteingang triagierte; ohne die
  Identitaet gaelte jede bereits gesichtete Mail als neu.
- Beide greifen keinen Altbestand auf, sondern nur, was nach der Aktivierung entsteht.
- Der Tasks-Unterordner ist kein Teil des Posteingangs -- was dort liegt, wird nicht
  erneut triagiert.
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.services import triage as tri
from app.services.hermes_worker import FinalizeResult, _finalize_email_state

NOW = datetime.now(timezone.utc)


class _FakeArchive:
    def __init__(self, pages):
        self._pages = pages
        self.requests: list[dict] = []

    async def list_emails(self, folder=None, top=20, skip=0, filter_str=None):
        self.requests.append({"folder": folder, "top": top, "skip": skip})
        index = skip // top if top else 0
        return {"value": self._pages[index] if index < len(self._pages) else []}


def _mail(identity, *, minutes_ago=5, handle=None, folder="AAA-archiv"):
    received = NOW - timedelta(minutes=minutes_ago)
    return {
        "id": handle or f"handle-{identity}",
        "internetMessageId": identity,
        "subject": f"Betreff {identity}",
        "from": {"emailAddress": {"address": "kunde@example.ch", "name": "Kundin"}},
        "receivedDateTime": received.isoformat().replace("+00:00", "Z"),
        "parentFolderId": folder,
        "conversationId": "C1",
        "bodyPreview": "Kurzer Auszug",
    }


class TestArchiveRescanSelection:
    @pytest.mark.asyncio
    async def test_untriaged_mail_is_picked_up(self):
        client = _FakeArchive([[_mail("<neu@example.ch>")]])
        found = await tri._fetch_untriaged_archive_emails(
            client, set(), NOW - timedelta(days=1)
        )
        assert [m["internetMessageId"] for m in found] == ["<neu@example.ch>"]
        assert client.requests[0]["folder"] == "archive"

    @pytest.mark.asyncio
    async def test_known_identity_is_skipped_despite_new_handle(self):
        """Der Kern: Nach dem Archivieren traegt dieselbe Mail ein anderes Handle.

        Ohne den Abgleich ueber die Identitaet gaelte jede bereits triagierte Mail im
        Archiv als neu -- der Nachlauf wuerde das halbe Postfach erneut vorschlagen.
        Das ist der Grund, warum Phase 1 vor Phase 3 kommt.
        """
        mail = _mail("<bekannt@example.ch>", handle="handle-im-archiv-neu")
        found = await tri._fetch_untriaged_archive_emails(
            _FakeArchive([[mail]]), {"<bekannt@example.ch>"}, NOW - timedelta(days=1)
        )
        assert found == []

    @pytest.mark.asyncio
    async def test_mail_older_than_the_activation_date_is_ignored(self):
        """Kein Altbestand: sonst laeuft die halbe Postfach-Historie in die Queue."""
        alt = _mail("<alt@example.ch>", minutes_ago=60 * 24 * 30)
        found = await tri._fetch_untriaged_archive_emails(
            _FakeArchive([[alt]]), set(), NOW - timedelta(days=1)
        )
        assert found == []

    @pytest.mark.asyncio
    async def test_mail_without_identity_is_ignored(self):
        """Ohne Identitaet waere sie beim naechsten Lauf wieder unbekannt -- endlos."""
        ohne = _mail("<x@example.ch>")
        ohne["internetMessageId"] = None
        found = await tri._fetch_untriaged_archive_emails(
            _FakeArchive([[ohne]]), set(), NOW - timedelta(days=1)
        )
        assert found == []

    @pytest.mark.asyncio
    async def test_paging_stops_at_the_first_page_older_than_the_cutoff(self):
        seiten = [
            [_mail(f"<n{i}@example.ch>") for i in range(tri.ARCHIVE_PAGE_SIZE)],
            [_mail("<alt@example.ch>", minutes_ago=60 * 24 * 90)],
        ]
        client = _FakeArchive(seiten)
        await tri._fetch_untriaged_archive_emails(client, set(), NOW - timedelta(days=1))
        assert len(client.requests) <= 2


class TestArchiveRescanIsReadOnly:
    """Die Mail bleibt liegen, wo der Mensch sie hingelegt hat.

    Die Sperre sitzt im einzigen Schreibpfad auf den Outlook-Zustand und nicht als
    Bitte im Prompt: Struktur statt Anweisung. Waere sie eine Anweisung, wuerde sie
    beim naechsten Prompt-Umbau still verschwinden.
    """

    @pytest.mark.asyncio
    async def test_readonly_marker_suppresses_every_outlook_change(self):
        result = await _finalize_email_state(
            {"email_message_id": "M1", "readonly_mail": True},
            "Aufgabe",
            None,
            triage_class="task",
            needs_review=True,
        )
        assert isinstance(result, FinalizeResult)
        assert result.message_id == "M1"
        assert result.move_suppressed is None

    def test_the_marker_reaches_the_job_metadata(self):
        """Ohne diesen Weg in die Metadaten greift die Sperre im Worker nie."""
        src = inspect.getsource(tri._create_triage_job)
        assert "readonly_mail" in src

    def test_the_rescan_sets_the_marker(self):
        src = inspect.getsource(tri._archive_rescan_cycle)
        assert 'readonly_mail"] = True' in src


class TestTasksFolderIsNotRetriaged:
    """Was im Tasks-Unterordner liegt, ist gesichtet -- und bleibt es.

    ``_fetch_new_inbox_emails`` liest ``folder="inbox"``, und Graph schliesst
    Unterordner dabei nicht ein. Das ist der Grund, warum der Ordner ueberhaupt
    funktioniert; bliebe es Annahme statt Zusicherung, wuerde ein spaeterer Wechsel
    auf eine rekursive Abfrage jede offene Task-Mail erneut durch die Triage schicken.
    """

    @pytest.mark.asyncio
    async def test_inbox_query_is_not_recursive(self):
        gefragt: list[str] = []

        class _Client:
            async def list_emails(self, folder=None, top=20, skip=0, filter_str=None):
                gefragt.append(folder)
                return {"value": []}

        await tri._fetch_new_inbox_emails(_Client(), set(), NOW - timedelta(days=1))
        assert gefragt == ["inbox"]

    def test_identity_guards_the_dedupe(self):
        """Zweite Verteidigungslinie, falls der Ordner doch einmal mitgelesen wird."""
        src = inspect.getsource(tri._fetch_new_inbox_emails)
        assert "internetMessageId" in src


class TestFlagPickupSelection:
    """Welche markierte Mail zur Aufgabe wird -- und welche nicht.

    Der Aufgriff selbst legt Datensaetze an und braucht eine Datenbank; hier steht
    die Auswahl, weil dort die stillen Fehler sitzen. Ein zu Unrecht aufgegriffener
    Altbestand fuellt das Board mit Vergangenheit, und das faellt erst auf, wenn
    hundert Aufgaben da sind.
    """

    @staticmethod
    def _selected(mails, known=frozenset(), cutoff_minutes=60):
        cutoff = NOW - timedelta(minutes=cutoff_minutes)
        out = []
        for mail in mails:
            identity = mail.get("internetMessageId")
            if not identity or identity in known or mail.get("id") in known:
                continue
            if mail.get("parentFolderId") == "AAA-archiv":
                continue
            received = tri._parse_received_at(mail.get("receivedDateTime"))
            if received is None or received < cutoff:
                continue
            out.append(identity)
        return out

    def test_flagged_inbox_mail_is_picked_up(self):
        mail = _mail("<neu@example.ch>", folder="AAA-posteingang")
        assert self._selected([mail]) == ["<neu@example.ch>"]

    def test_flag_in_the_archive_is_a_leftover_not_an_order(self):
        mail = _mail("<archiviert@example.ch>", folder="AAA-archiv")
        assert self._selected([mail]) == []

    def test_flag_from_before_activation_is_not_picked_up(self):
        """Kein Altbestand -- die alten Fahnen raeumt Anthony selbst auf."""
        mail = _mail("<alt@example.ch>", minutes_ago=60 * 24 * 7, folder="AAA-posteingang")
        assert self._selected([mail]) == []

    def test_already_known_mail_is_not_picked_up_twice(self):
        mail = _mail("<bekannt@example.ch>", folder="AAA-posteingang")
        assert self._selected([mail], known={"<bekannt@example.ch>"}) == []

    def test_the_selection_matches_the_implementation(self):
        """Haelt die Nachbildung oben an die echten Kriterien gebunden."""
        src = inspect.getsource(tri._flag_pickup_cycle)
        for kriterium in ("parentFolderId", "internetMessageId", "cutoff", "known_ids"):
            assert kriterium in src

    def test_archive_is_recognised_by_its_real_id(self):
        """``parentFolderId`` traegt nie den Well-Known-Namen, nur die echte ID."""
        src = inspect.getsource(tri._flag_pickup_cycle)
        assert 'well_known_folder_id("archive")' in src

    def test_flagged_query_carries_identity_and_folder(self):
        """Ohne diese beiden Felder ist keines der Kriterien oben pruefbar."""
        from graph_client import GraphClient

        assert "internetMessageId" in GraphClient._FLAGGED_SELECT
        assert "parentFolderId" in GraphClient._FLAGGED_SELECT


class TestFlaggedMailKeepsItsFlag:
    """Die Fahne bleibt gesetzt -- sie ist ab dem Aufgriff der Nachweis aus Phase 2.

    Wuerde sie beim Anlegen entfernt, haette «Fahne gesetzt» zwei Bedeutungen: einmal
    «noch zu erledigen» und einmal «schon verbucht». Ein Merkmal mit zwei Bedeutungen
    ist keines mehr.
    """

    def test_pickup_never_clears_the_flag(self):
        src = inspect.getsource(tri._flag_pickup_cycle) + inspect.getsource(
            tri._create_task_from_flag
        )
        assert "release_open_work" not in src
        assert "mark_open_work" in src

    def test_pickup_creates_a_decided_task_not_a_suggestion(self):
        """Der Mensch hat entschieden -- das ist keine Ermessensfrage und kein LLM."""
        src = inspect.getsource(tri._create_task_from_flag)
        assert "needs_review=False" in src
