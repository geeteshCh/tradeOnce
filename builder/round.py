"""One Builder tournament round."""

from collections.abc import Callable, Sequence
from pathlib import Path

from builder.candidates import CandidateWorktree
from builder.evaluate import commit_candidate, promote, run_training
from builder.models import CandidateResult, JudgeVerdict
from builder.propose import run_aider


class BuilderRound:
    """Build, probe, and select isolated candidates."""

    def __init__(
        self,
        *,
        repo: Path,
        worktree_root: Path,
        base_ref: str,
        training_command: list[str],
        judge: Callable[[Path, str], JudgeVerdict],
    ) -> None:
        self.repo = repo
        self.worktree_root = worktree_root
        self.base_ref = base_ref
        self.training_command = training_command
        self.judge = judge

    def run(
        self,
        *,
        round_number: int,
        failure_trace: str,
        candidate_names: Sequence[str] = ("candidate_a", "candidate_b"),
    ) -> tuple[str | None, list[CandidateResult], list[JudgeVerdict]]:
        """Run candidates and return the promoted ref, results, and verdicts."""
        results: list[CandidateResult] = []
        verdicts: list[JudgeVerdict] = []
        for name in candidate_names:
            candidate = CandidateWorktree(
                self.worktree_root / f"round-{round_number}",
                name,
                self.base_ref,
                self.repo,
            )
            path = candidate.create()
            output = run_aider(
                worktree=path,
                prompt=failure_trace,
                files=["executor", "executor/tools", "executor/LESSONS.md"],
                test_command=" ".join(self.training_command),
            )
            candidate.assert_allowed_changes(("executor",))
            passed, training_output = run_training(path, self.training_command)
            if not passed:
                training_output = f"{output}\n{training_output}".strip()
                results.append(
                    CandidateResult(
                        name=name,
                        worktree=str(path),
                        git_ref=self.base_ref,
                        training_passed=False,
                        training_output=training_output,
                    )
                )
                continue
            git_ref = commit_candidate(
                path, f"Builder round {round_number}: improve {name}"
            )
            verdict = self.judge(path, git_ref)
            verdicts.append(verdict)
            results.append(
                CandidateResult(
                    name=name,
                    worktree=str(path),
                    git_ref=git_ref,
                    training_passed=True,
                    training_output=f"{output}\n{training_output}".strip(),
                )
            )

        eligible = [verdict for verdict in verdicts if verdict.eligible]
        if not eligible:
            return None, results, verdicts
        winner = max(eligible, key=lambda verdict: verdict.hidden_pass_rate)
        promote(self.repo, next(r.git_ref for r in results if r.name == winner.candidate), f"v{round_number}")
        return winner.candidate, results, verdicts
