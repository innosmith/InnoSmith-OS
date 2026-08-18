"""Tests für den Kurz-Cache vor Microsoft Graph.

Vorfall 18.08.2026: Ein Deploy schrieb 216 Tasks, der Postgres-Trigger schickte
216 SSE-Events, und das offene Cockpit lud daraufhin 174 mal denselben Kalender
und 198 mal dieselben markierten Mails -- 2858 HTTP-Anfragen in 36 Sekunden. Am
Ende drosselte Microsoft das Postfach. Der Cache ist die zweite Bremse hinter
dem Debounce im Frontend; ohne diese Tests fehlt der Beleg, dass sie greift.
"""

import asyncio

import pytest

from app.services import graph_cache


@pytest.fixture(autouse=True)
def leerer_cache():
    graph_cache._cache.clear()
    graph_cache._inflight.clear()
    yield
    graph_cache._cache.clear()
    graph_cache._inflight.clear()


@pytest.mark.asyncio
async def test_zweiter_aufruf_kommt_aus_dem_cache():
    aufrufe = []

    async def laden():
        aufrufe.append(1)
        return {"wert": len(aufrufe)}

    erstes = await graph_cache.cached("k", laden)
    zweites = await graph_cache.cached("k", laden)

    assert len(aufrufe) == 1
    assert erstes == zweites == {"wert": 1}


@pytest.mark.asyncio
async def test_parallele_aufrufe_lösen_nur_einen_ladevorgang_aus():
    """Der eigentliche Sturm trifft gleichzeitig ein, nicht nacheinander.

    Ohne Single-Flight hilft eine TTL wenig: solange der erste Graph-Aufruf
    läuft, ist der Cache leer und alle Wartenden starten eigene Aufrufe.
    """
    aufrufe = []

    async def laden():
        aufrufe.append(1)
        await asyncio.sleep(0.05)
        return "daten"

    ergebnisse = await asyncio.gather(
        *[graph_cache.cached("k", laden) for _ in range(20)]
    )

    assert len(aufrufe) == 1
    assert ergebnisse == ["daten"] * 20


@pytest.mark.asyncio
async def test_abgelaufener_eintrag_wird_neu_geladen():
    aufrufe = []

    async def laden():
        aufrufe.append(1)
        return len(aufrufe)

    assert await graph_cache.cached("k", laden, ttl=0.01) == 1
    await asyncio.sleep(0.02)
    assert await graph_cache.cached("k", laden, ttl=0.01) == 2


@pytest.mark.asyncio
async def test_fehler_wird_nicht_gecacht():
    """Ein gedrosselter Aufruf darf sich nicht als Ergebnis festsetzen."""
    versuche = []

    async def laden():
        versuche.append(1)
        if len(versuche) == 1:
            raise RuntimeError("Graph drosselt")
        return "daten"

    with pytest.raises(RuntimeError):
        await graph_cache.cached("k", laden)

    assert await graph_cache.cached("k", laden) == "daten"
    assert len(versuche) == 2


@pytest.mark.asyncio
async def test_invalidate_verwirft_nur_das_präfix():
    async def laden_a():
        return "a"

    async def laden_b():
        return "b"

    await graph_cache.cached("events:heute", laden_a)
    await graph_cache.cached("flagged:20", laden_b)

    graph_cache.invalidate("events:")

    assert "flagged:20" in graph_cache._cache
    assert "events:heute" not in graph_cache._cache
