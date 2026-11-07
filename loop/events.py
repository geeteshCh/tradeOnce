"""events.jsonl: every step of a run, one JSON object per line. The live view and replay read this file.

Event shape (shared interface): {ts, round, actor, type, data}
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, round: int, actor: str, type: str, **data: Any) -> dict[str, Any]:
        event = {"ts": round_ts(), "round": round, "actor": actor, "type": type, "data": data}
        with self.path.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
        return event


def round_ts() -> float:
    return round(time.time(), 3)


def read_events(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
