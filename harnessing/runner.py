"""Run real Harness² recursion, then gate MongoDB memory on independent evidence."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from harness2.core.library import Library, has_experience
from harness2.core.proposer import Proposer
from harness2.core.schedule import mode_token, run_task
from pydantic import BaseModel, Field, TypeAdapter

from harnessing.adapter import TradeOnceAdapter
from harnessing.models import (
    EDITABLE_FILES,
    HarnessFiles,
    HarnessRunResult,
    HarnessTask,
    Probe,
    ProbeResult,
    VerificationOutcome,
    Verifier,
)
from providers import BinaryCriteria, BinaryQuestion, SubQClient
from storage import BuilderCandidate, HarnessMemory, MongoStore, VerificationResult


class HarnessSettings(BaseModel):
    """A bounded refinement run over one persistent memory scope."""

    scope: str = Field(default="executor", min_length=1)
    git_ref: str = Field(min_length=1)
    runs_dir: Path = Path("runs/harness2")
    k: int = Field(default=1, ge=1)
    mode: Literal["parallel", "sequential"] = "sequential"


class _RunArtifact(BaseModel):
    run_dir: Path


class _Selection(BaseModel):
    ok: bool
    chosen_label: str
    chosen_run_dir: Path
    experience: dict[str, str] = Field(default_factory=dict)


class _FrameworkResult(BaseModel):
    base: list[_RunArtifact]
    selections: dict[str, _Selection]
    errors: list[str]


def _library_path(relative: str) -> Path:
    """Accept only Harness² library artifacts, never arbitrary restored paths."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Memory file escapes its library directory")
    if path == Path("INDEX.md"):
        return path
    parts = path.parts
    if len(parts) == 3 and parts[0] == "harnesses" and parts[2] == "EXPERIENCE.md":
        return path
    if (
        len(parts) == 4
        and parts[0] == "harnesses"
        and parts[2] == "harness"
        and parts[3] in EDITABLE_FILES
    ):
        return path
    raise ValueError(f"Unsupported Harness² memory file: {relative}")


def _restore_library(library: Library, files: HarnessFiles) -> None:
    """Rehydrate only the last verified MongoDB snapshot into a fresh library."""
    for relative, content in files.items():
        path = library.dir / _library_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot_library(library: Library) -> HarnessFiles:
    """Capture the full upstream lesson index and adopted trees without loss."""
    if not library.dir.exists():
        return {}
    return {
        str(_library_path(str(path.relative_to(library.dir)))): path.read_text(
            encoding="utf-8"
        )
        for path in sorted(library.dir.rglob("*"))
        if path.is_file()
    }


def _improves(candidate: VerificationOutcome, baseline: VerificationOutcome) -> bool:
    """Require eligibility plus better pass coverage or fewer measured broker calls."""
    if candidate.decision != "promote":
        return False
    if baseline.decision == "reject":
        return True
    return (
        candidate.broker_calls is not None
        and baseline.broker_calls is not None
        and candidate.broker_calls < baseline.broker_calls
    )


