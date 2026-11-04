"""
PharmaGuard core -- all state and all rules. REST (app.py) and MCP (mcp_tools.py)
are thin wrappers over this module, so both see exactly the same incidents.

Verticals: the engine knows nothing about medicine. It reads a data file whose
products declare a `handling` block, and only evaluates the rules that block
defines (no temp_c -> no temperature rule, and so on). DATA_FILE picks the file:
data.json (pharma) or data_grocery.json (supermarket).

Nothing here is auto-approved. request_replacement() and any DISPOSE
recommendation park the incident at AWAITING_APPROVAL; only approve(), which the
dashboard calls with a human's name, can move an incident to RESOLVED.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

TYPES = ("TEMP_EXCURSION", "EXPIRY_RISK", "HANDLING_BREACH", "DELAY", "RECALL")
STATUSES = ("OPEN", "AWAITING_APPROVAL", "ESCALATED", "RESOLVED")
OPEN_STATUSES = ("OPEN", "AWAITING_APPROVAL", "ESCALATED")
RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Shock above this is a handling breach for a product marked fragile.
SHOCK_G_LIMIT = 3.0


class CoreError(Exception):
    """Business error with an HTTP-ish status: 404 unknown id, 409 conflict, 403 not allowed."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

DATA: dict = {}
PRODUCTS: dict = {}
DESTINATIONS: dict = {}
CARRIERS: dict = {}
SHIPMENTS: dict = {}
SUPPLIERS: dict = {}

READINGS: dict = {}      # shipment_id -> [{ts, temp_c?, humidity_pct?, shock_g?, in_range}]
INCIDENTS: dict = {}     # incident_id -> incident dict
DELAYS: dict = {}        # shipment_id -> extra hours
RECALLS: dict = {}       # batch_id -> reason
_seq = {"n": 0}


def _resolve_expiry(value: str) -> str:
    """Allow '+Nd' (N days from today) in data files so demo dates never go stale."""
    match = re.fullmatch(r"\+(\d+)d", str(value).strip())
    if match:
        return (date.today() + timedelta(days=int(match.group(1)))).isoformat()
    return str(value)


def load(path: str | None = None) -> dict:
    """Load a vertical data file into memory and clear all demo state."""
    global DATA, PRODUCTS, DESTINATIONS, CARRIERS, SHIPMENTS, SUPPLIERS
    filename = path or os.environ.get("DATA_FILE", "data.json")
    file_path = Path(filename)
    if not file_path.is_absolute():
        file_path = BASE_DIR / file_path
    if not file_path.exists():
        raise CoreError(404, f"Data file {filename!r} not found.")

    DATA = json.loads(file_path.read_text())
    PRODUCTS = {p["id"]: p for p in DATA["products"]}
    DESTINATIONS = {d["id"]: d for d in DATA["destinations"]}
    CARRIERS = {c["id"]: c for c in DATA["carriers"]}
    SUPPLIERS = {s["id"]: s for s in DATA["suppliers"]}
    SHIPMENTS = {}
    for shipment in DATA["shipments"]:
        shipment = json.loads(json.dumps(shipment))  # keep the file's own dicts pristine
        for batch in shipment["batches"]:
            batch["expiry_date"] = _resolve_expiry(batch["expiry_date"])
        SHIPMENTS[shipment["id"]] = shipment
    reset()
    return {"vertical": DATA["vertical"], "data_file": file_path.name, "shipments": len(SHIPMENTS)}


def reset() -> dict:
    """Clear incidents, readings, delays and recalls. Master data is untouched."""
    READINGS.clear()
    INCIDENTS.clear()
    DELAYS.clear()
    RECALLS.clear()
    _seq["n"] = 0
    return {"incidents": 0, "readings": 0}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise CoreError(422, f"timestamp is not ISO 8601: {value!r}")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_shipment(shipment_id: str) -> dict:
    shipment = SHIPMENTS.get(shipment_id)
    if not shipment:
        raise CoreError(404, f"Unknown shipment_id {shipment_id!r}.")
    return shipment


