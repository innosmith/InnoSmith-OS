"""Zugänge zu Bexio und Toggl — eine Stelle, nicht vier.

``_get_bexio_client`` stand wortgleich in ``routers/bexio.py``,
``routers/finance.py`` und ``routers/search.py``, ``_get_toggl_client`` ähnlich
in ``routers/toggl.py`` und ``routers/capacity.py``. Der Rechnungslauf wäre die
vierte beziehungsweise dritte Kopie gewesen.

Kopien sind hier nicht bloss unschön: die Reihenfolge Benutzereinstellung vor
Umgebungsvariable ist eine Entscheidung, und eine Entscheidung, die an vier
Stellen steht, ist an drei davon veraltet, sobald sie sich ändert. Genauso der
Fehlerfall — ein fehlender Zugang muss überall dieselbe Antwort geben, sonst
heisst derselbe Zustand einmal 400 und einmal 502.

Der Zugang kommt zuerst aus den Einstellungen des Benutzers und erst danach aus
der Umgebung. Das ist Absicht: die Umgebung trägt den Zugang des Betriebs, die
Einstellung den der Person.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import HTTPException

from app.models import User

_SRC = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_SRC / "bexio"))
sys.path.insert(0, str(_SRC / "toggl"))

from bexio_client import BexioClient, BexioConfig  # noqa: E402
from toggl_client import TogglClient, TogglConfig  # noqa: E402


def bexio_zugang(user: User) -> BexioClient:
    """Bexio-Client aus den Einstellungen des Benutzers oder aus der Umgebung."""
    einstellungen = user.settings or {}
    token = einstellungen.get("bexio_api_token") or ""
    if not token:
        from app.config import get_settings

        token = get_settings().bexio_api_token or ""
    if not token:
        raise HTTPException(status_code=400, detail="Bexio API-Token nicht konfiguriert")
    return BexioClient(BexioConfig(api_token=token))


def toggl_zugang(user: User) -> tuple[TogglClient, int]:
    """Toggl-Client samt Workspace.

    Der Workspace kommt mit zurück, weil ohne ihn keine Abfrage möglich ist und
    ein fehlender Workspace sonst erst tief in der Reports-API als Fehler
    aufträte — dort, wo er wie eine leere Antwort aussieht.
    """
    einstellungen = user.settings or {}
    token = einstellungen.get("toggl_api_token") or ""
    workspace = einstellungen.get("toggl_workspace_id") or 0
    if not token or not workspace:
        from app.config import get_settings

        cfg = get_settings()
        token = token or cfg.toggl_api_token or ""
        workspace = workspace or cfg.toggl_workspace_id or 0
    if not token:
        raise HTTPException(status_code=400, detail="Toggl API-Token nicht konfiguriert")
    if not workspace:
        raise HTTPException(status_code=400, detail="Toggl-Workspace nicht konfiguriert")
    workspace = int(workspace)
    return TogglClient(TogglConfig(api_token=token, workspace_id=workspace)), workspace
