"""Run or replay a real Argon → Harness² → Arena → MongoDB coding demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from builder.codegen import CodePatch, generate_patch
from builder.evidence import SuiteEvidence
from builder.workflow import (
    CodingDemoReport,
    CodingExperiment,
    CodingSettings,
    CodingTaskResult,
    Observer,
)
from harnessing import GeminiHarnessProposer
from harnessing.models import HarnessFiles
from providers import GeminiClient, GeminiSettings
from storage import MongoStore


def present(
    report: CodingDemoReport, console: Console, *, replay: bool = False
) -> None:
    """Present measured broker risk, a real patch, independent checks, and transfer."""
    label = "Recorded live run" if replay else "Verified live run"
    console.print(
        Panel(
            "[bold]One timeout. Two orders. One reusable lesson.[/bold]\n"
            f"{label} · {report.model} · Harness² sequential k=1",
            border_style="cyan",
        )
    )
    first = report.first
    initial = next(row for row in first.baseline.results if row.suite == "training")
    console.print(
        "[bold red]1. The bug[/bold red]  The broker filled the order, "
        "then the response timed out."
    )
    for symbol, quantity in initial.expected_positions.items():
        actual = initial.final_positions.get(symbol, 0)
        console.print(
            f"[bold]Authorized: {quantity:g} {symbol}  →  "
            f"Broker executed: {actual:g} {symbol}[/bold]"
        )
    console.print(
        "[bold cyan]2. The repair[/bold cyan]  Gemini wrote Python code "
        "and updated executor/LESSONS.md."
    )
    lines = first.diff.splitlines()
    code_start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("diff --git a/executor/run.py")
        ),
        0,
    )
    focus = next(
        (
            index
            for index, line in enumerate(lines[code_start:], start=code_start)
            if line.startswith("+")
            and "client_order_id" in line
            and ('"' in line or "'" in line)
            and not line.startswith('+"""')
        ),
        code_start,
    )
    start = max(0, focus - 6)
    console.print(
        Panel(
            Syntax("\n".join(lines[start : start + 34]), "diff", word_wrap=True),
            title=f"Actual repair excerpt · Git {first.git_ref[:10]} · full diff in report",
            border_style="cyan",
        )
    )
    table = Table(title="3. Independent verification", expand=True)
    table.add_column("Task")
    table.add_column("Training + regression")
    table.add_column("Fresh hidden cases")
    table.add_column("Result")
    for item in (first, report.transfer):
        if item is None:
            continue
        table.add_row(
            item.task.replace("_", " "),
            f"{item.public.passed}/{item.public.total}",
            f"{item.hidden_passed}/{item.hidden_total}",
            f"{item.decision.upper()} {item.version or ''}",
        )
    console.print(table)
    console.print(
        Panel(
            Markdown(first.lesson[:1200]),
            title=f"4. Approved lesson persisted in MongoDB · revision {first.memory_revision}",
            border_style="green",
        )
    )
    if report.transfer is not None:
        later = report.transfer
        transfer = Table(title="5. Apply the lesson to an accepted-then-500 failure")
        transfer.add_column("Measured work")
        transfer.add_column("First task")
        transfer.add_column("Later task")
        transfer.add_row(
            "Executor patch proposals",
            str(first.patch_proposals),
            str(later.patch_proposals),
        )
        transfer.add_row(
            "Memory loaded from MongoDB",
            str(first.loaded_memory_revision),
            str(later.loaded_memory_revision),
        )
        transfer.add_row(
            "Approved MongoDB revision",
            str(first.memory_revision),
            str(later.memory_revision),
        )
        transfer.add_row(
            "Measured elapsed seconds",
            f"{first.elapsed_seconds:.1f}",
            f"{later.elapsed_seconds:.1f}",
        )
        transfer.add_row("Execution path", first.mode, later.mode)
        console.print(transfer)
        console.print(
            "The later task starts from the original buggy Python code. "
            "Only approved Builder instructions and lessons transfer."
        )
        if later.mode == "memory_reuse":
            console.print(
                "[green]Approved Builder passed the new gate without another refinement.[/green]"
            )
    console.print(f"[dim]{report.measurement_note}[/dim]")


def _observer(console: Console, events: Path) -> Observer:
    """Record public progress and show compact live stage transitions."""

    def observe(event: str, message: str) -> None:
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "message": message}) + "\n")
        if event == "baseline":
            evidence = SuiteEvidence.model_validate_json(message)
            console.print(
                f"[red]Original Executor: {evidence.passed}/{evidence.total} public cases[/red]"
            )
        elif event in {"building", "public_check", "memory", "reuse_rejected"}:
            console.print(f"[cyan]{event}[/cyan]  {message}")
        elif event in {"promoted", "rejected"}:
            result = CodingTaskResult.model_validate_json(message)
            console.print(
                f"[bold]{result.decision.upper()}[/bold] {result.task}: "
                f"public {result.public.passed}/{result.public.total}, "
                f"hidden {result.hidden_passed}/{result.hidden_total}, "
                f"{result.patch_proposals} patch proposals"
            )

    return observe


def main() -> int:
    """Run two real coding tasks or replay a previously verified report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope", help="MongoDB Builder-memory scope; unique by default"
    )
    parser.add_argument("--runs-dir", type=Path, default=Path("runs/coding-demo"))
    parser.add_argument(
        "--first-only", action="store_true", help="Skip the transfer task"
    )
    parser.add_argument(
        "--replay", type=Path, help="Present an existing report without API calls"
    )
    args = parser.parse_args()
    console = Console()
    if args.replay is not None:
        present(
            CodingDemoReport.model_validate_json(args.replay.read_text()),
            console,
            replay=True,
        )
        return 0
    run_id = uuid4().hex[:12]
    scope = args.scope or f"coding-builder-{run_id}"
    root = args.runs_dir / run_id
    root.mkdir(parents=True, exist_ok=False)
    settings = GeminiSettings.from_env()
    # Code and reflection responses can be longer than the tiny provider smoke tests.
    settings = GeminiSettings.model_validate(
        settings.model_dump() | {"timeout_seconds": 90}
    )
    console.print(
        f"[bold]Live coding run {run_id}[/bold] · {settings.model} · scope {scope}"
    )
    with GeminiClient(settings) as client:
        proposer = GeminiHarnessProposer(client, settings.model, max_output_tokens=4096)

        def generate(
            source: HarnessFiles, harness: HarnessFiles, feedback: str
        ) -> CodePatch:
            return generate_patch(
                client,
                source_files=source,
                harness_files=harness,
                feedback=feedback,
            )

        with MongoStore.from_env() as store:
            store.initialize()
            experiment = CodingExperiment(
                store,
                proposer,
                generate,
                CodingSettings(root=root, scope=scope),
                observe=_observer(console, root / "events.jsonl"),
            )
            first = experiment.run_task("timeout", reuse_memory=False)
        later = None
        if first.decision == "promote" and not args.first_only:
            # Use a fresh connection: transfer relies on persisted MongoDB evidence.
            with MongoStore.from_env() as fresh_store:
                experiment.store = fresh_store
                later = experiment.run_task("server_error", reuse_memory=True)
    report = CodingDemoReport(
        run_id=run_id,
        model=settings.model,
        scope=scope,
        first=first,
        transfer=later,
    )
    report_path = root / "report.json"
    report_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    present(report, console)
    console.print(f"Report: {report_path}")
    console.print(f"Replay: uv run python -m builder.demo --replay {report_path}")
    return (
        0
        if first.decision == "promote"
        and (later is None or later.decision == "promote")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
