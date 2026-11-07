"""Fault engine: decides, per broker call, whether to misbehave. Deterministic for a given scenario."""

from __future__ import annotations

from fnmatch import fnmatchcase

from .models import FaultRule


class FaultEngine:
    def __init__(self, rules: list[FaultRule]):
        self.rules = rules
        self.counts = [0] * len(rules)  # matching-call count per rule
        # stale_status windows: rule index -> remaining stale responses
        self._stale_remaining: dict[int, int] = {}

    def next(self, endpoint: str) -> FaultRule | None:
        """Call once per incoming request. Returns the rule that fires, if any (first match wins)."""
        fired: FaultRule | None = None
        for i, rule in enumerate(self.rules):
            if not fnmatchcase(endpoint, rule.match):
                continue
            self.counts[i] += 1
            if fired is not None:
                continue
            if rule.fault == "stale_status":
                if self.counts[i] and rule.fires_on(self.counts[i]):
                    self._stale_remaining[i] = int(rule.params.get("calls", 2))
                if self._stale_remaining.get(i, 0) > 0:
                    self._stale_remaining[i] -= 1
                    fired = rule
            elif rule.fires_on(self.counts[i]):
                fired = rule
        return fired
