"""Live soak: N real orders to Alpaca PAPER (or a local Arena), each with a seeded random fault injected in transit.

    export APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...        # Alpaca PAPER keys, in your own shell
    uv run python -m arena.live_soak --n 200 --seed 7 --label v0 --executor-cmd "python examples/naive_executor.py"
    uv run python -m arena.live_soak --n 200 --seed 7 --label v2 --executor-dir runs/<run>/evo   # same faults, new version

Each intent: buy 1 share. The proxy forwards to the real broker and injects the chosen fault on the way back, so
the order really exists at the broker even when the Executor sees a timeout. Counting is from the broker side:
how many orders reached the broker per intent. A correct Executor places exactly one (zero for a real rejection).

Paced under Alpaca's ~200 requests/minute. On weekends use crypto (--symbols LTC/USD): Alpaca paper trades it 24/7
and fills right away. Stock orders placed while the market is closed just queue; the duplicate count is still exact.
Every order this run created and left open is canceled at the end. Paper money only; live Alpaca is refused.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn

from .alpaca_proxy import PAPER_HOST, create_proxy
from .server import _free_port

ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = f"https://{PAPER_HOST}"
FAULT_MENU = [  # (name, rules) for one intent; the first POST is the one attacked
    ("accept_then_timeout", [{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_timeout", "params": {"delay_s": 2.0}}]),
    ("accept_then_500", [{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_500"}]),
    ("timeout_then_stale", [{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_timeout", "params": {"delay_s": 2.0}},
                            {"match": "GET /v2/orders*", "nth": 1, "fault": "stale_status", "params": {"calls": 2}}]),
    ("transient_500", [{"match": "POST /v2/orders", "nth": 1, "fault": "transient_500"}]),
    ("rate_limit_429", [{"match": "POST /v2/orders", "nth": 1, "fault": "rate_limit_429"}]),
]


@contextmanager
def proxy_server(upstream: str):
    port = _free_port()
    app = create_proxy(upstream)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            if httpx.get(f"{url}/_proxy/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    try:
        yield url, {"X-Proxy-Admin": app.state.admin_token}
    finally:
        server.should_exit = True
        t.join(timeout=5)


def run(args) -> dict:
    is_paper = PAPER_HOST in args.upstream
    if is_paper and not (os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY")):
        sys.exit("set APCA_API_KEY_ID and APCA_API_SECRET_KEY (Alpaca PAPER keys) in this shell first")
    rng = random.Random(args.seed)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cmd = shlex.split(args.executor_cmd)
    min_gap = 60.0 / args.rpm                  # seconds per upstream request
    rows, created = [], []
    with proxy_server(args.upstream) as (purl, admin):
        if is_paper:  # fail fast on a symbol the paper account can't trade
            for s in symbols:
                r = httpx.get(f"{purl}/v2/assets/{s.replace('/', '%2F')}", timeout=10)
                if r.status_code != 200 or not r.json().get("tradable"):
                    sys.exit(f"{s} is not tradable on this Alpaca paper account ({r.status_code}): {r.text[:200]}")
            print(f"checked: {', '.join(symbols)} tradable on Alpaca paper")
        env = {k: v for k, v in os.environ.items() if k not in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")}
        env.update(BROKER_URL=purl, APCA_API_BASE_URL=purl,
                   PYTHONPATH=str(Path(args.executor_dir).resolve()) + os.pathsep + env.get("PYTHONPATH", ""))
        for i in range(args.n):
            name, rules = rng.choice(FAULT_MENU) if rng.random() < args.fault_rate else ("none", [])
            intent = {"intent_id": f"soak-{args.label}-{args.seed}-{i:05d}", "symbol": rng.choice(symbols),
                      "side": "buy", "qty": 1}
            httpx.post(f"{purl}/_proxy/clear_log", headers=admin).raise_for_status()
            httpx.post(f"{purl}/_proxy/load", json=rules, headers=admin).raise_for_status()
            t0 = time.time()
            try:
                p = subprocess.run(cmd, input=json.dumps([intent]), cwd=args.executor_dir, env=env,
                                   capture_output=True, text=True, timeout=60)
                lines = p.stdout.strip().splitlines()
                claim = json.loads(lines[-1])[0] if lines else None
            except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
                claim = None
            log = httpx.get(f"{purl}/_proxy/log", headers=admin).json()
            accepted = [e for e in log if e["endpoint"] == "POST /v2/orders" and e["upstream_status"] == 200]
            created += [e["order_id"] for e in accepted if e["order_id"]]
            upstream_calls = sum(1 for e in log if e["upstream_status"] is not None)
            rows.append({"i": i, "intent_id": intent["intent_id"], "symbol": intent["symbol"], "fault": name,
                         "orders_at_broker": len(accepted), "duplicate": len(accepted) > 1, "missing": not accepted,
                         "claim": claim, "calls": len(log)})
            mark = "DUP" if len(accepted) > 1 else ("MISSING" if not accepted else "ok")
            print(f"{i + 1:>4}/{args.n} {intent['symbol']:5} fault={name:20} orders_at_broker={len(accepted)} {mark}")
            wait = upstream_calls * min_gap - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
        # Clean up: cancel what this run created and left open (paper only matters; harmless on the Arena).
        httpx.post(f"{purl}/_proxy/load", json=[], headers=admin).raise_for_status()
        canceled = 0
        for oid in created:
            r = httpx.delete(f"{purl}/v2/orders/{oid}", timeout=10)
            canceled += r.status_code in (200, 204)
            time.sleep(min_gap)
    by_fault = Counter(r["fault"] for r in rows)
    dups = Counter(r["fault"] for r in rows if r["duplicate"])
    report = {"label": args.label, "seed": args.seed, "n": args.n, "upstream": args.upstream,
              "executor_cmd": args.executor_cmd, "executor_dir": str(args.executor_dir),
              "intents_by_fault": dict(by_fault), "duplicates_by_fault": dict(dups),
              "duplicates": sum(r["duplicate"] for r in rows), "missing": sum(r["missing"] for r in rows),
              "orders_at_broker": len(created), "canceled_at_end": canceled, "rows": rows}
    out = ROOT / "runs" / "soak" / f"{args.label}-seed{args.seed}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\n{args.n} intents through {args.upstream} | executor: {args.executor_cmd}")
    print(f"{'fault':22}{'intents':>8}{'duplicates':>12}")
    for f, c in sorted(by_fault.items()):
        print(f"{f:22}{c:>8}{dups.get(f, 0):>12}")
    print(f"\norders at broker: {len(created)} for {args.n} intents | duplicates: {report['duplicates']} | "
          f"missing: {report['missing']} | canceled at end: {canceled}\nreport: {out.relative_to(ROOT)}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m arena.live_soak")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--label", default="run")
    ap.add_argument("--upstream", default=PAPER_URL, help="Alpaca paper (default) or a local Arena URL")
    ap.add_argument("--executor-cmd", default=f"{sys.executable} -m executor.run")
    ap.add_argument("--executor-dir", default=str(ROOT))
    ap.add_argument("--symbols", default="LTC/USD", help="comma-separated; crypto like LTC/USD trades on weekends")
    ap.add_argument("--fault-rate", type=float, default=0.5, help="share of intents that get a fault")
    ap.add_argument("--rpm", type=float, default=150, help="max upstream requests per minute (Alpaca allows ~200)")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
