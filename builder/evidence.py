"""Typed, broker-grounded evidence for coding Builder candidates."""

from __future__ import annotations

import random
import secrets
import string
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from arena import Scenario
from auditor import audit_run
from builder.sandbox import SandboxRunner
from loop.judge import default_judge, ledger_to_snapshot

SuiteName = Literal["training", "regression", "hidden"]
TaskKind = Literal["timeout", "server_error"]

_ROOT = Path(__file__).resolve().parents[1]
_TRAINING_DIR = _ROOT / "arena" / "scenarios" / "training"
_PUBLIC_REGRESSION = (
    "t01_clean.json",
    "t04_transient_500.json",
    "t09_real_reject.json",
)
_PUBLIC_TRAINING = {
    "timeout": "t02_accept_then_timeout.json",
    "server_error": "t03_accept_then_500.json",
}


class ScenarioEvidence(BaseModel):
    """Trusted outcome and ledger facts for one isolated scenario execution."""

    model_config = ConfigDict(extra="forbid")

    suite: SuiteName
    scenario_id: str = Field(min_length=1)
    passed: bool
    failures: list[str] = Field(default_factory=list)
    broker_calls: int = Field(ge=0)
    expected_positions: dict[str, float]
    final_positions: dict[str, float]


class SuiteEvidence(BaseModel):
    """Aggregate trusted evidence across public and private scenario suites."""

    model_config = ConfigDict(extra="forbid")

    results: list[ScenarioEvidence] = Field(default_factory=list)

    @property
    def passed(self) -> int:
        """Return the number of scenarios with no trusted failure."""
        return sum(result.passed for result in self.results)

    @property
    def total(self) -> int:
        """Return the number of evaluated scenarios."""
        return len(self.results)

    @property
    def all_passed(self) -> bool:
        """Return whether at least one scenario ran and every scenario passed."""
        return bool(self.results) and self.passed == self.total

    @property
    def broker_calls(self) -> int:
        """Return total broker calls across all scenario executions."""
        return sum(result.broker_calls for result in self.results)

    @property
    def failures(self) -> list[str]:
        """Return stable suite/scenario-prefixed failure descriptions."""
        return [
            f"{result.suite}/{result.scenario_id}: {failure}"
            for result in self.results
            for failure in result.failures
        ]

    def to_public(self) -> dict[str, Any]:
        """Serialize public evidence while omitting every hidden-case detail."""
        public_results = [result for result in self.results if result.suite != "hidden"]
        public = SuiteEvidence(results=public_results)
        return {
            "results": [result.model_dump(mode="json") for result in public.results],
            "passed": public.passed,
            "total": public.total,
            "all_passed": public.all_passed,
            "broker_calls": public.broker_calls,
            "failures": public.failures,
        }


def evaluate(
    source_dir: Path,
    suites: dict[str, list[Scenario]],
    sandbox: SandboxRunner,
) -> SuiteEvidence:
    """Run immutable source through isolated Arena cases and score trusted ledgers.

    The sandbox owns process, filesystem, environment, and Arena isolation. Executor
    claims remain untrusted; pass/fail evidence comes from the Arena ledger, the
    independent auditor, completion checks, and an explicit per-intent fill check.
    """
    source = Path(source_dir).resolve(strict=True)
    results: list[ScenarioEvidence] = []
    for raw_suite, scenarios in suites.items():
        suite = _suite_name(raw_suite)
        for scenario in scenarios:
            trace = sandbox.run_scenario(source, scenario)
            checked = Scenario.model_validate(trace["scenario"])
            if checked != scenario:
                raise ValueError(
                    "sandbox trace scenario does not match requested scenario"
                )
            failures = _strict_failures(trace, checked)
            results.append(
                ScenarioEvidence(
                    suite=suite,
                    scenario_id=checked.id,
                    passed=not failures,
                    failures=failures,
                    broker_calls=len(trace["ledger"]["calls"]),
                    expected_positions=_positions(checked.intended_positions()),
                    final_positions=_positions(trace["ledger"]["positions"]),
                )
            )
    return SuiteEvidence(results=results)


def public_scenarios(task_kind: TaskKind) -> dict[str, list[Scenario]]:
    """Load the fixed public training case and its safety regressions."""
    training = [_load_scenario(_PUBLIC_TRAINING[task_kind])]
    regression = [_load_scenario(name) for name in _PUBLIC_REGRESSION]
    return {"training": training, "regression": regression}


