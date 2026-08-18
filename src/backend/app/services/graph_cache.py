"""Kurz-Cache für Graph-Abfragen, die das Cockpit im Sekundentakt stellt.

Am 18.08.2026 löste ein Deploy 216 SSE-Events aus; das offene Cockpit lud
darauf 174 mal denselben Kalender und 198 mal dieselben markierten Mails. Jede
Anfrage ging live an Microsoft Graph, bis das Postfach gedrosselt wurde. Ein
Cache von wenigen Sekunden macht daraus einen einzigen Aufruf, ohne dass die
Anzeige merklich altert.

Zwei Bausteine, mehr braucht es nicht: eine TTL und ein Single-Flight, damit
auch parallel eintreffende Anfragen nur einen Graph-Aufruf auslösen.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

DEFAULT_TTL_SECONDS = 45.0

# Deckel gegen unbegrenztes Wachstum. Die Schlüssel sind Query-Kombinationen,
# in der Praxis eine Handvoll -- der Deckel ist reine Vorsorge.
_MAX_ENTRIES = 200

_cache: dict[str, tuple[float, Any]] = {}
_inflight: dict[str, asyncio.Task] = {}


def _prune() -> None:
    jetzt = time.monotonic()
    for key in [k for k, (ablauf, _) in _cache.items() if ablauf <= jetzt]:
        _cache.pop(key, None)
    if len(_cache) > _MAX_ENTRIES:
        _cache.clear()


async def cached(
    key: str,
    laden: Callable[[], Awaitable[Any]],
    ttl: float = DEFAULT_TTL_SECONDS,
) -> Any:
    """Gibt den gecachten Wert zurück oder lädt ihn genau einmal.

    Treffen mehrere Anfragen gleichzeitig auf einen kalten Cache, wartet die
    zweite auf den Aufruf der ersten statt selbst einen zu starten. Fehler
    (auch ``GraphThrottledError``) werden an alle Wartenden weitergegeben --
    ein Fehlschlag wird nicht gecacht.
    """
    treffer = _cache.get(key)
    if treffer is not None and time.monotonic() < treffer[0]:
        return treffer[1]

    laufend = _inflight.get(key)
    if laufend is not None:
        return await asyncio.shield(laufend)

    aufgabe = asyncio.ensure_future(laden())
    _inflight[key] = aufgabe
    try:
        wert = await aufgabe
    finally:
        _inflight.pop(key, None)

    _cache[key] = (time.monotonic() + ttl, wert)
    _prune()
    return wert


def invalidate(prefix: str) -> None:
    """Verwirft alle Einträge mit diesem Präfix (nach schreibenden Zugriffen)."""
    for key in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(key, None)
