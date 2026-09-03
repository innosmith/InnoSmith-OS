"""Discover all tools and resources on the InvoiceInsight MCP server."""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = os.getenv("TP_INVOICEINSIGHT_URL", "http://127.0.0.1:8055/mcp")
API_KEY = os.environ["TP_INVOICEINSIGHT_API_KEY"]


async def main() -> None:
    async with create_mcp_http_client(
        headers={"Authorization": f"Bearer {API_KEY}"},
    ) as http:
        async with streamable_http_client(URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # --- Tools ---
                tools_result = await session.list_tools()
                tools = tools_result.tools
                print("=" * 80)
                print(f"TOOLS ({len(tools)})")
                print("=" * 80)
                for t in sorted(tools, key=lambda x: x.name):
                    print(f"\n--- {t.name} ---")
                    print(f"  Description: {t.description}")
                    schema = t.inputSchema
                    print(f"  Input Schema: {json.dumps(schema, indent=4, ensure_ascii=False)}")

                # --- Resources ---
                resources_result = await session.list_resources()
                resources = resources_result.resources
                print("\n" + "=" * 80)
                print(f"RESOURCES ({len(resources)})")
                print("=" * 80)
                for r in sorted(resources, key=lambda x: str(x.uri)):
                    print(f"\n--- {r.uri} ---")
                    print(f"  Name: {r.name}")
                    print(f"  Description: {getattr(r, 'description', 'n/a')}")
                    print(f"  MIME Type: {getattr(r, 'mimeType', 'n/a')}")


if __name__ == "__main__":
    asyncio.run(main())
