import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness2.core.proposer import Proposer, Reply

from arena import Scenario
from builder.codegen import CodePatch
from builder.evidence import evaluate, public_scenarios
from builder.sandbox import SandboxRunner
from builder.workflow import CodingExperiment, CodingSettings
from harnessing.proposer import GeminiChannel
from storage import BuilderCandidate, HarnessMemory, VerificationResult

ROOT = Path(__file__).resolve().parents[1]
SCRIPTED_SOURCE = (ROOT / "demo/scripted/round_01/a/executor/run.py").read_text(
    encoding="utf-8"
)
SCRIPTED_LESSONS = (ROOT / "demo/scripted/round_01/a/executor/LESSONS.md").read_text(
    encoding="utf-8"
)
SEED_SOURCE = (ROOT / "demo/seed/executor/run.py").read_text(encoding="utf-8")


class ScriptedProposer(Proposer):  # type: ignore[misc]
    """Drive actual Harness2 scheduling with deterministic test-only edits."""

    def __init__(self) -> None:
        super().__init__(
            spec=GeminiChannel("google_genai", "scripted-test"), attempts=1
        )

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
    ) -> Reply:
        del mission, system, timeout, max_attempts
        workspace = None
        if harvest is not None:
            if harvest != "context/harness" or context_dir is None:
                raise AssertionError("unexpected harvest request")
            workspace = Path(context_dir) / "harness"
            guidance = workspace / "toolguidance.md"
            guidance.write_text(
                guidance.read_text(encoding="utf-8")
                + "\nUse a stable client order ID and reconcile before retrying.",
                encoding="utf-8",
            )
            text = "<notes>Added reconciliation guidance.</notes>"
        elif label.startswith("experience."):
            text = (
                "<experience>\n"
                "cue: submission outcome is unknown after transport failure\n"
                "lesson: reconcile the stable client order ID before retrying\n"
                "evidence: public and independent cases passed after reconciliation\n"
                "confidence: high\n"
                "</experience>"
            )
        else:
            text = "scripted test response"
        reply = Reply(
            label=label,
            text=text,
            ok=True,
            attempts=1,
            channel="google_genai",
            workspace=workspace,
        )
        self._calls.append(reply)
        return reply


class InMemoryStore:
    """Small proof-enforcing store with the MongoStore methods the workflow uses."""

    def __init__(self) -> None:
        self.candidates: dict[str, BuilderCandidate] = {}
        self.verifications: dict[str, VerificationResult] = {}
        self.memories: dict[str, HarnessMemory] = {}

    def get_harness_memory(self, scope: str) -> HarnessMemory | None:
        return self.memories.get(scope)

    def save_candidate(self, candidate: BuilderCandidate) -> str:
        self.candidates[candidate.id] = candidate
        return candidate.id

    def get_candidate(self, candidate_id: str) -> BuilderCandidate | None:
        return self.candidates.get(candidate_id)

    def save_verification(self, verification: VerificationResult) -> str:
        self.verifications[verification.id] = verification
        return verification.id

    def get_verification(self, verification_id: str) -> VerificationResult | None:
        return self.verifications.get(verification_id)

    def save_harness_memory(
        self, memory: HarnessMemory, *, expected_revision: int = 0
    ) -> None:
        candidate = self.candidates.get(memory.candidate_id)
        verification = self.verifications.get(memory.verification_id)
        if memory.revision != expected_revision + 1:
            raise ValueError("stale memory revision")
        if candidate is None or verification is None:
            raise ValueError("missing memory proof")
        if candidate.git_ref != memory.git_ref:
            raise ValueError("git ref proof mismatch")
        if candidate.source_files != memory.source_files:
            raise ValueError("source proof mismatch")
        if candidate.harness_files != memory.harness_files:
            raise ValueError("harness proof mismatch")
        if (
            verification.candidate_id != candidate.id
            or verification.decision != "promote"
        ):
            raise ValueError("verification proof mismatch")
        self.memories[memory.scope] = memory


