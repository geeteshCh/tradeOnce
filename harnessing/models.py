"""Typed task, execution, and verification boundaries for Harness²."""

from collections.abc import Callable
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

HarnessFiles = dict[str, str]
EDITABLE_FILES = frozenset(
    {"systemprompt.md", "guardrails.md", "memory.md", "toolguidance.md"}
)


class HarnessTask(BaseModel):
    """Builder-visible work only; private verification inputs stay in the verifier."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    prompt: str = Field(min_length=1)
    domain: str = Field(default="execution", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    inputs: dict[str, JsonValue] = Field(default_factory=dict)


class ProbeResult(BaseModel):
    """An executor's public artifact and observations, without private verdicts."""

    output: str
    trace: str
    data: dict[str, JsonValue] = Field(default_factory=dict)
    git_ref: str | None = None
    source_files: dict[str, str] = Field(default_factory=dict)


class VerificationOutcome(BaseModel):
    """Trusted verification evidence; the decision is derived, never model supplied."""

    model_config = ConfigDict(extra="forbid")

    held_out_passed: int = Field(ge=0)
    held_out_total: int = Field(gt=0)
    invariant_failures: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    broker_calls: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _valid_counts(self) -> Self:
        """Reject impossible evidence before it can authorize memory adoption."""
        if self.held_out_passed > self.held_out_total:
            raise ValueError("held_out_passed cannot exceed held_out_total")
        return self

    @property
    def hidden_pass_rate(self) -> float:
        """Return the measured pass fraction over the required held-out work."""
        return self.held_out_passed / self.held_out_total

    @property
    def decision(self) -> Literal["promote", "reject"]:
        """Require all held-out cases and all training/regression invariants."""
        if not self.invariant_failures and self.held_out_passed == self.held_out_total:
            return "promote"
        return "reject"


Probe = Callable[[HarnessTask, HarnessFiles], ProbeResult]
Verifier = Callable[[HarnessTask, HarnessFiles, ProbeResult], VerificationOutcome]


class HarnessRunResult(BaseModel):
    """Persisted lineage and the outcome of one real Harness² refinement step."""

    run_id: str
    candidate_id: str
    verification_id: str
    decision: Literal["promote", "reject"]
    memory_scope: str
    memory_revision: int = Field(ge=0)
    artifacts_dir: str
    loaded_memory_revision: int = Field(ge=0)
    advisory_probability: float | None = Field(default=None, ge=0, le=1)
