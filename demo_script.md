# PharmaGuard — 4-minute demo run sheet

**Roles.** *Driver* at the laptop (terminal + dashboard). *Narrator* talks. *Pharmacist* answers
the phone/SMS from Talkdesk on a real mobile.

**Before you start** (5 min ahead)

```bash
.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
cloudflared tunnel --url http://localhost:8000     # paste https://<host>/mcp into Talkdesk
```

Open the dashboard full-screen, click **Reset demo**, confirm the Talkdesk agent is connected,
and put a real mobile number in `data.json` instead of the placeholder.

---

## 0:00 — Setup (30 s)

> "Cold-chain stock worth lakhs moves across Bengaluru every day. If a van's chiller fails, the
> damage is silent — nobody finds out until a vaccine has already been given. PharmaGuard watches
> the sensors and runs the response, with a human in the loop for every decision that costs money."

Point at the dashboard: two shipments, safe band 2–8 °C, "Simulated data" tag.

## 0:30 — Normal operation (20 s)

```bash
python simulator.py --shipment SHP-1001 --interval 2 --spike-after 6 --spike-to 13.5
```

> "Readings jitter between 4 and 6 degrees. No incident — the system stays quiet."

## 0:50 — The excursion (40 s)

Chart turns red, incident card appears: **TEMP_EXCURSION · HIGH · ₹1,27,200 at risk**.

> "The chiller fails. Thirteen and a half degrees against a two-to-eight limit — more than five
> degrees out, so it's HIGH immediately. One incident, not one per reading. Insulin tolerates
> sixty minutes out of range; this batch has burnt through it."

## 1:30 — The agent takes over (90 s)

Trigger the Talkdesk agent on SHP-1001. Narrate the tool calls as the timeline fills:

> "The agent confirms the incident, reads the shipment and the temperature history, pulls the
> pharmacy and carrier contacts, and asks for approved alternative suppliers — three of them, by
> ETA. The unapproved trader with 9,999 units and a one-hour ETA never appears: it's filtered out
> in the data, not in the prompt."

*Pharmacist* answers the call/SMS and confirms quarantine. Timeline shows
`destination_contacted` → `pharmacy_confirmed` → `replacement_requested`.

> "It requests 120 units from an approved supplier — and stops. Status: **AWAITING APPROVAL**.
> The agent has no tool that can approve this. It is not a prompt rule; the capability does not exist."

## 3:00 — The human decides (30 s)

Click **Approve quarantine and replace** on the dashboard, type the quality manager's name.
Status → **RESOLVED**, approver and timestamp in the timeline.

> "A named human approved the spend. Full audit trail: every reading, every call, every decision,
> who made it and when."

## 3:30 — The grocery beat (30 s)

```bash
python simulator.py --data data_grocery.json --scenario expiry
```

> "Same engine, different data file. Supermarket stock: the milk and the leafy greens run out of
> shelf life in transit, so the recommendation is DISPOSE — and disposal, like replacement, waits for a human. Milk, ice cream at
> minus eighteen, curd — the rules come from the product, not the code."

Dashboard header flips to **grocery**; incident shows **EXPIRY_RISK · HIGH · DISPOSE · needs human approval**.

## 4:00 — Close

> "Detection, assessment, outreach and sourcing are automated. Approval is not. That's the design."

**Backup if wifi dies:** everything but Talkdesk runs locally. Drive the same flow with curl:

```bash
curl -X POST localhost:8000/alert -H 'content-type: application/json' -d '{"shipment_id":"SHP-1001","temp_c":13.5}'
curl "localhost:8000/suppliers/alternatives?product_id=P-INS&units=120"
curl -X POST localhost:8000/incidents/INC-0001/replacement-request -H 'content-type: application/json' -d '{"supplier_id":"SUP-1","units":120}'
```

---

## Judge questions

1. **What's real and what's simulated?** Real: the backend, the rule engine, the MCP server, the
   Talkdesk agent and its live SMS/voice call, the dashboard, the audit trail. Simulated: the IoT
   sensor feed, the master data (products, pharmacies, suppliers) and the phone numbers. No real
   patient or commercial data is involved.
2. **What if the agent is wrong?** It cannot do damage by being wrong. It can read data, log, raise
   a request and escalate — that's the whole tool surface. Approving a spend or a disposal needs a
   named human on the dashboard, and every claim it makes is backed by a stored reading in the
   timeline, so a wrong call is visible and reversible.
3. **Why not let it approve when severity is obvious?** Money and destruction of medicine are
   accountability decisions, not accuracy decisions. We removed the capability rather than
   prompting against it, because a prompt can be talked around and a missing tool cannot.
4. **Is the severity an LLM judgement?** No. It's deterministic arithmetic in `core.py`: degrees
   outside the range, and minutes out of range against the product's tolerance. Same inputs, same
   severity, every time — which is what an auditor needs. The LLM handles conversation, not scoring.
5. **How do you avoid alert spam?** One open incident per shipment per incident type. Repeat
   breaches update it; severity can rise but never silently falls. We showed that live.
6. **Would this work for a supermarket?** That's the 30-second beat — same engine, `data_grocery.json`.
   Rules come from each product's `handling` block, so frozen at −18 °C and a 3-day shelf life work
   without code changes.
7. **Where does it integrate in real life?** Swap the simulator for the telematics/IoT webhook, the
   data file for the WMS/ERP, and keep the same REST and MCP surface. Nothing in the rules assumes
   mock data.
8. **Security?** `/mcp` requires a bearer token; REST writes can require an API key. The agent's
   tool list is the privilege boundary. Phone numbers are placeholders. No PII, no database.
9. **What's missing for production?** Persistence, auth per user, real supplier APIs, carrier
   telematics, per-tenant data, and an SOP sign-off on the thresholds — the thresholds here are
   plausible demo values, not regulatory ones.
10. **How long did this take?** A few hours. The engine is ~500 lines because the hard part was
    deciding what the AI is *not* allowed to do.
