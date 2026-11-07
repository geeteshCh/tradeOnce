"""Simulated broker with Alpaca's API shape. Point an Alpaca client's base URL here instead of paper-api.alpaca.markets."""

from __future__ import annotations

import asyncio
import copy
import os
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .models import OrderRequest, Scenario
from .state import ArenaState

OPEN = {"new", "accepted", "pending_new", "partially_filled"}
CLOSED = {"filled", "canceled", "rejected", "expired"}


def _err(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def create_app(admin_token: str | None = None) -> FastAPI:
    admin_token = admin_token or os.environ.get("ARENA_ADMIN_TOKEN") or secrets.token_hex(16)
    app = FastAPI(title="TradeOnce Arena")
    state = ArenaState()
    app.state.arena = state
    app.state.admin_token = admin_token

    def require_admin(token: str | None) -> None:
        if token != admin_token:
            raise HTTPException(403, "admin token required")

    def begin(request: Request, endpoint: str, body: Any = None) -> tuple[dict[str, Any], Any]:
        state.tick()
        rule = state.faults.next(endpoint)
        entry = state.log_call(request.method, request.url.path + (f"?{request.url.query}" if request.url.query else ""),
                               endpoint, body, rule.fault if rule else None)
        return entry, rule

    def done(entry: dict[str, Any], resp: Response) -> Response:
        entry["status_code"] = resp.status_code
        return resp

    async def get_fault_response(entry, rule) -> Response | None:
        """Faults that apply to read endpoints. stale_status is handled by the caller."""
        if rule is None:
            return None
        if rule.fault in ("transient_500", "accept_then_500"):
            return done(entry, _err(500, 50010000, "internal server error"))
        if rule.fault == "rate_limit_429":
            return done(entry, _err(429, 42910000, "rate limit exceeded"))
        if rule.fault == "accept_then_timeout":
            await asyncio.sleep(float(rule.params.get("delay_s", 2.0)))
        return None

    def stale(order: dict[str, Any]) -> dict[str, Any]:
        o = copy.deepcopy(order)
        o.update(status="new", filled_qty="0", filled_avg_price=None, filled_at=None)
        return o

    # ------------------------------------------------------------------ admin (orchestrator + Judge only)
    @app.get("/_arena/health")
    async def health():
        return {"ok": True, "scenario_id": state.scenario.id}

    @app.post("/_arena/load")
    async def load(scenario: Scenario, x_arena_admin: str | None = Header(default=None)):
        require_admin(x_arena_admin)
        state.load(scenario)
        return {"ok": True, "scenario_id": scenario.id}

    @app.post("/_arena/settle")
    async def settle(x_arena_admin: str | None = Header(default=None)):
        require_admin(x_arena_admin)
        state.settle()
        return {"ok": True}

    @app.get("/_arena/ledger")
    async def ledger(x_arena_admin: str | None = Header(default=None)):
        require_admin(x_arena_admin)
        return state.ledger()

    # ------------------------------------------------------------------ Alpaca-shaped endpoints (Executor)
    @app.post("/v2/orders")
    async def create_order(req: OrderRequest, request: Request):
        entry, rule = begin(request, "POST /v2/orders", req.model_dump())
        fault = rule.fault if rule else None
        p = rule.params if rule else {}

        # Faults where the broker records nothing.
        if fault == "transient_500":
            return done(entry, _err(500, 50010000, "internal server error"))
        if fault == "rate_limit_429":
            return done(entry, _err(429, 42910000, "rate limit exceeded"))
        if fault == "real_reject":
            return done(entry, _err(403, 40310000, "insufficient buying power"))

        if req.client_order_id and req.client_order_id in state.by_coid:
            return done(entry, _err(422, 40010001, "client_order_id must be unique"))
        try:
            if float(req.qty) <= 0:
                raise ValueError
        except ValueError:
            return done(entry, _err(422, 40010000, "qty must be > 0"))

        order = state.new_order(req)
        entry["order_id"] = order["id"]

        if fault == "delayed_fill":
            state.fill_later(order, int(p.get("after_calls", 3)))
        elif fault == "partial_fill_then_cancel":
            state.partial_then_cancel(order, float(p.get("fill_ratio", 0.5)))
        else:
            state.fill_now(order, duplicate_activity=(fault == "duplicate_fill"))

        # Faults where the order already exists but the client does not learn it.
        if fault == "accept_then_timeout":
            await asyncio.sleep(float(p.get("delay_s", 2.0)))
        if fault == "accept_then_500":
            return done(entry, _err(500, 50010000, "internal server error"))
        return done(entry, JSONResponse(copy.deepcopy(order)))

    @app.get("/v2/orders")
    async def list_orders(request: Request, status: str = "open", limit: int = 50):
        entry, rule = begin(request, "GET /v2/orders")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        orders = list(state.orders.values())
        if status == "open":
            orders = [o for o in orders if o["status"] in OPEN]
        elif status == "closed":
            orders = [o for o in orders if o["status"] in CLOSED]
        orders = orders[-limit:][::-1]
        if rule and rule.fault == "stale_status":
            orders = [stale(o) for o in orders]
        return done(entry, JSONResponse(copy.deepcopy(orders)))

    @app.get("/v2/orders:by_client_order_id")
    async def get_by_coid(request: Request, client_order_id: str):
        entry, rule = begin(request, "GET /v2/orders:by_client_order_id")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        oid = state.by_coid.get(client_order_id)
        if oid is None:
            return done(entry, _err(404, 40410000, "order not found"))
        order = state.orders[oid]
        return done(entry, JSONResponse(stale(order) if rule and rule.fault == "stale_status" else copy.deepcopy(order)))

    @app.get("/v2/orders/{order_id}")
    async def get_order(order_id: str, request: Request):
        entry, rule = begin(request, "GET /v2/orders/{id}")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        order = state.orders.get(order_id)
        if order is None:
            return done(entry, _err(404, 40410000, "order not found"))
        return done(entry, JSONResponse(stale(order) if rule and rule.fault == "stale_status" else copy.deepcopy(order)))

    @app.delete("/v2/orders/{order_id}")
    async def cancel_order(order_id: str, request: Request):
        entry, rule = begin(request, "DELETE /v2/orders/{id}")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        order = state.orders.get(order_id)
        if order is None:
            return done(entry, _err(404, 40410000, "order not found"))
        if order["status"] not in OPEN:
            return done(entry, _err(422, 42210000, f"order is not cancelable (status {order['status']})"))
        state.pending = [p for p in state.pending if p["order_id"] != order_id]
        order["status"] = "canceled"
        order["canceled_at"] = order["updated_at"]
        return done(entry, Response(status_code=204))

    @app.get("/v2/positions")
    async def positions(request: Request):
        entry, rule = begin(request, "GET /v2/positions")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        return done(entry, JSONResponse(state.positions_view()))

    @app.get("/v2/account")
    async def account(request: Request):
        entry, rule = begin(request, "GET /v2/account")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        return done(entry, JSONResponse(state.account_view()))

    @app.get("/v2/account/activities/FILL")
    async def fill_activities(request: Request):
        entry, rule = begin(request, "GET /v2/account/activities/FILL")
        if (r := await get_fault_response(entry, rule)) is not None:
            return r
        return done(entry, JSONResponse(copy.deepcopy(state.activities[::-1])))

    return app
