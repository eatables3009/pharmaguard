"""
PharmaGuard MCP server -- the 8 tools the Talkdesk agent is allowed to call.

Streamable HTTP, stateless, served at /mcp inside the same FastAPI process as the
REST API (app.py), so both share one in-memory state and one tunnel URL.

Every tool is a thin wrapper over core.py and returns compact JSON as a single
text block (short keys, no nulls, no echoed inputs, no whitespace): tool output
lands in the Talkdesk agent's context window, so it stays small.

Failures come back as {"error": "..."} with isError set and no stack trace.

There is deliberately NO approve tool. Approving a replacement or a disposal is a
human action on the dashboard; the agent cannot reach it.
"""

import json

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.server import TransportSecuritySettings

import core

DESCRIPTION = (
    "Cold-chain and perishable goods incident tools: shipments, temperature history, "
    "products, suppliers, incidents."
)

mcp_server = MCPServer(
    "PharmaGuard",
    description=DESCRIPTION,
    instructions=(
        "Tools for responding to cold-chain and perishable-goods disruptions. Confirm the incident, "
        "read the shipment and its temperature history, find approved alternative suppliers, log every "
        "action you take, request a replacement, and escalate to a human when severity is HIGH or the "
        "situation is ambiguous. You cannot approve a replacement or a disposal -- only a human can."
    ),
)


def _json(payload) -> str:
    """One compact JSON text block -- no indentation, no wasted tokens."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _guard(fn, *args, **kwargs) -> str:
    try:
        return _json(fn(*args, **kwargs))
    except Exception as exc:  # never leak internals or a stack trace to the agent
        message = exc.message if isinstance(exc, core.CoreError) else str(exc)
        raise ToolError(_json({"error": message}))


@mcp_server.tool()
def get_shipment_details(shipment_id: str) -> str:
    """Get one shipment: ETA, last temperature, destination and carrier contacts, and the batches on board."""
    return _guard(core.compact_shipment, shipment_id)


@mcp_server.tool()
def check_temperature_history(shipment_id: str, limit: int = 10) -> str:
    """Get the last few temperature readings for a shipment, with min, max and minutes out of range."""
    return _guard(core.compact_temperature_history, shipment_id, limit)


@mcp_server.tool()
def get_product_details(product_id: str) -> str:
    """Get a product's required temperature range, tolerance in minutes, unit value in INR and shelf life."""
    return _guard(core.compact_product, product_id)


@mcp_server.tool()
def create_incident(shipment_id: str, type: str = "TEMP_EXCURSION", reason: str = "") -> str:
    """Open an incident for a shipment, or return the existing open one of that type. Safe to call twice."""
    return _guard(lambda: core.compact_incident(core.create_incident(shipment_id, type, reason or None)))


@mcp_server.tool()
def find_alternative_suppliers(product_id: str, units: int) -> str:
    """List up to 3 approved suppliers that hold enough stock of a product, fastest ETA first."""
    return _guard(core.compact_alternatives, product_id, units)


@mcp_server.tool()
def log_incident_event(incident_id: str, actor: str, action: str, note: str = "") -> str:
    """Append one timestamped entry to an incident's timeline, e.g. that the destination was contacted."""
    return _guard(lambda: core.compact_incident(core.log_event(incident_id, actor, action, note)))


@mcp_server.tool()
def request_replacement(incident_id: str, supplier_id: str, units: int) -> str:
    """Request replacement stock from an approved supplier. Sets AWAITING_APPROVAL; it never approves anything."""
    return _guard(lambda: core.compact_incident(core.request_replacement(incident_id, supplier_id, units)))


@mcp_server.tool()
def escalate_incident(incident_id: str, summary: str) -> str:
    """Hand the incident to the human quality manager with a short written summary."""
    return _guard(lambda: core.compact_incident(core.escalate(incident_id, summary)))


TOOL_NAMES = [
    "get_shipment_details", "check_temperature_history", "get_product_details", "create_incident",
    "find_alternative_suppliers", "log_incident_event", "request_replacement", "escalate_incident",
]

# Stateless streamable HTTP with JSON responses, and DNS-rebinding protection off so
# the server answers on whatever Host a Cloudflare/ngrok tunnel presents.
mcp_asgi_app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    ),
)
