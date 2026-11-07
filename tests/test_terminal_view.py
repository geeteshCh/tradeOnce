"""Verify terminal folding and honest projection of real Builder evidence."""

import asyncio
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("textual")

from textual.widgets import Collapsible, Static

from builder.terminal_view import (
    DemoEvent,
    PresentationState,
    TerminalDemoApp,
    apply_demo_event,
    presentation_from_report,
)
from builder.workflow import CodingDemoReport

REPORT_PATH = Path(__file__).resolve().parents[1] / "demo/recorded/argon-coding.json"


def recorded_run() -> CodingDemoReport:
    return CodingDemoReport.model_validate_json(REPORT_PATH.read_text())


def test_report_uses_measured_verdict_and_does_not_adopt_rejected_lesson() -> None:
    report = recorded_run()
    state = presentation_from_report(report)
    assert "Wanted 1 AAPL; the broker bought 1" in state.stages[2].summary
    assert "5/5" not in state.stages[2].summary
    assert "Lost reply: no extra shares" in state.stages[2].details
    assert "Rejected trade: nothing was bought" in state.stages[2].details
    assert "3 attempts → related problem: 1" in state.stages[4].summary
    for stage in state.stages:
        assert "5/5" not in stage.summary + stage.details
        assert report.first.verification_id not in stage.summary + stage.details
        assert report.transfer.verification_id not in stage.summary + stage.details
    assert "1 repair proposal" in state.stages[4].details
    rejected = report.first.model_copy(
        update={"decision": "reject", "memory_revision": 0, "hidden_passed": 0}
    )
    state = presentation_from_report(
        report.model_copy(update={"first": rejected, "transfer": None})
    )
    assert "0/5" not in state.stages[2].details
    assert "original version kept" in state.stages[2].summary
    assert state.stages[3].summary == "No lesson was adopted."
    rejected_transfer = report.transfer.model_copy(update={"decision": "reject"})
    state = presentation_from_report(report.model_copy(update={"transfer": rejected_transfer}))
    assert "Not adopted" in state.stages[4].summary
    assert "First repair:" not in state.stages[4].summary


def test_pending_and_public_results_never_claim_independent_success() -> None:
    initial = PresentationState.initial()
    state = apply_demo_event(
        initial, DemoEvent(event="public_check", message="candidate-1: 1/4 public cases.")
    )
    assert initial.current_stage == 0  # reducer does not mutate its input
    assert "passed" not in state.stages[1].summary.lower()
    assert "1/4" not in state.stages[1].details
    assert "recorded holdings" in state.stages[1].details
    assert all("Accepted" not in stage.summary for stage in state.stages)
    state = apply_demo_event(state, DemoEvent(event="verifying", message="Checking."))
    state = apply_demo_event(state, DemoEvent(event="building", message="Another repair."))
    assert state.current_stage == 1
    assert state.stages[2].status == "pending"


def test_replay_headers_open_actual_details_without_starting_a_model() -> None:
    async def exercise() -> None:
        app = TerminalDemoApp(replay=REPORT_PATH)
        with patch("builder.terminal_view.subprocess.Popen") as spawn:
            async with app.run_test(size=(110, 38)) as pilot:
                await pilot.pause()
                assert "RECORDED" in str(app.query_one("#banner", Static).render())
                section = app.query_one("#stage-2", Collapsible)
                assert section.collapsed
                await pilot.click("#stage-2 CollapsibleTitle")
                await pilot.pause()
                assert not section.collapsed
                assert "Lost reply: no extra shares" in str(app.query_one("#body-2", Static).render())
                spawn.assert_not_called()

    asyncio.run(exercise())


def test_live_events_advance_sections_and_keep_manual_folding_until_next_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    report = recorded_run()
    process = Mock()
    process.poll.return_value = None

    async def exercise() -> None:
        app = TerminalDemoApp()
        with patch("builder.terminal_view.subprocess.Popen", return_value=process) as spawn:
            async with app.run_test(size=(110, 38)) as pilot:
                await pilot.pause()
                argv = spawn.call_args.args[0]
                assert argv[1:3] == ["-m", "builder.demo"]
                root = Path(argv[-1])
                events = root / "test-run" / "events.jsonl"
                events.parent.mkdir()

                def emit(kind: str, message: str) -> None:
                    with events.open("a") as stream:
                        stream.write(DemoEvent(event=kind, message=message).model_dump_json() + "\n")
                    app._poll()

                baseline = DemoEvent(
                    event="baseline", message=report.first.baseline.model_dump_json()
                ).model_dump_json()
                events.write_text(baseline[:30])
                app._poll()
                assert app.state.task_number == 0
                with events.open("a") as stream:
                    stream.write(baseline[30:] + "\n")
                app._poll()
                assert app.state.task_number == 1
                app._poll()
                assert app.state.task_number == 1  # each complete record consumed once
                emit("building", "Writing candidate 1.")
                await pilot.pause()
                assert app.query_one("#stage-0", Collapsible).collapsed
                assert not app.query_one("#stage-1", Collapsible).collapsed
                await pilot.click("#stage-0 CollapsibleTitle")
                await pilot.pause()
                assert not app.query_one("#stage-0", Collapsible).collapsed
                emit("public_check", "candidate-1: 1/4 public cases.")
                await pilot.pause()
                assert not app.query_one("#stage-0", Collapsible).collapsed
                emit("verifying", "Independent checks running.")
                await pilot.pause()
                assert app.query_one("#stage-0", Collapsible).collapsed
                assert app.query_one("#stage-1", Collapsible).collapsed
                assert not app.query_one("#stage-2", Collapsible).collapsed
                process.poll.return_value = 0

    asyncio.run(exercise())
