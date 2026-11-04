"""
PharmaGuard REST API + MCP server in one process.

  REST  (this file)  -> the dashboard and the simulator
  MCP   (/mcp)       -> the Talkdesk AI agent, 8 tools, see mcp_tools.py
  Rules (core.py)    -> shared state, so REST and MCP always agree

Env vars
  DATA_FILE   data.json (pharma, default) or data_grocery.json (supermarket)
  API_KEY     if set, non-GET REST routes require header X-API-Key
  MCP_TOKEN   bearer token for /mcp; generated and printed at startup if unset

Run: uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
"""

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import core
from mcp_tools import DESCRIPTION, TOOL_NAMES, mcp_asgi_app, mcp_server

API_KEY = os.environ.get("API_KEY")
MCP_TOKEN = os.environ.get("MCP_TOKEN") or secrets.token_urlsafe(18)
MCP_TOKEN_GENERATED = "MCP_TOKEN" not in os.environ
# /mcp accepts the MCP token, or the REST API key when one is configured.
MCP_ACCEPTED_TOKENS = {MCP_TOKEN} | ({API_KEY} if API_KEY else set())
OPEN_PATHS = ("/", "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the MCP streamable-HTTP session manager for the life of the API process."""
    banner = [
        "",
        "  PharmaGuard is up.",
        f"  vertical   : {core.DATA['vertical']}  (DATA_FILE={os.environ.get('DATA_FILE', 'data.json')})",
        f"  dashboard  : http://localhost:8000/",
        f"  MCP URL    : http://localhost:8000/mcp   (transport: Streamable HTTP)",
        f"  MCP token  : {MCP_TOKEN}" + ("   <- generated, set MCP_TOKEN to pin it" if MCP_TOKEN_GENERATED else ""),
        f"  MCP header : Authorization: Bearer {MCP_TOKEN}",
        f"  REST auth  : {'X-API-Key required on writes' if API_KEY else 'open'}",
        "",
    ]
    print("\n".join(banner), flush=True)
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(
    title="PharmaGuard API",
    version="1.0.0",
    description=(
        "Disruption response for perishable and sensitive goods (medicines, vaccines, and the same engine "
        "for supermarket stock) in Bengaluru, India. Temperatures °C, money INR. All data simulated. "
        f"An MCP server is mounted at /mcp: {DESCRIPTION}"
    ),
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class NormalizeMcpPath:
    """Make /mcp and /mcp/ behave identically.

    Starlette would answer a bare POST /mcp with a 307 to /mcp/, and not every MCP client
    follows redirects on a POST. This runs before routing and rewrites the path instead.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.inner(scope, receive, send)


app.add_middleware(NormalizeMcpPath)
app.mount("/mcp", mcp_asgi_app)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """/mcp always needs a token. REST writes need X-API-Key only if API_KEY is set."""
    path, method = request.url.path, request.method
    if method != "OPTIONS" and path.rstrip("/") == "/mcp":
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if token not in MCP_ACCEPTED_TOKENS:
            return JSONResponse(
                {"error": "unauthorized", "detail": "Send 'Authorization: Bearer <MCP_TOKEN>' (or X-API-Key)."},
                status_code=401,
            )
    elif API_KEY and method not in ("GET", "OPTIONS") and path not in OPEN_PATHS:
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse({"detail": "Missing or invalid X-API-Key header."}, status_code=401)
    return await call_next(request)


@app.exception_handler(core.CoreError)
async def core_error_handler(_request: Request, exc: core.CoreError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class AlertIn(BaseModel):
    shipment_id: str = Field(..., min_length=1, description="Shipment the sensor is on, e.g. SHP-1001.")
    temp_c: float | None = Field(None, ge=-60, le=80, description="Temperature in °C.")
    humidity_pct: float | None = Field(None, ge=0, le=100, description="Relative humidity in percent.")
    shock_g: float | None = Field(None, ge=0, le=50, description="Peak shock in g (matters for fragile goods).")
    timestamp: str | None = Field(None, description="ISO 8601 reading time. Defaults to now (UTC).")


class RecallIn(BaseModel):
    batch_id: str = Field(..., min_length=1, description="Batch being recalled, e.g. B-HEPB-12.")
    reason: str = Field(..., min_length=1, description="Why it is recalled.")


class DelayIn(BaseModel):
    shipment_id: str = Field(..., min_length=1)
    delay_hours: float = Field(..., gt=0, le=240, description="Extra hours added to the ETA.")


class EventIn(BaseModel):
    actor: str = Field(..., min_length=1, description="Who acted: ai_agent, pharmacy, store, carrier, human.")
    action: str = Field(..., min_length=1, description="Short label, e.g. destination_contacted.")
    note: str = Field("", description="Free-text detail, e.g. what the pharmacist said.")


class EscalateIn(BaseModel):
    summary: str = Field(..., min_length=1, description="AI handoff summary for the human quality manager.")


class ReplacementIn(BaseModel):
    supplier_id: str = Field(..., min_length=1, description="Approved supplier id.")
    units: int = Field(..., gt=0)
    product_id: str | None = Field(None, description="Defaults to the highest-value affected product.")


class ApproveIn(BaseModel):
    approver: str = Field(..., min_length=1, description="Name of the human approving.")
    note: str = Field("", description="Optional note.")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/", include_in_schema=False)
def dashboard():
    """Serve the operations dashboard."""
    return FileResponse(core.BASE_DIR / "dashboard.html")


@app.get("/health", summary="Health check", tags=["system"])
def health():
    """Liveness probe. Shows the active vertical, state counts and the MCP mount point."""
    return {
        "status": "ok",
        "vertical": core.DATA["vertical"],
        "data_file": os.environ.get("DATA_FILE", "data.json"),
        "time": core.now_iso(),
        "shipments": len(core.SHIPMENTS),
        "incidents": len(core.INCIDENTS),
        "readings": sum(len(v) for v in core.READINGS.values()),
        "mcp": {"path": "/mcp", "transport": "streamable-http", "tools": TOOL_NAMES},
        "rest_auth": "api_key" if API_KEY else "open",
    }


@app.post("/alert", summary="Report a sensor reading", tags=["telemetry"])
def alert(body: AlertIn):
    """Record one IoT reading (temperature, humidity and/or shock) and evaluate the rules.

    Inside limits: nothing is opened and `incident` is null. Outside limits: the excursion
    duration is measured from the current run of consecutive out-of-range readings, every batch
    on board is scored, and the shipment's single open incident of that type is created or
    updated -- never duplicated. One reading can raise both TEMP_EXCURSION and HANDLING_BREACH,
    so `incidents` lists all of them and `incident` is the most severe.
    """
    return core.record_reading(
        body.shipment_id, temp_c=body.temp_c, humidity_pct=body.humidity_pct,
        shock_g=body.shock_g, timestamp=body.timestamp,
    )


@app.post("/expiry-scan", summary="Scan batches for expiry risk", tags=["telemetry"])
def expiry_scan(shipment_id: str | None = Query(None, description="Limit the scan to one shipment.")):
    """Check every batch's shelf life against its arrival time (days_left - eta_hours/24).

    Opens or updates an EXPIRY_RISK incident per affected shipment, recommending REDIRECT,
    MARKDOWN or DISPOSE. A DISPOSE recommendation always needs human approval.
    """
    found = core.expiry_scan(shipment_id)
    return {"scanned": len(core.SHIPMENTS) if not shipment_id else 1, "incidents": found}


@app.post("/recall", summary="Recall a batch", tags=["telemetry"])
def recall(body: RecallIn):
    """Open a HIGH severity RECALL incident for the shipment carrying that batch. 404 if the batch is unknown."""
    return core.recall(body.batch_id, body.reason)


@app.post("/delay", summary="Report a transit delay", tags=["telemetry"])
def delay(body: DelayIn):
    """Add delay hours to a shipment's ETA, open or update a DELAY incident, and re-check expiry risk."""
    return core.delay(body.shipment_id, body.delay_hours)


@app.get("/shipments", summary="List shipments", tags=["shipments"])
def shipments():
    """All shipments with destination, ETA and latest temperature. Used by the dashboard selector."""
    return {"vertical": core.DATA["vertical"], "shipments": core.shipments_summary()}


@app.get("/shipments/{shipment_id}", summary="Get shipment detail", tags=["shipments"])
def shipment(shipment_id: str):
    """Full shipment context: origin, vehicle, ETA, allowed temperature window, batches with expiry,
    destination and carrier contacts, latest reading and any open incidents. 404 if unknown."""
    return core.shipment_view(shipment_id)


@app.get("/readings/{shipment_id}", summary="Sensor history", tags=["telemetry"])
def readings(shipment_id: str, limit: int = Query(300, gt=0, le=5000)):
    """Reading history for a shipment, oldest first, for the dashboard chart. 404 if unknown."""
    return core.readings_view(shipment_id, limit)


@app.get("/suppliers/alternatives", summary="Find approved replacement suppliers", tags=["suppliers"])
def supplier_alternatives(
    product_id: str = Query(..., description="Product needing replacement stock, e.g. P-INS."),
    units: int = Query(..., gt=0, description="Units required."),
):
    """Approved suppliers holding at least `units`, fastest ETA first.

    Suppliers flagged approved=false are excluded unconditionally, however fast or cheap they look.
    404 for an unknown product_id.
    """
    options = core.alternatives(product_id, units)
    return {
        "product_id": product_id,
        "product_name": core.get_product(product_id)["name"],
        "units_requested": units,
        "approved_only": True,
        "count": len(options),
        "alternatives": options,
    }


@app.get("/incidents", summary="List incidents", tags=["incidents"])
def incidents():
    """All incidents, newest first, with severity, status, value at risk and timeline."""
    items = core.incidents_list()
    return {"vertical": core.DATA["vertical"], "count": len(items), "incidents": items}


@app.get("/incidents/{incident_id}", summary="Get one incident", tags=["incidents"])
def incident(incident_id: str):
    """One incident in full. 404 if the id is unknown."""
    return core.get_incident(incident_id)


@app.post("/incidents/{incident_id}/event", summary="Log an action on an incident", tags=["incidents"])
def incident_event(incident_id: str, body: EventIn):
    """Append a timestamped entry to the incident timeline so the audit trail is complete. Status is unchanged."""
    return core.log_event(incident_id, body.actor, body.action, body.note)


@app.post("/incidents/{incident_id}/escalate", summary="Escalate to a human", tags=["incidents"])
def incident_escalate(incident_id: str, body: EscalateIn):
    """Set status ESCALATED and store the AI's handoff summary for the human quality manager."""
    return core.escalate(incident_id, body.summary)


@app.post("/incidents/{incident_id}/replacement-request", summary="Request a replacement (never auto-approves)",
          tags=["incidents"])
def incident_replacement(incident_id: str, body: ReplacementIn):
    """Raise a replacement request against an approved supplier and set status AWAITING_APPROVAL.

    404 unknown supplier, 403 unapproved supplier, 409 not enough stock. This never approves:
    only POST /incidents/{id}/approve, called by a human on the dashboard, can resolve it.
    """
    return core.request_replacement(incident_id, body.supplier_id, body.units, body.product_id)


@app.post("/incidents/{incident_id}/approve", summary="Human approval (no MCP tool exists for this)",
          tags=["incidents"])
def incident_approve(incident_id: str, body: ApproveIn):
    """Approve the recommended action (replacement, markdown or disposal) and resolve the incident.

    HUMAN ONLY: the dashboard calls this with a named approver. There is no MCP tool for it, so the
    AI agent cannot approve its own recommendation. 409 if the incident is already RESOLVED.
    """
    return core.approve(incident_id, body.approver, body.note)


@app.post("/reset", summary="Reset demo state", tags=["system"])
def reset():
    """Clear all incidents, readings, delays and recalls so the demo can be re-run. Master data untouched."""
    core.reset()
    return {"status": "reset", "vertical": core.DATA["vertical"], "time": core.now_iso()}


@app.post("/reload", summary="Reload the data file / switch vertical", tags=["system"])
def reload_data(data_file: str | None = Body(None, embed=True, description="e.g. data_grocery.json")):
    """Reload master data and clear state. Pass data_file to switch vertical without restarting the server."""
    if data_file:
        os.environ["DATA_FILE"] = data_file
    info = core.load(data_file)
    return {"status": "reloaded", **info}
