"""Scripted candidate r1-b: wait longer (3s client timeout) so the broker answers before we retry."""

import json
import os
import sys

import httpx

BROKER = os.environ["BROKER_URL"]
TIMEOUT_S = float(os.environ.get("EXECUTOR_TIMEOUT_S", "3.0"))


def execute(client: httpx.Client, intent: dict) -> dict:
    body = {"symbol": intent["symbol"], "qty": intent["qty"], "side": intent["side"], "type": "market", "time_in_force": "day"}
    for _attempt in range(3):
        try:
            r = client.post("/v2/orders", json=body)
        except httpx.TimeoutException:
            continue  # BUG (on purpose): the order may already exist
        if r.status_code == 200:
            o = r.json()
            return {"intent_id": intent["intent_id"], "status": o["status"], "broker_order_id": o["id"], "filled_qty": int(float(o["filled_qty"]))}
        if r.status_code >= 500 or r.status_code == 429:
            continue
        return {"intent_id": intent["intent_id"], "status": "rejected", "broker_order_id": None, "filled_qty": 0}
    return {"intent_id": intent["intent_id"], "status": "failed", "broker_order_id": None, "filled_qty": 0}


def main() -> None:
    intents = json.load(sys.stdin)
    with httpx.Client(base_url=BROKER, timeout=TIMEOUT_S) as client:
        results = [execute(client, i) for i in intents]
    print(json.dumps(results))


if __name__ == "__main__":
    main()
