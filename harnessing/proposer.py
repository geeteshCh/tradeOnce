"""Gemini-backed proposer that preserves Harness2's public call contract."""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from pathlib import Path

from harness2.core.proposer import Proposer, ProposerError, Reply
from harness2.core.spec import ChannelSpec
from pydantic import BaseModel, Field

from providers.gemini import GeminiClient

_EDIT_FILES = {
    "system_prompt": "systemprompt.md",
    "guardrails": "guardrails.md",
    "memory": "memory.md",
    "tool_guidance": "toolguidance.md",
}


@dataclass(frozen=True)
class GeminiChannel(ChannelSpec):  # type: ignore[misc]
    """Harness2 channel metadata for direct Google GenAI SDK calls."""

    def __post_init__(self) -> None:
        if self.kind != "google_genai":
            raise ValueError("GeminiChannel kind must be 'google_genai'")
        if not self.model.strip():
            raise ValueError("GeminiChannel model must not be empty")


class _HarnessEdits(BaseModel):
    """The complete, fixed write surface available to Gemini."""

    system_prompt: str | None = None
    guardrails: str | None = None
    memory: str | None = None
    tool_guidance: str | None = None
    notes: str = Field(min_length=1)


class _Selection(BaseModel):
    chosen_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    why: str = Field(min_length=1)


class _Experience(BaseModel):
    cue: str = Field(min_length=1)
    lesson: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    confidence: str | None = None
    edits: str | None = None


class GeminiHarnessProposer(Proposer):  # type: ignore[misc]
    """Use Gemini for Harness2 proposals without granting filesystem tools.

    Context is rendered into the model request from the caller-supplied root.
    Harvest calls may update only the four fixed Markdown harness components.
    Harness2 remains responsible for stage-level retries and validation.
    """

    def __init__(
        self,
        client: GeminiClient,
        model: str,
        *,
        max_output_tokens: int = 2048,
    ) -> None:
        """Create a single-attempt proposer around an existing Gemini client."""
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        super().__init__(
            spec=GeminiChannel(kind="google_genai", model=model, timeout=30),
            attempts=1,
        )
        self._client = client
        self._max_output_tokens = max_output_tokens

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
    ) -> Reply:
        """Run one SDK call and return a Harness2-compatible reply.

        Per-call timeout and attempt overrides are accepted for API compatibility;
        transport timeout belongs to the configured client and retries belong to
        the surrounding Harness2 stage.
        """
        del timeout, max_attempts
        started = time.monotonic()
        try:
            root, context = self._context(context_dir)
            prompt = f"{mission}\n\n# CONTEXT FILES\n\n{context}"
            workspace: Path | None = None

            if label.startswith("select."):
                selection = self._client.generate_structured(
                    prompt,
                    _Selection,
                    system_instruction=system or None,
                    max_output_tokens=self._max_output_tokens,
                )
                text = self._selection_text(selection)
            elif label.startswith("experience."):
                experience = self._client.generate_structured(
                    prompt,
                    _Experience,
                    system_instruction=system or None,
                    max_output_tokens=self._max_output_tokens,
                )
                text = self._experience_text(experience)
            elif harvest is not None:
                if harvest != "context/harness" or root is None:
                    raise ValueError(
                        "harvest must be 'context/harness' with a context root"
                    )
                workspace = root / "harness"
                if not workspace.is_dir():
                    raise ValueError("context root has no harness directory")
                edits = self._client.generate_structured(
                    prompt,
                    _HarnessEdits,
                    system_instruction=system or None,
                    max_output_tokens=self._max_output_tokens,
                )
                self._apply_edits(workspace, edits)
                text = f"<notes>{html.escape(edits.notes.strip())}</notes>"
            else:
                text = self._client.generate_text(
                    prompt,
                    system_instruction=system or None,
                    max_output_tokens=self._max_output_tokens,
                )

            reply = Reply(
                label=label,
                text=text,
                ok=True,
                wall_s=round(time.monotonic() - started, 3),
                attempts=1,
                channel="google_genai",
                workspace=workspace,
            )
            self._calls.append(reply)
            return reply
        except Exception as exc:
            elapsed = round(time.monotonic() - started, 3)
            self._calls.append(
                Reply(
                    label=label,
                    text="",
                    ok=False,
                    wall_s=elapsed,
                    attempts=1,
                    channel="google_genai",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise ProposerError(
                f"[{label}] google_genai/{self.spec.model}: {type(exc).__name__}: {exc}",
                attempts=1,
            ) from exc

    @staticmethod
    def _context(context_dir: Path | str | None) -> tuple[Path | None, str]:
        """Render UTF-8 context files after rejecting filesystem escapes."""
        if context_dir is None:
            return None, "(none supplied)"
        supplied = Path(context_dir)
        if supplied.is_symlink():
            raise ValueError("context root must not be a symlink")
        resolved_root = supplied.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("context root must be a directory")

        sections: list[str] = []
        for path in sorted(resolved_root.rglob("*")):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                raise ValueError(
                    f"context symlink escapes its root: {path.relative_to(resolved_root)}"
                )
            relative = path.relative_to(resolved_root)
            resolved_relative = resolved.relative_to(resolved_root)
            env_path = any(
                part.startswith(".env")
                for part in (*relative.parts, *resolved_relative.parts)
            )
            if not resolved.is_file() or env_path:
                continue
            content = resolved.read_text(encoding="utf-8")
            sections.append(f"## context/{relative.as_posix()}\n\n{content}")
        return supplied, "\n\n".join(sections) or "(no readable files)"

    @staticmethod
    def _apply_edits(workspace: Path, edits: _HarnessEdits) -> None:
        for field_name, filename in _EDIT_FILES.items():
            content = getattr(edits, field_name)
            if content is not None:
                (workspace / filename).write_text(content, encoding="utf-8")

    @staticmethod
    def _selection_text(selection: _Selection) -> str:
        why = html.escape(" ".join(selection.why.split()))
        return f"<why>{why}</why>\n```choice\n{selection.chosen_id}\n```"

    @staticmethod
    def _experience_text(experience: _Experience) -> str:
        rows = [
            f"{name}: {html.escape(' '.join(str(value).split()))}"
            for name, value in (
                ("cue", experience.cue),
                ("lesson", experience.lesson),
                ("evidence", experience.evidence),
                ("confidence", experience.confidence),
            )
            if value
        ]
        block = "<experience>\n" + "\n".join(rows) + "\n</experience>"
        if experience.edits:
            edits = experience.edits.replace("</edits>", "&lt;/edits&gt;").strip()
            return f"<edits>\n{edits}\n</edits>\n\n{block}"
        return block
