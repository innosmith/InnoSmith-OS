"""Permanenter Daemon fuer den semantischen Such-Index.

Laeuft als eigenstaendiger, langlaufender Prozess in einem dedizierten Container
(``taskpilot-indexer-prod``) -- mit demselben Image wie das Backend, aber ohne
API/Requests. So konkurrenziert die schwere Embedding-Arbeit den API-Prozess nicht,
und es gibt genau EINE Instanz, die indexiert (der In-Process-Scheduler im Backend
ist per ``search_index_in_process=False`` deaktiviert).

Ablauf: Endlosschleife ``sync_semantic_index()`` (E-Mails ueber alle Ordner voll
paginiert + OneDrive-Dokumente mit Ausschlussfilter), dazwischen schlaeft der Daemon
``search_index_interval_seconds``. Jeder Lauf ist idempotent (Vorhandenes wird
uebersprungen) und beginnt mit einem Purge der ausgeschlossenen Pfade. SIGTERM/SIGINT
beenden den Daemon sauber zwischen zwei Etappen.

Deploy (Prod): als Service ``indexer-prod`` in ``docker-compose.prod.yml`` (siehe dort).
Manuell:

    docker compose -p taskpilot-prod --env-file .env.prod \\
      -f docker/docker-compose.prod.yml up -d indexer-prod
    docker logs -f taskpilot-indexer-prod
"""

from __future__ import annotations

import asyncio
import logging
import signal


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run() -> int:
    from app.config import get_settings
    from app.services.semantic_index import sync_semantic_index

    log = logging.getLogger("taskpilot.index_daemon")
    cfg = get_settings()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - nicht auf allen Plattformen
            pass

    interval = cfg.search_index_interval_seconds
    log.info("Index-Daemon gestartet (Intervall %ds, Voll-Archiv-Pagination)", interval)

    while not stop.is_set():
        try:
            stats = await sync_semantic_index()
            log.info(
                "Lauf fertig: %d E-Mails, %d Dokumente, %d Chunks",
                stats.get("emails", 0), stats.get("documents", 0), stats.get("chunks", 0),
            )
        except Exception:  # noqa: BLE001 - Daemon darf nie sterben
            log.exception("Index-Lauf fehlgeschlagen (wird erneut versucht)")
        # Zwischen den Laeufen schlafen -- aber sofort aufwachen bei Shutdown-Signal.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue

    log.info("Index-Daemon beendet (Signal empfangen)")
    return 0


def main() -> int:
    _configure_logging()
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
