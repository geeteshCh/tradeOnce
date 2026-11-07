"""Scripted candidate r4-b: if an order ends partially filled, place the order again."""

import json
import os
import sys
import time

import httpx

BROKER = os.environ["BROKER_URL"]
TIMEOUT_S = float(os.environ.get("EXECUTOR_TIMEOUT_S", "0.5"))
MAX_POLLS = 200          # bounded by the deadline below, not by a guessed count
POLL_DEADLINE_S = 5.0
POLL_SLEEP_S = 0.02
FINAL = {"filled", "canceled", "rejected", "expired"}


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


def settle(client: httpx.Client, coid: str, order: dict) -> dict:
    """Poll until the order is final. A 'new' status right after a timeout may be stale."""
    deadline = time.time() + POLL_DEADLINE_S
    polls = 0
    while order["status"] not in FINAL and polls < MAX_POLLS and time.time() < deadline:
        time.sleep(POLL_SLEEP_S)
        polls += 1
        state, fresh = lookup(client, coid)
        if state == "found":
            order = fresh
    return order


def to_result(intent: dict, order: dict) -> dict:
    filled = int(float(order["filled_qty"]))
    if order["status"] == "filled":
        status = "filled"
    elif filled:
        status = "partially_filled"
    elif order["status"] in ("canceled", "rejected", "expired"):
        status = "failed"
    else:
        status = "unknown"
    return {"intent_id": intent["intent_id"], "status": status, "broker_order_id": order["id"], "filled_qty": filled}


def execute(client: httpx.Client, intent: dict) -> dict:
    """Fill the whole intent: keep placing follow-up orders for whatever was canceled unfilled."""
    total, last_id, remaining, n = 0, None, intent["qty"], 0
    while remaining > 0 and n < 5:
        coid = intent["intent_id"] if n == 0 else f"{intent['intent_id']}-{n}"
        res = execute_one(client, intent, coid)  # re-sends the full quantity
        if res["status"] in ("rejected", "unknown") and total == 0:
            return {**res, "intent_id": intent["intent_id"]}
        total += res["filled_qty"]
        last_id = res["broker_order_id"] or last_id
        if res["filled_qty"] == 0:
            break
        remaining = 0 if total >= intent["qty"] else remaining
        if n == 1:
            break
        n += 1
    status = "filled" if total >= intent["qty"] else ("partially_filled" if total else "failed")
    return {"intent_id": intent["intent_id"], "status": status, "broker_order_id": last_id, "filled_qty": total}


def execute_one(client: httpx.Client, intent: dict, coid: str) -> dict:
    body = {"symbol": intent["symbol"], "qty": intent["qty"], "side": intent["side"], "type": "market",
            "time_in_force": "day", "client_order_id": coid}
    intent = {**intent, "intent_id": coid}
    for _attempt in range(5):
        try:
            r = client.post("/v2/orders", json=body)
        except httpx.TimeoutException:
            r = None
        if r is not None and r.status_code == 200:
            return to_result(intent, settle(client, coid, r.json()))
        if r is not None and r.status_code in (403, 422) and "unique" not in r.text:
            return {"intent_id": coid, "status": "rejected", "broker_order_id": None, "filled_qty": 0}
        if r is not None and r.status_code == 429:
            time.sleep(0.05)
            continue
        state, order = lookup(client, coid)
        if state == "found":
            return to_result(intent, settle(client, coid, order))
    return {"intent_id": coid, "status": "unknown", "broker_order_id": None, "filled_qty": 0}


def main() -> None:
    intents = json.load(sys.stdin)
    with httpx.Client(base_url=BROKER, timeout=TIMEOUT_S) as client:
        print(json.dumps([execute(client, i) for i in intents]))


if __name__ == "__main__":
    main()
