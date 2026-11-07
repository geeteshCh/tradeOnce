"""ai-hedge-fund's own trades → fault proxy → Alpaca PAPER, once per Executor, side by side.

    cd agent_bridge
    set -a && source ../.env && set +a                                  # Alpaca PAPER keys
    uv run python run_side_by_side.py                                   # v0 (naive) vs v2 (Gemini Argon) on Alpaca paper
    uv run python run_side_by_side.py --upstream http://127.0.0.1:PORT  # dry run against a local Arena

For each fault and each Executor: the agent (unmodified pipeline, cached analysts, real Yahoo crypto closes) assesses
and executes one cycle. Its orders go through ArenaBroker → the Executor → the proxy (same seeded fault for both
Executors) → Alpaca paper, which fills crypto 24/7. Results are read from the broker side.

Each run gets a clean book on the shared paper account: the agent sees only the change since a snapshot taken just
before the run, on a fixed --capital, and everything the run bought is sold back afterwards. Paper money only; the
proxy refuses live Alpaca.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import httpx  # noqa: E402
from hedge_fund.brokers.models import Position  # noqa: E402
from hedge_fund.brokers.sim import SimBroker  # noqa: E402
from hedge_fund.pipeline.stages import assess_fund, execute_decision  # noqa: E402

from agent_setup import MAX_POSITION_PCT, build_fund  # noqa: E402
from arena.alpaca_proxy import PAPER_HOST  # noqa: E402
from arena.live_soak import FAULT_MENU, PAPER_URL, proxy_server  # noqa: E402
from arena_broker import ArenaBroker  # noqa: E402
from recorded_data import RecordedDataClient  # noqa: E402

FAULTS = {"none": [], **dict(FAULT_MENU)}
DEFAULT_EXECUTORS = "v0=python examples/naive_executor.py;v2=python demo/promoted/argon-v2/executor/run.py"
TERMINAL = {"filled", "canceled", "expired", "rejected"}


def norm(sym: str) -> str:
    return sym.replace("/", "").upper()


class SandboxBook(ArenaBroker):
    """The agent's Broker, scoped to one run: positions and cash are deltas from a snapshot, on a fixed capital."""

    def __init__(self, base_url, run_id, executor_cmd, universe, capital, base_positions, base_cash):
        super().__init__(base_url, run_id=run_id, executor_cmd=executor_cmd)
        self.names = {norm(s): s for s in universe}
        self.capital, self.base_positions, self.base_cash = capital, base_positions, base_cash

    def positions(self):
        now = raw_positions(self._http)
        out = {}
        for key in set(now) | set(self.base_positions):
            delta = round(now.get(key, 0.0) - self.base_positions.get(key, 0.0))
            if delta and key in self.names:
                out[self.names[key]] = Position(ticker=self.names[key], shares=int(delta))
        return out

    def cash(self) -> float:
        return self.capital + (raw_cash(self._http) - self.base_cash)


def raw_positions(http: httpx.Client) -> dict[str, float]:
    r = http.get("/v2/positions")
    r.raise_for_status()
    return {norm(p["symbol"]): float(p["qty"]) for p in r.json()}


def raw_cash(http: httpx.Client) -> float:
    r = http.get("/v2/account")
    r.raise_for_status()
    return float(r.json()["cash"])


def wait_terminal(http: httpx.Client, order_id: str, seconds: float = 8.0) -> dict:
    deadline, o = time.time() + seconds, {}
    while time.time() < deadline:
        o = http.get(f"/v2/orders/{order_id}").json()
        if o.get("status") in TERMINAL:
            return o
        time.sleep(0.4)
    http.delete(f"/v2/orders/{order_id}")      # still open (e.g. a stock while the market is closed): cancel it
    return http.get(f"/v2/orders/{order_id}").json()


def flatten(http: httpx.Client, base: dict[str, float], names: dict[str, str]) -> None:
    """Sell back whatever this run added, so the next run starts from the same book."""
    now = raw_positions(http)
    for key, qty in now.items():
        extra = qty - base.get(key, 0.0)
        if extra > 1e-9 and key in names:
            r = http.post("/v2/orders", json={"symbol": names[key], "qty": f"{extra:.9f}".rstrip("0").rstrip("."),
                                              "side": "sell", "type": "market", "time_in_force": "gtc"})
            if r.status_code == 200:
                wait_terminal(http, r.json()["id"])


def expected_cycle(args, data) -> dict:
    fund, broker = build_fund(args.signals, args.capital), SimBroker(cash=args.capital)
    rec = execute_decision(fund, assess_fund(fund, args.assess_day, data, args.universe), args.execute_day, broker, data)
    return {"positions": {t: s for t, s in rec.positions.items() if s},
            "orders": [o.model_dump(include={"ticker", "side", "quantity"}) for o in rec.orders]}


