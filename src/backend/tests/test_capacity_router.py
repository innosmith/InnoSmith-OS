"""Router-Contract-Tests für /api/capacity.

Testet RBAC (Owner-only), Schema-Validierung und Geschäftslogik.
"""

import uuid

import pytest

pytestmark = pytest.mark.asyncio


# ── RBAC: Nur Owner darf auf Kapazitätsplanung zugreifen ─────────────────────


async def test_capacity_projects_forbidden_for_member(client_as_member):
    """GET /api/capacity/projects als Member gibt 403 zurück."""
    resp = await client_as_member.get("/api/capacity/projects")
    assert resp.status_code == 403


async def test_capacity_create_project_forbidden_for_member(client_as_member):
    """POST /api/capacity/projects als Member gibt 403 zurück."""
    body = {"name": "Testprojekt"}
    resp = await client_as_member.post("/api/capacity/projects", json=body)
    assert resp.status_code == 403


async def test_capacity_allocations_forbidden_for_member(client_as_member):
    """GET /api/capacity/allocations als Member gibt 403 zurück."""
    resp = await client_as_member.get(
        "/api/capacity/allocations", params={"from": "2026-01-05", "to": "2026-03-30"}
    )
    assert resp.status_code == 403


async def test_capacity_create_allocation_forbidden_for_member(client_as_member):
    """POST /api/capacity/allocations als Member gibt 403 zurück."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-01",
        "minutes": 480,
    }
    resp = await client_as_member.post("/api/capacity/allocations", json=body)
    assert resp.status_code == 403


async def test_capacity_time_off_forbidden_for_member(client_as_member):
    """GET /api/capacity/time-off als Member gibt 403 zurück."""
    resp = await client_as_member.get("/api/capacity/time-off")
    assert resp.status_code == 403


async def test_capacity_weekly_summary_forbidden_for_member(client_as_member):
    """GET /api/capacity/weekly-summary als Member gibt 403 zurück."""
    resp = await client_as_member.get(
        "/api/capacity/weekly-summary", params={"from": "2026-01-05", "to": "2026-03-30"}
    )
    assert resp.status_code == 403


async def test_capacity_forbidden_for_anonymous(client_anonymous):
    """Alle Capacity-Endpoints ohne Token werden abgelehnt (401 oder 403)."""
    resp = await client_anonymous.get("/api/capacity/projects")
    assert resp.status_code in (401, 403)


# ── Schema-Validierung ───────────────────────────────────────────────────────


async def test_create_project_invalid_body_422(client_as_owner):
    """POST /api/capacity/projects ohne name gibt 422."""
    resp = await client_as_owner.post("/api/capacity/projects", json={})
    assert resp.status_code == 422


async def test_create_allocation_invalid_body_422(client_as_owner):
    """POST /api/capacity/allocations ohne Pflichtfelder gibt 422."""
    resp = await client_as_owner.post("/api/capacity/allocations", json={})
    assert resp.status_code == 422


async def test_create_allocation_non_monday_422(client_as_owner):
    """POST /api/capacity/allocations mit week_start != Montag (type=week) gibt 422."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-03",  # Mittwoch
        "minutes": 480,
        "allocation_type": "week",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    assert resp.status_code == 422
    assert "Montag" in resp.json()["detail"]


async def test_create_day_allocation_wednesday_ok(client_as_owner):
    """POST /api/capacity/allocations mit type=day an einem Mittwoch ist erlaubt."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-03",  # Mittwoch
        "minutes": 480,
        "allocation_type": "day",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    # 404 ist akzeptabel (Projekt existiert nicht), aber kein 422
    assert resp.status_code != 422


async def test_create_day_allocation_saturday_ok(client_as_owner):
    """POST /api/capacity/allocations mit type=day an einem Samstag ist erlaubt."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-06",  # Samstag
        "minutes": 480,
        "allocation_type": "day",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    assert resp.status_code != 422


async def test_create_day_allocation_sunday_422(client_as_owner):
    """POST /api/capacity/allocations mit type=day an einem Sonntag gibt 422."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-07",  # Sonntag
        "minutes": 480,
        "allocation_type": "day",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    assert resp.status_code == 422
    assert "Sonntag" in resp.json()["detail"]


async def test_create_allocation_invalid_type_422(client_as_owner):
    """POST /api/capacity/allocations mit ungültigem allocation_type gibt 422."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-01",
        "minutes": 480,
        "allocation_type": "invalid",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    assert resp.status_code == 422


