"""Einmal-Skript: Briefing-Datenkontexte (Daily/Weekly/Monthly) in Dev ausgeben.

Nutzt dieselben Builder wie der Scheduler — reine Lesezugriffe, kein LLM.
Aufruf: TP_APP_ENV=dev PYTHONPATH=src/backend python src/backend/scripts/preview_briefing_context.py
"""

import asyncio
import sys

from sqlalchemy import select

from app.database import async_session
from app.models import User
from app.services import briefing_data


async def main() -> None:
    async with async_session() as db:
        owner = (
            await db.execute(select(User).where(User.role == "owner").limit(1))
        ).scalar_one_or_none()
    if owner is None:
        print("Kein Owner in der Dev-DB gefunden", file=sys.stderr)
        sys.exit(1)

    for name, builder in briefing_data.BUILDERS.items():
        print("\n" + "=" * 80)
        print(f"### {name}")
        print("=" * 80)
        try:
            ctx = await builder(owner)
            print(ctx["markdown"])
            print("\n-- Quellen-Status:", ctx["sources"])
        except Exception as e:  # noqa: BLE001
            print(f"FEHLER: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
