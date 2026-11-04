#!/usr/bin/env python3
"""
PharmaGuard IoT simulator -- fakes the sensors on a refrigerated vehicle.

temp scenario: readings jitter inside the shipment's allowed window (4-6 °C for a
2-8 °C product), then ramp above the maximum and hold, which trips an incident.
Other scenarios fire a single disruption at the backend.

  python simulator.py --shipment SHP-1001 --interval 2 --spike-after 10 --spike-to 11.5
  python simulator.py --shipment SHP-1001 --once --temp 12.4
  python simulator.py --scenario expiry
  python simulator.py --data data_grocery.json --scenario expiry
  python simulator.py --scenario recall --shipment SHP-1001
  python simulator.py --scenario delay --shipment SHP-1002 --delay-hours 14
"""

import argparse
import random
import sys
import time

import requests

NORMAL_LOW, NORMAL_HIGH = 4.0, 6.0   # default band, used when the product allows 2-8 °C


def main() -> int:
    ap = argparse.ArgumentParser(description="Simulate cold-chain sensor readings and disruptions.")
    ap.add_argument("--shipment", default="SHP-1001", help="Shipment id to report against.")
    ap.add_argument("--url", default="http://localhost:8000", help="PharmaGuard base URL.")
    ap.add_argument("--interval", type=float, default=2.0, help="Seconds between readings.")
    ap.add_argument("--spike-after", type=int, default=10, help="Normal readings before the excursion starts.")
    ap.add_argument("--spike-to", type=float, default=None,
                    help="Hold temperature in °C (default: shipment max + 3.5, i.e. 11.5 for 2-8 °C goods).")
    ap.add_argument("--ramp-steps", type=int, default=3, help="Readings used to ramp up to --spike-to.")
    ap.add_argument("--count", type=int, default=0, help="Stop after N readings (0 = until Ctrl-C).")
    ap.add_argument("--once", action="store_true", help="Send a single reading at --temp and exit.")
    ap.add_argument("--temp", type=float, default=11.5, help="Temperature for --once, in °C.")
    ap.add_argument("--scenario", default="temp", choices=["temp", "expiry", "recall", "delay"],
                    help="Which disruption to simulate.")
    ap.add_argument("--delay-hours", type=float, default=6.0, help="Delay to report for --scenario delay.")
    ap.add_argument("--data", default=None, help="Switch vertical first, e.g. data_grocery.json.")
    ap.add_argument("--api-key", default=None, help="X-API-Key value, if the backend requires one.")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    headers = {"X-API-Key": args.api_key} if args.api_key else {}

    def post(path, payload=None):
        r = requests.post(base + path, json=payload or {}, headers=headers, timeout=10)
        if r.status_code >= 400:
            print(f"{path} -> {r.status_code} {r.text[:200]}", file=sys.stderr)
            r.raise_for_status()
        return r.json()

    def get(path):
        r = requests.get(base + path, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()

    if args.data:
        info = post("/reload", {"data_file": args.data})
        print(f"Switched vertical -> {info['vertical']} ({info['data_file']})")
        if args.shipment not in [s["shipment_id"] for s in get("/shipments")["shipments"]]:
            args.shipment = get("/shipments")["shipments"][0]["shipment_id"]
            print(f"Using shipment {args.shipment} from the new data file.")

    def line(result):
        """One printed line per reading."""
        reading = result["reading"]
        inc = result.get("incident")
        tail = ""
        if inc:
            tail = (f" | {inc['id']} {inc['type']} {inc['severity']} {inc['status']}"
                    f" | {result['minutes_out_of_range']} min out"
                    f" | INR {inc['value_at_risk_inr']:,} at risk")
        return (f"{reading['ts']}  {reading.get('temp_c', '--'):>6} °C  "
                f"{'OK    ' if reading['in_range'] else 'BREACH'}{tail}")

    # Non-temperature scenarios are one shot each.
    if args.scenario == "expiry":
        out = post(f"/expiry-scan?shipment_id={args.shipment}" if args.shipment else "/expiry-scan")
        for inc in out["incidents"]:
            print(f"{inc['id']} {inc['type']} {inc['severity']} -> {inc['recommended_action']['code']}"
                  f" | INR {inc['value_at_risk_inr']:,} at risk"
                  f" | batches: {', '.join(b['batch_id'] for b in inc['affected_batches'])}")
        if not out["incidents"]:
            print("No batches at expiry risk.")
        return 0

    if args.scenario == "recall":
        batch = get(f"/shipments/{args.shipment}")["batches"][0]["batch_id"]
        inc = post("/recall", {"batch_id": batch, "reason": "Supplier notice: sterility failure in this lot."})
        print(f"{inc['id']} RECALL {inc['severity']} on {batch} -> {inc['recommended_action']['code']}"
              f" | INR {inc['value_at_risk_inr']:,} at risk")
        return 0

    if args.scenario == "delay":
        inc = post("/delay", {"shipment_id": args.shipment, "delay_hours": args.delay_hours})
        print(f"{inc['id']} DELAY {inc['severity']} | revised ETA {inc['eta_hours']} h"
              f" -> {inc['recommended_action']['code']}")
        return 0

    # Temperature scenario: read the shipment's own window so this works for
    # chilled (2-8 °C) and frozen (-22 to -18 °C) goods alike.
    window = get(f"/shipments/{args.shipment}")["allowed_temp_c"]
    if not window:
        print(f"{args.shipment} has no temperature rule to simulate.", file=sys.stderr)
        return 1
    low, high = window["min"], window["max"]
    normal_low, normal_high = (NORMAL_LOW, NORMAL_HIGH) if (low, high) == (2, 8) else (low + 0.2, high - 0.2)
    spike_to = args.spike_to if args.spike_to is not None else round(high + 3.5, 2)

    if args.once:
        print(line(post("/alert", {"shipment_id": args.shipment, "temp_c": round(args.temp, 2)})))
        return 0

    print(f"Simulating {args.shipment} -> {base} every {args.interval}s "
          f"(allowed {low} to {high} °C, normal {normal_low}-{normal_high} °C, "
          f"spike to {spike_to} °C after {args.spike_after} readings).")
    sent = 0
    try:
        while True:
            if sent < args.spike_after:
                temp = random.uniform(normal_low, normal_high)
            else:
                step = sent - args.spike_after + 1
                if step <= args.ramp_steps:
                    temp = high + (spike_to - high) * step / args.ramp_steps  # linear ramp out of range
                else:
                    temp = spike_to + random.uniform(-0.3, 0.3)               # hold, with noise
            print(line(post("/alert", {"shipment_id": args.shipment, "temp_c": round(temp, 2)})), flush=True)
            sent += 1
            if args.count and sent >= args.count:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nStopped after {sent} readings.")
        return 0
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