def make_private_scenarios(task_kind: TaskKind) -> list[Scenario]:
    """Create fresh in-memory held-out variants without writing fixture files."""
    rng = secrets.SystemRandom()
    token = uuid4().hex[:12]
    scenarios = [
        _private_case(
            rng,
            token,
            "timeout",
            "accept_then_timeout",
            params={"delay_s": round(rng.uniform(0.8, 1.4), 3)},
        ),
        _private_case(rng, token, "transient", "transient_500"),
        _private_case(rng, token, "rate-limit", "rate_limit_429"),
        _private_case(
            rng,
            token,
            "reject",
            "real_reject",
            nth=list(range(1, 7)),
            expected_positions={},
        ),
        _private_case(
            rng,
            token,
            f"{task_kind}-accept-500",
            "accept_then_500",
        ),
    ]
    return scenarios


def _strict_failures(trace: dict[str, Any], scenario: Scenario) -> list[str]:
    """Require auditor checks plus exact fills for ordinary execution intents."""
    checked_trace = trace
    failures: list[str] = []
    if not isinstance(trace.get("executor_claims"), list):
        failures.append("arena:PROTOCOL:-: executor claims must be a JSON list")
        checked_trace = {**trace, "executor_claims": []}
    failures.extend(_failure_text(failure) for failure in default_judge(checked_trace))
    for index, intent_model in enumerate(scenario.intents):
        intent = intent_model.model_dump()
        snapshot = ledger_to_snapshot(trace, intent, include_unknown=index == 0)
        report = audit_run(intent, snapshot)
        for failure in report["invariant_failures"]:
            text = _failure_text(
                {**failure, "intent_id": intent_model.intent_id, "source": "auditor"}
            )
            if text not in failures:
                failures.append(text)
        if scenario.expected_positions is None and Decimal(
            report["total_filled_qty"]
        ) != Decimal(intent_model.qty):
            failures.append(
                "auditor:FULL_FILL:"
                f"{intent_model.intent_id}: filled {report['total_filled_qty']} "
                f"of required {intent_model.qty}"
            )
    return failures


def _failure_text(failure: dict[str, Any]) -> str:
    source = str(failure.get("source") or "judge")
    code = str(failure.get("code") or "UNKNOWN")
    intent = str(failure.get("intent_id") or "-")
    message = str(failure.get("message") or "verification failure")
    return f"{source}:{code}:{intent}: {message}"


def _suite_name(value: str) -> SuiteName:
    if value not in {"training", "regression", "hidden"}:
        raise ValueError(f"unsupported evidence suite: {value}")
    return cast(SuiteName, value)


def _load_scenario(filename: str) -> Scenario:
    return Scenario.model_validate_json(
        (_TRAINING_DIR / filename).read_text(encoding="utf-8")
    )


def _private_case(
    rng: random.Random,
    token: str,
    label: str,
    fault: str,
    *,
    nth: int | list[int] = 1,
    params: dict[str, Any] | None = None,
    expected_positions: dict[str, float] | None = None,
) -> Scenario:
    base = _load_scenario("t01_clean.json").model_copy(deep=True)
    symbol = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
    quantity = rng.randint(1, 12)
    side = rng.choice(("buy", "sell"))
    values: dict[str, Any] = base.model_dump()
    values.update(
        {
            "id": f"private-{token}-{label}",
            "seed": rng.randrange(1, 2**31),
            "description": "Runtime-private broker behavior variant.",
            "intents": [
                {
                    "intent_id": f"intent-{uuid4().hex[:12]}",
                    "symbol": symbol,
                    "side": side,
                    "qty": quantity,
                }
            ],
            "faults": [
                {
                    "match": "POST /v2/orders",
                    "nth": nth,
                    "fault": fault,
                    "params": params or {},
                }
            ],
            "prices": {symbol: float(rng.randint(20, 500))},
            "starting_cash": float(rng.randint(100_000, 250_000)),
        }
    )
    if expected_positions is not None:
        values["expected_positions"] = expected_positions
    return Scenario.model_validate(values)


def _positions(values: dict[str, Any]) -> dict[str, float]:
    return {
        str(symbol): float(quantity) for symbol, quantity in values.items() if quantity
    }
