"""Training signal for the Builder:  python -m arena.run_training [--executor-cmd "python -m executor.run"]

Runs the Executor against every training scenario and checks the basics against the true ledger. Exits 1 if any
scenario fails, so it works as Aider's --test-cmd. This is NOT the Auditor: the Auditor's invariants and the hidden
set live in auditor/ and decide promotion. Passing this is necessary, not sufficient.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from .models import IntentResult, Scenario
from .runner import run_scenario
from .server import arena_server

TRAINING_DIR = Path(__file__).parent / "scenarios" / "training"


def check(trace: dict) -> list[str]:
    s = Scenario.model_validate(trace["scenario"])
    L = trace["ledger"]
    problems: list[str] = []
    if trace["timed_out"]:
        problems.append("executor timed out")
    if trace["exit_code"] not in (0, None):
        problems.append(f"executor exited {trace['exit_code']}: {trace['stderr_tail'][-300:]}")
    if trace["claims_error"]:
        problems.append(trace["claims_error"])

    claims = {}
    for c in trace["executor_claims"]:
        try:
            r = IntentResult.model_validate(c)
            claims[r.intent_id] = r
        except Exception as e:  # noqa: BLE001
            problems.append(f"bad result {c}: {e}")
    missing = {i.intent_id for i in s.intents} - claims.keys()
    if missing:
        problems.append(f"no result for intents {sorted(missing)}")

    actual = {k: v for k, v in L["positions"].items() if v != 0}
    intended = s.intended_positions()
    if actual != intended:
        problems.append(f"final positions {actual} != intended {intended}")

    claimed: dict[str, float] = {}
    for i in s.intents:
        if i.intent_id in claims:
            q = claims[i.intent_id].filled_qty
            claimed[i.symbol] = claimed.get(i.symbol, 0) + (q if i.side == "buy" else -q)
    claimed = {k: v for k, v in claimed.items() if v != 0}
    if claimed != actual:
        problems.append(f"executor claims positions {claimed} but the broker holds {actual}")

    open_orders = [o["id"] for o in L["orders"] if o["status"] in ("new", "accepted", "pending_new", "partially_filled")]
    if open_orders:
        problems.append(f"orders left open: {open_orders}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m arena.run_training")
    ap.add_argument("--executor-cmd", default=f"{sys.executable} -m executor.run")
    ap.add_argument("--executor-dir", default=".")
    ap.add_argument("--scenarios", type=Path, default=TRAINING_DIR, help="directory of scenario JSON files")
    ap.add_argument("--trace-out", type=Path, help="write full traces here (JSON) for the Builder to read")
    args = ap.parse_args()

    files = sorted(args.scenarios.glob("*.json"))
    traces, failed = [], 0
    with arena_server() as arena:
        for f in files:
            trace = run_scenario(json.loads(f.read_text()), arena.base_url, arena.admin_token,
                                 args.executor_dir, shlex.split(args.executor_cmd))
            trace["training_problems"] = problems = check(trace)
            traces.append(trace)
            failed += bool(problems)
            print(f"{'FAIL' if problems else 'PASS'}  {f.stem}")
            for p in problems:
                print(f"      - {p}")
    print(f"\n{len(files) - failed}/{len(files)} training scenarios pass")
    if args.trace_out:
        args.trace_out.write_text(json.dumps(traces, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
