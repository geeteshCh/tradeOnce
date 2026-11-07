"""Versioning with git. Each run gets its own small git repo holding only the code the Builder may change.

    runs/<run_id>/evo/          the evolution repo (branch "main" = promoted line, tags v0, v1, ...)
    runs/<run_id>/cands/<name>/ one git worktree per candidate, on branch <name>

Promote = merge the candidate into main and tag vN. Reject = the candidate branch is never merged (it stays
in the history so the demo can show what was rolled back). Rollback of a promoted version = git revert.
Keeping this repo separate means the evolution history never touches the team's main repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {out.stderr.strip()}")
    return out.stdout.strip()


class VersionStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.repo = self.run_dir / "evo"
        self.cands = self.run_dir / "cands"

    # ----- setup -----
    def init(self, seed_dirs: dict[str, Path]) -> str:
        """Create the repo from seed code, e.g. {"executor": Path("examples/v0_executor")}. Returns "v0"."""
        self.repo.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "loop@tradeonce.local")
        _git(self.repo, "config", "user.name", "TradeOnce Loop")
        for name, src in seed_dirs.items():
            shutil.copytree(src, self.repo / name, ignore=shutil.ignore_patterns("__pycache__"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "v0: seed")
        _git(self.repo, "tag", "v0")
        return "v0"

    # ----- queries -----
    def current(self) -> str:
        tags = [t for t in _git(self.repo, "tag", "--list", "v*", "--merged", "main").split() if t[1:].isdigit()]
        return max(tags, key=lambda t: int(t[1:]))

    def promoted_dir(self) -> Path:
        return self.repo  # main is always checked out at the promoted version

    def log(self) -> str:
        return _git(self.repo, "log", "--oneline", "--decorate", "--graph", "--branches", "--tags")

    # ----- candidates -----
    def new_candidate(self, name: str) -> Path:
        path = self.cands / name
        if path.exists():
            raise RuntimeError(f"candidate {name} exists")
        self.cands.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "-q", "-b", name, str(path), "main")
        return path

    def commit_candidate(self, name: str, message: str) -> str | None:
        path = self.cands / name
        _git(path, "add", "-A")
        if not _git(path, "status", "--porcelain"):
            return None  # Builder made no change
        _git(path, "commit", "-q", "-m", message)
        return _git(path, "rev-parse", "--short", "HEAD")

    def diff(self, name: str) -> str:
        return _git(self.cands / name, "diff", "main...HEAD", "--stat")

    def promote(self, name: str, message: str) -> str:
        tag = f"v{int(self.current()[1:]) + 1}"
        _git(self.repo, "merge", "-q", "--no-ff", name, "-m", f"{tag}: {message}")
        _git(self.repo, "tag", tag)
        return tag

    def reject(self, name: str, reason: str) -> None:
        """The branch is never merged. A git note on its tip records why, visible in `git log --all`."""
        _git(self.repo, "notes", "add", "-f", "-m", f"REJECTED: {reason[:500]}", name)

    def revert_to(self, tag: str, reason: str) -> str:
        """Roll the promoted line back to an earlier tag with a new commit (history is kept)."""
        current = self.current()
        _git(self.repo, "checkout", "-q", tag, "--", ".")
        _git(self.repo, "commit", "-q", "-am", f"rollback {current} -> {tag}: {reason[:200]}")
        new = f"v{int(current[1:]) + 1}"
        _git(self.repo, "tag", new)
        return new

    def cleanup_worktrees(self) -> None:
        if self.repo.exists():
            subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
