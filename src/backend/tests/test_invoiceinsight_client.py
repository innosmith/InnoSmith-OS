"""InvoiceInsight-Client gegen MCP 2.0.

Der Prod-Start (03.09.2026) brach ab, weil MCP 2 den Transport umbenannt hat:
``streamablehttp_client`` heisst ``streamable_http_client``, liefert ein
Zweier-Tupel und nimmt Header nur noch über ``create_mcp_http_client``.
Ohne diesen Test fällt der Import wieder erst im Container auf.
"""

from pathlib import Path

_CLIENT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "services"
    / "invoiceinsight_client.py"
)


def test_invoiceinsight_client_importiert():
    from app.services.invoiceinsight_client import InvoiceInsightClient

    assert InvoiceInsightClient is not None


def test_invoiceinsight_client_nutzt_mcp2_transport():
    quelltext = _CLIENT.read_text(encoding="utf-8")
    assert "streamablehttp_client" not in quelltext
    assert "streamable_http_client" in quelltext
    assert "create_mcp_http_client" in quelltext
