"""Tests für den LLM-basierten LinkedIn-Profil-Extraktions-Endpoint.

Prüft:
- Plaintext-Cleaning (Whitespace-Konsolidierung)
- JSON-Extraktion aus verschiedenen LLM-Output-Formaten
- Erfolgreiche Extraktion mit gemocktem LiteLLM
- Fehlerbehandlung bei ungültigem LLM-Output
- Validierung der Request-Parameter
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.routers.linkedin import _clean_input, _extract_json, ExtractProfileRequest


# ── Fixtures: Realistische LinkedIn-Profiltext-Samples ────────────

PROFILE_DE_MULTI_POSITION = """Kim Tokarski
Professor (Entrepreneurship & Innovation) BFH | Speaker, Author, Consultant
Bern, Schweiz

Berufserfahrung
Professor (Entrepreneurship & Innovation)
Berner Fachhochschule
Sep 2010 – Heute · 15 Jahre 9 Monate
Bern, Schweiz

Visiting Professor
University of Applied Sciences Upper Austria
Mär 2020 – Heute · 5 Jahre 3 Monate

Direktor
Institute for Innovation, Strategic Entrepreneurship and Sustainable Development
Jan 2018 – Dez 2022 · 5 Jahre
Bern, Schweiz

Ausbildung
Universität Bern
Dr. rer. oec., Betriebswirtschaftslehre
2004 – 2008
"""

PROFILE_EN_SHORT = """Lars Kayser
Urbanist @ Urban Equipe
Bern, Switzerland

Experience
Urbanist
Urban Equipe
Oct 2018 – Present · 6 years 8 months
Bern, Switzerland

Co-Founder
Collectif Dynamo
Jan 2015 – Dec 2020 · 6 years

Education
ETH Zürich
MSc Architecture
2012 – 2014
"""

# ── Unit Tests ──────────────────────────────────────────────────

class TestInputCleaning:

    def test_consolidates_whitespace(self):
        text = "Zeile 1\n\n\n\n\nZeile 2"
        result = _clean_input(text)
        assert "Zeile 1" in result
        assert "Zeile 2" in result
        assert "\n\n\n" not in result

    def test_handles_empty_input(self):
        assert _clean_input("") == ""

    def test_handles_plain_text(self):
        result = _clean_input("Einfacher Text ohne HTML")
        assert "Einfacher Text" in result

    def test_handles_linkedin_plaintext(self):
        result = _clean_input(PROFILE_EN_SHORT)
        assert "Lars Kayser" in result
        assert "Urban Equipe" in result
        assert "Experience" in result

    def test_preserves_structure(self):
        result = _clean_input(PROFILE_DE_MULTI_POSITION)
        assert "Kim Tokarski" in result
        assert "Berner Fachhochschule" in result
        assert "Berufserfahrung" in result


class TestJsonExtraction:

    def test_plain_json(self):
        result = _extract_json('{"name": "Test"}')
        assert result["name"] == "Test"

    def test_json_in_code_block(self):
        text = '```json\n{"name": "Test"}\n```'
        result = _extract_json(text)
        assert result["name"] == "Test"

    def test_json_in_bare_code_block(self):
        text = '```\n{"name": "Test"}\n```'
        result = _extract_json(text)
        assert result["name"] == "Test"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_json("Das ist kein JSON")

    def test_whitespace_around_json(self):
        result = _extract_json('  \n {"name": "Test"} \n ')
        assert result["name"] == "Test"


class TestExtractProfileRequest:

    def test_rejects_short_text(self):
        with pytest.raises(Exception):
            ExtractProfileRequest(html="kurzer Text")

    def test_accepts_valid_text(self):
        req = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
        assert len(req.html) > 50


def _mock_litellm_response(content: dict) -> SimpleNamespace:
    """Erzeugt ein Mock-Response-Objekt wie von litellm.acompletion."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(content))
            )
        ]
    )


