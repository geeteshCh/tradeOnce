"""ArenaBroker: the live broker ai-hedge-fund doesn't have yet, built on its own Broker interface.

ai-hedge-fund's contract (hedge_fund/brokers/protocol.py): `place_order` either fills the order COMPLETELY
and returns a Fill, or raises. Real brokers break that assumption (timeouts after accept, partial fills,
late fills). This class keeps the agent's contract and pushes every order through TradeOnce's Executor, the
code the Builder improves, against an Alpaca-shaped broker (the Arena, or Alpaca paper).

The Executor is called exactly the way the Arena runner calls it (stdin intents, stdout results), so the
same Executor version can be scored by the Judge and used by the agent.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
from hedge_fund.brokers.models import Fill, Order, Position

REPO_ROOT = Path(__file__).resolve().parents[1]
NAIVE_EXECUTOR = f"{sys.executable} examples/naive_executor.py"


class BrokerExecutionError(RuntimeError):
    """The Executor could not confirm a complete fill. Raising keeps the agent's contract: no silent drops."""

    def __init__(self, order: Order, result: dict | None, detail: str):
        self.order, self.result = order, result
        super().__init__(f"{order.side} {order.quantity} {order.ticker}: {detail} (result={result})")


class ArenaBroker:
    def __init__(self, base_url: str, run_id: str, executor_cmd: str = NAIVE_EXECUTOR,
                 executor_dir: str | Path = REPO_ROOT, timeout_s: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.executor_cmd = shlex.split(executor_cmd)
        self.executor_dir = Path(executor_dir)
        self.timeout_s = timeout_s
        self.session: str = "unknown"        # set by the caller before each execution session
        self.results: list[dict] = []        # every Executor result, for the report
        self._http = httpx.Client(base_url=self.base_url, timeout=5.0)

    # ---- hedge_fund Broker protocol -------------------------------------------------------------
    def positions(self) -> dict[str, Position]:
        r = self._http.get("/v2/positions")
        r.raise_for_status()
        out = {}
        for p in r.json():
            shares = int(float(p["qty"]))
            if shares:
                out[p["symbol"]] = Position(ticker=p["symbol"], shares=shares)
        return out

    def cash(self) -> float:
        r = self._http.get("/v2/account")
        r.raise_for_status()
        return float(r.json()["cash"])

    def place_order(self, order: Order) -> Fill:
        # Stable per decision: the same order in the same session can never become two intents.
        ticker_id = order.ticker.replace("/", "")       # crypto symbols like ETH/USD; keep ids URL-safe
        intent = {"intent_id": f"{self.run_id}-{self.session}-{ticker_id}-{order.side}",
                  "symbol": order.ticker, "side": order.side, "qty": order.quantity}
        result = self._run_executor(intent, order)
        self.results.append({"intent": intent, "result": result})
        if result.get("status") == "filled" and int(result.get("filled_qty", 0)) == order.quantity:
            return Fill(ticker=order.ticker, side=order.side, quantity=order.quantity, price=order.price)
        raise BrokerExecutionError(order, result, "fill not confirmed")

    # ---- internals ------------------------------------------------------------------------------
    def _run_executor(self, intent: dict, order: Order) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "ARENA_ADMIN_TOKEN"}
        env.update(BROKER_URL=self.base_url, APCA_API_BASE_URL=self.base_url,
                   APCA_API_KEY_ID=env.get("APCA_API_KEY_ID", "arena"),
                   APCA_API_SECRET_KEY=env.get("APCA_API_SECRET_KEY", "arena"),
                   PYTHONPATH=str(self.executor_dir) + os.pathsep + env.get("PYTHONPATH", ""))
        try:
            proc = subprocess.run(self.executor_cmd, input=json.dumps([intent]), cwd=self.executor_dir, env=env,
                                  capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise BrokerExecutionError(order, None, "executor timed out")
        lines = proc.stdout.strip().splitlines()
        try:
            results = json.loads(lines[-1]) if lines else []
            return next(r for r in results if r.get("intent_id") == intent["intent_id"])
        except (json.JSONDecodeError, StopIteration) as e:
            raise BrokerExecutionError(order, None, f"unreadable executor output ({e}); stderr: {proc.stderr[-300:]}")