async def test_create_repeat_invalid_end_date_422(client_as_owner):
    """POST /api/capacity/allocations/repeat mit Enddatum vor Startdatum gibt 422."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2026-06-08",
        "end_date": "2026-06-01",
        "minutes": 480,
    }
    resp = await client_as_owner.post("/api/capacity/allocations/repeat", json=body)
    assert resp.status_code == 422
    assert "Enddatum" in resp.json()["detail"]


async def test_create_allocation_malformed_uuid_422(client_as_owner):
    """POST /api/capacity/allocations mit ungültiger UUID gibt 422 (kein 500)."""
    body = {
        "capacity_project_id": "nicht-eine-uuid",
        "week_start": "2026-06-01",
        "minutes": 480,
        "allocation_type": "week",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    assert resp.status_code == 422


# ── Geschäftslogik (benötigt DB) ────────────────────────────────────────────


@pytest.mark.db
async def test_create_project_and_list(client_as_owner):
    """Kapazitätsprojekt erstellen und in der Liste finden."""
    body = {"name": "BFH CAS TQM", "color": "#10B981", "client_name": "BFH", "status": "bestätigt"}
    resp = await client_as_owner.post("/api/capacity/projects", json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "BFH CAS TQM"
    assert data["status"] == "bestätigt"
    project_id = data["id"]

    resp2 = await client_as_owner.get("/api/capacity/projects")
    assert resp2.status_code == 200
    names = [p["name"] for p in resp2.json()]
    assert "BFH CAS TQM" in names

    # Aufräumen
    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_create_repeat_allocations(client_as_owner):
    """Wiederholte Zuweisungen erzeugen korrekte Anzahl mit gleicher series_id."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "Repeat-Test"}
    )
    project_id = proj_resp.json()["id"]

    body = {
        "capacity_project_id": project_id,
        "week_start": "2026-06-01",
        "end_date": "2026-06-29",
        "minutes": 960,
        "interval_weeks": 1,
    }
    resp = await client_as_owner.post("/api/capacity/allocations/repeat", json=body)
    assert resp.status_code == 201
    allocs = resp.json()
    assert len(allocs) == 5  # 5 Wochen: 1., 8., 15., 22., 29. Juni

    series_ids = {a["series_id"] for a in allocs}
    assert len(series_ids) == 1
    assert series_ids.pop() is not None

    # Aufräumen
    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_bulk_delete_series(client_as_owner):
    """Bulk-Delete einer Serie löscht alle Einträge."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "Bulk-Delete-Test"}
    )
    project_id = proj_resp.json()["id"]

    repeat_resp = await client_as_owner.post("/api/capacity/allocations/repeat", json={
        "capacity_project_id": project_id,
        "week_start": "2026-07-06",
        "end_date": "2026-07-27",
        "minutes": 480,
    })
    allocs = repeat_resp.json()
    series_id = allocs[0]["series_id"]

    bulk_resp = await client_as_owner.post("/api/capacity/allocations/bulk", json={
        "action": "delete",
        "series_id": series_id,
    })
    assert bulk_resp.status_code == 200

    # Prüfen: keine Allocations mehr für dieses Projekt
    list_resp = await client_as_owner.get(
        "/api/capacity/allocations",
        params={"from": "2026-07-06", "to": "2026-07-27"},
    )
    remaining = [a for a in list_resp.json() if a["capacity_project_id"] == project_id]
    assert len(remaining) == 0

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_bulk_delete_from_week(client_as_owner):
    """Bulk-Delete ab einer bestimmten Woche löscht nur nachfolgende Einträge."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "Delete-From-Test"}
    )
    project_id = proj_resp.json()["id"]

    repeat_resp = await client_as_owner.post("/api/capacity/allocations/repeat", json={
        "capacity_project_id": project_id,
        "week_start": "2026-08-03",
        "end_date": "2026-08-31",
        "minutes": 480,
    })
    allocs = repeat_resp.json()
    series_id = allocs[0]["series_id"]
    assert len(allocs) >= 4

    # Ab der 3. Woche löschen
    bulk_resp = await client_as_owner.post("/api/capacity/allocations/bulk", json={
        "action": "delete_from",
        "series_id": series_id,
        "from_week": "2026-08-17",
    })
    assert bulk_resp.status_code == 200

    list_resp = await client_as_owner.get(
        "/api/capacity/allocations",
        params={"from": "2026-08-03", "to": "2026-08-31"},
    )
    remaining = [a for a in list_resp.json() if a["capacity_project_id"] == project_id]
    assert len(remaining) == 2  # Nur 3. und 10. August bleiben

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_weekly_summary_basic(client_as_owner):
    """Wochen-Summary berechnet korrekte Auslastung."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "Summary-Test", "status": "bestätigt"}
    )
    project_id = proj_resp.json()["id"]

    # 20h (1200 min) pro Woche = 50% von 40h
    await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2026-09-07",
        "minutes": 1200,
    })

    resp = await client_as_owner.get(
        "/api/capacity/weekly-summary",
        params={"from": "2026-09-07", "to": "2026-09-07"},
    )
    assert resp.status_code == 200
    summary = resp.json()
    assert len(summary) == 1
    assert summary[0]["planned_minutes"] == 1200
    assert summary[0]["available_minutes"] == 2400
    assert summary[0]["utilization_pct"] == 50.0

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_day_allocation_aggregates_to_week(client_as_owner):
    """Tages-Allocations werden in der Weekly-Summary korrekt zur Woche aggregiert."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "Tag-Aggregation-Test", "status": "bestätigt"}
    )
    project_id = proj_resp.json()["id"]

    # Mittwoch 10.06.2026 → Woche 08.06.2026
    await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2026-06-10",
        "minutes": 480,
        "allocation_type": "day",
    })
    # Samstag 13.06.2026 → gleiche Woche 08.06.2026
    await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2026-06-13",
        "minutes": 480,
        "allocation_type": "day",
    })

    resp = await client_as_owner.get(
        "/api/capacity/weekly-summary",
        params={"from": "2026-06-08", "to": "2026-06-14"},
    )
    assert resp.status_code == 200
    summary = resp.json()
    assert len(summary) == 1
    assert summary[0]["week_start"] == "2026-06-08"
    assert summary[0]["planned_minutes"] == 960  # 480 + 480

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_create_allocation_duplicate_day_upserts(client_as_owner):
    """Erneutes Buchen desselben Tages überschreibt die Stunden (Upsert), kein 500."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "Upsert-Test", "status": "bestätigt"}
    )
    project_id = proj_resp.json()["id"]

    # Dienstag 09.03.2027 — entspricht genau dem Fall aus dem Bug-Report
    first = await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2027-03-09",
        "minutes": 240,
        "allocation_type": "day",
    })
    assert first.status_code == 201
    assert first.json()["minutes"] == 240

    # Gleicher Tag erneut, andere Stundenzahl → Upsert statt 500
    second = await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2027-03-09",
        "minutes": 480,
        "allocation_type": "day",
    })
    assert second.status_code == 201
    assert second.json()["minutes"] == 480

    # Es darf nur EINE Allocation für diesen Tag existieren
    list_resp = await client_as_owner.get(
        "/api/capacity/allocations",
        params={"from": "2027-03-08", "to": "2027-03-14"},
    )
    for_project = [a for a in list_resp.json() if a["capacity_project_id"] == project_id]
    assert len(for_project) == 1
    assert for_project[0]["minutes"] == 480

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_create_allocation_unknown_project_409(client_as_owner):
    """POST /api/capacity/allocations mit nicht existierendem Projekt gibt 409 (kein 500)."""
    body = {
        "capacity_project_id": str(uuid.uuid4()),
        "week_start": "2027-03-09",
        "minutes": 480,
        "allocation_type": "day",
    }
    resp = await client_as_owner.post("/api/capacity/allocations", json=body)
    assert resp.status_code == 409


@pytest.mark.db
async def test_projects_list_includes_alloc_count(client_as_owner):
    """GET /api/capacity/projects liefert alloc_count je Projekt."""
    proj_resp = await client_as_owner.post(
        "/api/capacity/projects", json={"name": "AllocCount-Test", "status": "bestätigt"}
    )
    project_id = proj_resp.json()["id"]

    # Neues Projekt: alloc_count == 0
    list_resp = await client_as_owner.get("/api/capacity/projects")
    entry = next(p for p in list_resp.json() if p["id"] == project_id)
    assert entry["alloc_count"] == 0

    await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2027-03-09",
        "minutes": 480,
        "allocation_type": "day",
    })

    list_resp2 = await client_as_owner.get("/api/capacity/projects")
    entry2 = next(p for p in list_resp2.json() if p["id"] == project_id)
    assert entry2["alloc_count"] == 1

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_time_off_creates_and_deletes(client_as_owner):
    """Freie Tage erstellen und löschen."""
    resp = await client_as_owner.post("/api/capacity/time-off", json={
        "date": "2026-12-25",
        "type": "feiertag",
        "label": "Weihnachten",
    })
    assert resp.status_code == 201
    entry_id = resp.json()["id"]
    assert resp.json()["type"] == "feiertag"

    del_resp = await client_as_owner.delete(f"/api/capacity/time-off/{entry_id}")
    assert del_resp.status_code == 204


@pytest.mark.db
async def test_time_off_duplicate_409(client_as_owner):
    """Doppelter freier Tag gibt 409 Conflict."""
    await client_as_owner.post("/api/capacity/time-off", json={
        "date": "2026-12-26",
        "type": "feiertag",
        "label": "Stephanstag",
    })
    resp2 = await client_as_owner.post("/api/capacity/time-off", json={
        "date": "2026-12-26",
        "type": "feiertag",
    })
    assert resp2.status_code == 409

    # Aufräumen
    list_resp = await client_as_owner.get("/api/capacity/time-off", params={"year": 2026})
    for entry in list_resp.json():
        if entry["date"] == "2026-12-26":
            await client_as_owner.delete(f"/api/capacity/time-off/{entry['id']}")


# ── Umsatzprognose: Regressionstests zu falschen Monatszahlen ────────────────


@pytest.mark.db
async def test_forecast_revenue_nutzt_default_stundensatz(client_as_owner):
    """Projekte ohne eigenen Stundensatz zählen mit dem Default-Satz mit.

    Vorfall: Die Prognose filterte auf ``hourly_rate IS NOT NULL``. In der
    Praxis trägt fast kein Kapazitätsprojekt einen individuellen Satz, weshalb
    die Prognose nahezu die gesamte verrechenbare Kapazität verschwieg — im
    Monatsbriefing erschien ein Soll-Umsatz von wenigen Hundert Franken.
    """
    proj = await client_as_owner.post(
        "/api/capacity/projects",
        json={"name": "Prognose-ohne-Satz", "status": "bestätigt", "is_billable": True},
    )
    project_id = proj.json()["id"]
    # Montag 03.05.2027, ganze Woche innerhalb des Monats
    await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2027-05-03",
        "minutes": 600,
        "allocation_type": "week",
    })

    resp = await client_as_owner.get(
        "/api/capacity/forecast-revenue", params={"from": "2027-05-01", "to": "2027-05-31"}
    )
    assert resp.status_code == 200
    mai = next(m for m in resp.json() if m["month"] == "2027-05")
    assert mai["hours"] == pytest.approx(10.0)
    assert mai["revenue"] > 0

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")


@pytest.mark.db
async def test_forecast_revenue_teilt_woche_ueber_monatsgrenze(client_as_owner):
    """Eine Woche über die Monatsgrenze wird arbeitstagweise aufgeteilt.

    Vorfall: Die Allokation wurde vollständig dem Monat ihres Montags
    zugeschlagen. Die Woche ab Montag 31.05.2027 landete damit ganz im Mai,
    obwohl vier der fünf Arbeitstage im Juni liegen.
    """
    proj = await client_as_owner.post(
        "/api/capacity/projects",
        json={"name": "Prognose-Monatsgrenze", "status": "bestätigt", "is_billable": True},
    )
    project_id = proj.json()["id"]
    await client_as_owner.post("/api/capacity/allocations", json={
        "capacity_project_id": project_id,
        "week_start": "2027-05-31",
        "minutes": 1500,
        "allocation_type": "week",
    })

    resp = await client_as_owner.get(
        "/api/capacity/forecast-revenue", params={"from": "2027-05-01", "to": "2027-06-30"}
    )
    by_month = {m["month"]: m for m in resp.json()}
    # 1 von 5 Arbeitstagen im Mai (Mo 31.05.), 4 im Juni (Di–Fr 01.–04.06.)
    assert by_month["2027-05"]["hours"] == pytest.approx(5.0)
    assert by_month["2027-06"]["hours"] == pytest.approx(20.0)

    await client_as_owner.delete(f"/api/capacity/projects/{project_id}")