class HarnessRunner:
    """Coordinate Harness², trusted callbacks, persisted lineage, and memory adoption.

    The caller owns the store, proposer, and optional SubQ client. Probe callbacks
    execute trusted application code; the framework never runs generated scripts.
    """

    def __init__(
        self,
        store: MongoStore,
        proposer: Proposer,
        settings: HarnessSettings,
        *,
        advisor: SubQClient | None = None,
    ) -> None:
        self.store = store
        self.proposer = proposer
        self.settings = settings
        self.advisor = advisor

    def run(
        self,
        task: HarnessTask,
        *,
        initial_harness: HarnessFiles,
        probe: Probe,
        verifier: Verifier,
        reference_probe: Probe | None = None,
    ) -> HarnessRunResult:
        """Refine one task and persist memory only for a verified improvement."""
        previous = self.store.get_harness_memory(self.settings.scope)
        revision = previous.revision if previous is not None else 0
        base = previous.harness_files if previous is not None else initial_harness
        run_id = uuid4().hex
        root = self.settings.runs_dir.resolve() / run_id
        adapter = TradeOnceAdapter(
            task,
            root,
            base,
            probe,
            verifier,
            channels={"proposer": self.proposer.spec, "selector": self.proposer.spec},
            reference_probe=reference_probe,
        )
        library = Library(root / "library", task.domain, adapter.substrate.surface())
        if previous is not None:
            _restore_library(library, previous.library_files)
        mode = mode_token(self.settings.mode)
        framework = run_task(
            task.id,
            self.settings.k,
            adapter=adapter,
            prop=self.proposer,
            selector_prop=self.proposer,
            runs_root=root / "framework",
            modes=(mode,),
            library=library,
        )
        if framework.errors:
            raise RuntimeError(f"Harness² refinement failed: {framework.errors}")
        result = _FrameworkResult.model_validate(framework.to_dict())
        if result.errors or not result.base or not result.selections[mode].ok:
            raise RuntimeError(
                f"Harness² did not finish a valid refinement: {result.errors}"
            )
        selected = result.selections[mode]
        for path in (selected.chosen_run_dir, result.base[0].run_dir):
            if not path.resolve().is_relative_to(root):
                raise ValueError("Harness² artifact escaped this run's directory")
        files = TypeAdapter(HarnessFiles).validate_json(
            (selected.chosen_run_dir / "harness.json").read_text(encoding="utf-8")
        )
        observed = ProbeResult.model_validate_json(
            (selected.chosen_run_dir / "probe.json").read_text(encoding="utf-8")
        )
        outcome = adapter.verify(selected.chosen_run_dir)
        baseline = adapter.verify(result.base[0].run_dir)
        reasons = list(outcome.reasons)
        adopt = files != base and _improves(outcome, baseline)
        if files == base:
            reasons.append("No harness change was produced.")
        elif not _improves(outcome, baseline):
            reasons.append(
                "The candidate did not improve the required verification results."
            )
        if not has_experience(selected.experience):
            adopt = False
            reasons.append("No evidence-supported lesson was produced.")
        advisory = self._advisory(observed)
        if advisory is not None:
            reasons.append(f"SubQ advisory probability of review: {advisory:.4f}")
        selected_git_ref = observed.git_ref or self.settings.git_ref
        candidate = BuilderCandidate(
            run_id=run_id,
            round=self.settings.k,
            candidate=selected.chosen_label,
            git_ref=selected_git_ref,
            summary=f"Harness² {self.settings.mode} refinement for {task.id}",
            harness_files=files,
            source_files=observed.source_files,
        )
        self.store.save_candidate(candidate)
        verdict = VerificationResult(
            candidate_id=candidate.id,
            decision="promote" if adopt else "reject",
            hidden_pass_rate=outcome.hidden_pass_rate,
            invariant_failures=outcome.invariant_failures,
            reasons=reasons,
        )
        self.store.save_verification(verdict)
        if adopt:
            memory = HarnessMemory(
                id=self.settings.scope,
                scope=self.settings.scope,
                revision=revision + 1,
                candidate_id=candidate.id,
                verification_id=verdict.id,
                git_ref=selected_git_ref,
                harness_files=files,
                source_files=observed.source_files,
                library_files=_snapshot_library(library),
            )
            self.store.save_harness_memory(memory, expected_revision=revision)
            revision = memory.revision
        report = HarnessRunResult(
            run_id=run_id,
            candidate_id=candidate.id,
            verification_id=verdict.id,
            decision=verdict.decision,
            memory_scope=self.settings.scope,
            memory_revision=revision,
            loaded_memory_revision=previous.revision if previous is not None else 0,
            artifacts_dir=str(root),
            advisory_probability=advisory,
        )
        (root / "result.json").write_text(report.model_dump_json(indent=2) + "\n")
        return report

    def _advisory(self, observed: ProbeResult) -> float | None:
        """Ask SubQ about public observations without changing the trusted verdict."""
        if self.advisor is None:
            return None
        response = self.advisor.evaluate(
            {"output": observed.output, "trace": observed.trace},
            {
                "needs_review": BinaryQuestion(
                    instructions="Does the execution suggest duplicated or missing orders?",
                    criteria=BinaryCriteria(
                        true="An authorized intent has duplicated or missing execution.",
                        false="The observed execution matches the authorized intent.",
                    ),
                )
            },
        )
        return response.answers["needs_review"].probability_yes
