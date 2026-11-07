"""The true record: every order, fill, cash movement and call. Only the Arena writes it; the Judge reads it."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from random import Random
from typing import Any

from .faults import FaultEngine
from .models import OrderRequest, Scenario

DEFAULT_PRICE = 100.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ArenaState:
    def __init__(self, scenario: Scenario | None = None):
        self.load(scenario or Scenario(id="empty", intents=[{"intent_id": "noop", "symbol": "NOOP", "side": "buy", "qty": 1}]))

    # ----- lifecycle -----
    def load(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.rng = Random(scenario.seed)
        self.faults = FaultEngine(scenario.faults)
        self.cash = scenario.starting_cash
        self.orders: dict[str, dict[str, Any]] = {}       # order_id -> true order
        self.by_coid: dict[str, str] = {}                 # client_order_id -> order_id
        self.fills: list[dict[str, Any]] = []             # true fills, one entry per execution
        self.activities: list[dict[str, Any]] = []        # what GET /activities/FILL shows (may contain duplicates)
        self.positions: dict[str, float] = {}
        self.calls: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []           # delayed fills: {order_id, remaining}
        self.started = time.time()

    # ----- ids -----
    def _uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    # ----- call log -----
    def log_call(self, method: str, path: str, endpoint: str, body: Any, fault: str | None) -> dict[str, Any]:
        entry = {
            "seq": len(self.calls) + 1,
            "t": round(time.time() - self.started, 4),
            "method": method,
            "path": path,
            "endpoint": endpoint,
            "body": body,
            "fault": fault,
            "status_code": None,
        }
        self.calls.append(entry)
        return entry

    def tick(self) -> None:
        """Advance delayed fills by one broker call."""
        still = []
        for p in self.pending:
            p["remaining"] -= 1
            if p["remaining"] <= 0:
                self._execute(self.orders[p["order_id"]], p["qty"])
            else:
                still.append(p)
        self.pending = still

    def settle(self) -> None:
        """Time passes after the Executor exits: every delayed fill completes. Run before reading the ledger."""
        for p in self.pending:
            self._execute(self.orders[p["order_id"]], p["qty"])
        self.pending = []

    # ----- orders -----
    def price(self, symbol: str) -> float:
        return self.scenario.prices.get(symbol, DEFAULT_PRICE)

    def new_order(self, req: OrderRequest) -> dict[str, Any]:
        order_id = self._uuid()
        coid = req.client_order_id or self._uuid()
        order = {
            "id": order_id,
            "client_order_id": coid,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "submitted_at": _now_iso(),
            "filled_at": None,
            "canceled_at": None,
            "symbol": req.symbol,
            "asset_class": "us_equity",
            "qty": str(req.qty),
            "filled_qty": "0",
            "filled_avg_price": None,
            "order_type": req.type,
            "type": req.type,
            "side": req.side,
            "time_in_force": req.time_in_force,
            "status": "new",
        }
        self.orders[order_id] = order
        self.by_coid[coid] = order_id
        return order

    def _execute(self, order: dict[str, Any], qty: float, *, duplicate_activity: bool = False) -> None:
        px = self.price(order["symbol"])
        signed = qty if order["side"] == "buy" else -qty
        self.cash -= signed * px
        self.positions[order["symbol"]] = self.positions.get(order["symbol"], 0.0) + signed
        prev = float(order["filled_qty"])
        order["filled_qty"] = _num(prev + qty)
        order["filled_avg_price"] = str(px)
        order["status"] = "filled" if prev + qty >= float(order["qty"]) else "partially_filled"
        order["filled_at"] = order["updated_at"] = _now_iso()
        fill = {
            "id": self._uuid(),
            "activity_type": "FILL",
            "order_id": order["id"],
            "client_order_id": order["client_order_id"],
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": _num(qty),
            "price": str(px),
            "transaction_time": _now_iso(),
        }
        self.fills.append(fill)
        self.activities.append(fill)
        if duplicate_activity:
            self.activities.append(dict(fill))

    def fill_now(self, order: dict[str, Any], *, duplicate_activity: bool = False) -> None:
        self._execute(order, float(order["qty"]), duplicate_activity=duplicate_activity)

    def fill_later(self, order: dict[str, Any], after_calls: int) -> None:
        self.pending.append({"order_id": order["id"], "qty": float(order["qty"]), "remaining": after_calls})

    def partial_then_cancel(self, order: dict[str, Any], ratio: float) -> None:
        qty = int(float(order["qty"]) * ratio)
        if qty > 0:
            self._execute(order, qty)
        order["status"] = "canceled"
        order["canceled_at"] = order["updated_at"] = _now_iso()

    # ----- views -----
    def positions_view(self) -> list[dict[str, Any]]:
        out = []
        for sym, qty in sorted(self.positions.items()):
            if qty == 0:
                continue
            px = self.price(sym)
            out.append({
                "symbol": sym,
                "qty": _num(qty),
                "side": "long" if qty > 0 else "short",
                "avg_entry_price": str(px),
                "market_value": str(qty * px),
                "current_price": str(px),
                "asset_class": "us_equity",
            })
        return out

    def account_view(self) -> dict[str, Any]:
        equity = self.cash + sum(q * self.price(s) for s, q in self.positions.items())
        return {
            "id": "arena-account",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": str(round(self.cash, 2)),
            "buying_power": str(round(self.cash, 2)),
            "equity": str(round(equity, 2)),
        }

    def ledger(self) -> dict[str, Any]:
        """The true record. Only reachable with the admin token."""
        return {
            "scenario_id": self.scenario.id,
            "starting_cash": self.scenario.starting_cash,
            "cash": round(self.cash, 2),
            "positions": {s: q for s, q in sorted(self.positions.items()) if q != 0},
            "orders": list(self.orders.values()),
            "fills": self.fills,
            "pending_fills": self.pending,
            "calls": self.calls,
            "prices": {s: self.price(s) for s in {o["symbol"] for o in self.orders.values()}},
        }


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)
