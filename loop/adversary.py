"""Bridge from the Loop to the Adversary. An Adversary returns the next attack Scenario (or None when out of ideas).

    fn(round, history) -> Scenario | None
    history = [{"round", "scenario_id", "faults", "broke_promoted": bool}, ...]

Adversaries:
- "queue:<dir>"     : replays scenario JSON files from <dir> in name order (testing and the scripted demo).
- "module:function" : the Judge team's red-team LLM.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable

from arena import Scenario

AdversaryFn = Callable[[int, list[dict[str, Any]]], Scenario | None]


def queue_adversary(directory: Path, order: list[str] | None = None) -> AdversaryFn:
    files = sorted(Path(directory).glob("*.json"))
    if order:
        by_stem = {f.stem: f for f in files}
        files = [by_stem[s] for s in order]

    def next_attack(round: int, history: list[dict[str, Any]]) -> Scenario | None:
        used = {h["scenario_id"] for h in history}
        for f in files:
            s = Scenario.model_validate_json(f.read_text())
            if s.id not in used:
                return s
        return None
    return next_attack


def load_adversary(spec: str) -> AdversaryFn:
    if spec.startswith("queue:"):
        rest = spec.split(":", 1)[1]
        directory, _, order = rest.partition("@")
        return queue_adversary(Path(directory), order.split(",") if order else None)
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)
