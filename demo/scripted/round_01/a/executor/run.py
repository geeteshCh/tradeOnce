"""Scripted candidate r1-a: client_order_id per intent + look the order up before any retry."""

import json
import os
import sys
import time

import httpx

BROKER = os.environ["BROKER_URL"]
TIMEOUT_S = float(os.environ.get("EXECUTOR_TIMEOUT_S", "0.5"))


def lookup(client: httpx.Client, coid: str):
    try:
        r = client.get("/v2/orders:by_client_order_id", params={"client_order_id": coid})
    except httpx.TimeoutException:
        return "error", None
    if r.status_code == 200:
        return "found", r.json()
    if r.status_code == 404:
        return "absent", None
    return "error", None


def to_result(intent: dict, order: dict) -> dict:
    filled = int(float(order["filled_qty"]))
    status = {"filled": "filled", "partially_filled": "partially_filled"}.get(order["status"])
    if status is None:
        status = "partially_filled" if filled else "unknown"
    return {"intent_id": intent["intent_id"], "status": status, "broker_order_id": order["id"], "filled_qty": filled}


def execute(client: httpx.Client, intent: dict) -> dict:
    coid = intent["intent_id"]
    body = {"symbol": intent["symbol"], "qty": intent["qty"], "side": intent["side"], "type": "market",
            "time_in_force": "day", "client_order_id": coid}
    for _attempt in range(5):
        try:
            r = client.post("/v2/orders", json=body)
        except httpx.TimeoutException:
            r = None
        if r is not None and r.status_code == 200:
            return to_result(intent, r.json())
        if r is not None and r.status_code in (403, 422) and "unique" not in r.text:
            return {"intent_id": coid, "status": "rejected", "broker_order_id": None, "filled_qty": 0}
        if r is not None and r.status_code == 429:
            time.sleep(0.05)
            continue
        # Timeout, 5xx or "already exists": the order may exist. Look before retrying.
        state, order = lookup(client, coid)
        if state == "found":
            return to_result(intent, order)
    return {"intent_id": coid, "status": "unknown", "broker_order_id": None, "filled_qty": 0}


def main() -> None:
    intents = json.load(sys.stdin)
    with httpx.Client(base_url=BROKER, timeout=TIMEOUT_S) as client:
        print(json.dumps([execute(client, i) for i in intents]))


if __name__ == "__main__":
    main()
