"""Isolated Git worktrees used for Builder candidates."""

from pathlib import Path
import subprocess


class CandidateWorktree:
    """Create and inspect one isolated candidate worktree."""

    def __init__(self, root: Path, name: str, base_ref: str, repo: Path) -> None:
        self.root = root
        self.name = name
        self.base_ref = base_ref
        self.repo = repo
        self.path = root / name
        self.branch = f"builder/{name}"

    def create(self) -> Path:
        """Create the candidate worktree and branch."""
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(f"Candidate worktree already exists: {self.path}")
        _run_git(
            ["worktree", "add", "-b", self.branch, str(self.path), self.base_ref],
            self.repo,
        )
        return self.path

    def changed_paths(self) -> list[Path]:
        """Return tracked and untracked paths changed by the candidate."""
        result = _run_git(
            ["status", "--short", "--untracked-files=all"], self.path
        ).stdout
        paths: list[Path] = []
        for line in result.splitlines():
            if not line:
                continue
            raw_path = line[3:]
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", maxsplit=1)[1]
            paths.append(Path(raw_path))
        return paths

    def assert_allowed_changes(self, allowed_roots: tuple[str, ...]) -> None:
        """Reject a candidate that modified files outside its assigned scope."""
        disallowed = [
            path
            for path in self.changed_paths()
            if not path.parts or path.parts[0] not in allowed_roots
        ]
        if disallowed:
            names = ", ".join(str(path) for path in disallowed)
            raise PermissionError(f"Candidate modified forbidden paths: {names}")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
