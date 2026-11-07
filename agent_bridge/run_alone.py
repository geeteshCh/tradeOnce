"""Run ai-hedge-fund by itself: its own pipeline and its own SimBroker. No Arena, no Executor, no TradeOnce.

    cd agent_bridge && uv run python run_alone.py

Same inputs as run_baseline.py (real Yahoo closes in recorded/closes.json, cached analyst signals), so this is
the agent's "ideal world" result: a broker that never fails. Compare it with run_baseline.py, where the same
agent meets an unreliable broker.
"""

from __future__ import annotations

import json
from pathlib import Path

from hedge_fund.brokers.sim import SimBroker
from hedge_fund.pipeline.stages import assess_fund, execute_decision

from agent_setup import ASSESS_DAY, CAPITAL, EXECUTE_DAY, MAX_POSITION_PCT, UNIVERSE, build_fund
from recorded_data import RecordedDataClient

HERE = Path(__file__).parent


def main() -> None:
    data, fund, broker = RecordedDataClient(), build_fund(), SimBroker(cash=CAPITAL)
    decision = assess_fund(fund, ASSESS_DAY, data, UNIVERSE)
    record = execute_decision(fund, decision, EXECUTE_DAY, broker, data)

    print(f"ai-hedge-fund alone  |  assess {ASSESS_DAY}, execute {EXECUTE_DAY} at real closes  |  broker: its own SimBroker")
    print("\nsignals (cached analysts):")
    for s in record.strategies[0].signals:
        print(f"  {s.model_name:16} {s.ticker:5} {s.value:+.2f}")
    print("\ntarget weights after its risk limits:", {t: round(w, 3) for t, w in record.final_weights.items()})
    print("orders:", ", ".join(f"{o.side} {o.quantity} {o.ticker} @ {o.price}" for o in record.orders))
    print("\nposition    shares     value   % of NAV")
    for t, sh in sorted(record.positions.items()):
        v = sh * record.marks[t]
        print(f"  {t:6} {sh:>9} {v:>10,.0f}   {v / record.nav:>6.1%}")
    print(f"cash {record.cash:,.2f}   NAV {record.nav:,.2f}   limit {MAX_POSITION_PCT:.0%} per name")

    out = HERE / "reports" / "alone.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(record.model_dump_json(indent=2))
    print(f"\nFull record: agent_bridge/reports/alone.json")


if __name__ == "__main__":
    main()
