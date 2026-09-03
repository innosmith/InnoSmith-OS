"""InvoiceInsight MCP-Client -- verbindet sich per Streamable HTTP zum MCP-Server."""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from cachetools import TTLCache
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

logger = logging.getLogger("taskpilot.invoiceinsight")

_resource_cache: TTLCache = TTLCache(maxsize=32, ttl=300)
_tool_cache: TTLCache = TTLCache(maxsize=64, ttl=120)


def _parse_content(result: Any) -> Any:
    """Extrahiert JSON aus MCP-Antworten (Resource oder Tool)."""
    if hasattr(result, "contents"):
        parts = []
        for c in result.contents:
            text = getattr(c, "text", None)
            if text:
                try:
                    parts.append(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    parts.append(text)
        return parts[0] if len(parts) == 1 else parts

    if hasattr(result, "content"):
        for c in result.content:
            text = getattr(c, "text", None)
            if text:
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
    return result


def _build_args(**kwargs: Any) -> dict[str, Any] | None:
    """Baut ein Argument-Dict nur aus gesetzten (nicht-None) Werten."""
    args = {k: v for k, v in kwargs.items() if v is not None}
    return args or None


@asynccontextmanager
async def _mcp_sitzung(url: str, api_key: str) -> AsyncIterator[ClientSession]:
    """MCP-2-Sitzung: Header über httpx2-Client, Transport liefert (read, write)."""
    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {api_key}"},
    ) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class InvoiceInsightClient:
    """Async Client fuer den InvoiceInsight MCP-Server."""

    def __init__(self, url: str, api_key: str):
        self._url = url
        self._api_key = api_key

    async def read_resource(self, uri: str, *, use_cache: bool = True) -> Any:
        if use_cache:
            cached = _resource_cache.get(uri)
            if cached is not None:
                return cached

        async with _mcp_sitzung(self._url, self._api_key) as session:
            raw = await session.read_resource(uri)
            data = _parse_content(raw)
            if use_cache:
                _resource_cache[uri] = data
            return data

    async def call_tool(self, name: str, arguments: dict | None = None, *, use_cache: bool = False) -> Any:
        cache_key = f"{name}:{json.dumps(arguments or {}, sort_keys=True)}"
        if use_cache:
            cached = _tool_cache.get(cache_key)
            if cached is not None:
                return cached

        async with _mcp_sitzung(self._url, self._api_key) as session:
            raw = await session.call_tool(name, arguments or {})
            data = _parse_content(raw)
            if use_cache:
                _tool_cache[cache_key] = data
            return data

    # ── Filterbare Tools (ehemals Resources) ─────────────

    async def get_kpis(
        self, year_from: int | None = None, year_to: int | None = None,
    ) -> dict:
        return await self.call_tool(
            "get_kpis",
            _build_args(year_from=year_from, year_to=year_to),
            use_cache=True,
        )

    async def get_cost_distribution(
        self,
        year_from: int | None = None,
        year_to: int | None = None,
        categories: list[str] | None = None,
    ) -> Any:
        return await self.call_tool(
            "get_cost_distribution",
            _build_args(year_from=year_from, year_to=year_to, categories=categories),
            use_cache=True,
        )

    async def get_anomalies(
        self, year_from: int | None = None, year_to: int | None = None,
    ) -> Any:
        return await self.call_tool(
            "get_anomalies",
            _build_args(year_from=year_from, year_to=year_to),
            use_cache=True,
        )

    async def get_yoy_comparison(
        self, year_from: int | None = None, year_to: int | None = None,
    ) -> Any:
        return await self.call_tool(
            "get_yoy_comparison",
            _build_args(year_from=year_from, year_to=year_to),
            use_cache=True,
        )

    async def get_recurring_vs_onetime(
        self, year_from: int | None = None, year_to: int | None = None,
    ) -> Any:
        return await self.call_tool(
            "get_recurring_vs_onetime",
            _build_args(year_from=year_from, year_to=year_to),
            use_cache=True,
        )

    async def get_renewal_calendar(
        self,
        vendors: list[str] | None = None,
        months_ahead: int | None = None,
    ) -> Any:
        return await self.call_tool(
            "get_renewal_calendar",
            _build_args(vendors=vendors, months_ahead=months_ahead),
            use_cache=True,
        )

    # ── Vollexport für den Datenraum ────────────────────

    async def export_alle_rechnungen(
        self, seitengroesse: int = 500,
    ) -> tuple[list[dict], dict]:
        """Den ganzen Rechnungsbestand blätternd holen -- Zeilen und Befund.

        Alle anderen Methoden dieses Clients holen Kennzahlen: aggregiert, klein,
        für den Dialog gedacht. Diese holt den Bestand selbst, und zwar für den
        Datenraum -- die Zeilen gehen in eine Parquet-Datei, nie in einen Kontext.

        Die Vollständigkeit wird **geprüft, nicht angenommen**. ``total`` aus der
        ersten Seite ist die Sollzahl; weicht die Anzahl geholter Zeilen davon ab,
        steht das im Befund und wandert in den Katalog. Ohne diesen Abgleich sieht
        ein abgebrochener Export aus wie ein kleiner Bestand -- und genau daran
        sind die Kreditorenwerkzeuge bisher gescheitert: ``search_invoices``
        schneidet bei 20 Zeilen ab, ohne dass irgendwo steht, dass abgeschnitten
        wurde.

        Die Notbremse bei 200 Seiten fängt einen Server ab, der nie eine leere
        Seite liefert. Eine Endlosschleife im Abgleich-Worker wäre schlimmer als
        ein unvollständiger Abzug, denn sie fällt erst auf, wenn nichts mehr geht.
        """
        zeilen: list[dict] = []
        soll = 0
        stand = ""
        schema: dict = {}
        fehlend: dict = {}
        offset = 0

        for _ in range(200):
            seite = await self.call_tool(
                "export_invoices",
                {"offset": offset, "limit": seitengroesse},
            )
            if not isinstance(seite, dict):
                raise RuntimeError(
                    f"export_invoices lieferte {type(seite).__name__} statt eines Objekts "
                    "-- läuft die passende Fassung des MCP-Servers?"
                )
            if offset == 0:
                soll = int(seite.get("total") or 0)
                stand = str(seite.get("as_of") or "")
                schema = seite.get("schema") or {}
                fehlend = seite.get("nicht_auswertbar") or {}
            teil = seite.get("rows") or []
            if not teil:
                break
            zeilen.extend(teil)
            offset += len(teil)
        else:
            logger.warning("InvoiceInsight-Export nach 200 Seiten abgebrochen")

        befund: dict = {"gemeldet": soll, "geholt": len(zeilen), "stand": stand}
        if schema.get("meaning"):
            befund["spalten_bedeutung"] = schema["meaning"]
        if fehlend:
            # Belege, die es gibt und die nicht im Bestand stehen. Gehört in den
            # Katalog, nicht ins Protokoll: ein blinder Fleck, den niemand nennt,
            # sieht aus wie Vollständigkeit.
            befund["nicht_auswertbare_belege"] = fehlend
        if soll and len(zeilen) != soll:
            befund["unvollstaendig"] = f"{len(zeilen)} von {soll} Zeilen"
            logger.warning(
                "InvoiceInsight-Export unvollständig: %d von %d Zeilen",
                len(zeilen), soll,
            )
        return zeilen, befund

    # ── Reine Resources (ohne Filterparameter) ──────────

    async def get_cashflow_forecast(self) -> Any:
        return await self.read_resource("invoices://cashflow-forecast")

    async def get_vendor_overview(self) -> Any:
        return await self.read_resource("invoices://vendor-overview")

    async def get_data_quality(self) -> Any:
        return await self.read_resource("invoices://data-quality")

    async def get_metadata(self) -> Any:
        return await self.read_resource("invoices://metadata")

    def invalidate_cache(self) -> None:
        _resource_cache.clear()
        _tool_cache.clear()
