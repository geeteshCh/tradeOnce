import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from harness2.core.spec import ChannelSpec

from auditor import audit_run
from harnessing.adapter import TradeOnceAdapter
from harnessing.demo import DEFAULT_HARNESS, DEMO_TASK, demo_probe, demo_verifier
from harnessing.models import HarnessTask, ProbeResult, VerificationOutcome


class TradeOnceAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        self.task = HarnessTask(
            id="adapter-task",
            prompt="Execute the supplied authorized intent once.",
            domain="execution",
            inputs={
                "intent": {"symbol": "AAPL", "qty": 1},
                "api_key": "must-not-enter-context",
            },
        )
        self.probe = MagicMock(
            return_value=ProbeResult(
                output="order accepted",
                trace="[ACTION] submit\n[OBSERVATION] accepted",
                data={"orders": ["order-1"]},
            )
        )
        self.verifier = MagicMock(
            return_value=VerificationOutcome(
                held_out_passed=2,
                held_out_total=2,
            )
        )
        channel = ChannelSpec(kind="claude_cli", model="test-model")
        self.adapter = TradeOnceAdapter(
            self.task,
            self.workspace,
            DEFAULT_HARNESS,
            self.probe,
            self.verifier,
            channels={"proposer": channel, "selector": channel},
        )

    def test_source_context_contains_only_sanitized_public_task_files(self):
        (self.workspace / ".env").write_text("PRIVATE=hidden", encoding="utf-8")

        source = self.adapter.task_files(self.task.id)

        self.assertEqual(
            sorted(path.name for path in source.iterdir()),
            ["inputs.json", "prompt.txt"],
        )
        rendered = "\n".join(path.read_text() for path in source.iterdir())
        self.assertNotIn("must-not-enter-context", rendered)
        self.assertNotIn("PRIVATE=hidden", rendered)
        self.assertEqual(
            json.loads((source / "inputs.json").read_text())["api_key"], "[redacted]"
        )

    def test_rejects_unknown_files_and_paths_outside_workspace(self):
        invalid_tree = DEFAULT_HARNESS | {"../.env": "not allowed"}
        with self.assertRaisesRegex(ValueError, "outside the editable surface"):
            self.adapter.validate_tree(invalid_tree)

        external = self.workspace.parent / "external-harness"
        external.mkdir(parents=True)
        for name, content in DEFAULT_HARNESS.items():
            (external / name).write_text(content, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the adapter workspace"):
            self.adapter.execute(self.task.id, external, "safe-run")
        with self.assertRaisesRegex(ValueError, "unsafe run id"):
            self.adapter.execute(self.task.id, None, "../../escape")

        linked = self.workspace / "linked-harness"
        linked.mkdir()
        for name, content in DEFAULT_HARNESS.items():
            (linked / name).write_text(content, encoding="utf-8")
        (linked / "toolguidance.md").unlink()
        (linked / "toolguidance.md").symlink_to(external / "toolguidance.md")
        with self.assertRaisesRegex(ValueError, "must not be symlinks"):
            self.adapter.execute(self.task.id, linked, "linked-run")
        self.probe.assert_not_called()

    def test_execute_is_idempotent_and_artifacts_are_validated(self):
        self.assertEqual(self.adapter.substrate.name, "tradeonce")
        self.assertEqual(self.adapter.substrate.surface().name, "opencode")
        run_dir = self.adapter.execute(self.task.id, None, "cell/run:1.base")
        cached = self.adapter.execute(self.task.id, None, "cell/run:1.base")

        self.assertEqual(cached, run_dir)
        self.probe.assert_called_once()
        self.assertEqual(
            {path.name for path in run_dir.iterdir()},
            {
                "harness.json",
                "output.txt",
                "probe.json",
                "trajectory.md",
            },
        )
        self.assertTrue(self.adapter.delivered(run_dir))
        self.assertEqual(self.adapter.deliverable_text(run_dir), "order accepted")
        self.assertEqual(
            self.adapter.trajectory(run_dir, obs_trunc=8),
            "[ACTION]",
        )
        self.assertEqual(
            self.adapter.score(run_dir, self.task.id),
            {"n_passed": 2, "n_criteria": 2, "all_pass": True},
        )
        self.verifier.assert_called_once()


class DemoPolicyFixtureTests(unittest.TestCase):
    def test_blind_retry_creates_real_duplicate_fill_failures(self):
        result = demo_probe(DEMO_TASK, DEFAULT_HARNESS)
        snapshot = result.data["snapshot"]
        self.assertIsInstance(snapshot, dict)
        intent = {
            "intent_id": f"{DEMO_TASK.id}-intent",
            "symbol": DEMO_TASK.inputs["symbol"],
            "side": "buy",
            "qty": DEMO_TASK.inputs["qty"],
        }
        report = audit_run(intent, snapshot)
        codes = {failure["code"] for failure in report["invariant_failures"]}

        self.assertEqual(len(snapshot["orders"]), 2)
        self.assertIn("OVERFILL", codes)
        self.assertIn("UNSAFE_RETRY", codes)
        self.assertNotIn("decision", result.model_dump_json())

    def test_never_retry_fails_private_transient_rejection(self):
        harness = DEFAULT_HARNESS | {
            "toolguidance.md": '{"retry_policy":"never_retry"}'
        }
        outcome = demo_verifier(DEMO_TASK, harness, demo_probe(DEMO_TASK, harness))

        self.assertEqual(outcome.decision, "reject")
        self.assertLess(outcome.held_out_passed, outcome.held_out_total)
        self.assertTrue(
            any("ACTUAL_FILL_MISMATCH" in code for code in outcome.invariant_failures)
        )

    def test_lookup_before_retry_passes_training_and_private_cases(self):
        harness = DEFAULT_HARNESS | {
            "toolguidance.md": '{"retry_policy":"lookup_before_retry"}'
        }
        probe = demo_probe(DEMO_TASK, harness)
        outcome = demo_verifier(DEMO_TASK, harness, probe)

        self.assertEqual(outcome.decision, "promote")
        self.assertEqual(outcome.held_out_passed, outcome.held_out_total)
        self.assertEqual(outcome.invariant_failures, [])


if __name__ == "__main__":
    unittest.main()
