"""Keep the audience view brief and faithful to the actual verification evidence."""

import io
import json
from pathlib import Path

from rich.console import Console

from builder.demo import _observer, present
from builder.workflow import CodingDemoReport


def recorded_run() -> CodingDemoReport:
    return CodingDemoReport.model_validate_json(
        (
            Path(__file__).resolve().parents[1] / "demo/recorded/argon-coding.json"
        ).read_text()
    )


def test_replay_uses_measured_results_without_dumping_code() -> None:
    report = recorded_run()
    output = io.StringIO()
    present(report, Console(file=output, width=100), replay=True)
    text = output.getvalue()
    assert "Recorded live run" in text
    assert "3 repair proposals" in text
    assert "1 repair proposal" in text
    assert "5/5 unseen tests" in text
    assert "client_order_id" not in text
    assert report.first.git_ref not in text


def test_rejected_replay_does_not_claim_memory_was_saved() -> None:
    report = recorded_run()
    rejected = report.first.model_copy(
        update={
            "decision": "reject",
            "memory_revision": 0,
            "version": None,
            "hidden_passed": 0,
        }
    )
    report = report.model_copy(update={"first": rejected, "transfer": None})
    output = io.StringIO()
    present(report, Console(file=output, width=100), replay=True)
    text = output.getvalue()
    assert "Not adopted; original version kept." in text
    assert "No lesson was adopted." in text
    assert "Previous approved lesson" not in text
    assert "First fix" not in text
    assert "Saved the verified lesson" not in text
    assert "0/5 unseen tests" in text


def test_live_view_waits_for_verdict_and_preserves_raw_events(tmp_path: Path) -> None:
    report = recorded_run()
    output = io.StringIO()
    path = tmp_path / "events.jsonl"
    observe = _observer(Console(file=output, width=100), path)
    observe("baseline", report.first.baseline.model_dump_json())
    observe("building", "Argon is writing timeout-patch-01.")
    observe("building", "Argon is writing timeout-patch-02.")
    observe("verifying", "Running independent checks.")
    assert "Accepted" not in output.getvalue()
    assert "Saved the verified lesson" not in output.getvalue()
    observe("promoted", report.first.model_dump_json())
    text = output.getvalue()
    assert text.count("2. The repair") == 1
    assert "5/5 unseen tests" in text
    assert "Saved the verified lesson" in text
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "baseline",
        "building",
        "building",
        "verifying",
        "promoted",
    ]
    assert events[-1]["message"] == report.first.model_dump_json()
