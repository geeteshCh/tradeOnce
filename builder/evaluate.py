"""Candidate probing and Git promotion helpers."""

from pathlib import Path
import subprocess


def run_training(worktree: Path, command: list[str]) -> tuple[bool, str]:
    """Run the visible training suite and return its status and output."""
    result = subprocess.run(
        command,
        cwd=worktree,
        check=False,
        text=True,
        capture_output=True,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    return result.returncode == 0, output


def commit_candidate(worktree: Path, message: str) -> str:
    """Commit a validated candidate and return its commit SHA."""
    subprocess.run(["git", "add", "--", "executor"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=worktree, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def promote(repo: Path, git_ref: str, tag: str) -> None:
    """Move a promotion tag to a verified candidate."""
    subprocess.run(
        ["git", "tag", "-f", tag, git_ref],
        cwd=repo,
        check=True,
    )
