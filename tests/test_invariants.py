import unittest

from auditor import audit_run


INTENT = {"intent_id": "buy-001", "symbol": "AAPL", "side": "buy", "qty": 10}


def broker_snapshot(orders=None, final_cash=900, final_qty=10, events=None):
    return {
        "initial_cash": 1000,
        "final_cash": final_cash,
        "initial_positions": {"AAPL": 0},
        "final_positions": {"AAPL": final_qty},
        "orders": orders if orders is not None else [{"order_id": "o1", "intent_id": "buy-001", "symbol": "AAPL", "side": "buy", "filled_qty": 10, "fill_price": 10, "status": "filled"}],
        "events": events or [],
    }


class AuditorTests(unittest.TestCase):
    def test_valid_fill_passes(self):
        self.assertEqual(audit_run(INTENT, broker_snapshot())["decision"], "promote")

    def test_duplicate_purchase_fails(self):
        orders = [
            {"order_id": oid, "intent_id": "buy-001", "symbol": "AAPL", "side": "buy", "filled_qty": 10, "fill_price": 10}
            for oid in ("o1", "o2")
        ]
        report = audit_run(INTENT, broker_snapshot(orders, 800, 20))
        self.assertIn("OVERFILL", [f["code"] for f in report["invariant_failures"]])

    def test_partial_fill_is_allowed(self):
        order = {"order_id": "o1", "intent_id": "buy-001", "symbol": "AAPL", "side": "buy", "filled_qty": 4, "fill_price": 10, "status": "partially_filled"}
        self.assertEqual(audit_run(INTENT, broker_snapshot([order], 960, 4))["decision"], "promote")

    def test_cash_mismatch_fails(self):
        self.assertIn("CASH_MISMATCH", [f["code"] for f in audit_run(INTENT, broker_snapshot(final_cash=950))["invariant_failures"]])

    def test_unknown_order_fails(self):
        orders = broker_snapshot()["orders"] + [{"order_id": "rogue", "intent_id": "other", "symbol": "AAPL", "side": "buy", "filled_qty": 0, "fill_price": 10}]
        self.assertIn("UNKNOWN_ORDER", [f["code"] for f in audit_run(INTENT, broker_snapshot(orders))["invariant_failures"]])

    def test_unresolved_retry_fails(self):
        events = [{"type": "submit_unknown", "intent_id": "buy-001"}, {"type": "retry", "intent_id": "buy-001"}]
        self.assertIn("UNSAFE_RETRY", [f["code"] for f in audit_run(INTENT, broker_snapshot(events=events))["invariant_failures"]])

    def test_reconciled_retry_is_allowed(self):
        events = [{"type": "submit_unknown", "intent_id": "buy-001"}, {"type": "reconciled", "intent_id": "buy-001"}, {"type": "retry", "intent_id": "buy-001"}]
        self.assertEqual(audit_run(INTENT, broker_snapshot(events=events))["decision"], "promote")


if __name__ == "__main__":
    unittest.main()
