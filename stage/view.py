"""Live terminal view of a run:  python -m stage.view [events.jsonl] [--replay] [--speed 1.0]

- Default: follows runs/latest/events.jsonl while the loop writes it.
- --replay: plays a finished run back with pauses (what the demo video is recorded from).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]

PAUSE = {  # seconds before showing each event type in replay mode
    "attack": 1.6, "attack_broke": 2.0, "attack_survived": 2.0, "candidate_proposed": 1.2, "judging": 1.0,
    "candidate_evaluated": 1.4, "verdict": 0.6, "promote": 2.2, "reject": 2.2, "round_start": 1.0, "run_end": 1.0,
}


class State:
    def __init__(self) -> None:
        self.run_id = "?"
        self.version = "v0"
        self.round = 0
        self.timeline: list[dict[str, Any]] = [{"version": "v0", "label": "naive seed", "round": 0}]
        self.rejected: list[dict[str, Any]] = []
        self.attack: dict[str, Any] | None = None
        self.outcome: dict[str, Any] | None = None
        self.cands: dict[str, dict[str, Any]] = {}
        self.feed: list[Text] = []
        self.meta: dict[str, Any] = {}
        self.done = False

    def say(self, round: int, style: str, msg: str) -> None:
        self.feed.append(Text.assemble((f"r{round} ", "dim"), (msg, style)))
        self.feed = self.feed[-12:]

    def apply(self, e: dict[str, Any]) -> None:
        t, d, r = e["type"], e["data"], e["round"]
        if t == "run_start":
            self.run_id, self.meta = d["run_id"], d
            self.say(r, "bold", f"run {d['run_id']} started at v0  ·  hidden set: {d['hidden_count']} scenarios")
        elif t == "round_start":
            self.round, self.attack, self.outcome, self.cands = r, None, None, {}
            self.say(r, "bold cyan", f"round {r} — promoted version {d['version']}")
        elif t == "attack":
            self.attack = d
            self.say(r, "magenta", f"Adversary attacks: {d['scenario_id']}  ({', '.join(d['faults'])})")
        elif t == "attack_broke":
            self.outcome = {"broke": True, **d}
            codes = sorted({f["code"] for f in d["failures"]})
            self.say(r, "red", f"{d['version']} BROKE: {', '.join(codes)}")
        elif t == "attack_survived":
            self.outcome = {"broke": False, **d}
            self.say(r, "green", f"{d['version']} survived the attack — added to regression")
        elif t == "candidate_proposed":
            self.cands[d["candidate"]] = {"summary": d["summary"], "status": "proposed"}
            self.say(r, "yellow", f"Builder proposes {d['candidate']}: {d['summary'][:80]}")
        elif t == "judging":
            for c in self.cands.values():
                c["status"] = "judging"
            n = sum(len(v) for v in d["suites"].values())
            self.say(r, "blue", f"Judge runs {n} scenarios per candidate (regression + training + hidden)")
        elif t == "candidate_evaluated":
            self.cands.setdefault(d["candidate"], {"summary": ""}).update(
                summary_suites=d["summary"], calls=d["broker_calls"], failed=d["failed"], status="evaluated")
        elif t == "promote":
            self.cands.get(d["candidate"], {})["status"] = "promoted"
            self.version = d["version"]
            label = self.cands.get(d["candidate"], {}).get("summary", "")
            self.timeline.append({"version": d["version"], "label": label, "round": r})
            self.say(r, "bold green", f"PROMOTED {d['candidate']} → {d['version']}")
        elif t == "reject":
            self.cands.get(d["candidate"], {})["status"] = "rejected"
            self.rejected.append({"candidate": d["candidate"], "round": r, "reasons": d["reasons"]})
            self.say(r, "bold red", f"ROLLED BACK {d['candidate']}: {d['reasons'][:90]}")
        elif t == "kept_version":
            self.say(r, "yellow", f"no candidate qualified — keeping {d['version']}")
        elif t == "run_end":
            self.done = True
            self.say(r, "bold", f"run finished at {d['version']}")


def suite_cell(s: dict[str, Any] | None, name: str) -> Text:
    if not s or name not in s:
        return Text("…", style="dim")
    p, n = s[name]["passed"], s[name]["total"]
    if n == 0:
        return Text("–", style="dim")
    return Text(f"{p}/{n}", style="green" if p == n else "red")


def render(st: State) -> Layout:
    root = Layout()
    root.split_column(Layout(name="head", size=3), Layout(name="body"), Layout(name="feed", size=14))
    root["body"].split_row(Layout(name="versions", ratio=2), Layout(name="round", ratio=5))

    head = Text.assemble(("TradeOnce ", "bold"), ("— the agent that learns not to lose your money   ", "dim"),
                         ("run ", "dim"), (st.run_id, "bold"), ("   promoted ", "dim"), (st.version, "bold green"),
                         ("   round ", "dim"), (str(st.round), "bold"))
    root["head"].update(Panel(head, border_style="cyan"))

    vt = Table.grid(padding=(0, 1))
    for i, v in enumerate(st.timeline):
        vt.add_row(Text(v["version"], style="bold green" if v["version"] == st.version else "green"),
                   Text(v["label"][:48], style="white" if i else "dim"))
        for rj in [x for x in st.rejected if x["round"] == (st.timeline[i + 1]["round"] if i + 1 < len(st.timeline) else -1)]:
            vt.add_row(Text("  ✗", style="red"), Text(f"{rj['candidate']} rolled back", style="red"))
    trailing = [x for x in st.rejected if x["round"] > st.timeline[-1]["round"]]
    for rj in trailing:
        vt.add_row(Text("  ✗", style="red"), Text(f"{rj['candidate']} rolled back", style="red"))
    root["versions"].update(Panel(vt, title="versions (git tags)", border_style="green"))

    parts: list[Any] = []
    if st.attack:
        a = st.attack
        parts.append(Text.assemble(("Attack  ", "bold magenta"), (a["scenario_id"], "magenta"),
                                   ("   faults: ", "dim"), (", ".join(a["faults"]), "magenta")))
        if a.get("description"):
            parts.append(Text(a["description"], style="dim"))
    if st.outcome:
        o = st.outcome
        if o["broke"]:
            claims = {c["intent_id"]: c.get("filled_qty") for c in o.get("executor_claims", [])}
            parts.append(Text.assemble(("Result  ", "bold red"), (f"{o['version']} broke  ", "red"),
                                       ("executor claims filled ", "dim"), (json.dumps(claims), "white"),
                                       ("  broker holds ", "dim"), (json.dumps(o.get("broker_positions")), "bold white")))
            for f in o["failures"][:3]:
                parts.append(Text(f"  • {f['code']}: {(f.get('message') or '')[:100]}", style="red"))
        else:
            parts.append(Text(f"Result  {o['version']} survived", style="bold green"))
    if st.cands:
        ct = Table(expand=True, header_style="bold")
        for col in ("candidate", "change", "regr", "train", "hidden", "calls", "verdict"):
            ct.add_column(col, no_wrap=col not in ("change",), ratio=4 if col == "change" else None)
        for name, c in st.cands.items():
            status = c.get("status", "")
            style = {"promoted": "bold green", "rejected": "bold red"}.get(status, "yellow")
            ct.add_row(name, Text(c.get("summary", "")[:90]), suite_cell(c.get("summary_suites"), "regression"),
                       suite_cell(c.get("summary_suites"), "training"), suite_cell(c.get("summary_suites"), "hidden"),
                       str(c.get("calls", "…")), Text(status.upper() if status in ("promoted", "rejected") else status, style=style))
        parts.append(ct)
        for name, c in st.cands.items():
            for f in (c.get("failed") or [])[:2]:
                parts.append(Text(f"  {name} failed {f['suite']}/{f['scenario_id']}: {', '.join(f['codes'])}", style="red"))
    root["round"].update(Panel(Group(*parts) if parts else Text("waiting…", style="dim"),
                               title=f"round {st.round}", border_style="magenta"))
    root["feed"].update(Panel(Group(*st.feed), title="events", border_style="dim"))
    return root


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m stage.view")
    ap.add_argument("events", nargs="?", type=Path, default=ROOT / "runs" / "latest" / "events.jsonl")
    ap.add_argument("--replay", action="store_true", help="play a finished run back with pauses")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    args = ap.parse_args()

    st = State()
    console = Console()
    with Live(render(st), console=console, refresh_per_second=8, screen=True) as live:
        if args.replay:
            for line in args.events.read_text().splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                time.sleep(PAUSE.get(e["type"], 0.3) / args.speed)
                st.apply(e)
                live.update(render(st))
            time.sleep(4 / args.speed)
            return
        pos = 0
        while True:
            if args.events.exists():
                with args.events.open() as f:
                    f.seek(pos)
                    for line in f:
                        if line.strip():
                            st.apply(json.loads(line))
                    pos = f.tell()
                live.update(render(st))
            if st.done:
                time.sleep(5)
                return
            time.sleep(0.3)


if __name__ == "__main__":
    main()
