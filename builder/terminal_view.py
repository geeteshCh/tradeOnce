"""Interactive terminal presentation for a real or recorded Builder run.

The live view delegates all model work to :mod:`builder.demo`.  It only reads the
demo's persisted event stream and report, so presentation code cannot influence
generation, verification, or promotion.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import IO, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Footer, Header, Static

from builder.evidence import SuiteEvidence
from builder.workflow import CodingDemoReport, CodingTaskResult

StageStatus = Literal["pending", "current", "complete"]


class DemoEvent(BaseModel):
    """One durable event emitted by the existing coding demo."""

    model_config = ConfigDict(extra="ignore")

    event: str
    message: str


class StageView(BaseModel):
    """Audience-facing state for one chapter of the five-part story."""

    title: str
    summary: str
    details: str = ""
    status: StageStatus = "pending"


class PresentationState(BaseModel):
    """Pure, testable projection of Builder evidence into five story stages."""

    stages: list[StageView] = Field(min_length=5, max_length=5)
    current_stage: int = Field(default=0, ge=0, le=4)
    task_number: int = Field(default=0, ge=0, le=2)
    first_proposals: int | None = Field(default=None, ge=0)
    complete: bool = False

    @classmethod
    def initial(cls) -> PresentationState:
        """Return the honest pending state shown before the first event arrives."""
        titles = (
            "1. The mistake",
            "2. The repair",
            "3. The check",
            "4. The lesson",
            "5. The next problem",
        )
        stages = [
            StageView(title=title, summary="Waiting for real evidence…")
            for title in titles
        ]
        stages[0].status = "current"
        return cls(stages=stages)


def _advance(state: PresentationState, index: int) -> None:
    """Focus the real active chapter, including another repair after a failed gate."""
    for position, stage in enumerate(state.stages):
        if position < index:
            stage.status = "complete"
        elif position == index:
            stage.status = "current"
        else:
            stage.status = "pending"
    state.current_stage = index


def _problem(evidence: SuiteEvidence) -> str:
    training = next(row for row in evidence.results if row.suite == "training")
    symbol, wanted = next(iter(training.expected_positions.items()))
    actual = training.final_positions.get(symbol, 0)
    return f"Wanted {wanted:g} {symbol}; the broker bought {actual:g}."


def _decision(result: CodingTaskResult) -> str:
    """Describe the observed trade outcome; keep test counts in expandable proof."""
    if result.decision == "promote":
        return f"Repair accepted. {_problem(result.public)}"
    retained = "previous" if result.loaded_memory_revision else "original"
    return (
        f"Not adopted; {retained} version kept."
    )


def _lesson(result: CodingTaskResult) -> str:
    if result.decision == "promote" and result.memory_revision > result.loaded_memory_revision:
        return "Saved the repair lesson for the next problem."
    if result.loaded_memory_revision:
        return "Kept the previous approved lesson."
    return "No lesson was adopted."


def apply_demo_event(state: PresentationState, event: DemoEvent) -> PresentationState:
    """Return a new presentation state after one actual Builder event.

    Malformed evidence is deliberately allowed to raise validation errors: a demo
    must never turn missing or invalid results into a friendly-looking success.
    """
    updated = state.model_copy(deep=True)
    kind = event.event
    if kind == "baseline":
        updated.task_number += 1
        evidence = SuiteEvidence.model_validate_json(event.message)
        if updated.task_number == 1:
            _advance(updated, 0)
            updated.stages[0].summary = _problem(evidence)
            updated.stages[0].details = (
                "The broker accepted the trade, but its reply was lost. "
                "The original code retried and bought twice."
            )
        else:
            _advance(updated, 4)
            updated.stages[4].summary = "A related broker failure starts from the original broken code."
            updated.stages[4].details = _problem(evidence)
    elif kind in {"building", "public_check", "reuse_rejected"}:
        index = 1 if updated.task_number <= 1 else 4
        _advance(updated, index)
        stage = updated.stages[index]
        if kind == "building":
            stage.summary = "Gemini is writing and testing a real Python repair."
        elif kind == "public_check":
            stage.summary = "Testing the repair against the known cases."
            stage.details = event.message
        else:
            stage.summary = "The first try was rejected; the approved version stayed in place."
    elif kind == "verifying":
        index = 2 if updated.task_number <= 1 else 4
        _advance(updated, index)
        updated.stages[index].summary = "Checking that it buys the right amount when broker replies go wrong."
    elif kind == "memory":
        _advance(updated, 4)
        updated.stages[4].summary = "Loaded the previously approved lesson for a related failure."
    elif kind in {"promoted", "rejected"}:
        result = CodingTaskResult.model_validate_json(event.message)
        if updated.task_number <= 1:
            _advance(updated, 3)
            updated.first_proposals = result.patch_proposals
            updated.stages[1].summary = (
                f"{result.patch_proposals} repair proposal(s) in {result.elapsed_seconds:.0f}s."
            )
            updated.stages[2].summary = _decision(result)
            updated.stages[2].details = (
                f"Independent checks: {result.hidden_passed}/{result.hidden_total} unseen cases passed.\n"
                f"Verification: {result.verification_id}"
            )
            updated.stages[3].summary = _lesson(result)
            updated.stages[3].details = result.lesson
        else:
            _advance(updated, 4)
            updated.stages[4].summary = _decision(result)
            if result.decision == "promote" and updated.first_proposals is not None:
                updated.stages[4].summary = (
                    f"First repair: {updated.first_proposals} attempts → "
                    f"related problem: {result.patch_proposals}."
                )
            updated.stages[4].details = (
                f"{_decision(result)}\n"
                f"{result.patch_proposals} repair proposal(s), {result.elapsed_seconds:.0f}s.\n"
                f"Saved lesson {'loaded' if result.loaded_memory_revision else 'not loaded'}. "
                f"Independent checks: {result.hidden_passed}/{result.hidden_total} unseen cases passed.\n"
                "Two different tasks; this run shows the comparison, not a guaranteed speedup."
            )
            updated.stages[4].status = "complete"
            updated.complete = True
    return updated


def presentation_from_report(report: CodingDemoReport) -> PresentationState:
    """Build a complete replay state from a validated, recorded report."""
    state = PresentationState.initial()
    state = apply_demo_event(
        state, DemoEvent(event="baseline", message=report.first.baseline.model_dump_json())
    )
    state = apply_demo_event(state, DemoEvent(event="building", message="recorded"))
    state = apply_demo_event(state, DemoEvent(event="verifying", message="recorded"))
    state = apply_demo_event(
        state,
        DemoEvent(
            event="promoted" if report.first.decision == "promote" else "rejected",
            message=report.first.model_dump_json(),
        ),
    )
    if report.transfer is not None:
        state = apply_demo_event(
            state,
            DemoEvent(event="baseline", message=report.transfer.baseline.model_dump_json()),
        )
        if report.transfer.loaded_memory_revision:
            state = apply_demo_event(state, DemoEvent(event="memory", message="recorded"))
        state = apply_demo_event(
            state,
            DemoEvent(
                event=(
                    "promoted"
                    if report.transfer.decision == "promote"
                    else "rejected"
                ),
                message=report.transfer.model_dump_json(),
            ),
        )
    else:
        state.stages[3].status = "complete"
        state.complete = True
    return state


class TerminalDemoApp(App[None]):
    """Textual five-stage view backed by a real subprocess or a recorded report."""

    TITLE = "TradeOnce — watch it learn"
    BINDINGS: ClassVar = [("q", "quit", "Quit")]
    CSS = """
    Screen { max-width: 100; align-horizontal: center; }
    #banner { width: 100%; padding: 1 2; background: $boost; text-style: bold; }
    #stages { width: 100%; height: 1fr; }
    Collapsible { width: 100%; margin: 0 1; }
    .stage-body { padding: 0 2 1 2; }
    """

    def __init__(self, replay: Path | None = None) -> None:
        super().__init__()
        self.replay = replay
        self.state = PresentationState.initial()
        self._process: subprocess.Popen[str] | None = None
        self._log_stream: IO[str] | None = None
        self._events_path: Path | None = None
        self._event_offset = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("RECORDED LIVE" if self.replay else "● LIVE · real Gemini model", id="banner")
        with VerticalScroll(id="stages"):
            for index, stage in enumerate(self.state.stages):
                yield Collapsible(
                    Static(stage.summary, classes="stage-body", id=f"body-{index}", markup=False),
                    title=stage.title,
                    collapsed=index != 0,
                    id=f"stage-{index}",
                )
        yield Footer()

    def on_mount(self) -> None:
        """Load a replay or start exactly one owned coding-demo subprocess."""
        if self.replay is not None:
            report = CodingDemoReport.model_validate_json(self.replay.read_text(encoding="utf-8"))
            self.state = presentation_from_report(report)
            self._render(force_stage=None)
            return
        run_root = Path("runs/terminal-view") / uuid4().hex[:12]
        data_root = run_root / "data"
        data_root.mkdir(parents=True, exist_ok=False)
        log_path = run_root / "builder.log"
        log_path.touch(mode=0o600)
        os.chmod(log_path, 0o600)
        self._log_stream = log_path.open("w", encoding="utf-8")
        self._process = subprocess.Popen(
            [sys.executable, "-m", "builder.demo", "--runs-dir", str(data_root)],
            stdout=self._log_stream,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._events_path = data_root
        self.set_interval(0.3, self._poll)

    def _poll(self) -> None:
        """Consume newly persisted events without inspecting private fixtures."""
        if self._events_path is None:
            return
        if self._events_path.is_dir() and self._events_path.name == "data":
            matches = list(self._events_path.glob("*/events.jsonl"))
            if matches:
                self._events_path = matches[0]
        if self._events_path.is_file():
            with self._events_path.open(encoding="utf-8") as stream:
                stream.seek(self._event_offset)
                payload = stream.read()
                # The producer may still be writing its last JSONL record.
                complete_payload, _, _ = payload.rpartition("\n")
                if "\n" in payload:
                    self._event_offset += len((complete_payload + "\n").encode("utf-8"))
                else:
                    complete_payload = ""
                for line in complete_payload.splitlines():
                    previous = self.state.current_stage
                    self.state = apply_demo_event(self.state, DemoEvent.model_validate_json(line))
                    force = self.state.current_stage if self.state.current_stage != previous else None
                    self._render(force_stage=force)
        if self._process is not None and self._process.poll() is not None:
            report_loaded = False
            if self._events_path.is_file():
                report_path = self._events_path.with_name("report.json")
                if report_path.exists():
                    report = CodingDemoReport.model_validate_json(
                        report_path.read_text(encoding="utf-8")
                    )
                    self.state = presentation_from_report(report)
                    self._render(force_stage=None)
                    report_loaded = True
            banner = self.query_one("#banner", Static)
            if self._process.returncode == 0 and report_loaded:
                banner.update("COMPLETE · real model run")
            else:
                banner.update("STOPPED · no complete report; see private builder log")
            self._process = None

    def _render(self, *, force_stage: int | None) -> None:
        """Update copy; only an actual stage change overrides manual folding."""
        for index, stage in enumerate(self.state.stages):
            body = stage.summary
            if stage.details:
                body += f"\n\n{stage.details}"
            self.query_one(f"#body-{index}", Static).update(body)
            panel = self.query_one(f"#stage-{index}", Collapsible)
            marker = {"pending": "○", "current": "●", "complete": "✓"}[stage.status]
            panel.title = f"{marker} {stage.title} — {stage.summary}"
            if force_stage is not None:
                panel.collapsed = index != force_stage

    def on_unmount(self) -> None:
        """Stop only the subprocess group owned by this view."""
        process = self._process
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        if self._log_stream is not None:
            self._log_stream.close()


def main() -> int:
    """Run the interactive presentation with live evidence or a recorded report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, help="Validated report.json; makes no API calls")
    args = parser.parse_args()
    TerminalDemoApp(replay=args.replay).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
