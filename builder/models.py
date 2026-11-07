"""Typed values exchanged by the Builder loop."""

from pydantic import BaseModel, ConfigDict, Field


class BuilderProposal(BaseModel):
    """A model-produced description of a proposed code improvement."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: str = Field(min_length=1)
    changes: list[str] = Field(min_length=1)
    lesson: str = Field(min_length=1)


class CandidateResult(BaseModel):
    """The reproducible result of building and probing one candidate."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    worktree: str = Field(min_length=1)
    git_ref: str = Field(min_length=1)
    training_passed: bool
    training_output: str = ""


class JudgeVerdict(BaseModel):
    """A locked Judge's result for a candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(min_length=1)
    decision: str = Field(pattern=r"^(promote|reject)$")
    hidden_pass_rate: float = Field(ge=0, le=1)
    invariant_failures: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def eligible(self) -> bool:
        """Return whether this candidate may be promoted."""
        return self.decision == "promote" and not self.invariant_failures
