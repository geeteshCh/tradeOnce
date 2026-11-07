"""Prompt construction for Builder proposals."""

from pathlib import Path


def build_prompt(
    *,
    failure_trace: str,
    editable_files: list[Path],
    lessons: str,
) -> str:
    """Build a bounded prompt that clearly separates evidence and constraints."""
    files = "\n\n".join(
        f"--- {path} ---\n{path.read_text(encoding='utf-8')}" for path in editable_files
    )
    return f"""Improve the TradeOnce executor using only the supplied evidence.

Constraints:
- Edit only executor/ and executor/tools/.
- Do not edit the Arena, auditor, hidden tests, cached signals, or analyst prompts.
- Preserve the JSON stdin/stdout protocol.
- Update executor/LESSONS.md with the durable lesson.
- A timed-out submission has an unknown outcome; reconcile before retrying.

Failure trace:
{failure_trace}

Existing lessons:
{lessons}

Editable source:
{files}
"""
