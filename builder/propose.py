"""Proposal generation and candidate patch application."""

from pathlib import Path
import subprocess

from builder.models import BuilderProposal
from providers import GeminiClient


def generate_proposal(
    client: GeminiClient,
    prompt: str,
) -> BuilderProposal:
    """Generate a validated Builder proposal from Gemini."""
    return client.generate_structured(
        prompt,
        BuilderProposal,
        system_instruction=(
            "You are the TradeOnce Builder. Propose minimal, testable changes. "
            "Never propose edits outside the explicitly allowed paths."
        ),
    )


def run_aider(
    *,
    worktree: Path,
    prompt: str,
    files: list[str],
    test_command: str,
) -> str:
    """Run Aider in a candidate worktree and return its combined output."""
    command = [
        "aider",
        "--yes-always",
        "--auto-test",
        "--test-cmd",
        test_command,
        "--message",
        prompt,
        *files,
    ]
    result = subprocess.run(
        command,
        cwd=worktree,
        check=False,
        text=True,
        capture_output=True,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise RuntimeError(f"Aider failed with exit code {result.returncode}:\n{output}")
    return output
