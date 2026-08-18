"""Router-Contract-Tests für /api/tasks.

Testet RBAC-Einschränkungen und Schema-Validierung.
Endpoints die DB-Queries ausführen sind mit @pytest.mark.db markiert.
"""

import uuid
from datetime import date

import pytest

from conftest import TEST_PROJECT_ID, TEST_COLUMN_BACKLOG_ID

pytestmark = pytest.mark.asyncio


def _minimal_task_body(**overrides) -> dict:
    """Erzeugt einen minimalen TaskCreate-Body mit echten Test-DB-UUIDs."""
    base = {
        "title": "Test-Aufgabe",
        "project_id": str(TEST_PROJECT_ID),
        "board_column_id": str(TEST_COLUMN_BACKLOG_ID),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# RBAC: Member darf assignee='agent' nicht setzen (403)
# ---------------------------------------------------------------------------


async def test_member_cannot_assign_agent(client_as_member):
    """POST /api/tasks mit assignee='agent' als Member wird abgelehnt."""
    body = _minimal_task_body(assignee="agent")
    resp = await client_as_member.post("/api/tasks", json=body)
    assert resp.status_code == 403
    assert "Agent" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# RBAC: Member-restricted Fields werden bei Member ignoriert/entfernt
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_member_restricted_fields_stripped(client_as_member):
    """POST /api/tasks als Member: eingeschränkte Felder (autonomy_level,
    llm_override etc.) werden stillschweigend entfernt, nicht blockiert.
    Benötigt DB für die tatsächliche Task-Erstellung."""
    body = _minimal_task_body(
        autonomy_level="L3",
        llm_override="gpt-4",
        data_class="confidential",
    )
    resp = await client_as_member.post("/api/tasks", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["autonomy_level"] != "L3"
    assert data["llm_override"] is None


# ---------------------------------------------------------------------------
# Schema-Validierung: ungültiger Body → 422
# ---------------------------------------------------------------------------


async def test_create_task_invalid_body_returns_422(client_as_owner):
    """POST /api/tasks ohne Pflichtfelder gibt 422 (Validation Error)."""
    resp = await client_as_owner.post("/api/tasks", json={})
    assert resp.status_code == 422


async def test_create_task_invalid_uuid_returns_422(client_as_owner):
    """POST /api/tasks mit ungültiger UUID für board_column_id gibt 422."""
    body = {
        "title": "Test",
        "project_id": "nicht-eine-uuid",
        "board_column_id": "auch-keine-uuid",
    }
    resp = await client_as_owner.post("/api/tasks", json=body)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth: Anonymer Zugriff auf /api/tasks/due-today → 403 (HTTPBearer)
# ---------------------------------------------------------------------------


async def test_due_today_rejects_anonymous(client_anonymous):
    """GET /api/tasks/due-today ohne Bearer-Token wird abgelehnt (401 oder 403)."""
    resp = await client_anonymous.get("/api/tasks/due-today")
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Wiederkehrende Vorlagen: due_date wird genullt, Erledigen blockiert (422)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_template_nulls_due_date_and_blocks_completion(client_as_owner):
    """Vorlagen-Härtung: recurrence_rule setzen nullt due_date; is_completed
    auf einer Vorlage wird mit 422 abgelehnt."""
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(due_date="2020-01-01")
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Task wird zur Vorlage: Fälligkeitsdatum muss verschwinden.
    resp = await client_as_owner.patch(
        f"/api/tasks/{task_id}", json={"recurrence_rule": "0 9 * * 1"}
    )
    assert resp.status_code == 200
    assert resp.json()["due_date"] is None

    # Vorlagen können nicht erledigt werden.
    resp = await client_as_owner.patch(
        f"/api/tasks/{task_id}", json={"is_completed": True}
    )
    assert resp.status_code == 422
    assert "Vorlage" in resp.json()["detail"]

    # due_date-Änderungen auf Vorlagen werden ignoriert.
    resp = await client_as_owner.patch(
        f"/api/tasks/{task_id}", json={"due_date": "2030-01-01"}
    )
    assert resp.status_code == 200
    assert resp.json()["due_date"] is None

    await client_as_owner.delete(f"/api/tasks/{task_id}")


async def test_create_task_rejects_invalid_cron(client_as_owner):
    """Eine kaputte Cron-Expression darf nicht stillschweigend in der DB landen.

    Ohne Validierung überspringt der Scheduler die Serie dauerhaft und meldet
    das nur ins Log — die Vorlage sieht in der UI trotzdem aktiv aus.
    """
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(recurrence_rule="jeden Dienstag")
    )
    assert resp.status_code == 422
    assert "Wiederholungsregel" in resp.json()["detail"]


@pytest.mark.db
async def test_update_task_rejects_invalid_cron(client_as_owner):
    resp = await client_as_owner.post("/api/tasks", json=_minimal_task_body())
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    try:
        resp = await client_as_owner.patch(
            f"/api/tasks/{task_id}", json={"recurrence_rule": "0 99 * * *"}
        )
        assert resp.status_code == 422
    finally:
        await client_as_owner.delete(f"/api/tasks/{task_id}")


# ---------------------------------------------------------------------------
# Serien-Verwaltung: Sichtbarkeit, Kadenz-Wechsel, Löschmodi
# ---------------------------------------------------------------------------


async def _set_task_fields(task_id: str, **fields) -> None:
    """Setzt Felder direkt in der DB — für Zustände, die die API bewusst blockt."""
    import uuid as _uuid

    from sqlalchemy import update as _update

    from app.database import async_session
    from app.models import Task

    async with async_session() as session:
        await session.execute(
            _update(Task).where(Task.id == _uuid.UUID(task_id)).values(**fields)
        )
        await session.commit()


async def _create_instance(template_id: str, **fields) -> str:
    """Erzeugt eine Serien-Instanz wie der Scheduler (template_id ist kein API-Feld)."""
    import uuid as _uuid

    from app.database import async_session
    from app.models import Task

    async with async_session() as session:
        instance = Task(
            title="Serien-Instanz",
            project_id=TEST_PROJECT_ID,
            board_column_id=TEST_COLUMN_BACKLOG_ID,
            board_position=999.0,
            template_id=_uuid.UUID(template_id),
            **fields,
        )
        session.add(instance)
        await session.commit()
        return str(instance.id)


@pytest.mark.db
async def test_completed_template_stays_visible_on_board(client_as_owner):
    """Regression: eine abgehakte Vorlage verschwand vom Board und war damit
    über die UI nicht mehr erreichbar — während der Scheduler weiter Instanzen
    erzeugte (Fall «Weekly KreditorenBot», jeden Dienstag ein neuer Ableger).

    Der Board-Filter liess nur offene Tasks durch, die Agenda blendet Vorlagen
    generell aus, und einen Task-Such-Endpoint gibt es nicht. Die Serie war
    damit aktiv, aber unsichtbar und weder änder- noch löschbar.
    """
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(recurrence_rule="30 13 * * TUE")
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    try:
        # Der MCP-Server schrieb is_completed früher per direktem UPDATE und
        # umging damit den 422-Guard des Routers.
        await _set_task_fields(task_id, is_completed=True)

        resp = await client_as_owner.get(f"/api/projects/{TEST_PROJECT_ID}/board")
        assert resp.status_code == 200
        column = next(
            c for c in resp.json()["columns"] if c["id"] == str(TEST_COLUMN_BACKLOG_ID)
        )
        assert task_id in [t["id"] for t in column["tasks"]]
    finally:
        await client_as_owner.delete(f"/api/tasks/{task_id}")


@pytest.mark.db
async def test_completed_normal_task_stays_hidden_on_board(client_as_owner):
    """Gegenprobe: die Ausnahme gilt nur für Vorlagen, nicht für alle Tasks."""
    resp = await client_as_owner.post("/api/tasks", json=_minimal_task_body())
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    try:
        resp = await client_as_owner.patch(
            f"/api/tasks/{task_id}", json={"is_completed": True}
        )
        assert resp.status_code == 200

        resp = await client_as_owner.get(f"/api/projects/{TEST_PROJECT_ID}/board")
        column = next(
            c for c in resp.json()["columns"] if c["id"] == str(TEST_COLUMN_BACKLOG_ID)
        )
        assert task_id not in [t["id"] for t in column["tasks"]]
    finally:
        await client_as_owner.delete(f"/api/tasks/{task_id}")


@pytest.mark.db
async def test_rule_change_resets_last_spawn(client_as_owner):
    """Der Spawn-Merker gehört zur alten Kadenz und würde die neue verzögern."""
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(recurrence_rule="30 13 * * TUE")
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    try:
        await _set_task_fields(task_id, recurrence_last_spawn=date(2026, 8, 18))
        resp = await client_as_owner.get(f"/api/tasks/{task_id}/recurrence")
        assert resp.json()["last_spawn"] == "2026-08-18"

        resp = await client_as_owner.patch(
            f"/api/tasks/{task_id}", json={"recurrence_rule": "0 8 * * MON"}
        )
        assert resp.status_code == 200

        resp = await client_as_owner.get(f"/api/tasks/{task_id}/recurrence")
        assert resp.json()["last_spawn"] is None

        # Unveränderte Regel darf den Merker nicht anfassen.
        await _set_task_fields(task_id, recurrence_last_spawn=date(2026, 8, 17))
        resp = await client_as_owner.patch(
            f"/api/tasks/{task_id}", json={"recurrence_rule": "0 8 * * MON"}
        )
        assert resp.status_code == 200
        resp = await client_as_owner.get(f"/api/tasks/{task_id}/recurrence")
        assert resp.json()["last_spawn"] == "2026-08-17"
    finally:
        await client_as_owner.delete(f"/api/tasks/{task_id}")


@pytest.mark.db
async def test_recurring_series_overview_lists_template(client_as_owner):
    """GET /api/tasks/recurring darf nicht von der /{task_id}-Route geschluckt werden."""
    resp = await client_as_owner.post(
        "/api/tasks",
        json=_minimal_task_body(title="Weekly Testserie", recurrence_rule="30 13 * * TUE"),
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]
    instance_id = await _create_instance(task_id, due_date=date(2026, 8, 18))

    try:
        resp = await client_as_owner.get("/api/tasks/recurring")
        assert resp.status_code == 200
        entry = next((s for s in resp.json() if s["id"] == task_id), None)
        assert entry is not None
        assert entry["recurrence_description"] == "Jeden Dienstag um 13:30"
        assert entry["next_occurrence"] is not None
        assert entry["instance_count"] == 1
        assert entry["open_instance_id"] == instance_id
        assert entry["is_valid"] is True
    finally:
        await client_as_owner.delete(f"/api/tasks/{task_id}?series=all")


@pytest.mark.db
async def test_delete_template_only_keeps_instances(client_as_owner):
    """Standardfall: die Serie endet, die erzeugte Historie bleibt bestehen.

    Ohne Entkopplung scheiterte das Löschen am Self-FK tasks.template_id, der
    kein ON DELETE-Verhalten hat.
    """
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(recurrence_rule="30 13 * * TUE")
    )
    task_id = resp.json()["id"]
    instance_id = await _create_instance(task_id, due_date=date(2026, 8, 18))

    try:
        resp = await client_as_owner.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 204

        assert (await client_as_owner.get(f"/api/tasks/{task_id}")).status_code == 404

        resp = await client_as_owner.get(f"/api/tasks/{instance_id}")
        assert resp.status_code == 200
        assert resp.json()["template_id"] is None
    finally:
        await client_as_owner.delete(f"/api/tasks/{instance_id}")


