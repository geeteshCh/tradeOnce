"""Run one Executor version against one scenario and return the trace (what it claimed + what really happened).

The Executor runs as a subprocess. It gets the intents on stdin and the broker URL in its environment, and
prints a JSON list of IntentResult on stdout. It never gets the admin token, so it cannot read the ledger.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from .models import Scenario

DEFAULT_EXECUTOR_CMD = [sys.executable, "-m", "executor.run"]


def run_scenario(
    scenario: Scenario | dict[str, Any],
    base_url: str,
    admin_token: str,
    executor_dir: str | Path = ".",
    executor_cmd: list[str] | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    scenario = scenario if isinstance(scenario, Scenario) else Scenario.model_validate(scenario)
    admin = {"X-Arena-Admin": admin_token}
    httpx.post(f"{base_url}/_arena/load", json=scenario.model_dump(), headers=admin, timeout=5).raise_for_status()

    env = {k: v for k, v in os.environ.items() if k != "ARENA_ADMIN_TOKEN"}
    env.update(
        BROKER_URL=base_url,
        APCA_API_BASE_URL=base_url,        # alpaca-py style
        APCA_API_KEY_ID="arena",
        APCA_API_SECRET_KEY="arena",
        PYTHONPATH=str(Path(executor_dir).resolve()) + os.pathsep + env.get("PYTHONPATH", ""),
    )
    intents = json.dumps([i.model_dump() for i in scenario.intents])
    t0 = time.time()
    timed_out = False
    try:
        proc = subprocess.run(executor_cmd or DEFAULT_EXECUTOR_CMD, input=intents, cwd=executor_dir, env=env,
                              capture_output=True, text=True, timeout=timeout_s)
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, rc, timed_out = (e.stdout or ""), (e.stderr or ""), None, True
        stdout = stdout.decode() if isinstance(stdout, bytes) else stdout
        stderr = stderr.decode() if isinstance(stderr, bytes) else stderr
    elapsed = round(time.time() - t0, 3)

    try:
        claims = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else []
        claims_error = None
    except (json.JSONDecodeError, IndexError) as e:
        claims, claims_error = [], f"could not parse executor stdout: {e}"

    httpx.post(f"{base_url}/_arena/settle", headers=admin, timeout=5).raise_for_status()
    ledger = httpx.get(f"{base_url}/_arena/ledger", headers=admin, timeout=5).json()

    return {
        "scenario": scenario.model_dump(),
        "executor_claims": claims,
        "claims_error": claims_error,
        "exit_code": rc,
        "timed_out": timed_out,
        "elapsed_s": elapsed,
        "stderr_tail": (stderr or "")[-3000:],
        "ledger": ledger,
    }
