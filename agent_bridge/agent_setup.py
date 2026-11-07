"""The one ai-hedge-fund configuration both runs use (alone and through TradeOnce), so they see identical inputs."""

from __future__ import annotations

from hedge_fund.fund.spec import Fund, FundSpec
from hedge_fund.signals import ALPHA_MODEL_REGISTRY

from recorded_data import CachedAnalyst, cached_analysts

ASSESS_DAY, EXECUTE_DAY = "2024-06-03", "2024-06-04"
UNIVERSE = ["AAPL", "AMZN", "MSFT", "NVDA"]
CAPITAL = 100_000.0
MAX_POSITION_PCT = 0.25


def build_fund() -> Fund:
    analysts = cached_analysts()
    for name in analysts:
        ALPHA_MODEL_REGISTRY[name] = CachedAnalyst   # the agent validates analyst names against its registry
    spec = FundSpec(
        schema_version=2, name="aihf-cached",
        strategies=[{"name": "main", "blend": {"mode": "long_short"},
                     "models": [{"name": n} for n in analysts]}],
        risk={"max_position_pct": MAX_POSITION_PCT, "max_gross_exposure": 1.0},
        costs={"spread_bps": 0, "commission_per_share": 0},
        capital=CAPITAL,
    )
    return Fund(spec, models={"main": list(analysts.values())})
