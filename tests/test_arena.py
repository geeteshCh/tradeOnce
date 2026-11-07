import json
import sys
from pathlib import Path

import httpx
import pytest

from arena import Scenario, arena_server, run_scenario

ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "arena" / "scenarios" / "training"
NAIVE = [sys.executable, str(ROOT / "examples" / "naive_executor.py")]


@pytest.fixture(scope="module")
def arena():
    with arena_server() as a:
        yield a


def load(arena, scenario: dict):
    httpx.post(f"{arena.base_url}/_arena/load", json=scenario, headers=arena.admin_headers).raise_for_status()


def ledger(arena):
    return httpx.get(f"{arena.base_url}/_arena/ledger", headers=arena.admin_headers).json()


def scen(**kw):
    base = {"id": "t", "intents": [{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 1}], "prices": {"AAPL": 200.0}}
    base.update(kw)
    return base


def test_all_training_scenarios_validate():
    files = sorted(TRAINING.glob("*.json"))
    assert files
    for f in files:
        Scenario.model_validate_json(f.read_text())


def test_unknown_fault_rejected():
    with pytest.raises(Exception):
        Scenario.model_validate(scen(faults=[{"match": "POST /v2/orders", "fault": "explode"}]))


def test_ledger_needs_admin_token(arena):
    assert httpx.get(f"{arena.base_url}/_arena/ledger").status_code == 403
    assert httpx.post(f"{arena.base_url}/_arena/load", json=scen()).status_code == 403


def test_clean_order_fills_once(arena):
    load(arena, scen())
    r = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"})
    assert r.status_code == 200 and r.json()["status"] == "filled"
    L = ledger(arena)
    assert len(L["fills"]) == 1 and L["positions"] == {"AAPL": 1.0} and L["cash"] == 100_000 - 200


def test_duplicate_client_order_id_rejected(arena):
    load(arena, scen())
    body = {"symbol": "AAPL", "qty": 1, "side": "buy", "client_order_id": "abc"}
    assert httpx.post(f"{arena.base_url}/v2/orders", json=body).status_code == 200
    r = httpx.post(f"{arena.base_url}/v2/orders", json=body)
    assert r.status_code == 422 and "unique" in r.json()["message"]
    assert len(ledger(arena)["fills"]) == 1


def test_naive_executor_duplicates_on_accept_then_timeout(arena):
    s = json.loads((TRAINING / "t02_accept_then_timeout.json").read_text())
    trace = run_scenario(s, arena.base_url, arena.admin_token, ROOT, NAIVE)
    L = trace["ledger"]
    assert len(L["orders"]) == 2, "naive retry should have placed a duplicate order"
    assert L["positions"] == {"AAPL": 2.0}
    assert L["calls"][0]["fault"] == "accept_then_timeout"
    assert trace["executor_claims"][0]["filled_qty"] == 1  # it believes it bought 1


def test_idempotent_client_survives_accept_then_timeout(arena):
    """What a fixed Executor does: client_order_id per intent, look it up after a timeout instead of re-posting."""
    load(arena, json.loads((TRAINING / "t02_accept_then_timeout.json").read_text()))
    with httpx.Client(base_url=arena.base_url, timeout=0.5) as c:
        body = {"symbol": "AAPL", "qty": 1, "side": "buy", "client_order_id": "intent-i1"}
        with pytest.raises(httpx.TimeoutException):
            c.post("/v2/orders", json=body)
        r = c.get("/v2/orders:by_client_order_id", params={"client_order_id": "intent-i1"})
        assert r.status_code == 200 and r.json()["status"] == "filled"
    L = ledger(arena)
    assert len(L["orders"]) == 1 and L["positions"] == {"AAPL": 1.0}


def test_stale_status_lies_then_recovers(arena):
    load(arena, scen(faults=[{"match": "GET /v2/orders*", "nth": 1, "fault": "stale_status", "params": {"calls": 2}}]))
    o = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"}).json()
    seen = [httpx.get(f"{arena.base_url}/v2/orders/{o['id']}").json()["status"] for _ in range(3)]
    assert seen == ["new", "new", "filled"]


def test_real_reject_and_transient_500_record_nothing(arena):
    for fault in ("real_reject", "transient_500", "rate_limit_429"):
        load(arena, scen(faults=[{"match": "POST /v2/orders", "nth": 1, "fault": fault}]))
        r = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"})
        assert r.status_code in (403, 500, 429)
        assert ledger(arena)["orders"] == []


def test_accept_then_500_records_order(arena):
    load(arena, scen(faults=[{"match": "POST /v2/orders", "nth": 1, "fault": "accept_then_500"}]))
    r = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"})
    assert r.status_code == 500
    assert len(ledger(arena)["fills"]) == 1


def test_delayed_fill_and_settle(arena):
    load(arena, scen(faults=[{"match": "POST /v2/orders", "nth": 1, "fault": "delayed_fill", "params": {"after_calls": 3}}]))
    o = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"}).json()
    assert o["status"] == "new"
    assert httpx.get(f"{arena.base_url}/v2/orders/{o['id']}").json()["status"] == "new"
    httpx.post(f"{arena.base_url}/_arena/settle", headers=arena.admin_headers)
    assert ledger(arena)["positions"] == {"AAPL": 1.0}


def test_cancel_prevents_delayed_fill(arena):
    load(arena, scen(faults=[{"match": "POST /v2/orders", "nth": 1, "fault": "delayed_fill", "params": {"after_calls": 5}}]))
    o = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"}).json()
    assert httpx.delete(f"{arena.base_url}/v2/orders/{o['id']}").status_code == 204
    httpx.post(f"{arena.base_url}/_arena/settle", headers=arena.admin_headers)
    assert ledger(arena)["fills"] == []


def test_partial_fill_then_cancel(arena):
    load(arena, scen(intents=[{"intent_id": "i1", "symbol": "AAPL", "side": "buy", "qty": 4}],
                     faults=[{"match": "POST /v2/orders", "nth": 1, "fault": "partial_fill_then_cancel"}]))
    o = httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 4, "side": "buy"}).json()
    assert o["status"] == "canceled" and o["filled_qty"] == "2"


def test_duplicate_fill_only_in_activity_feed(arena):
    load(arena, scen(faults=[{"match": "POST /v2/orders", "nth": 1, "fault": "duplicate_fill"}]))
    httpx.post(f"{arena.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"})
    feed = httpx.get(f"{arena.base_url}/v2/account/activities/FILL").json()
    assert len(feed) == 2 and len(ledger(arena)["fills"]) == 1


def test_same_seed_same_ids():
    ids = []
    for _ in range(2):
        with arena_server() as a:
            load(a, scen(seed=123))
            ids.append(httpx.post(f"{a.base_url}/v2/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy"}).json()["id"])
    assert ids[0] == ids[1]
