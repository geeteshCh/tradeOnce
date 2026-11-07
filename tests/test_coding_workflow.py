import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from harnessing.adapter import TradeOnceAdapter
from harnessing.models import (
    HarnessFiles,
    HarnessTask,
    ProbeResult,
    VerificationOutcome,
)
from harnessing.proposer import GeminiChannel
from harnessing.runner import HarnessRunner, HarnessSettings
from storage import BuilderCandidate, HarnessMemory, VerificationResult
from storage.client import HARNESS_MEMORY, MongoStore

HARNESS = {
    "systemprompt.md": "execute safely",
    "guardrails.md": "never duplicate",
    "memory.md": "reconcile unknown outcomes",
    "toolguidance.md": "look up client order ids",
}
SOURCE = {
    "executor/run.py": "print('candidate')\n",
    "executor/LESSONS.md": "Reconcile first.\n",
}


def _verifier(
    _task: HarnessTask, _harness: HarnessFiles, observed: ProbeResult
) -> VerificationOutcome:
    if observed.git_ref == "reference-ref":
        return VerificationOutcome(
            held_out_passed=0,
            held_out_total=1,
            invariant_failures=["reference failed"],
        )
    return VerificationOutcome(held_out_passed=1, held_out_total=1)


class ReferenceProbeTests(unittest.TestCase):
    def test_adapter_uses_reference_only_for_base_execution(self) -> None:
        calls: list[str] = []

        def candidate(_task: HarnessTask, _harness: HarnessFiles) -> ProbeResult:
            calls.append("candidate")
            return ProbeResult(output="candidate", trace="candidate trace")

        def reference(_task: HarnessTask, _harness: HarnessFiles) -> ProbeResult:
            calls.append("reference")
            return ProbeResult(output="reference", trace="reference trace")

        with tempfile.TemporaryDirectory() as temporary_dir:
            adapter = TradeOnceAdapter(
                HarnessTask(id="reference-gate", prompt="improve executor"),
                Path(temporary_dir),
                HARNESS,
                candidate,
                _verifier,
                channels={
                    "proposer": GeminiChannel("google_genai", "test"),
                    "selector": GeminiChannel("google_genai", "test"),
                },
                reference_probe=reference,
            )
            adapter.execute("reference-gate", None, "base")
            adapter.execute("reference-gate", adapter.base_harness(), "candidate")

        self.assertEqual(calls, ["reference", "candidate"])


class _Store:
    def __init__(self) -> None:
        self.candidate: BuilderCandidate | None = None
        self.verification: VerificationResult | None = None
        self.memory: HarnessMemory | None = None

    def get_harness_memory(self, _scope: str) -> HarnessMemory | None:
        return self.memory

    def save_candidate(self, candidate: BuilderCandidate) -> str:
        self.candidate = candidate
        return candidate.id

    def save_verification(self, verification: VerificationResult) -> str:
        self.verification = verification
        return verification.id

    def save_harness_memory(
        self, memory: HarnessMemory, *, expected_revision: int = 0
    ) -> None:
        self.assert_proof(memory, expected_revision)
        self.memory = memory

    def assert_proof(self, memory: HarnessMemory, expected_revision: int) -> None:
        if self.candidate is None or self.verification is None:
            raise AssertionError("proof records must be saved first")
        if expected_revision != 0:
            raise AssertionError("unexpected prior revision")
        if memory.git_ref != self.candidate.git_ref:
            raise AssertionError("git refs differ")
        if memory.source_files != self.candidate.source_files:
            raise AssertionError("source files differ")


class RunnerLineageTests(unittest.TestCase):
    def test_selected_probe_owns_persisted_git_and_exact_source_bytes(self) -> None:
        store = _Store()

        def candidate(_task: HarnessTask, _harness: HarnessFiles) -> ProbeResult:
            return ProbeResult(
                output="safe",
                trace="candidate trace",
                git_ref="observed-candidate-ref",
                source_files=SOURCE,
            )

        def reference(_task: HarnessTask, _harness: HarnessFiles) -> ProbeResult:
            return ProbeResult(
                output="unsafe",
                trace="reference trace",
                git_ref="reference-ref",
                source_files={"executor/run.py": "print('reference')\n"},
            )

        class Framework:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
                self.errors: list[str] = []

            def to_dict(self) -> dict[str, object]:
                return self.payload

        def run_task(task_id: str, _k: int, *, adapter, **_kwargs):
            base_dir = adapter.execute(task_id, None, "base")
            candidate_harness = adapter.base_harness().parent / "candidate-harness"
            candidate_harness.mkdir()
            for name, content in HARNESS.items():
                changed = content + "\nrefined" if name == "memory.md" else content
                (candidate_harness / name).write_text(changed, encoding="utf-8")
            candidate_dir = adapter.execute(task_id, candidate_harness, "candidate")
            return Framework(
                {
                    "base": [{"run_dir": str(base_dir)}],
                    "selections": {
                        "seq": {
                            "ok": True,
                            "chosen_label": "c1",
                            "chosen_run_dir": str(candidate_dir),
                            "experience": {
                                "cue": "unknown outcome",
                                "lesson": "reconcile first",
                                "evidence": "candidate passed",
                            },
                        }
                    },
                    "errors": [],
                }
            )

        with tempfile.TemporaryDirectory() as temporary_dir:
            proposer = SimpleNamespace(spec=GeminiChannel("google_genai", "scripted"))
            runner = HarnessRunner(  # type: ignore[arg-type]
                store,
                proposer,
                HarnessSettings(
                    scope="coding",
                    git_ref="settings-fallback-ref",
                    runs_dir=Path(temporary_dir),
                ),
            )
            with patch("harnessing.runner.run_task", side_effect=run_task):
                result = runner.run(
                    HarnessTask(id="coding-lineage", prompt="improve executor"),
                    initial_harness=HARNESS,
                    probe=candidate,
                    verifier=_verifier,
                    reference_probe=reference,
                )

        self.assertEqual(result.decision, "promote")
        assert store.candidate is not None and store.memory is not None
        self.assertEqual(store.candidate.git_ref, "observed-candidate-ref")
        self.assertEqual(store.memory.git_ref, "observed-candidate-ref")
        self.assertEqual(store.candidate.source_files, SOURCE)
        self.assertEqual(store.memory.source_files, SOURCE)


class SourceProofTests(unittest.TestCase):
    def test_mongodb_memory_rejects_tampered_source_bytes(self) -> None:
        candidate = BuilderCandidate(
            _id="candidate",
            run_id="run",
            round=1,
            candidate="c1",
            git_ref="candidate-ref",
            harness_files=HARNESS,
            source_files=SOURCE,
        )
        verification = VerificationResult(
            _id="verification",
            candidate_id=candidate.id,
            decision="promote",
            hidden_pass_rate=1,
        )
        store = MongoStore.__new__(MongoStore)
        store.get_candidate = MagicMock(return_value=candidate)  # type: ignore[method-assign]
        store.get_verification = MagicMock(  # type: ignore[method-assign]
            return_value=verification
        )
        database = MagicMock()
        store._database = database
        memory = HarnessMemory(
            _id="coding",
            scope="coding",
            revision=1,
            candidate_id=candidate.id,
            verification_id=verification.id,
            git_ref=candidate.git_ref,
            harness_files=HARNESS,
            source_files={**SOURCE, "executor/run.py": "print('tampered')\n"},
        )

        with self.assertRaisesRegex(ValueError, "source files do not match"):
            store.save_harness_memory(memory)

        database[HARNESS_MEMORY].insert_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