class DeterministicSandbox:
    """Return realistic Arena ledgers based only on source and requested scenarios."""

    def __init__(self) -> None:
        self.fail_private = False

    def run_scenario(self, source_dir: Path, scenario: Scenario | dict) -> dict:
        checked = Scenario.model_validate(scenario)
        source = (Path(source_dir) / "executor/run.py").read_text(encoding="utf-8")
        generated = "Scripted candidate r1-a" in source
        should_pass = generated and not (
            self.fail_private and checked.id.startswith("private-")
        )
        return _trace(checked, passing=should_pass)


class ScriptedGenerator:
    """Return a repository fixture while recording exactly what Builder observed."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], dict[str, str], str]] = []

    def __call__(
        self,
        source: dict[str, str],
        harness: dict[str, str],
        feedback: str,
    ) -> CodePatch:
        self.calls.append((dict(source), dict(harness), feedback))
        return CodePatch(
            run_source=SCRIPTED_SOURCE,
            lessons=SCRIPTED_LESSONS,
            summary="Test-only scripted reconciliation repair.",
        )


def _trace(scenario: Scenario, *, passing: bool) -> dict:
    orders = []
    claims = []
    positions: dict[str, float] = {}
    cash = scenario.starting_cash
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
    if passing and scenario.expected_positions == {}:
        orders = []
        claims = [
            {
                "intent_id": intent.intent_id,
                "status": "rejected",
                "broker_order_id": None,
                "filled_qty": 0,
            }
            for intent in scenario.intents
        ]
        positions = {}
        cash = scenario.starting_cash
    elif not passing:
        orders = []
        claims = []
        positions = {}
        cash = scenario.starting_cash
    return {
        "scenario": scenario.model_dump(),
        "executor_claims": claims,
        "claims_error": None if passing else "scripted executor failure",
        "exit_code": 0 if passing else 1,
        "timed_out": False,
        "elapsed_s": 0.01,
        "stderr_tail": "",
        "ledger": {
            "starting_cash": scenario.starting_cash,
            "cash": cash,
            "positions": {symbol: qty for symbol, qty in positions.items() if qty},
            "orders": orders,
            "calls": [],
        },
    }


class CodingExperimentTests(unittest.TestCase):
    def experiment(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = InMemoryStore()
        generator = ScriptedGenerator()
        sandbox = DeterministicSandbox()
        experiment = CodingExperiment(  # type: ignore[arg-type]
            store,
            ScriptedProposer(),
            generator,
            CodingSettings(root=Path(temporary.name), scope="coding-test"),
            sandbox=sandbox,  # type: ignore[arg-type]
        )
        return experiment, store, generator, sandbox

    def test_first_run_persists_private_gated_source_and_warm_transfer(self) -> None:
        experiment, store, generator, _sandbox = self.experiment()

        first = experiment.run_task("timeout", reuse_memory=False)

        self.assertEqual(first.decision, "promote")
        self.assertEqual(first.mode, "refinement")
        self.assertEqual(first.patch_proposals, 3)
        self.assertEqual(first.hidden_passed, first.hidden_total)
        memory = store.get_harness_memory("coding-test")
        self.assertIsNotNone(memory)
        assert memory is not None
        candidate = store.get_candidate(first.candidate_id)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(memory.git_ref, first.git_ref)
        self.assertEqual(memory.git_ref, candidate.git_ref)
        self.assertEqual(memory.source_files, candidate.source_files)
        self.assertEqual(memory.source_files["executor/run.py"], SCRIPTED_SOURCE)
        private_path = experiment.root / "private" / "timeout.json"
        self.assertEqual(private_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)
        private_ids = [item["id"] for item in json.loads(private_path.read_text())]
        self.assertTrue(
            all(
                private_id not in feedback
                for _source, _harness, feedback in generator.calls
                for private_id in private_ids
            )
        )
        verification = store.get_verification(first.verification_id)
        self.assertIsNotNone(verification)
        assert verification is not None
        self.assertTrue(
            any(
                "Frozen held-out suite SHA256:" in reason
                for reason in verification.reasons
            )
        )

        call_count = len(generator.calls)
        transfer = experiment.run_task("server_error", reuse_memory=True)

        self.assertEqual(transfer.decision, "promote")
        self.assertEqual(transfer.mode, "memory_reuse")
        self.assertEqual(transfer.patch_proposals, 1)
        self.assertEqual(transfer.loaded_memory_revision, 1)
        self.assertEqual(transfer.memory_revision, 2)
        self.assertEqual(len(generator.calls) - call_count, 1)
        transfer_source = generator.calls[-1][0]
        self.assertEqual(transfer_source["executor/run.py"], SEED_SOURCE)
        self.assertEqual(
            transfer_source["executor/LESSONS.md"],
            memory.source_files["executor/LESSONS.md"],
        )
        refreshed = store.get_harness_memory("coding-test")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        transfer_candidate = store.get_candidate(transfer.candidate_id)
        self.assertIsNotNone(transfer_candidate)
        assert transfer_candidate is not None
        self.assertEqual(refreshed.revision, 2)
        self.assertEqual(refreshed.source_files, transfer_candidate.source_files)
        self.assertEqual(refreshed.git_ref, transfer.git_ref)

        call_count = len(generator.calls)
        third = experiment.run_task("timeout", reuse_memory=True)

        self.assertEqual(third.decision, "promote")
        self.assertEqual(third.mode, "memory_reuse")
        self.assertEqual(third.patch_proposals, 1)
        self.assertEqual(third.loaded_memory_revision, 2)
        self.assertEqual(third.memory_revision, 3)
        self.assertEqual(len(generator.calls) - call_count, 1)
        self.assertEqual(
            generator.calls[-1][0]["executor/LESSONS.md"],
            refreshed.source_files["executor/LESSONS.md"],
        )

    def test_hidden_failure_refuses_first_promotion_and_memory(self) -> None:
        experiment, store, _generator, sandbox = self.experiment()
        sandbox.fail_private = True

        result = experiment.run_task("timeout", reuse_memory=False)

        self.assertEqual(result.decision, "reject")
        self.assertIsNone(result.version)
        self.assertLess(result.hidden_passed, result.hidden_total)
        self.assertIsNone(store.get_harness_memory("coding-test"))

    def test_failed_reuse_falls_back_but_keeps_approved_memory(self) -> None:
        experiment, store, _generator, sandbox = self.experiment()
        first = experiment.run_task("timeout", reuse_memory=False)
        approved = store.get_harness_memory("coding-test")
        self.assertEqual(first.decision, "promote")
        self.assertIsNotNone(approved)
        prior_candidate_ids = set(store.candidates)
        sandbox.fail_private = True

        transfer = experiment.run_task("server_error", reuse_memory=True)

        self.assertEqual(transfer.mode, "reuse_then_refinement")
        self.assertEqual(transfer.decision, "reject")
        self.assertEqual(transfer.patch_proposals, 4)
        self.assertEqual(store.get_harness_memory("coding-test"), approved)
        new_candidates = [
            candidate
            for candidate_id, candidate in store.candidates.items()
            if candidate_id not in prior_candidate_ids
        ]
        reuse_candidates = [
            candidate
            for candidate in new_candidates
            if candidate.summary.startswith("Builder reuse:")
        ]
        self.assertEqual(len(reuse_candidates), 1)
        reuse_verdicts = [
            verdict
            for verdict in store.verifications.values()
            if verdict.candidate_id == reuse_candidates[0].id
        ]
        self.assertEqual(len(reuse_verdicts), 1)
        self.assertEqual(reuse_verdicts[0].decision, "reject")


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
    "requires macOS sandbox-exec",
)
class RealSandboxFixtureTests(unittest.TestCase):
    def test_scripted_fixture_passes_real_timeout_arena_case(self) -> None:
        """Prove the test-only repair is executable, not fabricated evidence."""
        with tempfile.TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir)
            executor = source / "executor"
            executor.mkdir()
            (executor / "run.py").write_text(SCRIPTED_SOURCE, encoding="utf-8")
            evidence = evaluate(
                source,
                {"training": public_scenarios("timeout")["training"]},
                SandboxRunner(timeout_s=10),
            )

        self.assertTrue(evidence.all_passed)
        self.assertEqual(evidence.passed, 1)


if __name__ == "__main__":
    unittest.main()
