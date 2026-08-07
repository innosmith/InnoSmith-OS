"""OpenAPI-Contract-Guard: Prueft ob alle vom Frontend genutzten API-Endpoints
im Backend-Schema existieren.

Schuetzt gegen die haeufigste Fehlerklasse bei getrennter Frontend/Backend-Entwicklung:
ein Endpoint wird umbenannt, geloescht oder seine Methode aendert sich.

Ausfuehrung:
    make test-contract
    # oder: python -m pytest tests/contract/ -v
"""

import os
import json

import httpx
import pytest

BACKEND_URL = os.environ.get("TP_SMOKE_BACKEND_URL", "http://localhost:8000")

FRONTEND_EXPECTED_ENDPOINTS: list[tuple[str, str]] = [
    # Auth
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("GET", "/api/auth/me"),
    ("PATCH", "/api/auth/me"),
    ("POST", "/api/auth/change-password"),
    ("POST", "/api/auth/mfa/verify"),
    ("POST", "/api/auth/mfa/disable"),
    # Tasks
    ("POST", "/api/tasks"),
    ("POST", "/api/tasks/reorder"),
    ("GET", "/api/tasks/due-today"),
    ("PATCH", "/api/tasks/{task_id}"),
    ("DELETE", "/api/tasks/{task_id}"),
    ("POST", "/api/tasks/{task_id}/confirm"),
    ("POST", "/api/tasks/{task_id}/dismiss-review"),
    ("POST", "/api/tasks/{task_id}/checklist"),
    ("PATCH", "/api/tasks/{task_id}/checklist/{item_id}"),
    ("DELETE", "/api/tasks/{task_id}/checklist/{item_id}"),
    ("POST", "/api/tasks/{task_id}/activity"),
    ("POST", "/api/tasks/{task_id}/attachments"),
    ("DELETE", "/api/tasks/{task_id}/attachments/{attachment_id}"),
    # Projects
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),
    ("PATCH", "/api/projects/{project_id}"),
    ("DELETE", "/api/projects/{project_id}"),
    ("POST", "/api/projects/{project_id}/columns"),
    ("PATCH", "/api/projects/{project_id}/columns/{column_id}"),
    ("DELETE", "/api/projects/{project_id}/columns/{column_id}"),
    # Pipeline
    ("GET", "/api/pipeline"),
    # Tags
    ("POST", "/api/tags/tasks/{task_id}/tags/{tag_id}"),
    ("DELETE", "/api/tags/tasks/{task_id}/tags/{tag_id}"),
    # Agent Jobs
    ("PATCH", "/api/agent-jobs/{job_id}"),
    # Emails
    ("PATCH", "/api/emails/{message_id}/read"),
    ("POST", "/api/emails/{draft_id}/send"),
    # Triage
    ("POST", "/api/triage/{triage_id}/dismiss"),
    ("POST", "/api/triage/{triage_id}/act"),
    # Calendar
    ("POST", "/api/calendar/events"),
    # Settings
    ("PATCH", "/api/settings"),
    ("PUT", "/api/settings/llm"),
    ("DELETE", "/api/settings/extension-api-key"),
    # Uploads
    ("POST", "/api/uploads/avatars"),
    ("POST", "/api/uploads/icons"),
]


def _normalize_path(path: str) -> str:
    """Wandelt Frontend-Pfade mit Parametern in OpenAPI-Format um.

    z.B. /api/tasks/{task_id} → /api/tasks/{task_id}
    Die Pfade sind bereits im OpenAPI-Format geschrieben.
    """
    return path


def _match_path(expected_path: str, schema_paths: dict) -> str | None:
    """Findet den passenden OpenAPI-Pfad, auch bei leicht abweichenden Parameternamen."""
    if expected_path in schema_paths:
        return expected_path

    expected_parts = expected_path.strip("/").split("/")
    for schema_path in schema_paths:
        schema_parts = schema_path.strip("/").split("/")
        if len(expected_parts) != len(schema_parts):
            continue
        match = True
        for ep, sp in zip(expected_parts, schema_parts):
            if ep.startswith("{") and sp.startswith("{"):
                continue
            if ep != sp:
                match = False
                break
        if match:
            return schema_path
    return None


@pytest.fixture(scope="module")
def openapi_schema():
    """Laedt das OpenAPI-Schema vom Backend."""
    try:
        r = httpx.get(f"{BACKEND_URL}/openapi.json", timeout=10.0)
        r.raise_for_status()
        return r.json()
    except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
        pytest.skip(f"Backend nicht erreichbar: {exc}")


class TestOpenAPIContract:
    """Prueft ob alle vom Frontend erwarteten Endpoints im Schema existieren."""

    @pytest.mark.parametrize(
        "method,path",
        FRONTEND_EXPECTED_ENDPOINTS,
        ids=[f"{m} {p}" for m, p in FRONTEND_EXPECTED_ENDPOINTS],
    )
    def test_endpoint_exists(self, openapi_schema, method, path):
        """Endpoint muss im OpenAPI-Schema existieren."""
        matched = _match_path(path, openapi_schema["paths"])
        assert matched is not None, (
            f"Endpoint {method} {path} fehlt im OpenAPI-Schema. "
            f"Frontend erwartet ihn, Backend bietet ihn nicht an."
        )
        path_item = openapi_schema["paths"][matched]
        assert method.lower() in path_item, (
            f"Endpoint {path} existiert, aber Methode {method} fehlt. "
            f"Verfuegbare Methoden: {list(path_item.keys())}"
        )
