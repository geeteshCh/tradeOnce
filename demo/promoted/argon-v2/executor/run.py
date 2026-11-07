"""Idempotent, reconciliation-first Executor.

Protocol:
  stdin : JSON list of intents [{intent_id, symbol, side, qty}]
  env   : BROKER_URL (also APCA_API_BASE_URL)
  stdout: last line is a JSON list of results [{intent_id, status, broker_order_id, filled_qty}]
"""

import json
import os
import sys

import httpx

BROKER = os.environ["BROKER_URL"]
TIMEOUT_S = float(os.environ.get("EXECUTOR_TIMEOUT_S", "0.5"))
NON_TERMINAL_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "calculated"}


def _resolve_order(client: httpx.Client, intent_id: str, order: dict) -> dict:
    order_id = order["id"]
    for _ in range(4):
        if order.get("status") not in NON_TERMINAL_STATUSES:
            break
        try:
            r = client.get(f"/v2/orders/{order_id}")
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            order = r.json()
    return {
        "intent_id": intent_id,
        "status": order["status"],
        "broker_order_id": order_id,
        "filled_qty": int(float(order.get("filled_qty") or 0)),
    }


def execute(client: httpx.Client, intent: dict) -> dict:
    intent_id = intent["intent_id"]
    client_order_id = str(intent_id)
    body = {
        "symbol": intent["symbol"],
        "qty": intent["qty"],
        "side": intent["side"],
        "type": "market",
        "time_in_force": "day",
        "client_order_id": client_order_id,
    }

    needs_reconcile = False
    for _attempt in range(6):
        if needs_reconcile:
            try:
                r = client.get(
                    "/v2/orders:by_client_order_id",
                    params={"client_order_id": client_order_id},
                )
            except httpx.HTTPError:
                continue

            if r.status_code == 200:
                return _resolve_order(client, intent_id, r.json())
            if r.status_code == 404:
                needs_reconcile = False
            else:
                continue

        try:
            r = client.post("/v2/orders", json=body)
        except httpx.HTTPError:
            needs_reconcile = True
            continue

        if r.status_code in (200, 201):
            return _resolve_order(client, intent_id, r.json())
        if r.status_code in (422, 429) or r.status_code >= 500:
            needs_reconcile = True
            continue
        return {
            "intent_id": intent_id,
            "status": "rejected",
            "broker_order_id": None,
            "filled_qty": 0,
        }

    return {
        "intent_id": intent_id,
        "status": "unknown" if needs_reconcile else "failed",
        "broker_order_id": None,
        "filled_qty": 0,
    }


def main() -> None:
    intents = json.load(sys.stdin)
    with httpx.Client(base_url=BROKER, timeout=TIMEOUT_S) as client:
        results = [execute(client, i) for i in intents]
    print(json.dumps(results))


if __name__ == "__main__":
    main()
