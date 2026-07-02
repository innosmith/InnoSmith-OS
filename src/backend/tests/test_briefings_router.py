"""Tests für den manuellen Briefing-Trigger (POST /api/briefings/generate).

`create_briefing_job` wird gemockt, damit der Test ohne DB/Graph/Toggl läuft —
geprüft werden Owner-RBAC und der 202-Contract.
"""

import uuid

import pytest


@pytest.mark.asyncio
async def test_generate_briefing_owner_ok(client_as_owner, monkeypatch):
    fake_id = uuid.uuid4()

    async def _fake_create(briefing_type, owner, scheduled, *, manual=False):
        assert briefing_type == "daily_briefing"
        assert manual is True
        return fake_id

    monkeypatch.setattr("app.routers.briefings.create_briefing_job", _fake_create)

    resp = await client_as_owner.post(
        "/api/briefings/generate", json={"type": "daily_briefing"}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["briefing_type"] == "daily_briefing"
    assert body["agent_job_id"] == str(fake_id)


@pytest.mark.asyncio
async def test_generate_briefing_member_forbidden(client_as_member, monkeypatch):
    async def _fake_create(*a, **k):  # sollte nie aufgerufen werden
        raise AssertionError("create_briefing_job darf für Member nicht laufen")

    monkeypatch.setattr("app.routers.briefings.create_briefing_job", _fake_create)

    resp = await client_as_member.post(
        "/api/briefings/generate", json={"type": "weekly_briefing"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_generate_briefing_invalid_type(client_as_owner):
    resp = await client_as_owner.post(
        "/api/briefings/generate", json={"type": "quarterly_briefing"}
    )
    assert resp.status_code == 422
