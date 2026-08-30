"""Tests für das Gegenstück zu ``mark_open_work`` beim Löschen einer Aufgabe.

Das Bestätigen eines Mail-Vorschlags setzt zwei Abbildungen im Postfach: die Fahne und
den Ordner ``Posteingang/Tasks``. Für das Erledigen gab es das Gegenstück
(``release_open_work`` im PATCH-Pfad), für das **Löschen** nicht. Die Mail blieb dann
für immer markiert im Ordner liegen, ohne Aufgabe dahinter -- und weil der Abgleich
Mails ohne Aufgabe bewusst liegen lässt, räumte sie auch niemand nachträglich weg.

Die Bedingung ist dabei so wichtig wie der Aufruf. Ein bedingungsloses Auflösen wäre
zweimal falsch: Bei ``needs_review`` wurde nie eine Projektion gesetzt, und bei einer
erledigten Aufgabe hat der PATCH-Pfad sie längst aufgelöst -- der zweite Aufruf wäre
eine Graph-Anfrage für nichts.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.routers.tasks as tasks_router


class _FakeDB:
    """Antwortet auf die eine Abfrage, die ``delete_task`` selbst stellt."""

    def __init__(self, task):
        self._task = task
        self.deleted = []

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._task)

    async def flush(self):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)


def _task(*, needs_review=False, completed=False, mail="handle-1"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        title="Angebot prüfen",
        project_id=uuid.uuid4(),
        recurrence_rule=None,
        template_id=None,
        email_message_id=mail,
        internet_message_id="<abc@example.com>",
        needs_review=needs_review,
        is_completed=completed,
    )


async def _delete(task):
    """Führt ``delete_task`` aus und gibt zurück, ob aufgelöst wurde."""
    db = _FakeDB(task)
    release = AsyncMock()
    with (
        patch.object(tasks_router, "check_project_access", AsyncMock(return_value=True)),
        patch.object(tasks_router, "_detach_agent_job_references", AsyncMock()),
        patch.object(
            tasks_router, "_resolve_task_origin", AsyncMock(return_value=(False, None, None))
        ),
        patch.object(tasks_router, "release_open_work", release),
    ):
        await tasks_router.delete_task(
            task.id, series="template_only", db=db, user=SimpleNamespace(role="owner")
        )
    return release, db


class TestLoeschpfad:
    @pytest.mark.asyncio
    async def test_bestaetigte_offene_mailaufgabe_wird_aufgeloest(self):
        task = _task()
        release, db = await _delete(task)
        release.assert_awaited_once()
        assert release.await_args.args[1] is task
        assert db.deleted == [task]

    @pytest.mark.asyncio
    async def test_vorschlag_wird_nicht_aufgeloest(self):
        """Bei ``needs_review`` wurde nie eine Projektion gesetzt.

        ``move_target()`` unterdrückt Moves bei ``needs_review``, damit ein Fehlgriff
        des Agenten keine echte Kundenmail aus dem Blick schafft. Hier etwas
        aufzulösen, hiesse eine Fahne zu entfernen, die niemand gesetzt hat -- und die
        Mail ins Archiv zu schieben, obwohl sie nie gesichtet wurde.
        """
        release, _ = await _delete(_task(needs_review=True))
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_erledigte_aufgabe_wird_nicht_zweimal_aufgeloest(self):
        release, _ = await _delete(_task(completed=True))
        release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aufgabe_ohne_mail_bleibt_unberuehrt(self):
        release, _ = await _delete(_task(mail=None))
        release.assert_not_awaited()
