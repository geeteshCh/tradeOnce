"""Typed Gemini code generation for the bounded Executor edit surface."""

from collections.abc import Mapping

from pydantic import BaseModel, Field, field_validator

from providers.gemini import GeminiClient

_SOURCE_PATHS = ("executor/run.py", "executor/LESSONS.md")
_HARNESS_PATHS = (
    "systemprompt.md",
    "guardrails.md",
    "memory.md",
    "toolguidance.md",
)
_SYSTEM_INSTRUCTION = """You are the TradeOnce Executor code builder.
Return complete replacement contents for executor/run.py and executor/LESSONS.md.
Use only the supplied source, approved harness guidance, and public training feedback.
Preserve the executor's JSON stdin/stdout protocol and make the smallest safe change.
Do not invent hidden-verifier results, credentials, files, tools, or shell actions."""


class CodePatch(BaseModel):
    """Complete generated contents for the Builder's two editable files."""

    run_source: str = Field(min_length=1)
    lessons: str = Field(min_length=1)
    summary: str = Field(min_length=1)

    @field_validator("run_source", "lessons", "summary")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Reject whitespace-only model fields at the provider boundary."""
        if not value.strip():
            raise ValueError("generated patch fields must not be blank")
        return value


def generate_patch(
    client: GeminiClient,
    *,
    source_files: Mapping[str, str],
    harness_files: Mapping[str, str],
    feedback: str,
    max_output_tokens: int = 8192,
) -> CodePatch:
    """Generate one complete Executor patch from public, caller-supplied evidence.

    The function performs exactly one model call and has no filesystem or execution
    access. Callers own candidate retries (at most two), sandboxing, and verification.
    Only the fixed Executor source and approved four-file harness are admitted.
    """
    sources = _fixed_files(source_files, _SOURCE_PATHS, "source")
    harness = _fixed_files(harness_files, _HARNESS_PATHS, "harness")
    if not feedback.strip():
        raise ValueError("feedback must not be blank")
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")

    prompt = "\n\n".join(
        (
            "# EDITABLE EXECUTOR SOURCE\n" + _render_files(sources),
            "# APPROVED HARNESS GUIDANCE\n" + _render_files(harness),
            "# PUBLIC TRAINING FEEDBACK\n" + feedback,
        )
    )
    return client.generate_structured(
        prompt,
        CodePatch,
        system_instruction=_SYSTEM_INSTRUCTION,
        max_output_tokens=max_output_tokens,
    )


def _fixed_files(
    files: Mapping[str, str], expected_paths: tuple[str, ...], label: str
) -> dict[str, str]:
    """Copy and validate one exact public file surface in stable order."""
    missing = [path for path in expected_paths if path not in files]
    unexpected = sorted(set(files).difference(expected_paths))
    if missing or unexpected:
        raise ValueError(
            f"{label} files must exactly match {list(expected_paths)!r}; "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )
    return {path: files[path] for path in expected_paths}


def _render_files(files: Mapping[str, str]) -> str:
    """Render trusted public files with explicit path boundaries."""
    return "\n\n".join(f"## {path}\n{content}" for path, content in files.items())