def get_product(product_id: str) -> dict:
    product = PRODUCTS.get(product_id)
    if not product:
        raise CoreError(404, f"Unknown product_id {product_id!r}.")
    return product


def get_incident(incident_id: str) -> dict:
    incident = INCIDENTS.get(incident_id)
    if not incident:
        raise CoreError(404, f"Unknown incident id {incident_id!r}.")
    return incident


def handling(product_id: str) -> dict:
    return get_product(product_id).get("handling") or {}


def eta_hours(shipment: dict) -> float:
    """Planned ETA plus any reported delay."""
    return round(shipment["eta_hours"] + DELAYS.get(shipment["id"], 0.0), 2)


def batch_value(batch: dict) -> int:
    return batch["units"] * get_product(batch["product_id"])["unit_value_inr"]


def days_left(batch: dict) -> float:
    return (date.fromisoformat(batch["expiry_date"]) - date.today()).days


def temp_bounds(shipment: dict) -> tuple | None:
    """Strictest window across the products on board, for display (the dashboard band).

    None when no product declares a temperature rule, or when the products on board
    want incompatible windows (frozen next to chilled) -- there is no single safe band
    to draw then, so compliance is judged per batch by temp_violations().
    """
    windows = [handling(b["product_id"]).get("temp_c") for b in shipment["batches"]]
    windows = [w for w in windows if w]
    if not windows:
        return None
    low, high = max(w["min"] for w in windows), min(w["max"] for w in windows)
    return (low, high) if low <= high else None


def temp_violations(shipment: dict, temp_c: float) -> list:
    """Per batch: (batch, window, degrees outside its own window) for every batch out of range."""
    out = []
    for batch in shipment["batches"]:
        window = handling(batch["product_id"]).get("temp_c")
        if not window:
            continue  # product declares no temperature rule -- skip it
        deviation = round(max(temp_c - window["max"], window["min"] - temp_c, 0.0), 2)
        if deviation > 0:
            out.append((batch, window, deviation))
    return out


