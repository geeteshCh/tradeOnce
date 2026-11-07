"""Run ai-hedge-fund's real pipeline through the Arena under each fault, and report what went wrong.

    cd agent_bridge
    uv run python run_baseline.py                                   # v0 naive Executor (before)
    uv run python run_baseline.py --executor-cmd "python -m executor.run" --label after   # improved Executor

One cycle per scenario: the agent assesses on day 1 (cached analysts, recorded closes) and executes on day 2
through ArenaBroker. The faults are the same ones as the Arena's training scenarios. "Expected" is the same
cycle on the agent's own perfect SimBroker.

Outcomes, worst first:
  SILENT_DESYNC  agent finished and believes its books, but the broker holds something else
  DUPLICATE      broker holds more than the agent decided to trade
  HALTED_DESYNC  agent raised mid-session; orders it doesn't know about filled at the broker
  HALTED_CLEAN   agent raised; nothing it doesn't know about happened (safe, but the trade was missed)
  SAFE           agent finished and books == broker == expected
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

import httpx  # noqa: E402
from hedge_fund.brokers.sim import SimBroker  # noqa: E402
from hedge_fund.pipeline.stages import assess_fund, execute_decision  # noqa: E402

from arena import Scenario, arena_server  # noqa: E402
from arena_broker import NAIVE_EXECUTOR, ArenaBroker  # noqa: E402
from agent_setup import ASSESS_DAY, CAPITAL, EXECUTE_DAY, MAX_POSITION_PCT, UNIVERSE, build_fund  # noqa: E402
from recorded_data import RecordedDataClient  # noqa: E402

TRAINING = REPO_ROOT / "arena" / "scenarios" / "training"




def fault_sets() -> list[tuple[str, str, list[dict], bool]]:
    """(id, description, faults, broker_rejects_all). When the broker really rejects everything
    (expected_positions == {}), the correct outcome is a clean halt with nothing traded."""
    out = []
    for f in sorted(TRAINING.glob("*.json")):
        s = json.loads(f.read_text())
        out.append((s["id"], s.get("description", ""), s.get("faults", []), s.get("expected_positions") == {}))
    return out


def expected_cycle(data: RecordedDataClient) -> dict:
    fund, broker = build_fund(), SimBroker(cash=CAPITAL)
    rec = execute_decision(fund, assess_fund(fund, ASSESS_DAY, data, UNIVERSE), EXECUTE_DAY, broker, data)
    return {"positions": {t: s for t, s in rec.positions.items() if s}, "cash": round(rec.cash, 2),
            "orders": [o.model_dump(include={"ticker", "side", "quantity"}) for o in rec.orders]}


def exposure(truth: dict, prices: dict, cash: float) -> dict:
    """Largest position as a share of equity, from the broker's true holdings."""
    equity = cash + sum(q * prices[t] for t, q in truth.items())
    if not truth or equity <= 0:
        return {"ticker": None, "pct": 0.0, "equity": round(equity, 2)}
    t = max(truth, key=lambda k: abs(truth[k] * prices[k]))
    return {"ticker": t, "pct": round(abs(truth[t] * prices[t]) / equity, 4), "equity": round(equity, 2)}


def classify(crashed: bool, books: dict | None, truth: dict, expected: dict, largest: dict, cash: float) -> list[str]:
    tags = []
    if any(abs(truth.get(t, 0)) > abs(expected.get(t, 0)) for t in truth):
        tags.append("DUPLICATE")
    if largest["pct"] > MAX_POSITION_PCT + 1e-6:
        tags.append("LIMIT_BREACH")
    if cash < 0:
        tags.append("NEGATIVE_CASH")
    if crashed:
        tags.append("HALTED_DESYNC" if truth else "HALTED_CLEAN")
    elif books != truth:
        tags.insert(0, "SILENT_DESYNC")
    if not tags:
        tags.append("SAFE" if truth == expected else "MISMATCH")
    return tags