@pytest.mark.db
async def test_delete_series_all_removes_instances(client_as_owner):
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(recurrence_rule="30 13 * * TUE")
    )
    task_id = resp.json()["id"]
    instance_id = await _create_instance(task_id, due_date=date(2026, 8, 18))

    resp = await client_as_owner.delete(f"/api/tasks/{task_id}?series=all")
    assert resp.status_code == 204
    assert (await client_as_owner.get(f"/api/tasks/{task_id}")).status_code == 404
    assert (await client_as_owner.get(f"/api/tasks/{instance_id}")).status_code == 404


@pytest.mark.db
async def test_delete_rejects_unknown_series_mode(client_as_owner):
    resp = await client_as_owner.post("/api/tasks", json=_minimal_task_body())
    task_id = resp.json()["id"]
    try:
        resp = await client_as_owner.delete(f"/api/tasks/{task_id}?series=vielleicht")
        assert resp.status_code == 422
    finally:
        await client_as_owner.delete(f"/api/tasks/{task_id}")


# ---------------------------------------------------------------------------
# Quell-E-Mail: Outlook-Link kommt als Feld, nicht mehr im Beschreibungstext
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_detail_exposes_outlook_deeplink(client_as_owner):
    """GET /api/tasks/{id} liefert source_email_web_link zu email_message_id.

    Vorher hängte der Worker die rohe URL an die Beschreibung, wo sie über ein
    Dutzend Zeilen umbrach. Jetzt baut der Detail-Endpoint sie als Feld.
    """
    from app.services.email_links import outlook_deeplink

    message_id = "AAMk=test/router+id"
    resp = await client_as_owner.post(
        "/api/tasks", json=_minimal_task_body(email_message_id=message_id)
    )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    resp = await client_as_owner.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["source_email_web_link"] == outlook_deeplink(message_id)

    await client_as_owner.delete(f"/api/tasks/{task_id}")


