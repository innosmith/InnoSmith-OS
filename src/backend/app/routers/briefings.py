"""FastAPI-Router für manuelle Briefing-Auslösung (Owner-only, Testzwecke).

Erzeugt sofort einen Daily/Weekly/Monthly-Briefing-Job über denselben Pfad wie
der Scheduler (``create_briefing_job``), umgeht dabei aber bewusst Zeitplan,
Karenzfrist und Dedupe — so lässt sich der Inhalt jederzeit prüfen.
"""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.deps import require_role
from app.models import User
from app.services.briefing import _TZ, create_briefing_job

logger = logging.getLogger("taskpilot.briefings_api")

router = APIRouter(prefix="/api/briefings", tags=["briefings"])


class GenerateBriefingBody(BaseModel):
    type: Literal["daily_briefing", "weekly_briefing", "monthly_briefing"]


@router.post("/generate", status_code=202)
async def generate_briefing_now(
    body: GenerateBriefingBody,
    user: User = Depends(require_role("owner")),
) -> dict:
    """Löst sofort ein Briefing des gewählten Typs aus (queued, für Tests)."""
    job_id = await create_briefing_job(body.type, user, datetime.now(_TZ), manual=True)
    logger.info("Manueller Briefing-Trigger: %s (Job %s)", body.type, job_id)
    return {"status": "queued", "agent_job_id": str(job_id), "briefing_type": body.type}
