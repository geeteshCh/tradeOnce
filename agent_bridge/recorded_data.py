"""Offline inputs for ai-hedge-fund: recorded closes (its DataClient interface) and cached analyst views.

Both follow the agent's own interfaces, so its real pipeline (blending, risk limits, validation, sizing) runs
unchanged. Only the data source and the analysts are replaced: the analysts are frozen on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

from hedge_fund.data.models import Price
from hedge_fund.models import Signal

HERE = Path(__file__).parent / "recorded"


def _load(name: str) -> dict:
    data = json.loads((HERE / name).read_text())
    data.pop("_note", None)
    return data


class RecordedDataClient:
    """Implements the parts of hedge_fund's DataClient the pipeline uses: one bar per recorded day."""

    def __init__(self, closes: dict[str, dict[str, float]] | None = None):
        self.closes = closes or _load("closes.json")
        self.days = sorted(self.closes)

    def get_prices(self, ticker: str, start_date: str, end_date: str, **kwargs) -> list[Price]:
        out = []
        for day in self.days:
            if start_date <= day <= end_date and ticker in self.closes[day]:
                c = self.closes[day][ticker]
                out.append(Price(open=c, close=c, high=c, low=c, volume=1_000_000, time=f"{day}T00:00:00Z"))
        return out

    def get_snapshot(self, ticker: str):
        return None

    def close(self, day: str, ticker: str) -> float:
        return self.closes[day][ticker]


class CachedAnalyst:
    """Replays a frozen analyst's conviction per (date, ticker). Matches the agent's AlphaModel interface."""

    investment_approach = "long_short"
    horizon_days = 1

    def __init__(self, name: str, views_by_date: dict[str, dict[str, float]]):
        self._name = name
        self._views = views_by_date

    @property
    def name(self) -> str:
        return self._name

    def predict(self, ticker: str, date: str, data_client) -> Signal:
        value = self._views.get(date, {}).get(ticker, 0.0)
        return Signal(model_name=self._name, ticker=ticker, date=date, value=value, metadata={"cached": True})


def cached_analysts() -> dict[str, CachedAnalyst]:
    signals = _load("signals.json")
    names = sorted({n for day in signals.values() for n in day})
    return {n: CachedAnalyst(n, {d: v.get(n, {}) for d, v in signals.items()}) for n in names}
