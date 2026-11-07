"""Generate a local hidden held-out set:  python -m loop.make_hidden [--out hidden_set]

A stand-in until the Judge team's own hidden set (auditor/hidden/) exists. The output directory is gitignored and
must never be committed: the repo is public. Variants differ from training in timing, symbols, sizes and mixes, and
include counter-cases where retrying or rejecting is the correct answer.
"""

from __future__ import annotations

import argparse
import json
import zlib
from pathlib import Path

P = {"AAPL": 200.0, "MSFT": 400.0, "NVDA": 120.0, "TSLA": 250.0, "AMZN": 180.0, "GOOG": 170.0}


def sc(id, intents, faults=(), **kw):
    return {"id": id, "seed": zlib.crc32(id.encode()) % 10_000, "intents": intents, "faults": list(faults), "prices": P, **kw}


def it(iid, sym, side, qty):
    return {"intent_id": iid, "symbol": sym, "side": side, "qty": qty}


POST, GETS = "POST /v2/orders", "GET /v2/orders*"

HIDDEN = [
    # accept_then_timeout: longer delays, other symbols, the 2nd of several intents
    sc("h01_timeout_long", [it("h1", "MSFT", "buy", 3)], [{"match": POST, "nth": 1, "fault": "accept_then_timeout", "params": {"delay_s": 4.0}}]),
    sc("h02_timeout_second_intent", [it("a", "NVDA", "buy", 2), it("b", "GOOG", "buy", 1)],
       [{"match": POST, "nth": 2, "fault": "accept_then_timeout", "params": {"delay_s": 1.5}}]),
    # accept_then_500
    sc("h03_accept_500", [it("h3", "AMZN", "buy", 5)], [{"match": POST, "nth": 1, "fault": "accept_then_500"}]),
    # counter-cases: retry is right / rejection is right
    sc("h04_transient_twice", [it("h4", "GOOG", "buy", 2)], [{"match": POST, "nth": [1, 2], "fault": "transient_500"}]),
    sc("h05_rate_limited", [it("h5", "AAPL", "sell", 1)], [{"match": POST, "nth": 1, "fault": "rate_limit_429"}]),
    sc("h06_reject_all", [it("h6", "NVDA", "buy", 10)], [{"match": POST, "nth": list(range(1, 11)), "fault": "real_reject"}],
       expected_positions={}),
    sc("h07_reject_one_of_two", [it("x", "AAPL", "buy", 1), it("y", "MSFT", "buy", 1)],
       [{"match": POST, "nth": list(range(2, 12)), "fault": "real_reject"}], expected_positions={"AAPL": 1}),
    # stale status: lasts longer than in training
    sc("h08_timeout_stale_long", [it("h8", "TSLA", "buy", 2)],
       [{"match": POST, "nth": 1, "fault": "accept_then_timeout", "params": {"delay_s": 1.0}},
        {"match": GETS, "nth": 1, "fault": "stale_status", "params": {"calls": 5}}]),
    # delayed fill: slower than training
    sc("h09_delayed_long", [it("h9", "AAPL", "buy", 3)], [{"match": POST, "nth": 1, "fault": "delayed_fill", "params": {"after_calls": 6}}]),
    # partial fills: different ratio and size
    sc("h10_partial_quarter", [it("h10", "MSFT", "buy", 8)], [{"match": POST, "nth": 1, "fault": "partial_fill_then_cancel", "params": {"fill_ratio": 0.25}}]),
    # duplicate fill in the activity feed, multi-intent
    sc("h11_dup_activity_mix", [it("p", "NVDA", "buy", 2), it("q", "NVDA", "sell", 1)], [{"match": POST, "nth": 1, "fault": "duplicate_fill"}]),
    # clean multi-intent baseline
    sc("h12_clean_basket", [it("c1", "AAPL", "buy", 1), it("c2", "MSFT", "buy", 2), it("c3", "TSLA", "sell", 1)]),
]


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m loop.make_hidden")
    ap.add_argument("--out", type=Path, default=Path("hidden_set"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for s in HIDDEN:
        (args.out / f"{s['id']}.json").write_text(json.dumps(s, indent=1))
    print(f"wrote {len(HIDDEN)} hidden scenarios to {args.out}/ (gitignored; never commit)")


if __name__ == "__main__":
    main()
