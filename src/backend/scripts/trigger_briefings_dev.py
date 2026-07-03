"""Einmal-Skript: alle drei Briefing-Jobs in Dev einreihen (Worker im Backend pollt).

Aufruf: TP_APP_ENV=dev PYTHONPATH=src/backend python src/backend/scripts/trigger_briefings_dev.py
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database import async_session
from app.models import User
from app.services.briefing import create_briefing_job


async def main() -> None:
    async with async_session() as db:
        owner = (
            await db.execute(select(User).where(User.role == "owner").limit(1))
        ).scalar_one_or_none()
    if owner is None:
        raise SystemExit("Kein Owner in der Dev-DB gefunden")

    now = datetime.now(ZoneInfo("Europe/Zurich"))
    for briefing_type in ("daily_briefing", "weekly_briefing", "monthly_briefing"):
        job_id = await create_briefing_job(briefing_type, owner, now, manual=True)
        print(f"{briefing_type}: {job_id}")


if __name__ == "__main__":
    asyncio.run(main())