def affected_batch(batch: dict, **extra) -> dict:
    product = get_product(batch["product_id"])
    row = {
        "batch_id": batch["batch_id"],
        "product_id": product["id"],
        "product_name": product["name"],
        "units": batch["units"],
        "expiry_date": batch["expiry_date"],
        "days_left": days_left(batch),
        "value_inr": batch_value(batch),
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# Recommended actions. DISPOSE / replacement / return always need a human.
# --------------------------------------------------------------------------- #

NEEDS_HUMAN = ("DISPOSE", "QUARANTINE_AND_REPLACE", "QUARANTINE_AND_RETURN", "MARKDOWN")

ACTION_TEXT = {
    "MONITOR": "Continue delivery. Ask the carrier to restore the required range and keep logging.",
    "HOLD_FOR_QA": "Deliver but hold the affected batches for QA review before use. Inform the destination.",
    "QUARANTINE_AND_REPLACE": "Quarantine the affected batches and source replacement stock from an approved supplier.",
    "QUARANTINE_AND_RETURN": "Quarantine the recalled batches, do not dispense, and return them to the supplier.",
    "REDIRECT": "Redirect the affected batches to a nearer, faster-turnover destination before expiry.",
    "MARKDOWN": "Move the affected batches to same-day markdown at the destination.",
    "DISPOSE": "Dispose of the affected batches per SOP. Needs human approval before anything is destroyed.",
    "NOTIFY_DESTINATION": "Tell the destination the revised ETA and confirm someone can receive the cold chain.",
}


def action(code: str) -> dict:
    return {"code": code, "text": ACTION_TEXT[code], "needs_human_approval": code in NEEDS_HUMAN}


# --------------------------------------------------------------------------- #
# Deterministic severity rules -- one comment per rule
# --------------------------------------------------------------------------- #

def temp_severity(window: dict, deviation: float, minutes_out: float) -> str:
    """Temperature: how far outside the window, and how much of the product's
    tolerance_min has been burnt. First match wins.
      HIGH    minutes_out >= tolerance_min (tolerance used up) or deviation >= 5 °C (thermal shock)
      MEDIUM  minutes_out >= half the tolerance, or deviation >= 2 °C
      LOW     anything else
    """
    tolerance = max(window.get("tolerance_min") or 1, 1)
    ratio = minutes_out / tolerance
    if ratio >= 1.0 or deviation >= 5.0:
        return "HIGH"
    if ratio >= 0.5 or deviation >= 2.0:
        return "MEDIUM"
    return "LOW"


def expiry_severity(effective_days: float, shelf_life_days: float) -> str:
    """Expiry: days left once the shipment actually arrives (days_left - eta_hours/24).
      HIGH    <= 0 days -- it expires in transit, so it can never be dispensed or sold
      MEDIUM  <= 2 days left, or <= 10% of the product's shelf life
      LOW     inside the at-risk threshold but still usable on arrival
    """
    if effective_days <= 0:
        return "HIGH"
    if effective_days <= 2 or effective_days <= 0.10 * shelf_life_days:
        return "MEDIUM"
    return "LOW"


def expiry_at_risk(effective_days: float, shelf_life_days: float) -> bool:
    """At risk when under 5 days remain on arrival, or under 25% of the product's shelf life."""
    return effective_days <= 5 or effective_days <= 0.25 * shelf_life_days


def humidity_severity(over: float) -> str:
    """Humidity: percentage points above the product's maximum.
      HIGH >= 10 pts, MEDIUM >= 5 pts, LOW otherwise.
    """
    return "HIGH" if over >= 10 else "MEDIUM" if over >= 5 else "LOW"


def shock_severity(shock_g: float) -> str:
    """Shock on a fragile product, in g.
      HIGH >= 6 g (likely breakage), MEDIUM >= 3 g (inspect), LOW otherwise.
    """
    return "HIGH" if shock_g >= 6 else "MEDIUM" if shock_g >= SHOCK_G_LIMIT else "LOW"


def delay_severity(total_eta: float, delay_h: float, expiry_breached: bool) -> str:
    """Delay: a delay that pushes a batch past expiry is always HIGH.
      HIGH    expiry breached on arrival, or delay >= 12 h
      MEDIUM  delay >= 4 h
      LOW     anything else
    """
    if expiry_breached or delay_h >= 12:
        return "HIGH"
    return "MEDIUM" if delay_h >= 4 else "LOW"


# --------------------------------------------------------------------------- #
# Incident store -- one open incident per (shipment, type)
# --------------------------------------------------------------------------- #

def open_incident(shipment_id: str, kind: str) -> dict | None:
    return next(
        (i for i in INCIDENTS.values()
         if i["shipment_id"] == shipment_id and i["type"] == kind and i["status"] in OPEN_STATUSES),
        None,
    )


def event(actor: str, act: str, note: str = "") -> dict:
    return {"ts": now_iso(), "actor": actor, "action": act, "note": note}


def upsert_incident(shipment_id, kind, severity, batches, action_code, note, detail=None) -> dict:
    """Create the one open incident for (shipment, type), or update it in place.

    Severity is monotonic: a cooler reading or a shorter queue never undoes damage
    already assessed, so it can be raised but not lowered.
    """
    if kind not in TYPES:
        raise CoreError(422, f"Unknown incident type {kind!r}. Expected one of {', '.join(TYPES)}.")
    shipment = get_shipment(shipment_id)
    incident = open_incident(shipment_id, kind)
    value = sum(b["value_inr"] for b in batches)

    if incident is None:
        _seq["n"] += 1
        incident = {
            "id": f"INC-{_seq['n']:04d}",
            "shipment_id": shipment_id,
            "type": kind,
            "status": "OPEN",
            "severity": severity,
            "opened_at": now_iso(),
            "updated_at": now_iso(),
            "vertical": DATA["vertical"],
            "affected_batches": batches,
            "value_at_risk_inr": value,
            "destination": DESTINATIONS[shipment["destination_id"]],
            "carrier": CARRIERS[shipment["carrier_id"]],
            "vehicle": shipment["vehicle"],
            "eta_hours": eta_hours(shipment),
            "detail": detail or {},
            "recommended_action": action(action_code),
            "escalation_summary": None,
            "replacement_request": None,
            "approval": None,
            "timeline": [event("system", "incident_opened", note)],
        }
        INCIDENTS[incident["id"]] = incident
        return incident

    if RANK[severity] > RANK[incident["severity"]]:
        incident["timeline"].append(
            event("system", "severity_raised", f"{incident['severity']} -> {severity}. {note}".strip())
        )
        incident["severity"] = severity
        incident["recommended_action"] = action(action_code)
    incident["affected_batches"] = batches
    incident["value_at_risk_inr"] = value
    incident["eta_hours"] = eta_hours(shipment)
    incident["detail"] = detail or incident["detail"]
    incident["updated_at"] = now_iso()
    return incident


# --------------------------------------------------------------------------- #
# Rule entry points
# --------------------------------------------------------------------------- #

def minutes_out_of_range(shipment_id: str) -> float:
    """Minutes spanned by the current run of consecutive out-of-range readings."""
    series = [r for r in READINGS.get(shipment_id, []) if r.get("temp_c") is not None]
    if not series or series[-1]["in_range"]:
        return 0.0
    i = len(series) - 1
    while i > 0 and not series[i - 1]["in_range"]:
        i -= 1
    return max((parse_ts(series[-1]["ts"]) - parse_ts(series[i]["ts"])).total_seconds() / 60.0, 0.0)


def record_reading(shipment_id, temp_c=None, humidity_pct=None, shock_g=None, timestamp=None) -> dict:
    """Store one sensor reading and evaluate every rule the products on board declare.

    Returns {reading, incidents: [...]} -- a single reading can raise both a
    temperature excursion and a handling breach.
    """
    shipment = get_shipment(shipment_id)
    if temp_c is None and humidity_pct is None and shock_g is None:
        raise CoreError(422, "Send at least one of temp_c, humidity_pct, shock_g.")
    ts = parse_ts(timestamp).isoformat(timespec="seconds") if timestamp else now_iso()
    bounds = temp_bounds(shipment)
    in_range = temp_c is None or not temp_violations(shipment, temp_c)

    reading = {"ts": ts, "in_range": in_range}
    for key, val in (("temp_c", temp_c), ("humidity_pct", humidity_pct), ("shock_g", shock_g)):
        if val is not None:
            reading[key] = val
    READINGS.setdefault(shipment_id, []).append(reading)

    incidents = []
    if temp_c is not None and not in_range:
        incidents.append(_temp_incident(shipment, temp_c, ts))
    breach = _handling_incident(shipment, humidity_pct, shock_g)
    if breach:
        incidents.append(breach)

    return {
        "shipment_id": shipment_id,
        "reading": reading,
        "allowed_temp_c": f"{bounds[0]} to {bounds[1]}" if bounds else None,
        "minutes_out_of_range": round(minutes_out_of_range(shipment_id), 1),
        "incidents": incidents,
        "incident": max(incidents, key=lambda i: RANK[i["severity"]]) if incidents else None,
    }


def _temp_incident(shipment: dict, temp_c: float, ts: str) -> dict:
    minutes = minutes_out_of_range(shipment["id"])
    batches, worst = [], "LOW"
    for batch, window, deviation in temp_violations(shipment, temp_c):
        severity = temp_severity(window, deviation, minutes)
        worst = severity if RANK[severity] > RANK[worst] else worst
        batches.append(affected_batch(
            batch, deviation_c=deviation, severity=severity,
            required_temp_c=f"{window['min']} to {window['max']}",
            tolerance_min=window["tolerance_min"],
        ))
    code = {"HIGH": "QUARANTINE_AND_REPLACE", "MEDIUM": "HOLD_FOR_QA", "LOW": "MONITOR"}[worst]
    return upsert_incident(
        shipment["id"], "TEMP_EXCURSION", worst, batches, code,
        f"{temp_c} °C at {ts}, {round(minutes, 1)} min outside range.",
        detail={"temp_c": temp_c, "minutes_out_of_range": round(minutes, 1)},
    )


def _handling_incident(shipment: dict, humidity_pct, shock_g) -> dict | None:
    """Humidity above a declared maximum, or shock on a product marked fragile."""
    batches, worst, notes = [], "LOW", []
    for batch in shipment["batches"]:
        rules = handling(batch["product_id"])
        hits = {}
        if humidity_pct is not None and (rules.get("humidity_pct") or {}).get("max") is not None:
            over = round(humidity_pct - rules["humidity_pct"]["max"], 2)
            if over > 0:
                hits["humidity_over_pct"] = over
                hits["severity"] = humidity_severity(over)
        if shock_g is not None and rules.get("fragile") and shock_g >= SHOCK_G_LIMIT:
            hits["shock_g"] = shock_g
            hits["severity"] = max(hits.get("severity", "LOW"), shock_severity(shock_g), key=lambda s: RANK[s])
        if hits:
            worst = hits["severity"] if RANK[hits["severity"]] > RANK[worst] else worst
            batches.append(affected_batch(batch, **hits))
            notes.append(f"{batch['batch_id']}: " + ", ".join(f"{k}={v}" for k, v in hits.items() if k != "severity"))
    if not batches:
        return None
    code = {"HIGH": "QUARANTINE_AND_REPLACE", "MEDIUM": "HOLD_FOR_QA", "LOW": "MONITOR"}[worst]
    return upsert_incident(shipment["id"], "HANDLING_BREACH", worst, batches, code, "; ".join(notes),
                           detail={"humidity_pct": humidity_pct, "shock_g": shock_g})


def expiry_scan(shipment_id: str | None = None) -> list:
    """Check every batch (or one shipment's batches) for expiry risk on arrival."""
    targets = [get_shipment(shipment_id)] if shipment_id else list(SHIPMENTS.values())
    out = []
    for shipment in targets:
        transit_days = eta_hours(shipment) / 24.0
        batches, worst, codes = [], "LOW", []
        for batch in shipment["batches"]:
            shelf = handling(batch["product_id"]).get("shelf_life_days")
            if not shelf:
                continue  # no shelf life declared -- no expiry rule
            effective = round(days_left(batch) - transit_days, 2)
            if not expiry_at_risk(effective, shelf):
                continue
            severity = expiry_severity(effective, shelf)
            worst = severity if RANK[severity] > RANK[worst] else worst
            # DISPOSE once it expires in transit; otherwise sell it down (grocery) or move it (pharma).
            code = "DISPOSE" if effective <= 0 else ("MARKDOWN" if DATA["vertical"] == "grocery" and effective <= 2 else "REDIRECT")
            codes.append(code)
            batches.append(affected_batch(batch, effective_days_left=effective, severity=severity, action=code))
        if not batches:
            continue
        code = "DISPOSE" if "DISPOSE" in codes else ("MARKDOWN" if "MARKDOWN" in codes else "REDIRECT")
        out.append(upsert_incident(
            shipment["id"], "EXPIRY_RISK", worst, batches, code,
            f"{len(batches)} batch(es) at expiry risk on arrival (ETA {eta_hours(shipment)} h).",
            detail={"eta_hours": eta_hours(shipment)},
        ))
    return out


def recall(batch_id: str, reason: str) -> dict:
    """Regulatory recall of a batch. Always HIGH -- a recalled batch must not be dispensed or sold."""
    for shipment in SHIPMENTS.values():
        for batch in shipment["batches"]:
            if batch["batch_id"] == batch_id:
                RECALLS[batch_id] = reason
                return upsert_incident(
                    shipment["id"], "RECALL", "HIGH",
                    [affected_batch(batch, reason=reason)], "QUARANTINE_AND_RETURN",
                    f"Recall on {batch_id}: {reason}",
                    detail={"batch_id": batch_id, "reason": reason},
                )
    raise CoreError(404, f"Unknown batch_id {batch_id!r}.")


def delay(shipment_id: str, delay_hours: float) -> dict:
    """Report a transit delay; re-checks expiry against the new arrival time."""
    shipment = get_shipment(shipment_id)
    DELAYS[shipment_id] = DELAYS.get(shipment_id, 0.0) + delay_hours
    total = eta_hours(shipment)
    transit_days = total / 24.0
    batches, breached = [], False
    for batch in shipment["batches"]:
        effective = round(days_left(batch) - transit_days, 2)
        if effective <= 0:
            breached = True
        batches.append(affected_batch(batch, effective_days_left=effective))
    severity = delay_severity(total, DELAYS[shipment_id], breached)
    code = "REDIRECT" if severity == "HIGH" else ("NOTIFY_DESTINATION" if severity == "MEDIUM" else "MONITOR")
    incident = upsert_incident(
        shipment_id, "DELAY", severity, batches, code,
        f"Delayed {DELAYS[shipment_id]} h; revised ETA {total} h.",
        detail={"delay_hours": DELAYS[shipment_id], "eta_hours": total},
    )
    expiry_scan(shipment_id)  # a delay can push a batch past expiry
    return incident


def create_incident(shipment_id: str, kind: str = "TEMP_EXCURSION", reason: str | None = None) -> dict:
    """Idempotent: return the existing open incident of that type, else evaluate current state and open one."""
    shipment = get_shipment(shipment_id)
    existing = open_incident(shipment_id, kind)
    if existing:
        if reason:
            existing["timeline"].append(event("ai_agent", "incident_confirmed", reason))
        return existing

    if kind == "EXPIRY_RISK":
        found = expiry_scan(shipment_id)
        if found:
            return found[0]
    if kind == "TEMP_EXCURSION":
        last = next((r for r in reversed(READINGS.get(shipment_id, [])) if r.get("temp_c") is not None), None)
        if last and not last["in_range"]:
            return _temp_incident(shipment, last["temp_c"], last["ts"])

    # Nothing measurable to score: open a LOW incident carrying the agent's reason.
    batches = [affected_batch(b) for b in shipment["batches"]]
    return upsert_incident(shipment_id, kind, "LOW", batches, "MONITOR",
                           reason or f"{kind} opened by agent without sensor evidence.")


# --------------------------------------------------------------------------- #
# Incident workflow
# --------------------------------------------------------------------------- #

def log_event(incident_id: str, actor: str, act: str, note: str = "") -> dict:
    incident = get_incident(incident_id)
    entry = event(actor, act, note)
    incident["timeline"].append(entry)
    incident["updated_at"] = entry["ts"]
    return incident


def escalate(incident_id: str, summary: str) -> dict:
    """Hand the incident to the human quality manager and store the AI's summary."""
    incident = get_incident(incident_id)
    incident["status"] = "ESCALATED"
    incident["escalation_summary"] = summary
    incident["timeline"].append(event("ai_agent", "escalated_to_human", summary))
    incident["updated_at"] = now_iso()
    return incident


def alternatives(product_id: str, units: int, limit: int | None = None) -> list:
    """Approved suppliers holding at least `units`, fastest ETA first. Unapproved suppliers are never returned."""
    get_product(product_id)
    if units <= 0:
        raise CoreError(422, "units must be greater than 0.")
    options = [
        {
            "supplier_id": s["id"], "name": s["name"], "city": s["city"],
            "contact_person": s["contact_person"], "phone": s["phone"], "approved": True,
            "stock": s["stock"].get(product_id, 0), "eta_hours": s["eta_hours"].get(product_id),
            "estimated_cost_inr": units * get_product(product_id)["unit_value_inr"],
        }
        for s in SUPPLIERS.values()
        if s["approved"] and s["stock"].get(product_id, 0) >= units
    ]
    options.sort(key=lambda o: (o["eta_hours"] is None, o["eta_hours"], o["name"]))
    return options[:limit] if limit else options


def request_replacement(incident_id: str, supplier_id: str, units: int, product_id: str | None = None) -> dict:
    """Raise a replacement request against an approved supplier and park it for human sign-off.

    NEVER approves. Sets AWAITING_APPROVAL; only approve() can resolve the incident.
    """
    incident = get_incident(incident_id)
    supplier = SUPPLIERS.get(supplier_id)
    if not supplier:
        raise CoreError(404, f"Unknown supplier_id {supplier_id!r}.")
    if not supplier["approved"]:
        raise CoreError(403, f"Supplier {supplier_id} is not an approved vendor and cannot be used.")
    if units <= 0:
        raise CoreError(422, "units must be greater than 0.")

    if not product_id:
        batches = incident["affected_batches"] or []
        if not batches:
            raise CoreError(422, "Incident has no affected batches; pass product_id explicitly.")
        product_id = max(batches, key=lambda b: b["value_inr"])["product_id"]
    product = get_product(product_id)
    stock = supplier["stock"].get(product_id, 0)
    if stock < units:
        raise CoreError(409, f"Supplier {supplier_id} has {stock} units of {product_id}, {units} requested.")

    incident["replacement_request"] = {
        "supplier_id": supplier_id, "supplier_name": supplier["name"], "supplier_phone": supplier["phone"],
        "product_id": product_id, "product_name": product["name"], "units": units,
        "eta_hours": supplier["eta_hours"].get(product_id),
        "estimated_cost_inr": units * product["unit_value_inr"],
        "requested_at": now_iso(), "approved": False,
    }
    incident["status"] = "AWAITING_APPROVAL"
    incident["timeline"].append(event(
        "ai_agent", "replacement_requested",
        f"{units} x {product_id} from {supplier['name']} (ETA {supplier['eta_hours'].get(product_id)} h). Awaiting human approval.",
    ))
    incident["updated_at"] = now_iso()
    return incident


def approve(incident_id: str, approver: str, note: str = "") -> dict:
    """HUMAN ONLY -- resolves the incident and releases the replacement or disposal.

    There is deliberately no MCP tool for this: the AI agent cannot reach it.
    """
    incident = get_incident(incident_id)
    if incident["status"] == "RESOLVED":
        raise CoreError(409, f"Incident {incident_id} is already RESOLVED.")
    incident["status"] = "RESOLVED"
    incident["approval"] = {"approver": approver, "approved_at": now_iso(), "note": note}
    if incident.get("replacement_request"):
        incident["replacement_request"]["approved"] = True
    incident["timeline"].append(event("human", "approved", f"{incident['recommended_action']['code']} approved by {approver}. {note}".strip()))
    incident["updated_at"] = now_iso()
    return incident


def incidents_list() -> list:
    return sorted(INCIDENTS.values(), key=lambda i: i["id"], reverse=True)


def shipment_view(shipment_id: str) -> dict:
    """Full shipment detail for REST and the dashboard."""
    shipment = get_shipment(shipment_id)
    bounds = temp_bounds(shipment)
    series = READINGS.get(shipment_id, [])
    return {
        "shipment_id": shipment_id,
        "vertical": DATA["vertical"],
        "origin": shipment["origin"],
        "vehicle": shipment["vehicle"],
        "eta_hours": eta_hours(shipment),
        "delay_hours": DELAYS.get(shipment_id, 0.0),
        "allowed_temp_c": {"min": bounds[0], "max": bounds[1]} if bounds else None,
        "batches": [
            affected_batch(b, recalled=b["batch_id"] in RECALLS)
            for b in shipment["batches"]
        ],
        "destination": DESTINATIONS[shipment["destination_id"]],
        "carrier": CARRIERS[shipment["carrier_id"]],
        "latest_reading": series[-1] if series else None,
        "open_incidents": [
            {"id": i["id"], "type": i["type"], "severity": i["severity"], "status": i["status"]}
            for i in INCIDENTS.values()
            if i["shipment_id"] == shipment_id and i["status"] in OPEN_STATUSES
        ],
    }


def readings_view(shipment_id: str, limit: int = 300) -> dict:
    shipment = get_shipment(shipment_id)
    bounds = temp_bounds(shipment)
    series = READINGS.get(shipment_id, [])[-limit:]
    return {
        "shipment_id": shipment_id,
        "min_c": bounds[0] if bounds else None,
        "max_c": bounds[1] if bounds else None,
        "count": len(series),
        "readings": series,
    }


def shipments_summary() -> list:
    out = []
    for shipment in SHIPMENTS.values():
        series = [r for r in READINGS.get(shipment["id"], []) if r.get("temp_c") is not None]
        out.append({
            "shipment_id": shipment["id"],
            "destination": DESTINATIONS[shipment["destination_id"]]["name"],
            "vehicle": shipment["vehicle"],
            "eta_hours": eta_hours(shipment),
            "latest_temp_c": series[-1]["temp_c"] if series else None,
            "readings": len(series),
        })
    return out


# --------------------------------------------------------------------------- #
# Compact payloads for MCP -- these land in the Talkdesk agent's context window,
# so: short keys, no nulls, no echoed inputs, no nesting we do not need.
# --------------------------------------------------------------------------- #

def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def compact_incident(incident: dict) -> dict:
    out = {
        "id": incident["id"],
        "type": incident["type"],
        "status": incident["status"],
        "severity": incident["severity"],
        "value_at_risk_inr": incident["value_at_risk_inr"],
        "batches": [b["batch_id"] for b in incident["affected_batches"]],
        "action": incident["recommended_action"]["code"],
        "action_note": incident["recommended_action"]["text"],
        "needs_human_approval": incident["recommended_action"]["needs_human_approval"],
        "timeline": [[e["ts"][11:16], e["actor"], e["action"]] for e in incident["timeline"][-3:]],
    }
    return _drop_empty(out)


def compact_shipment(shipment_id: str) -> dict:
    view = shipment_view(shipment_id)
    dest, carrier = view["destination"], view["carrier"]
    last = view["latest_reading"] or {}
    return _drop_empty({
        "id": view["shipment_id"],
        "status": "DELAYED" if view["delay_hours"] else "IN_TRANSIT",
        "eta_h": view["eta_hours"],
        "last_temp_c": last.get("temp_c"),
        "destination": {"name": dest["name"], "contact": dest["contact_person"], "phone": dest["phone"]},
        "carrier": {"name": carrier["name"], "dispatcher": carrier["dispatcher_name"], "phone": carrier["phone"]},
        "batches": [
            {"batch_id": b["batch_id"], "product_id": b["product_id"], "name": b["product_name"], "units": b["units"]}
            for b in view["batches"]
        ],
        "open_incidents": [i["id"] for i in view["open_incidents"]],
    })


def compact_temperature_history(shipment_id: str, limit: int = 10) -> dict:
    get_shipment(shipment_id)
    limit = max(1, min(int(limit), 30))  # cap: this output enters the agent's context
    view = readings_view(shipment_id, limit=limit)
    series = [r for r in view["readings"] if r.get("temp_c") is not None]
    if not series:
        return {"id": shipment_id, "readings": [], "summary": {"in_range": True, "mins_out_of_range": 0}}
    temps = [r["temp_c"] for r in series]
    return {
        "id": shipment_id,
        "range_c": [view["min_c"], view["max_c"]],
        "readings": [[r["ts"][11:16].replace(":", ""), r["temp_c"]] for r in series],
        "summary": {
            "min": min(temps), "max": max(temps),
            "mins_out_of_range": round(minutes_out_of_range(shipment_id), 1),
            "in_range": series[-1]["in_range"],
        },
    }


def compact_product(product_id: str) -> dict:
    product = get_product(product_id)
    rules = product.get("handling") or {}
    window = rules.get("temp_c") or {}
    return _drop_empty({
        "id": product["id"],
        "name": product["name"],
        "temp_c": [window.get("min"), window.get("max")] if window else None,
        "tolerance_min": window.get("tolerance_min"),
        "unit_value_inr": product["unit_value_inr"],
        "shelf_life_days": rules.get("shelf_life_days"),
    })


def compact_alternatives(product_id: str, units: int) -> list:
    return [
        {"id": o["supplier_id"], "name": o["name"], "stock": o["stock"], "eta_h": o["eta_hours"]}
        for o in alternatives(product_id, units, limit=3)
    ]


load()