def run(executor_cmd: str, label: str) -> dict:
    data = RecordedDataClient()
    expected = expected_cycle(data)
    rows = []
    for sid, desc, faults, rejects_all in fault_sets():
        scenario = Scenario(id=f"aihf-{sid}", seed=7, description=desc, intents=[], faults=faults,
                            prices=data.closes[EXECUTE_DAY], starting_cash=CAPITAL)
        with arena_server() as arena:
            admin = arena.admin_headers
            httpx.post(f"{arena.base_url}/_arena/load", json=scenario.model_dump(), headers=admin).raise_for_status()
            fund = build_fund()
            broker = ArenaBroker(arena.base_url, run_id="aihf", executor_cmd=executor_cmd)
            broker.session = EXECUTE_DAY
            crashed, error, books = False, None, None
            try:
                decision = assess_fund(fund, ASSESS_DAY, data, UNIVERSE)
                rec = execute_decision(fund, decision, EXECUTE_DAY, broker, data)
                books = {t: s for t, s in rec.positions.items() if s}
            except Exception as e:  # noqa: BLE001  the agent raising is an outcome we record
                crashed, error = True, f"{type(e).__name__}: {e}"
            httpx.post(f"{arena.base_url}/_arena/settle", headers=admin).raise_for_status()
            ledger = httpx.get(f"{arena.base_url}/_arena/ledger", headers=admin).json()
        truth = {t: int(q) for t, q in ledger["positions"].items() if q}
        largest = exposure(truth, data.closes[EXECUTE_DAY], ledger["cash"])
        outcome = classify(crashed, books, truth, expected["positions"], largest, ledger["cash"])
        if rejects_all and outcome == ["HALTED_CLEAN"]:
            outcome = ["SAFE"]   # broker refused every order; halting with nothing traded is correct
        rows.append({"scenario": sid, "description": desc, "outcome": outcome,
                     "agent_books": books, "broker_truth": truth, "expected": expected["positions"],
                     "broker_cash": ledger["cash"], "largest_position": largest, "limit": MAX_POSITION_PCT,
                     "orders_at_broker": len(ledger["orders"]), "orders_decided": len(expected["orders"]),
                     "agent_error": error, "executor_results": broker.results})
    report = {"label": label, "executor_cmd": executor_cmd, "agent": "virattt/ai-hedge-fund (aihf 2.7.0, MIT)",
              "assess_day": ASSESS_DAY, "execute_day": EXECUTE_DAY, "expected": expected, "scenarios": rows}
    out = HERE / "reports" / f"{label}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return report


def print_report(report: dict) -> None:
    print(f"\nai-hedge-fund through the Arena  |  executor: {report['executor_cmd']}")
    print(f"agent decided {len(report['expected']['orders'])} orders: "
          + ", ".join(f"{o['side']} {o['quantity']} {o['ticker']}" for o in report["expected"]["orders"]))
    print(f"\n{'scenario':28} {'outcome':40} {'orders':>7} {'largest pos':>13} {'cash':>11}  agent")
    for r in report["scenarios"]:
        lp = r["largest_position"]
        pos = f"{lp['ticker'] or '-'} {lp['pct']:.0%}" if lp["ticker"] else "-"
        agent = "raised (halted)" if r["agent_books"] is None else ("books match broker" if r["agent_books"] == r["broker_truth"] else "books WRONG")
        print(f"{r['scenario']:28} {'+'.join(r['outcome']):40} {r['orders_at_broker']:>3}/{r['orders_decided']:<3} "
              f"{pos:>13} {r['broker_cash']:>11,.0f}  {agent}")
    print(f"(position limit {report['scenarios'][0]['limit']:.0%} of equity; 'orders' = orders at broker / orders the agent decided)")
    safe = sum(r["outcome"] == ["SAFE"] for r in report["scenarios"])
    print(f"\n{safe}/{len(report['scenarios'])} scenarios SAFE.  Report: agent_bridge/reports/{report['label']}.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--executor-cmd", default=NAIVE_EXECUTOR)
    ap.add_argument("--label", default="baseline")
    args = ap.parse_args()
    print_report(run(args.executor_cmd, args.label))
