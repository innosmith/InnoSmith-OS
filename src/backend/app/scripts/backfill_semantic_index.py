"""Entkoppelter Backfill des semantischen Such-Index (E-Mails + OneDrive-Dokumente).

Dieser Runner ist bewusst als **eigenständiger, langlaufender Einmal-Job** gedacht
und NICHT an den Backend-Lifespan gekoppelt: So blockiert er weder den Boot noch
wird er von einem `make prod` (Container-Recreate) beeinflusst, wenn er als
separater One-Off-Container läuft (siehe unten).

Ausführen (Produktion), entkoppelt vom laufenden Backend und von `make prod`:

    docker compose -p taskpilot-prod --env-file .env.prod \\
      -f docker/docker-compose.prod.yml run --rm -d \\
      --name taskpilot-backfill \\
      backend-prod python -m app.scripts.backfill_semantic_index

    # Fortschritt verfolgen:
    docker logs -f taskpilot-backfill

Der Job ist **idempotent**: E-Mails werden übersprungen, wenn bereits indexiert;
Dokumente nur bei geändertem ``lastModifiedDateTime`` neu eingebettet. Ein Abbruch
(Container-Stop) ist unkritisch -- einfach erneut starten, er macht dort weiter,
wo noch nichts indexiert wurde.

Optionen:
    --mail-top N     Neueste E-Mails je Ordner (Default: sehr hoch = alles)
    --mails-only     Nur E-Mails indexieren
    --docs-only      Nur OneDrive-Dokumente indexieren
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Ollama/HTTP-Rauschen dämpfen -- der Ingest loggt seinen eigenen Fortschritt.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run(mail_top: int, index_mails: bool, index_docs: bool) -> int:
    # Import erst nach dem Logging-Setup, damit Modul-Logger die Config erben.
    from app.services.semantic_index import sync_semantic_index

    log = logging.getLogger("taskpilot.backfill")
    log.info(
        "Backfill-Start (mail_top=%s, mails=%s, docs=%s)",
        mail_top, index_mails, index_docs,
    )
    t0 = time.monotonic()
    stats = await sync_semantic_index(
        mail_top=mail_top,
        index_mails=index_mails,
        index_docs=index_docs,
    )
    dt = time.monotonic() - t0
    log.info(
        "Backfill fertig in %.1f min: %d E-Mails, %d Dokumente, %d Chunks",
        dt / 60.0, stats["emails"], stats["documents"], stats["chunks"],
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill des semantischen Such-Index")
    parser.add_argument("--mail-top", type=int, default=100_000,
                        help="Neueste E-Mails je Ordner (Default: alles)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mails-only", action="store_true", help="Nur E-Mails")
    group.add_argument("--docs-only", action="store_true", help="Nur Dokumente")
    args = parser.parse_args()

    index_mails = not args.docs_only
    index_docs = not args.mails_only

    _configure_logging()
    return asyncio.run(_run(args.mail_top, index_mails, index_docs))


if __name__ == "__main__":
    raise SystemExit(main())
