import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness2.core.proposer import Proposer, ProposerError, Reply

from harnessing.demo import DEFAULT_HARNESS, demo_probe, make_demo_verifier
from harnessing.models import HarnessFiles, HarnessTask, ProbeResult
from harnessing.proposer import GeminiChannel
from harnessing.runner import HarnessRunner, HarnessSettings
from providers import DecisionResponse, SubQClient
from storage import BuilderCandidate, HarnessMemory, VerificationResult


class ScriptedProposer(Proposer):  # type: ignore[misc]
    """Drive real Harness2 scheduling with deterministic, inspectable replies."""

    def __init__(
        self, policy: str = "lookup_before_retry", *, fail_proposals: bool = False
    ) -> None:
        super().__init__(
            spec=GeminiChannel(kind="google_genai", model="scripted", timeout=30),
            attempts=1,
        )
        self.policy = policy
        self.fail_proposals = fail_proposals
        self.record_calls: list[str] = []
        self.context_history: list[dict[str, str]] = []

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
        """Return a valid edit or lesson while recording only safe public context."""
        del mission, system, timeout, max_attempts
        self.record_calls.append(label)
        root = Path(context_dir) if context_dir is not None else None
        self.context_history.append(self._public_context(root))

        if harvest is not None:
            if self.fail_proposals:
                raise ProposerError(f"[{label}] scripted proposal failure", attempts=1)
            if harvest != "context/harness" or root is None:
                raise AssertionError("unexpected Harness2 harvest request")
            workspace = root / "harness"
            (workspace / "toolguidance.md").write_text(
                f'{{"retry_policy":"{self.policy}"}}', encoding="utf-8"
            )
            return self._reply(label, "<notes>scripted policy edit</notes>", workspace)

        if label.startswith("experience."):
            text = (
                "<experience>\n"
                "cue: submission timeout leaves broker outcome unknown\n"
                "lesson: reconcile broker ledger before retry\n"
                "evidence: lookup-before-retry passed training and private cases\n"
                "confidence: high\n"
                "</experience>\n"
                "<edits>Changed retry_policy to require reconciliation.</edits>"
            )
            return self._reply(label, text)

        if label.startswith("select."):
            return self._reply(
                label,
                "<why>scripted deterministic choice</why>\n```choice\nk1\n```",
            )
        return self._reply(label, "scripted response")

    def _reply(self, label: str, text: str, workspace: Path | None = None) -> Reply:
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

    @staticmethod
    def _public_context(root: Path | None) -> dict[str, str]:
        """Snapshot readable context while excluding environment-shaped paths."""
        if root is None:
            return {}
        files: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part.startswith(".env") for part in relative.parts):
                continue
            if path.is_file():
                files[relative.as_posix()] = path.read_text(encoding="utf-8")
        return files


class InMemoryStore:
    """Minimal typed store enforcing the same lineage and revision rules as Mongo."""

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

    def save_verification(self, result: VerificationResult) -> str:
        self.verifications[result.id] = result
        return result.id

    def get_verification(self, result_id: str) -> VerificationResult | None:
        return self.verifications.get(result_id)

    def save_harness_memory(
        self, memory: HarnessMemory, *, expected_revision: int = 0
    ) -> None:
        candidate = self.candidates.get(memory.candidate_id)
        verification = self.verifications.get(memory.verification_id)
        if memory.id != memory.scope or memory.revision != expected_revision + 1:
            raise ValueError("invalid memory revision")
        if candidate is None or candidate.git_ref != memory.git_ref:
            raise ValueError("invalid memory candidate")
        if candidate.harness_files != memory.harness_files:
            raise ValueError("memory harness differs from candidate")
        if (
            verification is None
            or verification.candidate_id != candidate.id
            or verification.decision != "promote"
            or verification.invariant_failures
            or verification.hidden_pass_rate != 1
        ):
            raise ValueError("invalid memory verification")
        current = self.memories.get(memory.scope)
        if (current.revision if current is not None else 0) != expected_revision:
            raise ValueError("stale memory revision")
        self.memories[memory.scope] = memory


