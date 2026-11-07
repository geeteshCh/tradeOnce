"""Bridge from the Loop to the Judge.

- `ledger_to_snapshot`: converts an Arena trace into the snapshot shape `auditor.audit_run(intent, snapshot)` expects.
- `judge_trace`: all failures for one trace = the Judge's auditor (per intent) + the Arena's completion checks.
- `evaluate`: runs one Executor version on every scenario of every suite, each candidate in its own Arena.
- `decide`: the tournament rule from AGENTS.md.

Order -> intent attribution: an order belongs to an intent if its client_order_id is the intent_id or starts with
"<intent_id>-" / "<intent_id>:". Orders without that are attributed by (symbol, side) when exactly one intent
matches; otherwise they are unknown orders. So Executors should use intent_id-based client_order_ids.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from arena import Scenario, arena_server, run_scenario
from arena.run_training import check as completion_check

# Faults where the right move is to retry or report a rejection. Hidden scenarios using only these (plus faults
# already attacked) are always active, so a fix that stops retrying altogether gets caught.
COUNTER_CASE_FAULTS = {"transient_500", "rate_limit_429", "real_reject"}

JudgeFn = Callable[[dict[str, Any]], list[dict[str, Any]]]


def faults_of(s: Scenario) -> set[str]:
    return {f.fault for f in s.faults}


def active(scenarios: list[Scenario], seen_faults: set[str]) -> list[Scenario]:
    allowed = seen_faults | COUNTER_CASE_FAULTS
    return [s for s in scenarios if faults_of(s) <= allowed]


# ---------------------------------------------------------------------------------------------- snapshot adapter
def _attribute(order_coid: str | None, symbol: str, side: str, intents: list[dict]) -> str | None:
    for i in intents:
        iid = i["intent_id"]
        if order_coid and (order_coid == iid or order_coid.startswith(f"{iid}-") or order_coid.startswith(f"{iid}:")):
            return iid
    matches = [i["intent_id"] for i in intents if i["symbol"] == symbol and i["side"] == side]
    return matches[0] if len(matches) == 1 else None


def ledger_to_snapshot(trace: dict[str, Any], intent: dict[str, Any], *, include_unknown: bool) -> dict[str, Any]:
    L = trace["ledger"]
    intents = trace["scenario"]["intents"]
    single = len(intents) == 1

    orders = []
    order_owner: dict[str, str | None] = {}
    for o in L["orders"]:
        owner = _attribute(o["client_order_id"], o["symbol"], o["side"], intents)
        order_owner[o["id"]] = owner
        if owner == intent["intent_id"] or (owner is None and include_unknown):
            orders.append({
                "order_id": o["id"], "intent_id": owner, "symbol": o["symbol"], "side": o["side"],
                "filled_qty": float(o["filled_qty"]), "fill_price": float(o["filled_avg_price"] or 0),
                "status": o["status"], "client_order_id": o["client_order_id"],
            })

    if single:  # real check against the broker's true positions and cash
        initial_positions, final_positions = {}, dict(L["positions"])
        initial_cash, final_cash = L["starting_cash"], L["cash"]
    else:       # several intents share the account: scope to this intent's orders
        pos: dict[str, float] = {}
        cash = L["starting_cash"]
        for o in orders:
            q = o["filled_qty"] * (1 if o["side"] == "buy" else -1)
            pos[o["symbol"]] = pos.get(o["symbol"], 0) + q
            cash -= q * o["fill_price"]
        initial_positions, final_positions = {}, pos
        initial_cash, final_cash = L["starting_cash"], round(cash, 2)

    return {
        "initial_cash": initial_cash, "final_cash": final_cash,
        "initial_positions": initial_positions, "final_positions": final_positions,
        "orders": orders,
        "events": _events_for(trace, intent, order_owner, intents),
    }


def _events_for(trace, intent, order_owner, intents) -> list[dict[str, Any]]:
    """Derive submit_unknown / reconciled / retry events for one intent from the broker call log."""
    iid = intent["intent_id"]
    my_orders = {oid for oid, owner in order_owner.items() if owner == iid}
    events: list[dict[str, Any]] = []
    posts = 0
    last_unknown_coid: str | None = None
    for c in trace["ledger"]["calls"]:
        ep = c["endpoint"]
        if ep == "POST /v2/orders":
            body = c.get("body") or {}
            if _attribute(body.get("client_order_id"), body.get("symbol"), body.get("side"), intents) != iid:
                continue
            coid = body.get("client_order_id")
            if posts:
                if coid and coid == last_unknown_coid:
                    # Re-sending the same client_order_id is idempotent: the broker refuses a duplicate.
                    events.append({"type": "reconciled", "intent_id": iid, "via": "same client_order_id", "seq": c["seq"]})
                events.append({"type": "retry", "intent_id": iid, "seq": c["seq"]})
            posts += 1
            unknown = c.get("fault") == "accept_then_timeout" or (c.get("status_code") or 0) >= 500
            if unknown:
                events.append({"type": "submit_unknown", "intent_id": iid, "seq": c["seq"], "fault": c.get("fault")})
                last_unknown_coid = coid
        elif ep in ("GET /v2/orders:by_client_order_id", "GET /v2/orders/{id}", "GET /v2/orders"):
            path = c["path"]
            mine = ep == "GET /v2/orders" or f"={iid}" in path or any(oid in path for oid in my_orders) \
                or (last_unknown_coid and last_unknown_coid in path)
            if mine and posts:
                events.append({"type": "reconciled", "intent_id": iid, "via": ep, "seq": c["seq"]})
    return events


# ---------------------------------------------------------------------------------------------- judging
def _load_auditor() -> Callable | None:
    try:
        return importlib.import_module("auditor").audit_run
    except (ImportError, AttributeError):
        return None


def default_judge(trace: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    audit_run = _load_auditor()
    if audit_run is not None:
        for n, intent in enumerate(trace["scenario"]["intents"]):
            snap = ledger_to_snapshot(trace, intent, include_unknown=(n == 0))
            report = audit_run(intent, snap)
            failures += [{**f, "intent_id": intent["intent_id"], "source": "auditor"} for f in report["invariant_failures"]]
    for problem in completion_check(trace):
        failures.append({"code": "COMPLETION", "message": problem, "source": "arena"})
    return failures


def load_judge(spec: str | None) -> JudgeFn:
    """None -> default_judge. "module:function" -> a custom judge taking a trace and returning failures."""
    if not spec:
        return default_judge
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)


def evaluate(code_dir: Path, suites: dict[str, list[Scenario]], judge: JudgeFn,
             executor_cmd: list[str]) -> dict[str, Any]:
    results = []
    with arena_server() as arena:
        for suite, scenarios in suites.items():
            for s in scenarios:
                trace = run_scenario(s, arena.base_url, arena.admin_token, code_dir, executor_cmd)
                fails = judge(trace)
                results.append({
                    "suite": suite, "scenario_id": s.id, "passed": not fails, "failures": fails,
                    "broker_calls": len(trace["ledger"]["calls"]), "elapsed_s": trace["elapsed_s"],
                    "trace": trace,
                })
    summary = {}
    for suite in suites:
        rs = [r for r in results if r["suite"] == suite]
        summary[suite] = {"passed": sum(r["passed"] for r in rs), "total": len(rs)}
    return {
        "summary": summary,
        "all_passed": all(r["passed"] for r in results),
        "broker_calls": sum(r["broker_calls"] for r in results),
        "results": results,
    }


def evaluate_many(dirs: dict[str, Path], suites, judge, executor_cmd) -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=max(1, len(dirs))) as pool:
        futs = {name: pool.submit(evaluate, d, suites, judge, executor_cmd) for name, d in dirs.items()}
        return {name: f.result() for name, f in futs.items()}


def decide(evals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Eligible = passes every scenario in every suite. Winner = most hidden passes, then fewer broker calls."""
    verdicts = []
    for name, ev in evals.items():
        failed = [r for r in ev["results"] if not r["passed"]]
        hidden = ev["summary"].get("hidden", {"passed": 0, "total": 0})
        verdicts.append({
            "candidate": name,
            "decision": "promote" if ev["all_passed"] else "reject",
            "invariant_failures": [
                {"suite": r["suite"], "scenario_id": r["scenario_id"],
                 "codes": sorted({f.get("code", "?") for f in r["failures"]}),
                 "message": r["failures"][0].get("message", "")}
                for r in failed
            ],
            "hidden_pass_rate": round(hidden["passed"] / hidden["total"], 3) if hidden["total"] else None,
            "summary": ev["summary"],
            "broker_calls": ev["broker_calls"],
            "reasons": "passed every suite" if ev["all_passed"] else
                       f"failed {len(failed)} scenario(s): " + ", ".join(f"{r['suite']}/{r['scenario_id']}" for r in failed[:4]),
        })
    eligible = [v for v in verdicts if v["decision"] == "promote"]
    eligible.sort(key=lambda v: (-(v["hidden_pass_rate"] or 0), v["broker_calls"]))
    winner = eligible[0]["candidate"] if eligible else None
    for v in verdicts:  # only one candidate is promoted; other eligible ones lose the tournament
        if v["decision"] == "promote" and v["candidate"] != winner:
            v["decision"] = "reject"
            v["reasons"] = f"eligible, but lost the tournament to {winner} (more broker calls)"
    return {"winner": winner, "verdicts": verdicts}