@pytest.mark.db
async def test_detail_link_is_null_without_email(client_as_owner):
    resp = await client_as_owner.post("/api/tasks", json=_minimal_task_body())
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    resp = await client_as_owner.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["source_email_web_link"] is None

    await client_as_owner.delete(f"/api/tasks/{task_id}")


# ---------------------------------------------------------------------------
# Bulk-Reorder: Arbeitsreihenfolge innerhalb einer Spalte
# ---------------------------------------------------------------------------


async def test_reorder_pipeline_forbidden_for_member(client_as_member):
    """Die Agenda ist Owner-Territorium (pipeline_* ist member-restricted)."""
    resp = await client_as_member.post(
        "/api/tasks/reorder",
        json={
            "scope": "pipeline",
            "columns": [{"column_id": str(uuid.uuid4()), "task_ids": [str(uuid.uuid4())]}],
        },
    )
    assert resp.status_code == 403
    assert "Owner" in resp.json()["detail"]


async def test_reorder_rejects_unknown_scope(client_as_owner):
    resp = await client_as_owner.post(
        "/api/tasks/reorder",
        json={"scope": "kanban", "columns": [{"column_id": str(uuid.uuid4()), "task_ids": []}]},
    )
    assert resp.status_code == 422


async def test_reorder_rejects_duplicate_task_ids(client_as_owner):
    """Doppelte IDs würden beim Durchnummerieren stillschweigend Plätze schlucken."""
    task_id = str(uuid.uuid4())
    resp = await client_as_owner.post(
        "/api/tasks/reorder",
        json={
            "scope": "board",
            "columns": [{"column_id": str(TEST_COLUMN_BACKLOG_ID), "task_ids": [task_id, task_id]}],
        },
    )
    assert resp.status_code == 422
    assert "Doppelte" in resp.json()["detail"]


