# PharmaGuard

AI-driven disruption response for perishable and sensitive goods. Medicines and vaccines
first; the same engine runs a supermarket vertical by swapping one data file.
A Talkdesk AI agent drives it through an **MCP server**; humans approve through a dashboard.
Bengaluru, INR, °C. **All data is simulated.**

```
core.py          state + all rules (one open incident per shipment+type, deterministic severity)
app.py           FastAPI REST  (dashboard, simulator)  +  mounts the MCP server at /mcp
mcp_tools.py     8 MCP tools for the Talkdesk agent (no approve tool, on purpose)
data.json        pharma vertical      data_grocery.json  supermarket vertical
simulator.py     fake IoT sensor + disruption scenarios
dashboard.html   single-file ops dashboard (no CDN, no build step)
```

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
```

Startup prints the dashboard URL, the MCP URL and the **MCP token**. Dashboard: http://localhost:8000
· API docs: http://localhost:8000/docs

Env vars: `DATA_FILE` (`data.json` default, or `data_grocery.json`) · `API_KEY` (if set, REST writes
need `X-API-Key`) · `MCP_TOKEN` (bearer token for `/mcp`; generated and printed if unset).
Switch vertical without a restart: `curl -X POST localhost:8000/reload -H 'content-type: application/json' -d '{"data_file":"data_grocery.json"}'`

## Simulator

```bash
python simulator.py --shipment SHP-1001 --interval 2 --spike-after 10 --spike-to 11.5   # excursion
python simulator.py --shipment SHP-1001 --spike-after 6 --spike-to 13.5                 # HIGH severity
python simulator.py --shipment SHP-1001 --once --temp 12.4                              # one reading
python simulator.py --scenario expiry                                                   # expiry risk
python simulator.py --scenario recall --shipment SHP-1001                               # batch recall
python simulator.py --scenario delay --shipment SHP-1002 --delay-hours 14               # transit delay
python simulator.py --data data_grocery.json --scenario expiry                          # grocery vertical
```

`--spike-to` defaults to the shipment's own max + 3.5 °C, so frozen goods (−22 to −18 °C) work too.

## Tunnel

```bash
cloudflared tunnel --url http://localhost:8000      # or: ngrok http 8000
```

Keep `--proxy-headers --forwarded-allow-ips='*'` on uvicorn. DNS-rebinding protection on `/mcp` is
off, so any tunnel Host header is accepted.

## Connect to Talkdesk (custom MCP server)

| Field | Value |
|---|---|
| Name | `PharmaGuard` |
| Description | Cold-chain and perishable goods incident tools: shipments, temperature history, products, suppliers, incidents. |
| URL | `https://<tunnel-host>/mcp` |
| Transport | Streamable HTTP (stateless, JSON responses) |
| Auth header | `Authorization: Bearer <MCP_TOKEN>` (or `X-API-Key:` with the MCP token or `API_KEY`) |
| Token | printed in the server banner at startup; pin it with `MCP_TOKEN=...` |

Tools exposed: `get_shipment_details`, `check_temperature_history`, `get_product_details`,
`create_incident`, `find_alternative_suppliers`, `log_incident_event`, `request_replacement`,
`escalate_incident`. There is **no approve tool** — only a human on the dashboard can approve a
replacement or a disposal.

Smoke test the tunnel:

```bash
curl -s https://<tunnel-host>/mcp -H "Authorization: Bearer $MCP_TOKEN" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | head -c 400
```

## Notes

* Incident types: `TEMP_EXCURSION`, `EXPIRY_RISK`, `HANDLING_BREACH`, `DELAY`, `RECALL`.
  Statuses: `OPEN` → `AWAITING_APPROVAL` / `ESCALATED` → `RESOLVED`.
* Severity is deterministic — see the rule comments in `core.py` (`temp_severity`,
  `expiry_severity`, `humidity_severity`, `shock_severity`, `delay_severity`).
* `expiry_date` in the data files accepts `+Nd` (N days from today), so demo dates never go stale.
* State is in memory. `POST /reset` clears incidents and readings; restarting the server does too.
* Replace the placeholder phone numbers (`+9100000000xx`) in the data files with a real mobile
  before demoing outbound SMS or voice from Talkdesk.
