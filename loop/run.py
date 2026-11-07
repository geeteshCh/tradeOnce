"""The round loop:  python -m loop.run [options]

One round: attack -> (did it break the promoted version?) -> Builder proposes candidates -> Judge evaluates them on
regression + training + hidden -> promote the winner (git tag vN) or reject both and keep the current version.

Everything is written to runs/<run_id>/: events.jsonl (for the live view and replay), the evolution git repo, and
rounds/NN/{attack.json, traces.json, verdict.json}. runs/latest points at the newest run.

Examples:
  python -m loop.run --builder scripted:demo/scripted --adversary queue:arena/scenarios/training@t02_accept_then_timeout,t05_timeout_then_stale,t07_delayed_fill,t06_partial_fill
  python -m loop.run --builder aider --adversary judge_team.adversary:next_attack --judge judge_team.audit:judge_trace
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from arena import Scenario

from .adversary import load_adversary
from .builder import BuildContext, load_builder
from .events import EventLog
from .judge import active, decide, evaluate, evaluate_many, faults_of, load_judge
from .versions import VersionStore

ROOT = Path(__file__).resolve().parents[1]
TRAINING_DIR = ROOT / "arena" / "scenarios" / "training"
DEFAULT_ATTACKS = "t02_accept_then_timeout,t05_timeout_then_stale,t07_delayed_fill,t06_partial_fill"
DEFAULT_BASE = "t01_clean,t04_transient_500,t09_real_reject"
EXECUTOR_CMD = [sys.executable, "-m", "executor.run"]


def load_dir(d: Path, only: list[str] | None = None) -> list[Scenario]:
    files = sorted(Path(d).glob("*.json"))
    if only is not None:
        by = {f.stem: f for f in files}
        missing = [s for s in only if s not in by]
        if missing:
            raise SystemExit(f"scenarios not found in {d}: {missing}")
        files = [by[s] for s in only]
    return [Scenario.model_validate_json(f.read_text()) for f in files]


def slim(ev: dict[str, Any]) -> dict[str, Any]:
    """An evaluation without full traces (for events and verdict files)."""
    return {**ev, "results": [{k: v for k, v in r.items() if k != "trace"} for r in ev["results"]]}


def failing_traces(ev: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in ev["results"]:
        if not r["passed"]:
            t = r["trace"]
            out.append({"scenario": t["scenario"], "failures": r["failures"], "executor_claims": t["executor_claims"],
                        "stderr_tail": t["stderr_tail"], "ledger": t["ledger"]})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m loop.run")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--builder", default="scripted:demo/scripted")
    ap.add_argument("--adversary", default=f"queue:{TRAINING_DIR}@{DEFAULT_ATTACKS}")
    ap.add_argument("--judge", default=None, help="module:function taking a trace; default = auditor.audit_run + Arena checks")
    ap.add_argument("--seed", type=Path, default=ROOT / "demo" / "seed", help="directory containing executor/ (v0)")
    ap.add_argument("--base-training", default=DEFAULT_BASE, help="training scenarios present from round 1")
    ap.add_argument("--hidden", type=Path, default=None, help="hidden set dir (default: auditor/hidden, else hidden_set)")
    ap.add_argument("--candidates", default="a,b")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    args = ap.parse_args(argv)

    hidden_dir = args.hidden or next((d for d in (ROOT / "auditor" / "hidden", ROOT / "hidden_set") if d.exists()), None)
    if hidden_dir is None:
        raise SystemExit("no hidden set: run `python -m loop.make_hidden` or pass --hidden")
    hidden = load_dir(hidden_dir)

    run_id = args.run_id or time.strftime("run-%H%M%S")
    run_dir = args.runs_dir / run_id
    if run_dir.exists():
        raise SystemExit(f"{run_dir} exists")
    latest = args.runs_dir / "latest"
    run_dir.mkdir(parents=True)
    args.seed = args.seed.resolve()
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)

    log = EventLog(run_dir / "events.jsonl")
    store = VersionStore(run_dir)
    builder = load_builder(args.builder)
    adversary = load_adversary(args.adversary)
    judge = load_judge(args.judge)
    names = [n.strip() for n in args.candidates.split(",") if n.strip()]

    store.init({"executor": args.seed / "executor"})
    training = load_dir(TRAINING_DIR, [s for s in args.base_training.split(",") if s])
    regression: list[Scenario] = []
    seen_faults: set[str] = set()
    history: list[dict[str, Any]] = []
    log.emit(0, "loop", "run_start", run_id=run_id, version="v0", builder=args.builder, adversary=args.adversary,
             judge=args.judge or "auditor.audit_run + arena checks", hidden_count=len(hidden), hidden_dir=str(hidden_dir),
             base_training=[s.id for s in training], candidates=names)

    for r in range(1, args.rounds + 1):
        rdir = run_dir / "rounds" / f"{r:02d}"
        rdir.mkdir(parents=True)
        current = store.current()
        log.emit(r, "loop", "round_start", version=current)

        # ---- 1. Adversary's move
        attack = adversary(r, history)
        if attack is None:
            log.emit(r, "adversary", "adversary_exhausted")
            break
        (rdir / "attack.json").write_text(attack.model_dump_json(indent=1))
        log.emit(r, "adversary", "attack", scenario_id=attack.id, description=attack.description,
                 faults=sorted(faults_of(attack)), fault_rules=[f.model_dump() for f in attack.faults])

        # ---- 2. Does it break the promoted version?
        ev = evaluate(store.promoted_dir(), {"attack": [attack]}, judge, EXECUTOR_CMD)
        broke = not ev["all_passed"]
        history.append({"round": r, "scenario_id": attack.id, "faults": sorted(faults_of(attack)), "broke_promoted": broke})
        seen_faults |= faults_of(attack)
        if not broke:
            log.emit(r, "judge", "attack_survived", version=current, scenario_id=attack.id,
                     broker_calls=ev["broker_calls"])
            regression.append(attack)
            continue
        res = ev["results"][0]
        log.emit(r, "judge", "attack_broke", version=current, scenario_id=attack.id,
                 failures=[{k: f.get(k) for k in ("code", "message", "intent_id")} for f in res["failures"]],
                 executor_claims=res["trace"]["executor_claims"], broker_positions=res["trace"]["ledger"]["positions"])
        training.append(attack)

        # ---- 3. Builder's move: traces of what the promoted version fails in training
        train_ev = evaluate(store.promoted_dir(), {"training": training}, judge, EXECUTOR_CMD)
        traces_path = rdir / "traces.json"
        traces_path.write_text(json.dumps(failing_traces(train_ev), indent=1, default=str))
        training_dir = rdir / "training"
        training_dir.mkdir()
        for s in training:
            (training_dir / f"{s.id}.json").write_text(s.model_dump_json(indent=1))

        cand_dirs: dict[str, Path] = {}
        for i, name in enumerate(names):
            branch = f"r{r:02d}-{name}"
            wt = store.new_candidate(branch)
            log.emit(r, "builder", "candidate_started", candidate=branch)
            summary = builder(BuildContext(r, name, i, wt, training_dir, traces_path, ROOT))
            sha = store.commit_candidate(branch, f"{branch}: {summary[:120]}")
            if sha is None:
                log.emit(r, "builder", "candidate_empty", candidate=branch, summary=summary)
                continue
            log.emit(r, "builder", "candidate_proposed", candidate=branch, commit=sha, summary=summary,
                     diffstat=store.diff(branch))
            cand_dirs[branch] = wt

        if not cand_dirs:
            log.emit(r, "loop", "no_candidates", version=current)
            continue

        # ---- 4. Judge: regression + training + hidden (only fault families attacked so far, plus counter-cases)
        suites = {"regression": regression, "training": training, "hidden": active(hidden, seen_faults)}
        log.emit(r, "judge", "judging", candidates=list(cand_dirs),
                 suites={k: [s.id for s in v] for k, v in suites.items()})
        evals = evaluate_many(cand_dirs, suites, judge, EXECUTOR_CMD)
        for name, e in evals.items():
            log.emit(r, "judge", "candidate_evaluated", candidate=name, summary=e["summary"],
                     broker_calls=e["broker_calls"], all_passed=e["all_passed"],
                     failed=[{"suite": x["suite"], "scenario_id": x["scenario_id"],
                              "codes": sorted({f.get("code", "?") for f in x["failures"]})}
                             for x in e["results"] if not x["passed"]])
        decision = decide(evals)
        (rdir / "verdict.json").write_text(json.dumps(
            {"decision": decision, "evaluations": {k: slim(v) for k, v in evals.items()}}, indent=1, default=str))
        log.emit(r, "judge", "verdict", winner=decision["winner"], verdicts=decision["verdicts"])

        # ---- 5. Promote or roll back
        for v in decision["verdicts"]:
            if v["candidate"] == decision["winner"]:
                tag = store.promote(v["candidate"], v["reasons"])
                log.emit(r, "loop", "promote", candidate=v["candidate"], version=tag, previous=current)
            else:
                store.reject(v["candidate"], v["reasons"])
                log.emit(r, "loop", "reject", candidate=v["candidate"], reasons=v["reasons"],
                         invariant_failures=v["invariant_failures"][:5])
        if decision["winner"]:
            regression = regression + [s for s in training if s.id == attack.id]
        else:
            log.emit(r, "loop", "kept_version", version=current)

    log.emit(args.rounds, "loop", "run_end", version=store.current(), git_log=store.log())
    store.cleanup_worktrees()
    print(f"run {run_id}: final version {store.current()}  ({run_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
