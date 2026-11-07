"""Broker-ground-truth checks; no model decides the verdict.

Contract (standalone fixture):
  intent: {intent_id, symbol, side, qty}
  snapshot: {initial_cash, final_cash, initial_positions, final_positions,
             orders: [{order_id, intent_id, symbol, side, filled_qty, fill_price,
                       status}], events: [{type, intent_id, order_id, ...}]}

The simulator should populate these values from its *true* ledger, not from
the Builder's returned result. All amounts here use simulated USD.
"""

from collections import defaultdict
from decimal import Decimal


def _dec(value):
    return Decimal(str(value))


def _failure(code, message, **evidence):
    return {"code": code, "message": message, "evidence": evidence}


def audit_run(intent: dict, snapshot: dict) -> dict:
    """Return a reproducible PASS/FAIL report for one authorized intent.

    Assumptions: zero fees and one asset per intent for MVP. Partial fills are
    legitimate. Rejections and cancellations may leave unfilled quantity.
    """
    failures = []
    intent_id = intent["intent_id"]
    symbol = intent["symbol"]
    side = intent["side"].lower()
    requested = _dec(intent["qty"])
    if side not in ("buy", "sell") or requested <= 0:
        raise ValueError("Intent must have side buy/sell and positive qty")

    orders = snapshot.get("orders", [])
    events = snapshot.get("events", [])
    authorized = [o for o in orders if o.get("intent_id") == intent_id]
    unexpected = [o for o in orders if o.get("intent_id") != intent_id]
    if unexpected:
        failures.append(_failure("UNKNOWN_ORDER", "Ledger contains orders outside this intent", order_ids=[o.get("order_id") for o in unexpected]))

    for order in authorized:
        if order.get("symbol") != symbol or order.get("side", "").lower() != side:
            failures.append(_failure("ORDER_MISMATCH", "Order does not match authorized symbol/side", order_id=order.get("order_id")))

    # Duplicate IDs are invalid even if quantities do not exceed the intent.
    ids = [o.get("order_id") for o in orders]
    if len(ids) != len(set(ids)):
        failures.append(_failure("DUPLICATE_ORDER_ID", "Order IDs are not unique"))

    filled = sum((_dec(o.get("filled_qty", 0)) for o in authorized), Decimal(0))
    if filled > requested:
        failures.append(_failure("OVERFILL", "Total filled quantity exceeds authorization", authorized=str(requested), filled=str(filled)))
    if any(_dec(o.get("filled_qty", 0)) < 0 for o in orders):
        failures.append(_failure("NEGATIVE_FILL", "Negative fill quantity found"))

    initial_positions = {k: _dec(v) for k, v in snapshot.get("initial_positions", {}).items()}
    final_positions = {k: _dec(v) for k, v in snapshot.get("final_positions", {}).items()}
    signed_qty = sum(((_dec(o.get("filled_qty", 0)) * (1 if o.get("side", "").lower() == "buy" else -1)) for o in orders), Decimal(0))
    observed_change = final_positions.get(symbol, Decimal(0)) - initial_positions.get(symbol, Decimal(0))
    if observed_change != signed_qty:
        failures.append(_failure("POSITION_MISMATCH", "Position change differs from ledger fills", expected=str(signed_qty), observed=str(observed_change)))

    expected_cash_change = sum(((_dec(o.get("filled_qty", 0)) * _dec(o.get("fill_price", 0)) * (-1 if o.get("side", "").lower() == "buy" else 1)) for o in orders), Decimal(0))
    actual_cash_change = _dec(snapshot["final_cash"]) - _dec(snapshot["initial_cash"])
    if actual_cash_change != expected_cash_change:
        failures.append(_failure("CASH_MISMATCH", "Cash delta differs from ledger fills (zero-fee assumption)", expected=str(expected_cash_change), observed=str(actual_cash_change)))

    # A retry must not occur while the previous attempt has unknown outcome.
    unknown = defaultdict(bool)
    for event in events:
        if event.get("intent_id") != intent_id:
            continue
        typ = event.get("type")
        if typ == "submit_unknown":
            unknown[intent_id] = True
        elif typ == "reconciled":
            unknown[intent_id] = False
        elif typ == "retry" and unknown[intent_id]:
            failures.append(_failure("UNSAFE_RETRY", "Retried before resolving prior unknown submission", event=event))

    return {"intent_id": intent_id, "decision": "reject" if failures else "promote", "invariant_failures": failures, "total_filled_qty": str(filled), "checks_passed": len(failures) == 0}
