"""Filesystem-bounded Harness² adapter for trusted TradeOnce callbacks."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec
from harness2.core.tree import SurfaceSpec
from pydantic import TypeAdapter

from harnessing.models import (
    EDITABLE_FILES,
    HarnessFiles,
    HarnessTask,
    Probe,
    ProbeResult,
    VerificationOutcome,
    Verifier,
)

TRADEONCE_SURFACE = SurfaceSpec(
    # Harness² currently ships prompt text only for its two native surfaces.
    # OpenCode is the compatible four-file surface; adapter validation below
    # still narrows it to TradeOnce's exact declarative files.
    name="opencode",
    always_on=(
        "systemprompt.md",
        "guardrails.md",
        "memory.md",
        "toolguidance.md",
    ),
    always_on_max=100_000,
    always_on_cap_reason="Keep the complete policy readable in one refinement context.",
    skill_dir="skills",
    skill_name_rule="lowercase letters, digits, hyphens, and underscores",
    trigger_hint="Use a named skill only when its description applies.",
    autotrigger_file="systemprompt.md",
    trigger_line="Use {name} when {desc}.",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
)

_HARNESS_ADAPTER = TypeAdapter(dict[str, str])
_RUN_PART = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|credential|token)", re.IGNORECASE
)


class _TradeOnceSubstrate:
    """The small substrate view required by Harness² core scheduling."""

    name = "tradeonce"

    def surface(self) -> SurfaceSpec:
        """Return the fixed four-file editable surface."""
        return TRADEONCE_SURFACE


class TradeOnceAdapter:
    """Adapt one trusted TradeOnce task to Harness² without executing generated code."""

    def __init__(
        self,
        task: HarnessTask,
        workspace: Path,
        base_harness: HarnessFiles,
        probe: Probe,
        verifier: Verifier,
        *,
        channels: dict[str, ChannelSpec],
    ) -> None:
        self._task = task
        self._workspace = Path(workspace).resolve()
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._base = dict(base_harness)
        self._probe = probe
        self._verifier = verifier
        self.validate_tree(self._base)
        self.substrate = _TradeOnceSubstrate()
        self.spec = BenchSpec(
            name="tradeonce",
            runs_root=self._workspace / "runs",
            install_mode="workspace_config",
            prompts=BenchPrompts(
                work="execute authorized trading intents once",
                domain_word="execution policy",
                deliverable_word="execution record",
                inv_levers="Preserve authorization, reconciliation, and ledger accuracy.",
                dom_levers="Use the supplied execution context and declarative policy files.",
                yardstick="Did the policy execute the authorized intent exactly once?",
            ),
            channels=channels,
            leak_globs=(".env*", "**/.env*"),
            allow_subagents=False,
        )

    def tasks(self, split: str = "all") -> list[str]:
        """Return the single configured task for the supported split names."""
        return [self._task.id] if split in {"all", "train"} else []

    def domain_of(self, task_id: str) -> str:
        """Return the task's configured learning domain."""
        self._require_task(task_id)
        return self._task.domain

    def task_prompt(self, task_id: str) -> str:
        """Return only the Builder-visible prompt."""
        self._require_task(task_id)
        return self._task.prompt

    def task_files(self, task_id: str) -> Path:
        """Materialize only the public prompt and redacted JSON inputs."""
        self._require_task(task_id)
        source = self._within_workspace(
            self._workspace / "source" / task_id, must_exist=False
        )
        source.mkdir(parents=True, exist_ok=True)
        prompt_path = source / "prompt.txt"
        inputs_path = source / "inputs.json"
        if prompt_path.is_symlink() or inputs_path.is_symlink():
            raise ValueError("task context files must not be symlinks")
        prompt_path.write_text(self._task.prompt, encoding="utf-8")
        safe_inputs = _sanitize(self._task.inputs)
        inputs_path.write_text(
            json.dumps(safe_inputs, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return source

    def execute(
        self,
        task_id: str,
        harness_dir: Path | None,
        run_id: str,
        *,
        timeout: int | None = None,
    ) -> Path:
        """Call the trusted probe once per run id and persist validated artifacts."""
        del timeout
        self._require_task(task_id)
        run_dir = self._run_dir(run_id)
        probe_path = run_dir / "probe.json"
        if probe_path.is_file():
            self._load_probe(run_dir)
            return run_dir

        harness = self._read_harness(harness_dir)
        result = self._probe(self._task, harness)
        if not isinstance(result, ProbeResult):
            raise TypeError("probe must return ProbeResult")
        run_dir.mkdir(parents=True, exist_ok=True)
        probe_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        (run_dir / "harness.json").write_text(
            json.dumps(harness, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (run_dir / "output.txt").write_text(result.output, encoding="utf-8")
        (run_dir / "trajectory.md").write_text(result.trace, encoding="utf-8")
        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        """Return whether a validated probe produced non-empty output."""
        try:
            return bool(self._load_probe(run_dir).output.strip())
        except (OSError, ValueError):
            return False

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        """Return the validated probe trace, bounded for Harness² context."""
        return self._load_probe(run_dir).trace[:obs_trunc]

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        """Expose the validated probe output as the sole deliverable file."""
        return {"output.txt": self._load_probe(run_dir).output}

    def deliverable_text(self, run_dir: Path) -> str:
        """Return the validated probe output."""
        return self._load_probe(run_dir).output

    def base_harness(self) -> Path:
        """Materialize and return the validated unmodified harness."""
        root = self._workspace / "base_harness"
        root.mkdir(parents=True, exist_ok=True)
        for name, content in self._base.items():
            path = root / name
            if path.is_symlink():
                raise ValueError("base harness files must not be symlinks")
            path.write_text(content, encoding="utf-8")
        return root

    def validate_tree(self, tree: dict[str, str]) -> None:
        """Require exactly the four bounded Markdown policy files."""
        unknown = sorted(set(tree) - EDITABLE_FILES)
        missing = sorted(EDITABLE_FILES - set(tree))
        errors: list[str] = []
        if unknown:
            errors.append(f"files outside the editable surface: {unknown}")
        if missing:
            errors.append(f"missing required files: {missing}")
        if any(not isinstance(value, str) for value in tree.values()):
            errors.append("all harness contents must be text")
        if (
            sum(len(value) for value in tree.values() if isinstance(value, str))
            > 100_000
        ):
            errors.append("combined harness content exceeds 100000 characters")
        if errors:
            raise ValueError("; ".join(errors))

    def verify(self, run_dir: Path) -> VerificationOutcome:
        """Run the trusted private verifier over persisted, validated inputs."""
        checked = self._checked_run_dir(run_dir)
        harness = _HARNESS_ADAPTER.validate_json(
            (checked / "harness.json").read_text(encoding="utf-8")
        )
        self.validate_tree(harness)
        outcome = self._verifier(self._task, harness, self._load_probe(checked))
        if not isinstance(outcome, VerificationOutcome):
            raise TypeError("verifier must return VerificationOutcome")
        return outcome

    def score(self, run_dir: Path, task_id: str) -> dict[str, Any]:
        """Map trusted measured evidence to Harness²'s expected score fields."""
        self._require_task(task_id)
        outcome = self.verify(run_dir)
        return {
            "n_passed": outcome.held_out_passed,
            "n_criteria": outcome.held_out_total,
            "all_pass": outcome.decision == "promote",
        }

    def _require_task(self, task_id: str) -> None:
        if task_id != self._task.id:
            raise KeyError(f"unknown task id: {task_id}")

    def _read_harness(self, harness_dir: Path | None) -> HarnessFiles:
        root = self.base_harness() if harness_dir is None else Path(harness_dir)
        checked = self._within_workspace(root)
        paths = [
            path for path in checked.rglob("*") if path.is_file() or path.is_symlink()
        ]
        if any(path.is_symlink() for path in paths):
            raise ValueError("harness files must not be symlinks")
        names = {path.relative_to(checked).as_posix() for path in paths}
        unknown = sorted(names - EDITABLE_FILES)
        if unknown:
            raise ValueError(f"files outside the editable surface: {unknown}")
        tree = {
            name: (checked / name).read_text(encoding="utf-8")
            for name in EDITABLE_FILES
            if (checked / name).is_file()
        }
        self.validate_tree(tree)
        return tree

    def _run_dir(self, run_id: str) -> Path:
        parts = run_id.split("/")
        if not parts or any(
            not part or part in {".", ".."} or not _RUN_PART.fullmatch(part)
            for part in parts
        ):
            raise ValueError(f"unsafe run id: {run_id!r}")
        return self._within_workspace(
            self._workspace / "executions" / "__".join(parts), must_exist=False
        )

    def _checked_run_dir(self, run_dir: Path) -> Path:
        checked = self._within_workspace(run_dir)
        executions = (self._workspace / "executions").resolve()
        if not checked.is_relative_to(executions):
            raise ValueError("run directory is outside the execution store")
        return checked

    def _load_probe(self, run_dir: Path) -> ProbeResult:
        checked = self._checked_run_dir(run_dir)
        return ProbeResult.model_validate_json(
            (checked / "probe.json").read_text(encoding="utf-8")
        )

    def _within_workspace(self, path: Path, *, must_exist: bool = True) -> Path:
        resolved = path.resolve(strict=must_exist)
        if not resolved.is_relative_to(self._workspace):
            raise ValueError(f"path is outside the adapter workspace: {path}")
        return resolved


def _sanitize(value: Any, *, key: str = "") -> Any:
    """Redact credential-shaped input fields before creating model context."""
    if key and _SENSITIVE_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(name): _sanitize(item, key=str(name)) for name, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value
