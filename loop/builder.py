"""Bridge from the Loop to the Builder. A Builder edits one candidate worktree in place and returns a short summary.

    ctx = BuildContext(round, candidate, index, workdir, training_dir, traces_path, project_root)

Builders:
- "aider"            : runs Aider in the worktree (models from TRADEONCE_AIDER_MODELS, comma-separated, one per
                       candidate; extra flags from TRADEONCE_AIDER_ARGS). Aider's --test-cmd is the Arena's
                       training check on this round's training scenarios.
- "scripted:<dir>"   : copies prepared candidate files from <dir>/round_NN/<candidate>/ (testing the loop and a
                       deterministic fallback; label it as scripted in any demo).
- "module:function"  : the Builder team's own implementation, called as function(ctx) -> summary.
"""

from __future__ import annotations

import importlib
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BUILDER_PROMPT = """These training scenarios violate invariants against a simulated, unreliable broker (Alpaca API shape).
Read the failure traces in {traces}. Fix executor/run.py so every intent is executed exactly once and the results you
print match what the broker really holds. You may add helper tools under executor/tools/. Keep the protocol: intents
as JSON on stdin, BROKER_URL in the environment, last stdout line = JSON list of
{{intent_id, status, broker_order_id, filled_qty}} with status in filled|partially_filled|rejected|unknown|failed.
Use client_order_id = intent_id (or "<intent_id>-<n>" for follow-up orders). Do not special-case scenario ids or
timings: hidden tests use different timings, symbols and sizes. Update executor/LESSONS.md with what you learned."""


@dataclass
class BuildContext:
    round: int
    candidate: str
    index: int
    workdir: Path
    training_dir: Path
    traces_path: Path
    project_root: Path


BuilderFn = Callable[[BuildContext], str]


def scripted_builder(base: Path) -> BuilderFn:
    def build(ctx: BuildContext) -> str:
        src = base / f"round_{ctx.round:02d}" / ctx.candidate
        if not src.exists():
            return "no scripted change for this round"
        for f in src.rglob("*"):
            if f.is_file() and f.name != "NOTE.md":
                dest = ctx.workdir / f.relative_to(src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        note = src / "NOTE.md"
        return note.read_text().strip() if note.exists() else "scripted candidate"
    return build


def aider_builder() -> BuilderFn:
    models = [m.strip() for m in os.environ.get("TRADEONCE_AIDER_MODELS", "").split(",") if m.strip()]
    extra = shlex.split(os.environ.get("TRADEONCE_AIDER_ARGS", ""))
    if not models:
        raise SystemExit("set TRADEONCE_AIDER_MODELS (comma-separated, one model per candidate)")

    def build(ctx: BuildContext) -> str:
        model = models[ctx.index % len(models)]
        test_cmd = (f"{shlex.quote(sys.executable)} -m arena.run_training --executor-dir . "
                    f"--scenarios {shlex.quote(str(ctx.training_dir))}")
        files = [str(p.relative_to(ctx.workdir)) for p in (ctx.workdir / "executor").rglob("*.py")]
        lessons = ctx.workdir / "executor" / "LESSONS.md"
        lessons.touch()
        cmd = ["aider", "--model", model, "--yes-always", "--no-auto-commits", "--no-gitignore",
               "--auto-test", "--test-cmd", test_cmd, "--read", str(ctx.traces_path),
               "--message", BUILDER_PROMPT.format(traces=ctx.traces_path), *extra,
               *files, str(lessons.relative_to(ctx.workdir))]
        env = {**os.environ, "PYTHONPATH": str(ctx.project_root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        out = subprocess.run(cmd, cwd=ctx.workdir, env=env, capture_output=True, text=True, timeout=900)
        (ctx.workdir.parent / f"{ctx.candidate}.aider.log").write_text(out.stdout + "\n" + out.stderr)
        return f"aider ({model}) exit {out.returncode}"
    return build


def load_builder(spec: str) -> BuilderFn:
    if spec == "aider":
        return aider_builder()
    if spec.startswith("scripted:"):
        return scripted_builder(Path(spec.split(":", 1)[1]))
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)