class RecordingAdvisor(SubQClient):
    """Return a maximum advisory score and retain the submitted public state."""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def evaluate(self, state, questions) -> DecisionResponse:
        self.states.append(state)
        return DecisionResponse.model_validate(
            {
                "model": "scripted-subq",
                "answers": {"needs_review": {"type": "noul", "noul": 1.0}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )


def retry_task(task_id: str) -> HarnessTask:
    """Create a public accepted-then-timeout task for the deterministic fixture."""
    return HarnessTask(
        id=task_id,
        prompt="Choose a safe declarative retry policy for one trading intent.",
        domain="trade_execution",
        inputs={
            "symbol": "AAPL",
            "qty": 10,
            "price": 100,
            "accepted_before_timeout": True,
        },
    )


class HarnessRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.runs_dir = Path(self.temporary_directory.name)
        self.scope = "runner-test-scope"
        self.store = InMemoryStore()

    def runner(
        self,
        proposer: ScriptedProposer,
        *,
        advisor: SubQClient | None = None,
    ) -> HarnessRunner:
        settings = HarnessSettings(
            scope=self.scope,
            git_ref="test-git-ref",
            runs_dir=self.runs_dir,
            k=1,
            mode="sequential",
        )
        return HarnessRunner(  # type: ignore[arg-type]
            self.store, proposer, settings, advisor=advisor
        )

    def test_real_schedule_promotes_then_reloads_and_rejects_no_change(self):
        proposer = ScriptedProposer()
        advisor = RecordingAdvisor()
        probe_calls: list[HarnessFiles] = []
        verifier = make_demo_verifier(seed=101)

        def counting_probe(task: HarnessTask, harness: HarnessFiles) -> ProbeResult:
            probe_calls.append(dict(harness))
            return demo_probe(task, harness)

        first = self.runner(proposer, advisor=advisor).run(
            retry_task("first-task"),
            initial_harness=dict(DEFAULT_HARNESS),
            probe=counting_probe,
            verifier=verifier,
        )

        self.assertEqual(first.decision, "promote")
        self.assertEqual(first.loaded_memory_revision, 0)
        self.assertEqual(first.memory_revision, 1)
        self.assertEqual(first.advisory_probability, 1.0)
        self.assertEqual(len(probe_calls), 4)
        self.assertFalse(
            any(label.startswith("select.") for label in proposer.record_calls)
        )
        self.assertEqual(set(advisor.states[0]), {"output", "trace"})
        self.assertNotIn("private-", str(advisor.states[0]))

        memory = self.store.get_harness_memory(self.scope)
        self.assertIsNotNone(memory)
        assert memory is not None
        self.assertEqual(
            memory.harness_files["toolguidance.md"],
            '{"retry_policy":"lookup_before_retry"}',
        )
        self.assertTrue(memory.library_files)
        self.assertTrue(
            any(path.endswith("EXPERIENCE.md") for path in memory.library_files)
        )

        fresh_proposer = ScriptedProposer()
        second = self.runner(fresh_proposer).run(
            retry_task("reuse-persisted-memory"),
            initial_harness=dict(DEFAULT_HARNESS),
            probe=demo_probe,
            verifier=verifier,
        )

        self.assertEqual(second.decision, "reject")
        self.assertEqual(second.loaded_memory_revision, 1)
        self.assertEqual(second.memory_revision, 1)
        self.assertEqual(self.store.get_harness_memory(self.scope), memory)
        restored_context = "\n".join(
            content
            for snapshot in fresh_proposer.context_history
            for content in snapshot.values()
        )
        self.assertIn("reconcile broker ledger before retry", restored_context)
        self.assertNotIn("private-", restored_context)
        self.assertFalse(
            any(
                ".env" in path
                for snapshot in proposer.context_history
                for path in snapshot
            )
        )

    def test_overfit_policy_passes_training_but_cannot_advance_memory(self):
        verifier = make_demo_verifier(seed=202)
        approved = self.runner(ScriptedProposer()).run(
            retry_task("approved-task"),
            initial_harness=dict(DEFAULT_HARNESS),
            probe=demo_probe,
            verifier=verifier,
        )
        prior_memory = self.store.get_harness_memory(self.scope)

        overfit = self.runner(ScriptedProposer("never_retry")).run(
            retry_task("overfit-task"),
            initial_harness=dict(DEFAULT_HARNESS),
            probe=demo_probe,
            verifier=verifier,
        )

        self.assertEqual(approved.decision, "promote")
        self.assertEqual(overfit.decision, "reject")
        self.assertEqual(overfit.loaded_memory_revision, 1)
        self.assertEqual(overfit.memory_revision, 1)
        self.assertEqual(self.store.get_harness_memory(self.scope), prior_memory)
        verdict = self.store.get_verification(overfit.verification_id)
        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertTrue(
            any("case-" in failure for failure in verdict.invariant_failures)
        )

    def test_proposer_error_raises_and_does_not_advance_memory(self):
        verifier = make_demo_verifier(seed=303)
        approved = self.runner(ScriptedProposer()).run(
            retry_task("approved-before-error"),
            initial_harness=dict(DEFAULT_HARNESS),
            probe=demo_probe,
            verifier=verifier,
        )
        prior_memory = self.store.get_harness_memory(self.scope)
        candidate_count = len(self.store.candidates)
        verdict_count = len(self.store.verifications)

        with self.assertRaisesRegex(RuntimeError, "Harness² refinement failed"):
            self.runner(ScriptedProposer(fail_proposals=True)).run(
                retry_task("proposal-error"),
                initial_harness=dict(DEFAULT_HARNESS),
                probe=demo_probe,
                verifier=verifier,
            )

        self.assertEqual(approved.memory_revision, 1)
        self.assertEqual(self.store.get_harness_memory(self.scope), prior_memory)
        self.assertEqual(len(self.store.candidates), candidate_count)
        self.assertEqual(len(self.store.verifications), verdict_count)


if __name__ == "__main__":
    unittest.main()
