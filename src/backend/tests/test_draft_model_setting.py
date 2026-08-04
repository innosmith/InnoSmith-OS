"""Tests: Wer schreibt den E-Mail-Entwurf -- lokal oder ein oeffentliches Modell?

Die Wahl liegt beim Berater (Owner-Settings), nicht bei einem Deployment-Flag. Der
Schalter ``draft_cloud_enabled`` ist die eigentliche Freigabe: ohne ihn bleibt ein
hinterlegter Modellname wirkungslos. So kann ein Cloud-Modell konfiguriert und die
Anonymisierung geprueft werden, bevor tatsaechlich Text das Haus verlaesst.

Beim Ausfall gilt fail-closed in die datenschutzfreundliche Richtung: ist die DB
nicht lesbar, schreibt das lokale Modell -- nie umgekehrt.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.hermes_worker as hw
import app.services.llm_defaults as ld


def _owner(settings: dict):
    return patch.object(ld, "get_owner_settings", new=AsyncMock(return_value=settings))


@pytest.mark.asyncio
async def test_disabled_means_local_even_with_model():
    with _owner({"draft_cloud_enabled": False, "draft_model": "anthropic/claude"}):
        assert await ld.get_draft_model(None) == ""


@pytest.mark.asyncio
async def test_missing_flag_means_local():
    with _owner({"draft_model": "anthropic/claude"}):
        assert await ld.get_draft_model(None) == ""


@pytest.mark.asyncio
async def test_enabled_uses_settings_model():
    with _owner({"draft_cloud_enabled": True, "draft_model": "anthropic/claude"}):
        assert await ld.get_draft_model(None) == "anthropic/claude"


@pytest.mark.asyncio
async def test_enabled_falls_back_to_env_model():
    """``TP_DRAFT_MODEL`` bleibt der Startwert einer frischen Installation."""
    with _owner({"draft_cloud_enabled": True}), patch(
        "app.config.get_settings",
        return_value=SimpleNamespace(draft_model="openai/gpt"),
    ):
        assert await ld.get_draft_model(None) == "openai/gpt"


@pytest.mark.asyncio
async def test_worker_falls_back_to_local_when_db_unreachable():
    def _boom():
        raise RuntimeError("keine DB")

    with patch.object(hw, "async_session", _boom):
        assert await hw._resolve_draft_model() == ""
