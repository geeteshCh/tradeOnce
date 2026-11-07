"""Typed candidate, verification, and approved harness-memory records."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class _Record(BaseModel):
    """Common identity and UTC creation time for a MongoDB document."""

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", validate_assignment=True
    )

    id: str = Field(default_factory=lambda: uuid4().hex, alias="_id", min_length=1)
    created_at: AwareDatetime = Field(
        default_factory=lambda: datetime.now(UTC), validate_default=True
    )

    @field_validator("created_at")
    @classmethod
    def _bson_timestamp(cls, value: datetime) -> datetime:
        """Normalize to UTC and BSON's millisecond precision before saving."""
        utc = value.astimezone(UTC)
        return utc.replace(microsecond=(utc.microsecond // 1000) * 1000)


class BuilderCandidate(_Record):
    """A Builder proposal with source provenance and optional exact harness bytes.

    For harness-only proposals, git_ref identifies the application source checkout;
    this document's ID identifies the generated harness version.
    """

    run_id: str = Field(min_length=1)
    round: int = Field(ge=0)
    candidate: str = Field(min_length=1)
    git_ref: str = Field(min_length=1)
    summary: str = ""
    harness_files: dict[str, str] = Field(default_factory=dict)
    source_files: dict[str, str] = Field(default_factory=dict)


class VerificationResult(_Record):
    """A Judge verdict referencing a candidate; multiple evaluations may be kept."""

    candidate_id: str = Field(min_length=1)
    decision: Literal["promote", "reject"]
    hidden_pass_rate: float = Field(ge=0, le=1)
    invariant_failures: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class HarnessMemory(_Record):
    """A fully rehydratable harness snapshot backed by held-out approval proof."""

    scope: str = Field(min_length=1)
    revision: int = Field(ge=1)
    candidate_id: str = Field(min_length=1)
    verification_id: str = Field(min_length=1)
    git_ref: str = Field(min_length=1)
    harness_files: dict[str, str]
    source_files: dict[str, str] = Field(default_factory=dict)
    library_files: dict[str, str] = Field(default_factory=dict)
