import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from builder.candidates import CandidateWorktree
from builder.context import build_prompt
from builder.models import BuilderProposal, JudgeVerdict


class BuilderModelTests(unittest.TestCase):
    def test_proposal_requires_actionable_content(self):
        proposal = BuilderProposal(
            diagnosis="A timed out submission is retried blindly.",
            changes=["Look up the client order ID before retrying."],
            lesson="Unknown outcomes require reconciliation.",
        )
        self.assertEqual(len(proposal.changes), 1)

    def test_verdict_is_eligible_only_without_failures(self):
        self.assertTrue(
            JudgeVerdict(
                candidate="candidate_a",
                decision="promote",
                hidden_pass_rate=1,
            ).eligible
        )
        self.assertFalse(
            JudgeVerdict(
                candidate="candidate_a",
                decision="promote",
                hidden_pass_rate=1,
                invariant_failures=["UNSAFE_RETRY"],
            ).eligible
        )


class BuilderContextTests(unittest.TestCase):
    def test_prompt_contains_constraints_trace_lessons_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "executor.py"
            source.write_text("print('executor')", encoding="utf-8")
            prompt = build_prompt(
                failure_trace="duplicate fill",
                editable_files=[source],
                lessons="reconcile unknown outcomes",
            )
        self.assertIn("duplicate fill", prompt)
        self.assertIn("reconcile unknown outcomes", prompt)
        self.assertIn("print('executor')", prompt)
        self.assertIn("Do not edit the Arena", prompt)


class CandidateWorktreeTests(unittest.TestCase):
    @patch("builder.candidates._run_git")
    def test_forbidden_changes_are_rejected(self, run_git):
        run_git.return_value.stdout = " M executor/run.py\n M auditor/invariants.py\n"
        candidate = CandidateWorktree(
            Path("/tmp/worktrees"),
            "candidate_a",
            "HEAD",
            Path("/tmp/repo"),
        )
        with self.assertRaises(PermissionError):
            candidate.assert_allowed_changes(("executor",))

    @patch("builder.candidates._run_git")
    def test_allowed_changes_pass(self, run_git):
        run_git.return_value.stdout = " M executor/run.py\n?? executor/tools/reconcile.py\n"
        candidate = CandidateWorktree(
            Path("/tmp/worktrees"),
            "candidate_a",
            "HEAD",
            Path("/tmp/repo"),
        )
        candidate.assert_allowed_changes(("executor",))


if __name__ == "__main__":
    unittest.main()
