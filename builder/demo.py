"""Run or replay a real Argon → Harness² → Arena → MongoDB coding demo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

from rich.console import Console
from rich.status import Status

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


def _problem(evidence: SuiteEvidence, console: Console) -> None:
    """Show the requested trade and observed broker position in one sentence."""
    initial = next(row for row in evidence.results if row.suite == "training")
    for symbol, wanted in initial.expected_positions.items():
        actual = initial.final_positions.get(symbol, 0)
        console.print(f"   Wanted {wanted:g} {symbol}; the broker bought {actual:g}.")


def _verdict(result: CodingTaskResult, console: Console) -> None:
    """Show actual unseen-test evidence and the independent adoption decision."""
    decision = "Accepted."
    if result.decision == "reject":
        retained = "previous" if result.loaded_memory_revision else "original"
        decision = f"Not adopted; {retained} version kept."
    console.print(
        f"   {result.hidden_passed}/{result.hidden_total} unseen tests. {decision}",
        style="green" if result.decision == "promote" else "red",
    )


def _effort(result: CodingTaskResult) -> str:
    """Format actual model proposal count and measured task time."""
    noun = "proposal" if result.patch_proposals == 1 else "proposals"
    return f"{result.patch_proposals} repair {noun}, {result.elapsed_seconds:.0f}s"


def _lesson(result: CodingTaskResult, console: Console) -> None:
    """Describe memory adoption without implying a prior approved lesson exists."""
    if (
        result.decision == "promote"
        and result.memory_revision > result.loaded_memory_revision
    ):
        console.print("   Saved the verified lesson in MongoDB.")
    elif result.loaded_memory_revision:
        console.print("   Previous approved lesson kept.")
    else:
        console.print("   No lesson was adopted.")


def present(
    report: CodingDemoReport, console: Console, *, replay: bool = False
) -> None:
    """Tell the five-part story; detailed source and lineage stay in the report."""
    first = report.first
    if replay:
        console.print("TradeOnce — watch it learn", style="bold")
        console.print("Recorded live run", style="dim")
        console.print("\n1. The mistake", style="bold red")
        _problem(first.baseline, console)
        console.print("   The trade went through. The reply got lost. It bought again.")
        console.print("\n2. The repair", style="bold cyan")
        console.print(
            "   Gemini rewrote the Python: check with the broker before retrying."
        )
        console.print("\n3. The check", style="bold")
        _verdict(first, console)
        console.print("\n4. The lesson", style="bold")
        _lesson(first, console)
        if report.transfer is not None:
            console.print("\n5. The next problem", style="bold cyan")
            if report.transfer.loaded_memory_revision:
                console.print("   Original broken code again, with the saved lesson.")
            _verdict(report.transfer, console)
    console.print("\nMeasured work", style="bold")
    console.print(f"   First task: {_effort(first)}")
    if report.transfer is not None:
        console.print(f"   Next task:  {_effort(report.transfer)}")
        console.print("   Two related tasks; future results can vary.", style="dim")


def _observer(console: Console, events: Path, status: Status | None = None) -> Observer:
    """Keep full events while presenting only real stage changes to the audience.

    Harness² can run candidate probes concurrently. The lock keeps headings and
    event records ordered without exposing candidate chatter or private inputs.
    """
    lock = Lock()
    task_number = 0
    repair_announced = False
    check_announced = False

    def working(message: str) -> None:
        if status is not None:
            status.update(message)

    def observe(event: str, message: str) -> None:
        nonlocal task_number, repair_announced, check_announced
        with lock:
            with events.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "message": message}) + "\n")
            if event == "baseline":
                task_number += 1
                repair_announced = check_announced = False
                if task_number == 1:
                    console.print("\n1. The mistake", style="bold red")
                    _problem(SuiteEvidence.model_validate_json(message), console)
                    console.print(
                        "   The trade went through. The reply got lost. It bought again."
                    )
                else:
                    console.print("\n5. The next problem", style="bold cyan")
                    console.print(
                        "   A different broker error, starting with the original broken code."
                    )
                working("Preparing a repair from the observed failure...")
            elif event == "building":
                if not repair_announced:
                    if task_number == 1:
                        console.print("\n2. The repair", style="bold cyan")
                        console.print("   Gemini is writing a Python repair...")
                    repair_announced = True
                working("AI is writing and testing code changes...")
            elif event == "public_check":
                working("Testing repairs against the known cases...")
            elif event == "verifying":
                if not check_announced:
                    if task_number == 1:
                        console.print("\n3. The check", style="bold")
                    console.print("   Independent checker is trying unseen cases...")
                    check_announced = True
                working("Independent checks are running...")
            elif event == "memory":
                console.print("   Loaded the previously approved lesson.")
                working("Applying the saved lesson to the new problem...")
            elif event == "reuse_rejected":
                console.print(
                    "   First try not adopted; approved version kept.", style="yellow"
                )
                working("Refining the repair after the rejected attempt...")
            elif event in {"promoted", "rejected"}:
                result = CodingTaskResult.model_validate_json(message)
                _verdict(result, console)
                if task_number == 1:
                    console.print("\n4. The lesson", style="bold")
                    _lesson(result, console)
                working(
                    "Repair verified."
                    if result.decision == "promote"
                    else "Keeping the starting version."
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
    console.print("[bold]TradeOnce — watch it learn[/bold]")
    with (
        console.status("Starting the broker test...") as status,
        GeminiClient(settings) as client,
    ):
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
                observe=_observer(console, root / "events.jsonl", status),
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
    console.print(f"Details saved in {report_path}", style="dim")
    return (
        0
        if first.decision == "promote"
        and (later is None or later.decision == "promote")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