class TestExtractProfileEndpoint:

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        from app.routers.linkedin import extract_profile_from_html, ExtractProfileRequest

        mock_user = SimpleNamespace(
            id=1, email="test@example.com", role="owner", settings={}
        )

        expected_output = {
            "name": "Max Mustermann",
            "headline": "Senior Consultant bei InnoSmith GmbH",
            "location": "Bern, Schweiz",
            "job_title": "Senior Consultant",
            "companies": ["InnoSmith GmbH"],
        }

        with patch("app.routers.linkedin.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(
                return_value=_mock_litellm_response(expected_output)
            )

            body = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
            result = await extract_profile_from_html(body=body, user=mock_user)

            assert result.name == "Max Mustermann"
            assert result.headline == "Senior Consultant bei InnoSmith GmbH"
            assert result.location == "Bern, Schweiz"
            assert result.job_title == "Senior Consultant"
            assert "InnoSmith GmbH" in result.companies
            assert result.extraction_method == "llm"

    @pytest.mark.asyncio
    async def test_empty_fields_from_llm(self):
        from app.routers.linkedin import extract_profile_from_html, ExtractProfileRequest

        mock_user = SimpleNamespace(
            id=1, email="test@example.com", role="owner", settings={}
        )

        empty_output = {
            "name": "",
            "headline": "",
            "location": "",
            "job_title": "",
            "companies": [],
        }

        with patch("app.routers.linkedin.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(
                return_value=_mock_litellm_response(empty_output)
            )

            body = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
            result = await extract_profile_from_html(body=body, user=mock_user)

            assert result.name == ""
            assert result.companies == []

    @pytest.mark.asyncio
    async def test_invalid_json_from_llm(self):
        from app.routers.linkedin import extract_profile_from_html, ExtractProfileRequest
        from fastapi import HTTPException

        mock_user = SimpleNamespace(
            id=1, email="test@example.com", role="owner", settings={}
        )

        broken_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Das ist kein JSON")
                )
            ]
        )

        with patch("app.routers.linkedin.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=broken_response)

            body = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
            with pytest.raises(HTTPException) as exc_info:
                await extract_profile_from_html(body=body, user=mock_user)
            assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_llm_connection_error(self):
        from app.routers.linkedin import extract_profile_from_html, ExtractProfileRequest
        from fastapi import HTTPException

        mock_user = SimpleNamespace(
            id=1, email="test@example.com", role="owner", settings={}
        )

        with patch("app.routers.linkedin.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(
                side_effect=ConnectionError("API nicht erreichbar")
            )

            body = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
            with pytest.raises(HTTPException) as exc_info:
                await extract_profile_from_html(body=body, user=mock_user)
            assert exc_info.value.status_code == 503
            assert "fehlgeschlagen" in exc_info.value.detail.lower() or "nicht erreichbar" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_llm_empty_content(self):
        from app.routers.linkedin import extract_profile_from_html, ExtractProfileRequest
        from fastapi import HTTPException

        mock_user = SimpleNamespace(
            id=1, email="test@example.com", role="owner", settings={}
        )

        empty_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="")
                )
            ]
        )

        with patch("app.routers.linkedin.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=empty_response)

            body = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
            with pytest.raises(HTTPException) as exc_info:
                await extract_profile_from_html(body=body, user=mock_user)
            assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_uses_gemini_model_and_fallback(self):
        from app.routers.linkedin import (
            LINKEDIN_EXTRACT_FALLBACK_MODEL,
            LINKEDIN_EXTRACT_MODEL,
            extract_profile_from_html,
            ExtractProfileRequest,
        )

        mock_user = SimpleNamespace(
            id=1, email="test@example.com", role="owner", settings={}
        )
        expected_output = {
            "name": "Max Mustermann",
            "headline": "Consultant",
            "location": "Bern",
            "job_title": "Consultant",
            "companies": ["InnoSmith"],
        }

        assert LINKEDIN_EXTRACT_MODEL.startswith("gemini/")
        assert LINKEDIN_EXTRACT_FALLBACK_MODEL.startswith("gemini/")

        with patch("app.routers.linkedin.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(
                side_effect=[
                    ConnectionError("primary down"),
                    _mock_litellm_response(expected_output),
                ]
            )

            body = ExtractProfileRequest(html=PROFILE_DE_MULTI_POSITION)
            result = await extract_profile_from_html(body=body, user=mock_user)

            assert result.name == "Max Mustermann"
            assert mock_litellm.acompletion.await_count == 2
            first_model = mock_litellm.acompletion.await_args_list[0].kwargs["model"]
            second_model = mock_litellm.acompletion.await_args_list[1].kwargs["model"]
            assert first_model == LINKEDIN_EXTRACT_MODEL
            assert second_model == LINKEDIN_EXTRACT_FALLBACK_MODEL
            assert mock_litellm.acompletion.await_args_list[0].kwargs["reasoning_effort"] == "none"