def one_run(args, data, purl, admin, http, label, cmd, fault, expected) -> dict:
    base_pos, base_cash = raw_positions(http), raw_cash(http)
    httpx.post(f"{purl}/_proxy/clear_log", headers=admin).raise_for_status()
    httpx.post(f"{purl}/_proxy/load", json=FAULTS[fault], headers=admin).raise_for_status()
    run_id = f"tos-{label}-{fault[:6]}-{secrets.token_hex(3)}"          # unique: Alpaca keeps client_order_ids
    book = SandboxBook(purl, run_id, cmd, args.universe, args.capital, base_pos, base_cash)
    book.session = args.execute_day
    fund = build_fund(args.signals, args.capital)
    raised, books = None, None
    try:
        rec = execute_decision(fund, assess_fund(fund, args.assess_day, data, args.universe), args.execute_day, book, data)
        books = {t: s for t, s in rec.positions.items() if s}
    except Exception as e:  # noqa: BLE001  the agent raising is an outcome we record
        raised = f"{type(e).__name__}: {str(e)[:160]}"
    httpx.post(f"{purl}/_proxy/load", json=[], headers=admin).raise_for_status()
    log = httpx.get(f"{purl}/_proxy/log", headers=admin).json()
    accepted = [e["order_id"] for e in log if e["endpoint"] == "POST /v2/orders" and e["upstream_status"] == 200 and e["order_id"]]
    orders = [wait_terminal(http, oid) for oid in accepted]
    per_symbol: dict[str, int] = {}
    for o in orders:
        per_symbol[o["symbol"]] = per_symbol.get(o["symbol"], 0) + 1
    now = raw_positions(http)
    names = {norm(s): s for s in args.universe}
    held = {names[k]: round(v - base_pos.get(k, 0.0), 6) for k, v in now.items()
            if k in names and abs(v - base_pos.get(k, 0.0)) > 1e-9}
    prices = data.closes[args.execute_day]
    largest = max(((t, abs(q) * prices[t] / args.capital) for t, q in held.items()), key=lambda x: x[1], default=(None, 0.0))
    dup_orders = sum(max(0, n - 1) for n in per_symbol.values())
    outcome = []
    if dup_orders:
        outcome.append("DUPLICATE")
    if largest[1] > MAX_POSITION_PCT + 0.01:
        outcome.append("LIMIT_BREACH")
    if raised:
        outcome.append("HALTED")
    elif books is not None and {t: round(q) for t, q in held.items()} != books:
        outcome.append("BOOKS_WRONG")
    if not outcome:
        outcome.append("SAFE" if {t: round(q) for t, q in held.items()} == expected["positions"] else "MISMATCH")
    flatten(http, base_pos, names)
    return {"fault": fault, "executor": label, "outcome": outcome, "orders_at_broker": len(orders),
            "orders_decided": len(expected["orders"]), "duplicate_orders": dup_orders, "broker_holds": held,
            "expected": expected["positions"], "largest_position": {"ticker": largest[0], "pct": round(largest[1], 4)},
            "agent_books": books, "agent_raised": raised, "run_id": run_id,
            "broker_orders": [{k: o.get(k) for k in ("id", "client_order_id", "symbol", "side", "qty", "filled_qty", "status")} for o in orders]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default=PAPER_URL)
    ap.add_argument("--executors", default=DEFAULT_EXECUTORS, help='"label=cmd;label=cmd"')
    ap.add_argument("--faults", default=",".join(FAULTS))
    ap.add_argument("--universe", default="ETH/USD,LTC/USD,SOL/USD")
    ap.add_argument("--closes", default="crypto_closes.json")
    ap.add_argument("--signals", default="crypto_signals.json")
    ap.add_argument("--assess-day", default="2026-11-05")
    ap.add_argument("--execute-day", default="2026-11-06")
    ap.add_argument("--capital", type=float, default=20_000.0)
    ap.add_argument("--label", default="side_by_side")
    args = ap.parse_args()
    args.universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    executors = [tuple(x.split("=", 1)) for x in args.executors.split(";") if x.strip()]
    faults = [f.strip() for f in args.faults.split(",") if f.strip()]

    data = RecordedDataClient(closes_file=args.closes)
    expected = expected_cycle(args, data)
    print(f"ai-hedge-fund decides (cached analysts, real {args.execute_day} closes, ${args.capital:,.0f} book): "
          + ", ".join(f"{o['side']} {o['quantity']} {o['ticker']}" for o in expected["orders"]))

    rows = []
    with proxy_server(args.upstream) as (purl, admin):
        http = httpx.Client(base_url=purl, timeout=10.0)
        if PAPER_HOST in args.upstream:
            for s in args.universe:
                a = http.get(f"/v2/assets/{s.replace('/', '%2F')}")
                if a.status_code != 200 or not a.json().get("tradable"):
                    sys.exit(f"{s} is not tradable on this Alpaca paper account: {a.status_code} {a.text[:150]}")
        for fault in faults:
            for label, cmd in executors:
                r = one_run(args, data, purl, admin, http, label, cmd, fault, expected)
                rows.append(r)
                print(f"  {fault:20} {label:4} {'+'.join(r['outcome']):24} orders {r['orders_at_broker']}/{r['orders_decided']}"
                      f"  dup {r['duplicate_orders']}  largest {r['largest_position']['ticker'] or '-'} {r['largest_position']['pct']:.0%}")
                time.sleep(1.0)                                       # stay well under Alpaca's ~200 requests/minute

    labels = [lbl for lbl, _ in executors]
    print(f"\n{'fault':22}" + "".join(f"{lbl:>34}" for lbl in labels))
    for fault in faults:
        cells = []
        for lbl in labels:
            r = next(x for x in rows if x["fault"] == fault and x["executor"] == lbl)
            cells.append(f"{'+'.join(r['outcome'])} ({r['orders_at_broker']}/{r['orders_decided']} orders)")
        print(f"{fault:22}" + "".join(f"{c:>34}" for c in cells))
    for lbl in labels:
        mine = [x for x in rows if x["executor"] == lbl]
        print(f"{lbl}: {sum(x['outcome'] == ['SAFE'] for x in mine)}/{len(mine)} SAFE, "
              f"{sum(x['duplicate_orders'] for x in mine)} duplicate orders at the broker")
    out = HERE / "reports" / f"{args.label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"upstream": args.upstream, "executors": dict(executors), "expected": expected,
                               "capital": args.capital, "rows": rows}, indent=2))
    print(f"report: agent_bridge/reports/{args.label}.json")


if __name__ == "__main__":
    main()
