"""Fault-injecting proxy in front of a real broker (Alpaca PAPER) or a local Arena.

    Executor ──BROKER_URL──▶ this proxy ──(adds Alpaca auth)──▶ https://paper-api.alpaca.markets
                               │
                               └─ injects faults on the way back, from the same fault rules as the Arena

Real brokers don't fail on demand. This makes them: the order really reaches Alpaca paper, then the proxy
withholds or rewrites the response. Only faults that can be produced in transit are supported; the broker-side
ones (delayed_fill, partial_fill_then_cancel, duplicate_fill) come from Alpaca itself or the Arena.

Safety: the upstream must be Alpaca's paper endpoint or localhost. Live trading is refused, always.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .faults import FaultEngine
from .models import FaultRule

PAPER_HOST = "paper-api.alpaca.markets"
PROXY_FAULTS = {"accept_then_timeout", "accept_then_500", "transient_500", "rate_limit_429", "real_reject", "stale_status"}


def check_upstream(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host != PAPER_HOST and host not in ("127.0.0.1", "localhost"):
        raise ValueError(f"refusing upstream {url!r}: only {PAPER_HOST} (paper) or a local Arena is allowed")
    return url.rstrip("/")


def _endpoint(method: str, path: str) -> str:
    """Map a request onto the Arena's endpoint keys so the same fault rules work here."""
    if path == "/v2/orders":
        return f"{method} /v2/orders"
    if path.startswith("/v2/orders:by_client_order_id"):
        return "GET /v2/orders:by_client_order_id"
    if path.startswith("/v2/orders/"):
        return f"{method} /v2/orders/{{id}}"
    return f"{method} {path}"


def _err(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _stale(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_stale(o) for o in obj]
    if isinstance(obj, dict) and "status" in obj:
        return {**obj, "status": "new", "filled_qty": "0", "filled_avg_price": None, "filled_at": None}
    return obj


def create_proxy(upstream: str, admin_token: str | None = None, key_id: str | None = None,
                 secret_key: str | None = None) -> FastAPI:
    upstream = check_upstream(upstream)
    admin_token = admin_token or secrets.token_hex(16)
    auth = {"APCA-API-KEY-ID": key_id or os.environ.get("APCA_API_KEY_ID", ""),
            "APCA-API-SECRET-KEY": secret_key or os.environ.get("APCA_API_SECRET_KEY", "")}
    app = FastAPI(title="TradeOnce fault proxy")
    state: dict[str, Any] = {"faults": FaultEngine([]), "log": []}
    app.state.admin_token = admin_token
    client = httpx.AsyncClient(base_url=upstream, timeout=10.0, headers=auth)

    def require_admin(token: str | None) -> None:
        if token != admin_token:
            raise HTTPException(403, "admin token required")

    @app.post("/_proxy/load")
    async def load(rules: list[FaultRule], x_proxy_admin: str | None = Header(default=None)):
        require_admin(x_proxy_admin)
        bad = [r.fault for r in rules if r.fault not in PROXY_FAULTS]
        if bad:
            raise HTTPException(422, f"faults not possible in a proxy: {bad}")
        state["faults"] = FaultEngine(rules)
        return {"ok": True, "rules": len(rules)}

    @app.get("/_proxy/log")
    async def log(x_proxy_admin: str | None = Header(default=None)):
        require_admin(x_proxy_admin)
        return state["log"]

    @app.post("/_proxy/clear_log")
    async def clear_log(x_proxy_admin: str | None = Header(default=None)):
        require_admin(x_proxy_admin)
        state["log"] = []
        return {"ok": True}

    @app.get("/_proxy/health")
    async def health():
        return {"ok": True, "upstream": upstream}

    @app.api_route("/v2/{rest:path}", methods=["GET", "POST", "DELETE", "PATCH"])
    async def forward(rest: str, request: Request):
        path = "/v2/" + rest
        endpoint = _endpoint(request.method, path)
        rule = state["faults"].next(endpoint)
        fault = rule.fault if rule else None
        entry = {"seq": len(state["log"]) + 1, "method": request.method, "endpoint": endpoint, "fault": fault,
                 "upstream_status": None, "returned_status": None, "order_id": None, "client_order_id": None}
        state["log"].append(entry)

        # Faults where the request never reaches the broker.
        if fault == "transient_500":
            entry["returned_status"] = 500
            return _err(500, 50010000, "internal server error (injected)")
        if fault == "rate_limit_429":
            entry["returned_status"] = 429
            return _err(429, 42910000, "rate limit exceeded (injected)")
        if fault == "real_reject" and endpoint == "POST /v2/orders":
            entry["returned_status"] = 403
            return _err(403, 40310000, "insufficient buying power (injected)")

        body = await request.body()
        if endpoint == "POST /v2/orders" and body:
            # Crypto (symbols like "LTC/USD") trades 24/7 but only accepts gtc/ioc; Executors send "day".
            try:
                order = json.loads(body)
                if "/" in str(order.get("symbol", "")) and order.get("time_in_force") not in ("gtc", "ioc"):
                    order["time_in_force"] = "gtc"
                    body = json.dumps(order).encode()
            except ValueError:
                pass
        up = await client.request(request.method, path, params=dict(request.query_params), content=body or None,
                                  headers={"content-type": request.headers.get("content-type", "application/json")})
        entry["upstream_status"] = up.status_code
        try:
            data = up.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            entry["order_id"] = data.get("id")
            entry["client_order_id"] = data.get("client_order_id")

        # Faults where the broker acted, but the caller doesn't learn it.
        if fault == "accept_then_timeout":
            await asyncio.sleep(float(rule.params.get("delay_s", 2.0)))
        if fault == "accept_then_500":
            entry["returned_status"] = 500
            return _err(500, 50010000, "internal server error (injected after the broker accepted)")
        if fault == "stale_status" and up.status_code == 200 and data is not None:
            data = _stale(data)

        entry["returned_status"] = up.status_code
        if data is None:
            return Response(content=up.content, status_code=up.status_code)
        return JSONResponse(data, status_code=up.status_code)

    @app.on_event("shutdown")
    async def _close() -> None:
        await client.aclose()

    return app
