"""Shared data shapes for the Arena. Builder and Judge import these; do not change field names without telling the team."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

FaultType = Literal[
    "accept_then_timeout",      # order is recorded and filled, then the response hangs past the client timeout
    "accept_then_500",          # order is recorded and filled, then the broker answers 500
    "transient_500",            # broker answers 500 and records nothing (retrying is correct)
    "rate_limit_429",           # broker answers 429 and records nothing (retry after a pause is correct)
    "real_reject",              # broker answers 403 insufficient buying power and records nothing
    "stale_status",             # GET order endpoints report status "new", filled_qty "0" for `calls` responses
    "delayed_fill",             # order is accepted as "new" and only fills after `after_calls` more broker calls
    "partial_fill_then_cancel", # order fills `fill_ratio` of qty, then the remainder is canceled
    "duplicate_fill",           # the fill shows up twice in GET /v2/account/activities/FILL (true ledger has one)
]

FAULT_TYPES: tuple[str, ...] = FaultType.__args__  # type: ignore[attr-defined]

# Endpoint keys a fault rule can match. `match` accepts these exactly or as fnmatch globs ("GET /v2/orders*").
ENDPOINT_KEYS = (
    "POST /v2/orders",
    "GET /v2/orders",                      # list
    "GET /v2/orders/{id}",
    "DELETE /v2/orders/{id}",
    "GET /v2/orders:by_client_order_id",
    "GET /v2/positions",
    "GET /v2/account",
    "GET /v2/account/activities/FILL",
)


class Intent(BaseModel):
    """One thing the Executor is asked to do. The Executor must make this happen exactly once."""

    intent_id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0)


class IntentResult(BaseModel):
    """What the Executor claims happened. The Judge compares this against the ledger and never trusts it alone."""

    intent_id: str
    status: Literal["filled", "partially_filled", "rejected", "unknown", "failed"]
    broker_order_id: str | None = None
    filled_qty: int = 0


class FaultRule(BaseModel):
    match: str
    nth: int | list[int] = 1           # fires on the nth matching call (1-based); a list fires on each listed call
    fault: FaultType
    params: dict[str, Any] = Field(default_factory=dict)

    def fires_on(self, n: int) -> bool:
        return n in self.nth if isinstance(self.nth, list) else n == self.nth


class Scenario(BaseModel):
    id: str
    seed: int = 0
    description: str = ""
    intents: list[Intent]
    faults: list[FaultRule] = Field(default_factory=list)
    prices: dict[str, float] = Field(default_factory=dict)   # symbol -> fill price; unknown symbols use 100.0
    starting_cash: float = 100_000.0
    # Final positions a correct Executor ends with. None means "every intent fully filled".
    # Set it when the right answer is to not trade, e.g. {} when every order is really rejected.
    expected_positions: dict[str, float] | None = None

    def intended_positions(self) -> dict[str, float]:
        if self.expected_positions is not None:
            return {s: q for s, q in self.expected_positions.items() if q != 0}
        out: dict[str, float] = {}
        for i in self.intents:
            out[i.symbol] = out.get(i.symbol, 0.0) + (i.qty if i.side == "buy" else -i.qty)
        return {s: q for s, q in out.items() if q != 0}

    @field_validator("intents")
    @classmethod
    def unique_intent_ids(cls, v: list[Intent]) -> list[Intent]:
        ids = [i.intent_id for i in v]
        if len(ids) != len(set(ids)):
            raise ValueError("intent_id values must be unique within a scenario")
        return v


class OrderRequest(BaseModel):
    """Subset of Alpaca's POST /v2/orders body. Alpaca sends qty as a string; we accept both."""

    symbol: str
    qty: float | str
    side: Literal["buy", "sell"]
    type: Literal["market"] = "market"
    time_in_force: Literal["day", "gtc", "ioc"] = "day"
    client_order_id: str | None = Field(default=None, max_length=128)
