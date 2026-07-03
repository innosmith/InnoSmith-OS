"""Router-Contract-Tests für /api/tasks.

Testet RBAC-Einschränkungen und Schema-Validierung.
Endpoints die DB-Queries ausführen sind mit @pytest.mark.db markiert.
"""

import uuid

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
