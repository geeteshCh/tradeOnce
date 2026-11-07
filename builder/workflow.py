"""Real-code Harness² refinement, independent verification, and lesson transfer."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Literal

from harness2.core.proposer import Proposer
from pydantic import BaseModel, ConfigDict, Field

from arena import Scenario
from builder.codegen import CodePatch
from builder.evidence import (
    SuiteEvidence,
    TaskKind,
    evaluate,
    make_private_scenarios,
    public_scenarios,
)
from builder.sandbox import SandboxRunner
from harnessing import HarnessRunner, HarnessSettings, HarnessTask, ProbeResult
from harnessing.models import HarnessFiles, VerificationOutcome
from loop.versions import VersionStore
from storage import BuilderCandidate, HarnessMemory, MongoStore, VerificationResult

PatchGenerator = Callable[[HarnessFiles, HarnessFiles, str], CodePatch]
Observer = Callable[[str, str], None]

INITIAL_BUILDER_HARNESS: HarnessFiles = {
    "systemprompt.md": (
        "You maintain a trading Executor. Repair the supplied Python source from "
        "public failure evidence. Preserve its JSON stdin/stdout interface."
    ),
    "guardrails.md": (
        "Change only executor/run.py and executor/LESSONS.md. Execute only authorized "
        "intents. Read BROKER_URL from the environment. Use httpx and Python stdlib."
    ),
    "memory.md": "No independently validated Builder lessons have been adopted yet.",
    "toolguidance.md": (
        "Write a small complete Python repair and a durable lesson. Use the supplied "
        "broker contract and public feedback. The caller executes the proposed code."
    ),
}

BROKER_CONTRACT = """Executor contract:
Read a JSON list of {intent_id, symbol, side, qty} from stdin. Print a JSON list
with intent_id, status (filled|partially_filled|rejected|unknown|failed),
broker_order_id, filled_qty. Use BROKER_URL from env. httpx and stdlib are available.
POST /v2/orders accepts symbol, qty, side, type=market, time_in_force=day and
client_order_id. A stable client_order_id identifies an intent; duplicates get 422.
GET /v2/orders:by_client_order_id?client_order_id=... returns the order or 404.
GET /v2/orders/{id} returns current order state. An order has id, status,
filled_qty, filled_avg_price, client_order_id. A POST timeout or 500 can occur
after the broker already filled the order; 429 or transient 500 may record nothing.
A genuine 403 rejection must be reported without claiming a fill.
Never change hosts, read local files, install packages, or use any other service.
Do not special-case scenario IDs, tickers, quantities, prices, or fault timing.
"""


class CodingSettings(BaseModel):
    """Bound one real-code experiment and its persistent Builder memory scope."""

    model_config = ConfigDict(extra="forbid")

    root: Path
    scope: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    max_patch_attempts: int = Field(default=2, ge=1, le=2)


class CodeArtifact(BaseModel):
    """Trusted source snapshot, Git provenance, and public execution evidence."""

    name: str
    path: Path
    git_ref: str
    source_files: HarnessFiles
    public: SuiteEvidence
    summary: str


class CodingTaskResult(BaseModel):
    """Measured result of one repair task; private case inputs are excluded."""

    task: TaskKind
    decision: Literal["promote", "reject"]
    mode: Literal["refinement", "memory_reuse", "reuse_then_refinement"]
    git_ref: str
    version: str | None
    patch_proposals: int = Field(ge=0)
    loaded_memory_revision: int = Field(ge=0)
    memory_revision: int = Field(ge=0)
    public: SuiteEvidence
    baseline: SuiteEvidence
    hidden_passed: int
    hidden_total: int
    candidate_id: str
    verification_id: str
    elapsed_seconds: float
    diff: str
    lesson: str


class CodingDemoReport(BaseModel):
    """Replayable evidence from an actual Argon run, never a scripted success."""

    run_id: str
    model: str
    scope: str
    first: CodingTaskResult
    transfer: CodingTaskResult | None = None
    measurement_note: str = (
        "Patch proposals count actual Executor generation calls, including failed "
        "attempts. The tasks differ; this is observed transfer, not a controlled "
        "causal estimate of model improvement."
    )


class CodingExperiment:
    """Coordinate trusted Arena execution with the existing Harness² runner.

    Gemini receives only source, Builder guidance, and public feedback. A separate
    in-memory verifier freezes hidden cases per task. Each generated source is
    versioned in an isolated Git repository, executed through SandboxRunner, and
    promoted only after public, regression, and hidden checks all pass.
    """

    def __init__(
        self,
        store: MongoStore,
        proposer: Proposer,
        generate: PatchGenerator,
        settings: CodingSettings,
        *,
        sandbox: SandboxRunner | None = None,
        observe: Observer | None = None,
    ) -> None:
        self.store = store
        self.proposer = proposer
        self.generate = generate
        self.settings = settings
        self.sandbox = sandbox if sandbox is not None else SandboxRunner(timeout_s=15)
        self.observe = observe or (lambda _event, _message: None)
        self.root = settings.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        seed = Path(__file__).resolve().parents[1] / "demo" / "seed" / "executor"
        self.seed_files = {
            f"executor/{name}": (seed / name).read_text(encoding="utf-8")
            for name in ("run.py", "LESSONS.md")
        }
        self.versions = VersionStore(self.root)
        self.versions.init({"executor": seed})
        self.seed_ref = self._git(self.versions.repo, "rev-parse", "v0")
        self.seed_dir = self.root / "original"
        self._write_sources(self.seed_dir, self.seed_files)
        self._proposals = 0
        # Harness² may run independent invariant/domain probes concurrently.
        self._proposal_lock = Lock()
        self._artifacts: dict[str, CodeArtifact] = {}

    def run_task(self, kind: TaskKind, *, reuse_memory: bool) -> CodingTaskResult:
        """Repair a fresh copy of the naive Executor, optionally reusing lessons.

        The first task always runs one real sequential Harness² step (k=1).
        A later task tries the approved Builder once before paying for another
        refinement. A failed reuse attempt falls through to Harness²; it never
        bypasses the independent gate or overwrites approved memory.
        """
        started = time.monotonic()
        count_before = self._proposals
        public = public_scenarios(kind)
        if kind == "server_error":
            # Preserve the previously solved timeout as an explicit regression.
            public["regression"] += public_scenarios("timeout")["training"]
        hidden = make_private_scenarios(kind)
        # Preserve the frozen evidence for local audit, outside all model contexts.
        private_dir = self.root / "private"
        private_dir.mkdir(mode=0o700, exist_ok=True)
        hidden_json = json.dumps(
            [scenario.model_dump(mode="json") for scenario in hidden], sort_keys=True
        )
        private_path = private_dir / f"{kind}.json"
        with private_path.open("w", encoding="utf-8") as stream:
            private_path.chmod(0o600)
            stream.write(hidden_json + "\n")
        hidden_digest = hashlib.sha256(hidden_json.encode()).hexdigest()
        private_evidence: dict[str, SuiteEvidence] = {}
        memory = self.store.get_harness_memory(self.settings.scope)
        loaded_revision = memory.revision if memory is not None else 0
        baseline = CodeArtifact(
            name="v0",
            path=self.seed_dir,
            git_ref=self.seed_ref,
            source_files=dict(self.seed_files),
            public=evaluate(self.seed_dir, public, self.sandbox),
            summary="Original naive Executor; no generated repair.",
        )
        self._artifacts[baseline.git_ref] = baseline
        self.observe("baseline", baseline.public.model_dump_json())
        feedback = (
            BROKER_CONTRACT
            + "\nPublic original-Executor evidence:\n"
            + json.dumps(baseline.public.to_public(), indent=2)
        )
        source = dict(self.seed_files)
        if reuse_memory and memory is not None:
            # Reset code to v0: only approved Builder guidance/lessons transfer.
            source["executor/LESSONS.md"] = memory.source_files.get(
                "executor/LESSONS.md", source["executor/LESSONS.md"]
            )
        task = HarnessTask(
            id=f"repair-{kind}",
            domain="executor_code",
            prompt=(
                "Improve the Builder's four instruction files so its generated Python "
                "Executor fixes the supplied broker failure and preserves the public "
                "regressions. toolguidance.md is plain-text coding guidance, not a "
                "retry_policy JSON selector. Actual source is written by a separate "
                "Gemini code-generation call using those instructions. Derive a "
                "reusable reconciliation lesson from observed public executions."
            ),
            inputs={"source_files": source, "public_feedback": feedback},
        )

        def probe(_task: HarnessTask, harness: HarnessFiles) -> ProbeResult:
            artifact = self._build(kind, source, harness, feedback, public)
            self._artifacts[artifact.git_ref] = artifact
            return self._probe_result(artifact)

        def verify(
            _task: HarnessTask, _harness: HarnessFiles, observed: ProbeResult
        ) -> VerificationOutcome:
            artifact = self._artifacts[observed.git_ref or ""]
            if observed.source_files != artifact.source_files:
                raise ValueError("verification source does not match trusted artifact")
            if artifact.git_ref not in private_evidence:
                self.observe("verifying", "Running independent checks on unseen cases.")
                private_evidence[artifact.git_ref] = evaluate(
                    artifact.path, {"hidden": hidden}, self.sandbox
                )
            private = private_evidence[artifact.git_ref]
            return VerificationOutcome(
                held_out_passed=private.passed,
                held_out_total=private.total,
                invariant_failures=artifact.public.failures + private.failures,
                broker_calls=artifact.public.broker_calls + private.broker_calls,
                reasons=[
                    "Independent auditor and broker-ledger completion checks.",
                    f"Frozen held-out suite SHA256: {hidden_digest}",
                ],
            )

        mode: Literal["refinement", "memory_reuse", "reuse_then_refinement"]
        mode = "refinement"
        if reuse_memory and memory is not None:
            self.observe(
                "memory", f"Loaded approved MongoDB revision {memory.revision}."
            )
            observed = probe(task, memory.harness_files)
            outcome = verify(task, memory.harness_files, observed)
            artifact = self._artifacts[observed.git_ref or ""]
            candidate = BuilderCandidate(
                run_id=self.root.name,
                round=2,
                candidate=artifact.name,
                git_ref=artifact.git_ref,
                source_files=artifact.source_files,
                harness_files=memory.harness_files,
                summary=f"Builder reuse: {artifact.summary}",
            )
            self.store.save_candidate(candidate)
            verdict = VerificationResult(
                candidate_id=candidate.id,
                decision=outcome.decision,
                hidden_pass_rate=outcome.hidden_pass_rate,
                invariant_failures=outcome.invariant_failures,
                reasons=outcome.reasons,
            )
            self.store.save_verification(verdict)
            if outcome.decision == "promote":
                updated = HarnessMemory(
                    id=self.settings.scope,
                    scope=self.settings.scope,
                    revision=memory.revision + 1,
                    candidate_id=candidate.id,
                    verification_id=verdict.id,
                    git_ref=artifact.git_ref,
                    source_files=artifact.source_files,
                    harness_files=memory.harness_files,
                    library_files=memory.library_files,
                )
                self.store.save_harness_memory(
                    updated, expected_revision=memory.revision
                )
                return self._finish(
                    kind,
                    "memory_reuse",
                    artifact,
                    baseline,
                    outcome,
                    candidate.id,
                    verdict.id,
                    loaded_revision,
                    updated.revision,
                    count_before,
                    started,
                )
            self.versions.reject(artifact.name, "; ".join(outcome.invariant_failures))
            mode = "reuse_then_refinement"
            self.observe(
                "reuse_rejected", "Reuse failed the gate; approved memory retained."
            )

        runner = HarnessRunner(
            self.store,
            self.proposer,
            HarnessSettings(
                scope=self.settings.scope,
                git_ref=self.seed_ref,
                runs_dir=self.root / "harness2",
                k=1,
                mode="sequential",
            ),
        )
        result = runner.run(
            task,
            initial_harness=INITIAL_BUILDER_HARNESS,
            probe=probe,
            verifier=verify,
            reference_probe=lambda _task, _harness: self._probe_result(baseline),
        )
        selected = self.store.get_candidate(result.candidate_id)
        if selected is None:
            raise RuntimeError("Harness² candidate was not persisted")
        artifact = self._artifacts[selected.git_ref]
        outcome = verify(task, selected.harness_files, self._probe_result(artifact))
        return self._finish(
            kind,
            mode,
            artifact,
            baseline,
            outcome,
            result.candidate_id,
            result.verification_id,
            loaded_revision,
            result.memory_revision,
            count_before,
            started,
            approved=result.decision == "promote",
        )

    def _build(
        self,
        kind: TaskKind,
        source: HarnessFiles,
        harness: HarnessFiles,
        feedback: str,
        public: dict[str, list[Scenario]],
    ) -> CodeArtifact:
        current = dict(source)
        artifact: CodeArtifact | None = None
        for _ in range(self.settings.max_patch_attempts):
            with self._proposal_lock:
                self._proposals += 1
                name = f"{kind}-patch-{self._proposals:02d}"
            self.observe("building", f"Argon is writing {name}.")
            patch = self.generate(current, harness, feedback)
            path = self.versions.new_candidate(name)
            current = {
                "executor/run.py": patch.run_source,
                "executor/LESSONS.md": patch.lessons,
            }
            self._write_sources(path, current)
            self.versions.commit_candidate(name, f"{name}: {patch.summary[:120]}")
            git_ref = self._git(path, "rev-parse", "HEAD")
            evidence = evaluate(path, public, self.sandbox)
            artifact = CodeArtifact(
                name=name,
                path=path,
                git_ref=git_ref,
                source_files=current,
                public=evidence,
                summary=patch.summary,
            )
            self.observe(
                "public_check",
                f"{name}: {evidence.passed}/{evidence.total} public cases.",
            )
            if evidence.all_passed:
                break
            feedback = (
                BROKER_CONTRACT
                + "\nPublic repair feedback:\n"
                + json.dumps(evidence.to_public(), indent=2)
            )
        if artifact is None:
            raise RuntimeError("No Builder attempt executed")
        return artifact

    def _finish(
        self,
        kind: TaskKind,
        mode: Literal["refinement", "memory_reuse", "reuse_then_refinement"],
        artifact: CodeArtifact,
        baseline: CodeArtifact,
        outcome: VerificationOutcome,
        candidate_id: str,
        verification_id: str,
        loaded_revision: int,
        revision: int,
        count_before: int,
        started: float,
        *,
        approved: bool = True,
    ) -> CodingTaskResult:
        promote = approved and outcome.decision == "promote"
        version = None
        if promote:
            version = self.versions.promote(artifact.name, artifact.summary)
        else:
            self.versions.reject(artifact.name, "; ".join(outcome.invariant_failures))
        result = CodingTaskResult(
            task=kind,
            decision="promote" if promote else "reject",
            mode=mode,
            git_ref=artifact.git_ref,
            version=version,
            patch_proposals=self._proposals - count_before,
            loaded_memory_revision=loaded_revision,
            memory_revision=revision,
            public=artifact.public,
            baseline=baseline.public,
            hidden_passed=outcome.held_out_passed,
            hidden_total=outcome.held_out_total,
            candidate_id=candidate_id,
            verification_id=verification_id,
            elapsed_seconds=round(time.monotonic() - started, 3),
            diff=self._git(
                artifact.path, "diff", "v0", artifact.git_ref, "--", "executor"
            ),
            lesson=artifact.source_files["executor/LESSONS.md"],
        )
        (self.root / f"{kind}.json").write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        self.observe("promoted" if promote else "rejected", result.model_dump_json())
        return result

    @staticmethod
    def _probe_result(artifact: CodeArtifact) -> ProbeResult:
        return ProbeResult(
            output=json.dumps(
                {"summary": artifact.summary, "source_files": artifact.source_files},
                indent=2,
            ),
            trace=json.dumps(artifact.public.to_public(), indent=2),
            git_ref=artifact.git_ref,
            source_files=dict(artifact.source_files),
        )

    @staticmethod
    def _write_sources(root: Path, files: HarnessFiles) -> None:
        """Write only the two fixed files admitted by the typed code generator."""
        if set(files) != {"executor/run.py", "executor/LESSONS.md"}:
            raise ValueError("unexpected Executor edit surface")
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    @staticmethod
    def _git(path: Path, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=path, text=True, stderr=subprocess.PIPE
        ).strip()
