"""Deterministic retry-policy fixture, not a full trading agent or executor."""

from __future__ import annotations

import json
import random
import secrets
import string
from decimal import Decimal
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from auditor import audit_run
from harnessing.models import (
    HarnessFiles,
    HarnessTask,
    ProbeResult,
    VerificationOutcome,
    Verifier,
)

DEFAULT_HARNESS: HarnessFiles = {
    "systemprompt.md": "Execute each authorized intent at most once.",
    "guardrails.md": "Treat a submit timeout as an unknown outcome.",
    "memory.md": "Use broker ledger state as the source of truth.",
    "toolguidance.md": '{"retry_policy":"blind_retry"}',
}

DEMO_TASK = HarnessTask(
    id="retry-policy",
    prompt="Choose a declarative retry policy for one authorized trading intent.",
    domain="trade_execution",
    inputs={
        "accepted_before_timeout": True,
        "qty": 10,
        "symbol": "AAPL",
        "price": 100,
    },
)


class _RetryPolicy(BaseModel):
    """The complete executable policy surface for the deterministic fixture."""

    model_config = ConfigDict(extra="forbid")

    retry_policy: Literal["blind_retry", "lookup_before_retry", "never_retry"]


def demo_probe(task: HarnessTask, harness: HarnessFiles) -> ProbeResult:
    """Apply one declarative policy to the public accepted-then-timeout case."""
    policy = _policy(harness)
    intent = _intent_from_task(task)
    accepted = task.inputs.get("accepted_before_timeout") is True
    snapshot, trace = _simulate(intent, accepted=accepted, policy=policy)
    orders = cast(list[dict[str, object]], snapshot["orders"])
    order_ids = [str(order["order_id"]) for order in orders]
    return ProbeResult(
        output=json.dumps({"order_ids": order_ids}, sort_keys=True),
        trace="\n".join(trace),
        data={"snapshot": snapshot},
    )


def _generate_private_cases(
    seed: int | None,
) -> tuple[tuple[str, dict[str, object], bool], ...]:
    """Generate uncommitted cases once for fair replay within one trusted run."""
    rng: random.Random = secrets.SystemRandom() if seed is None else random.Random(seed)
    accepted_values = [True, True, False]
    rng.shuffle(accepted_values)
    symbols: set[str] = set()
    cases: list[tuple[str, dict[str, object], bool]] = []
    for index, accepted in enumerate(accepted_values, start=1):
        symbol = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        while symbol in symbols:
            symbol = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        symbols.add(symbol)
        cases.append(
            (
                f"case-{index}",
                {
                    "intent_id": f"private-{rng.getrandbits(64):016x}",
                    "symbol": symbol,
                    "side": "buy",
                    "qty": rng.randint(1, 20),
                    "price": rng.randint(5, 250),
                },
                accepted,
            )
        )
    return tuple(cases)


def make_demo_verifier(seed: int | None = None) -> Verifier:
    """Freeze runtime-private cases for baseline and candidate verification."""
    held_out = _generate_private_cases(seed)

    def verify(
        task: HarnessTask, harness: HarnessFiles, probe: ProbeResult
    ) -> VerificationOutcome:
        return _verify_cases(task, harness, probe, held_out)

    return verify


def _verify_cases(
    task: HarnessTask,
    harness: HarnessFiles,
    probe: ProbeResult,
    held_out: tuple[tuple[str, dict[str, object], bool], ...],
) -> VerificationOutcome:
    """Check training evidence and frozen private cases with the real auditor."""
    policy = _policy(harness)
    failures: list[str] = []
    reasons: list[str] = []

    training = probe.data.get("snapshot")
    if not isinstance(training, dict):
        raise TypeError("probe snapshot is missing or invalid")
    training_snapshot = cast(dict[str, object], training)
    training_failures = _failure_codes(_intent_from_task(task), training_snapshot)
    failures.extend(f"training:{code}" for code in training_failures)
    reasons.extend(f"training case failed {code}" for code in training_failures)

    passed = 0
    for name, intent, accepted in held_out:
        snapshot, _ = _simulate(intent, accepted=accepted, policy=policy)
        case_failures = _failure_codes(intent, snapshot, require_full_fill=True)
        if case_failures:
            failures.extend(f"{name}:{code}" for code in case_failures)
            reasons.extend(f"{name} failed {code}" for code in case_failures)
        else:
            passed += 1

    return VerificationOutcome(
        held_out_passed=passed,
        held_out_total=len(held_out),
        invariant_failures=failures,
        reasons=reasons,
    )


demo_verifier = make_demo_verifier()


def _policy(harness: HarnessFiles) -> str:
    try:
        return _RetryPolicy.model_validate_json(harness["toolguidance.md"]).retry_policy
    except KeyError as exc:
        raise ValueError("toolguidance.md is required") from exc


def _intent_from_task(task: HarnessTask) -> dict[str, object]:
    required = ("symbol", "qty", "price")
    if any(name not in task.inputs for name in required):
        raise ValueError("task inputs must contain symbol, qty, and price")
    return {
        "intent_id": f"{task.id}-intent",
        "symbol": task.inputs["symbol"],
        "side": "buy",
        "qty": task.inputs["qty"],
        "price": task.inputs["price"],
    }


def _simulate(
    intent: dict[str, object], *, accepted: bool, policy: str
) -> tuple[dict[str, object], list[str]]:
    """Return broker ledger truth for an accepted or rejected timed-out submission."""
    intent_id = str(intent["intent_id"])
    symbol = str(intent["symbol"])
    side = str(intent["side"])
    price = Decimal(str(intent.get("price", 10)))
    orders: list[dict[str, object]] = []
    events: list[dict[str, object]] = [
        {"type": "submit_unknown", "intent_id": intent_id}
    ]
    trace = ["[ACTION] submit authorized intent", "[OBSERVATION] timeout"]

    if accepted:
        orders.append(_filled_order("original", intent, price))

    should_retry = policy == "blind_retry"
    if policy == "lookup_before_retry":
        events.append({"type": "reconciled", "intent_id": intent_id})
        trace.extend(["[ACTION] lookup broker ledger", "[OBSERVATION] reconciled"])
        should_retry = not accepted
    if should_retry:
        events.append({"type": "retry", "intent_id": intent_id})
        orders.append(_filled_order("retry", intent, price))
        trace.append("[ACTION] retry submission")

    filled = sum(Decimal(str(order["filled_qty"])) for order in orders)
    signed = filled if side == "buy" else -filled
    cash_delta = -(filled * price) if side == "buy" else filled * price
    snapshot: dict[str, object] = {
        "initial_cash": 10_000,
        "final_cash": float(Decimal(10000) + cash_delta),
        "initial_positions": {symbol: 0},
        "final_positions": {symbol: float(signed)},
        "orders": orders,
        "events": events,
    }
    return snapshot, trace


def _filled_order(
    order_id: str, intent: dict[str, object], price: Decimal
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "intent_id": intent["intent_id"],
        "symbol": intent["symbol"],
        "side": intent["side"],
        "filled_qty": intent["qty"],
        "fill_price": float(price),
        "status": "filled",
    }


def _failure_codes(
    intent: dict[str, object],
    snapshot: dict[str, object],
    *,
    require_full_fill: bool = False,
) -> list[str]:
    report = audit_run(intent, snapshot)
    failures = [str(item["code"]) for item in report["invariant_failures"]]
    if require_full_fill and Decimal(report["total_filled_qty"]) != Decimal(
        str(intent["qty"])
    ):
        failures.append("ACTUAL_FILL_MISMATCH")
    return failures
