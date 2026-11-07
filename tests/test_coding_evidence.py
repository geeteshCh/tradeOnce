import tempfile
import unittest
from pathlib import Path

from arena import Scenario
from builder.evidence import (
    ScenarioEvidence,
    SuiteEvidence,
    evaluate,
    make_private_scenarios,
    public_scenarios,
)


class TraceSandbox:
    """Return requested prebuilt trusted traces through the sandbox interface."""

    def __init__(self, traces: dict[str, dict]) -> None:
        self.traces = traces
        self.calls: list[tuple[Path, str]] = []

    def run_scenario(self, source_dir: Path, scenario: Scenario | dict) -> dict:
        checked = Scenario.model_validate(scenario)
        self.calls.append((source_dir, checked.id))
        return self.traces[checked.id]


def passing_trace(scenario: Scenario) -> dict:
    orders = []
    positions: dict[str, float] = {}
    cash = scenario.starting_cash
    claims = []
    for index, intent in enumerate(scenario.intents, start=1):
        price = scenario.prices.get(intent.symbol, 100.0)
        signed = intent.qty if intent.side == "buy" else -intent.qty
        positions[intent.symbol] = positions.get(intent.symbol, 0) + signed
        cash -= signed * price
        orders.append(
            {
                "id": f"order-{index}",
                "client_order_id": intent.intent_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "filled_qty": float(intent.qty),
                "filled_avg_price": price,
                "status": "filled",
            }
        )
        claims.append(
            {
                "intent_id": intent.intent_id,
                "status": "filled",
                "broker_order_id": f"order-{index}",
                "filled_qty": intent.qty,
            }
        )
    return {
        "scenario": scenario.model_dump(),
        "executor_claims": claims,
        "claims_error": None,
        "exit_code": 0,
        "timed_out": False,
        "elapsed_s": 0.01,
        "stderr_tail": "",
        "ledger": {
            "starting_cash": scenario.starting_cash,
            "cash": cash,
            "positions": {key: value for key, value in positions.items() if value},
            "orders": orders,
            "calls": [],
        },
    }


class EvidenceModelTests(unittest.TestCase):
    def test_summary_properties_and_public_redaction(self):
        evidence = SuiteEvidence(
            results=[
                ScenarioEvidence(
                    suite="training",
                    scenario_id="public-1",
                    passed=True,
                    broker_calls=2,
                    expected_positions={"AAPL": 1},
                    final_positions={"AAPL": 1},
                ),
                ScenarioEvidence(
                    suite="hidden",
                    scenario_id="private-secret",
                    passed=False,
                    failures=["private failure detail"],
                    broker_calls=3,
                    expected_positions={"MSFT": 2},
                    final_positions={},
                ),
            ]
        )

        self.assertEqual(evidence.passed, 1)
        self.assertEqual(evidence.total, 2)
        self.assertFalse(evidence.all_passed)
        self.assertEqual(evidence.broker_calls, 5)
        self.assertEqual(len(evidence.failures), 1)
        self.assertEqual(
            SuiteEvidence.model_validate_json(evidence.model_dump_json()), evidence
        )
        rendered = evidence.to_public()
        self.assertEqual(rendered["total"], 1)
        self.assertNotIn("private-secret", str(rendered))
        self.assertNotIn("private failure detail", str(rendered))


class EvidenceEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.source_dir = Path(self.temporary_directory.name)

    def test_evaluate_returns_broker_grounded_evidence(self):
        scenario = Scenario(
            id="clean",
            intents=[{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 2}],
            prices={"AAPL": 50},
        )
        sandbox = TraceSandbox({scenario.id: passing_trace(scenario)})

        evidence = evaluate(self.source_dir, {"training": [scenario]}, sandbox)

        self.assertTrue(evidence.all_passed)
        self.assertEqual(evidence.passed, 1)
        self.assertEqual(evidence.results[0].expected_positions, {"AAPL": 2.0})
        self.assertEqual(evidence.results[0].final_positions, {"AAPL": 2.0})
        self.assertEqual(sandbox.calls, [(self.source_dir.resolve(), "clean")])

    def test_exact_fill_rejects_offsetting_fake_claims(self):
        scenario = Scenario(
            id="offsetting",
            intents=[
                {"intent_id": "buy", "symbol": "AAPL", "side": "buy", "qty": 1},
                {"intent_id": "sell", "symbol": "AAPL", "side": "sell", "qty": 1},
            ],
        )
        trace = passing_trace(scenario)
        trace["ledger"]["orders"] = []
        trace["ledger"]["positions"] = {}
        trace["ledger"]["cash"] = scenario.starting_cash
        sandbox = TraceSandbox({scenario.id: trace})

        evidence = evaluate(self.source_dir, {"hidden": [scenario]}, sandbox)

        self.assertFalse(evidence.all_passed)
        self.assertEqual(len(evidence.results[0].failures), 2)
        self.assertTrue(
            all("FULL_FILL" in failure for failure in evidence.results[0].failures)
        )

    def test_sandbox_must_return_the_requested_scenario(self):
        requested = Scenario(
            id="requested",
            intents=[{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 1}],
        )
        different = requested.model_copy(update={"id": "different"})
        sandbox = TraceSandbox({requested.id: passing_trace(different)})

        with self.assertRaisesRegex(ValueError, "does not match"):
            evaluate(self.source_dir, {"training": [requested]}, sandbox)

    def test_non_list_executor_claims_are_a_protocol_failure(self):
        scenario = Scenario(
            id="bad-claims",
            intents=[{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 1}],
        )
        trace = passing_trace(scenario)
        trace["executor_claims"] = None
        sandbox = TraceSandbox({scenario.id: trace})

        evidence = evaluate(self.source_dir, {"training": [scenario]}, sandbox)

        self.assertFalse(evidence.all_passed)
        self.assertTrue(
            any("PROTOCOL" in failure for failure in evidence.results[0].failures)
        )


class ScenarioFactoryTests(unittest.TestCase):
    def test_public_scenarios_select_task_case_and_fixed_regressions(self):
        timeout = public_scenarios("timeout")
        server_error = public_scenarios("server_error")

        self.assertEqual(
            [scenario.id for scenario in timeout["training"]],
            ["t02_accept_then_timeout"],
        )
        self.assertEqual(
            [scenario.id for scenario in server_error["training"]],
            ["t03_accept_then_500"],
        )
        self.assertEqual(
            [scenario.id for scenario in timeout["regression"]],
            ["t01_clean", "t04_transient_500", "t09_real_reject"],
        )

    def test_private_scenarios_are_fresh_validated_and_never_written(self):
        before = set(self.source_dir_files())
        first = make_private_scenarios("timeout")
        second = make_private_scenarios("timeout")
        transfer = make_private_scenarios("server_error")

        self.assertEqual(len(first), 5)
        self.assertEqual(len(transfer), 5)
        self.assertTrue(all(isinstance(scenario, Scenario) for scenario in first))
        self.assertTrue(
            {scenario.id for scenario in first}.isdisjoint(
                scenario.id for scenario in second
            )
        )
        self.assertEqual(
            {rule.fault for scenario in first for rule in scenario.faults},
            {
                "accept_then_timeout",
                "accept_then_500",
                "transient_500",
                "rate_limit_429",
                "real_reject",
            },
        )
        self.assertIn(
            "accept_then_500",
            {rule.fault for scenario in transfer for rule in scenario.faults},
        )
        self.assertEqual(before, set(self.source_dir_files()))

    @staticmethod
    def source_dir_files() -> list[Path]:
        return list(Path(__file__).resolve().parents[1].glob("private-*.json"))


if __name__ == "__main__":
    unittest.main()
