"""Pydantic contracts for the binary questions supported by SubQ Snap."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

DecisionState = str | dict[str, JsonValue] | list[JsonValue]


class BinaryCriteria(BaseModel):
    """Describe what counts as a yes answer and what counts as a no answer."""

    model_config = ConfigDict(extra="forbid")

    true: str = Field(min_length=1)
    false: str = Field(min_length=1)


class BinaryQuestion(BaseModel):
    """A named yes/no rubric question, serialized as SubQ's `noul` type."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["noul"] = "noul"
    instructions: str = Field(min_length=1)
    criteria: BinaryCriteria


class _DecisionRequest(BaseModel):
    """Validate the provider request, including its 1–128 question limit."""

    model: str
    state: DecisionState
    questions: dict[str, BinaryQuestion] = Field(min_length=1, max_length=128)


class BinaryAnswer(BaseModel):
    """A model's probability of yes; this is advisory, not a verified verdict."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["noul"]
    probability_yes: float = Field(alias="noul", ge=0, le=1)


class DecisionUsage(BaseModel):
    """Token counts returned by SubQ for a decision request."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class DecisionResponse(BaseModel):
    """Typed scores keyed by the same question names supplied in the request."""

    model: str
    answers: dict[str, BinaryAnswer] = Field(min_length=1)
    usage: DecisionUsage
