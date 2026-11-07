import json
import subprocess
import sys
from pathlib import Path

import pytest

from arena import Scenario, arena_server, run_scenario
from loop.events import read_events
from loop.judge import decide, default_judge, ledger_to_snapshot
from loop.make_hidden import HIDDEN
from loop.run import main as run_loop

ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "arena" / "scenarios" / "training"
V0 = ROOT / "demo" / "seed"
V1 = ROOT / "demo" / "scripted" / "round_01" / "a"
CMD = [sys.executable, "-m", "executor.run"]


def trace_for(code_dir: Path, scenario_file: str) -> dict:
    with arena_server() as a:
        return run_scenario(json.loads((TRAINING / scenario_file).read_text()), a.base_url, a.admin_token, code_dir, CMD)


def test_snapshot_flags_v0_duplicate_as_overfill_and_unsafe_retry():
    trace = trace_for(V0, "t02_accept_then_timeout.json")
    intent = trace["scenario"]["intents"][0]
    snap = ledger_to_snapshot(trace, intent, include_unknown=True)
    assert len(snap["orders"]) == 2 and all(o["intent_id"] == "i1" for o in snap["orders"])
    assert [e["type"] for e in snap["events"]][:2] == ["submit_unknown", "retry"]
    codes = {f["code"] for f in default_judge(trace)}
    assert {"OVERFILL", "UNSAFE_RETRY"} <= codes


def test_v1_passes_timeout_scenario():
    assert default_judge(trace_for(V1, "t02_accept_then_timeout.json")) == []


def test_decide_rule():
    def ev(all_passed, hidden_passed, calls):
        return {"all_passed": all_passed, "broker_calls": calls, "results": [] if all_passed else
                [{"passed": False, "suite": "hidden", "scenario_id": "h", "failures": [{"code": "X"}]}],
                "summary": {"hidden": {"passed": hidden_passed, "total": 4}}}
    d = decide({"a": ev(True, 4, 30), "b": ev(True, 4, 20), "c": ev(False, 3, 10)})
    assert d["winner"] == "b"
    assert {v["candidate"]: v["decision"] for v in d["verdicts"]} == {"a": "reject", "b": "promote", "c": "reject"}
    assert decide({"a": ev(False, 1, 5)})["winner"] is None


def test_hidden_scenarios_validate():
    for s in HIDDEN:
        Scenario.model_validate(s)


def test_scripted_loop_two_rounds(tmp_path):
    hidden = tmp_path / "hidden"
    subprocess.run([sys.executable, "-m", "loop.make_hidden", "--out", str(hidden)], cwd=ROOT, check=True,
                   capture_output=True)
    rc = run_loop(["--rounds", "2", "--runs-dir", str(tmp_path / "runs"), "--run-id", "t", "--hidden", str(hidden)])
    assert rc == 0
    events = read_events(tmp_path / "runs" / "t" / "events.jsonl")
    promotes = [(e["data"]["candidate"], e["data"]["version"]) for e in events if e["type"] == "promote"]
    rejects = {e["data"]["candidate"] for e in events if e["type"] == "reject"}
    assert promotes == [("r01-a", "v1"), ("r02-b", "v2")]
    assert rejects == {"r01-b", "r02-a"}
    # r02-a overfits: it passes all training but fails the hidden long-stale case.
    r02a = next(e for e in events if e["type"] == "candidate_evaluated" and e["data"]["candidate"] == "r02-a")
    assert r02a["data"]["summary"]["training"]["passed"] == r02a["data"]["summary"]["training"]["total"]
    assert [f["scenario_id"] for f in r02a["data"]["failed"]] == ["h08_timeout_stale_long"]