async def test_reorder_rejects_anonymous(client_anonymous):
    resp = await client_anonymous.post(
        "/api/tasks/reorder", json={"scope": "board", "columns": []}
    )
    assert resp.status_code in (401, 403)


@pytest.mark.db
async def test_reorder_board_persists_order(client_as_owner):
    """Regression: Die manuelle Reihenfolge innerhalb einer Spalte überlebt den Reload.

    Vorher schickte das Frontend nach einem Drop nur den gezogenen Task -- und
    zwar mit der Position, die es unmittelbar nach einem asynchronen `setState`
    aus dem noch alten React-State las. Innerhalb einer Spalte wurde damit die
    ALTE Position gespeichert. Zusätzlich kollidierte der 0-basierte Index mit
    den Float-Werten der Geschwister (1.0, 2.0, ...), womit PostgreSQL keine
    definierte Reihenfolge mehr garantierte: die Arbeitsreihenfolge sprang bei
    jedem Neuladen. Kein Test bemerkte es, weil der PATCH selbst erfolgreich war.
    """
    task_ids = []
    for i in range(3):
        resp = await client_as_owner.post(
            "/api/tasks", json=_minimal_task_body(title=f"Reorder-{i}")
        )
        assert resp.status_code == 201
        task_ids.append(resp.json()["id"])

    try:
        reversed_ids = list(reversed(task_ids))
        resp = await client_as_owner.post(
            "/api/tasks/reorder",
            json={
                "scope": "board",
                "columns": [
                    {"column_id": str(TEST_COLUMN_BACKLOG_ID), "task_ids": reversed_ids}
                ],
            },
        )
        assert resp.status_code == 204

        assert await _board_order(client_as_owner, task_ids) == reversed_ids

        # Zweiter Drop: erster Task nach hinten -- auch wiederholtes Umsortieren
        # muss stabil bleiben (früher entstanden hier Positionskollisionen).
        rotated = reversed_ids[1:] + reversed_ids[:1]
        resp = await client_as_owner.post(
            "/api/tasks/reorder",
            json={
                "scope": "board",
                "columns": [{"column_id": str(TEST_COLUMN_BACKLOG_ID), "task_ids": rotated}],
            },
        )
        assert resp.status_code == 204
        assert await _board_order(client_as_owner, task_ids) == rotated
    finally:
        for task_id in task_ids:
            await client_as_owner.delete(f"/api/tasks/{task_id}")


@pytest.mark.db
async def test_reorder_pipeline_persists_order(client_as_owner):
    """Dasselbe für die Agenda-Spalten (pipeline_position)."""
    resp = await client_as_owner.get("/api/pipeline")
    assert resp.status_code == 200
    pipeline_column_id = resp.json()["columns"][0]["id"]

    task_ids = []
    for i in range(3):
        resp = await client_as_owner.post(
            "/api/tasks",
            json=_minimal_task_body(
                title=f"Agenda-Reorder-{i}", pipeline_column_id=pipeline_column_id
            ),
        )
        assert resp.status_code == 201
        task_ids.append(resp.json()["id"])

    try:
        reversed_ids = list(reversed(task_ids))
        resp = await client_as_owner.post(
            "/api/tasks/reorder",
            json={
                "scope": "pipeline",
                "columns": [{"column_id": pipeline_column_id, "task_ids": reversed_ids}],
            },
        )
        assert resp.status_code == 204

        resp = await client_as_owner.get("/api/pipeline")
        assert resp.status_code == 200
        column = next(c for c in resp.json()["columns"] if c["id"] == pipeline_column_id)
        order = [t["id"] for t in column["tasks"] if t["id"] in set(task_ids)]
        assert order == reversed_ids
    finally:
        for task_id in task_ids:
            await client_as_owner.delete(f"/api/tasks/{task_id}")


async def _board_order(client, task_ids: list[str]) -> list[str]:
    """Reihenfolge der übergebenen Tasks laut Board-Endpoint (Reload-Sicht)."""
    resp = await client.get(f"/api/projects/{TEST_PROJECT_ID}/board")
    assert resp.status_code == 200
    column = next(
        c for c in resp.json()["columns"] if c["id"] == str(TEST_COLUMN_BACKLOG_ID)
    )
    wanted = set(task_ids)
    return [t["id"] for t in column["tasks"] if t["id"] in wanted]
