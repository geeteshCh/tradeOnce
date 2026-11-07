"""Run the Arena standalone:  python -m arena --port 8765 [--scenario path.json] [--run 'python -m executor.run']

With --run, it loads the scenario, runs the Executor once and prints the trace as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import sys
from pathlib import Path

from .models import Scenario


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m arena")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scenario", type=Path, help="scenario JSON to load")
    ap.add_argument("--run", help="executor command to run once against the scenario, then exit")
    ap.add_argument("--executor-dir", default=".")
    args = ap.parse_args()

    if args.run:
        from .runner import run_scenario
        from .server import arena_server

        if not args.scenario:
            sys.exit("--run needs --scenario")
        scenario = Scenario.model_validate_json(args.scenario.read_text())
        with arena_server(args.port) as arena:
            trace = run_scenario(scenario, arena.base_url, arena.admin_token, args.executor_dir, shlex.split(args.run))
        print(json.dumps(trace, indent=2))
        return

    import uvicorn

    from .broker import create_app

    token = os.environ.get("ARENA_ADMIN_TOKEN") or secrets.token_hex(16)
    app = create_app(token)
    if args.scenario:
        app.state.arena.load(Scenario.model_validate_json(args.scenario.read_text()))
    print(f"Arena on http://127.0.0.1:{args.port}  admin token: {token}", file=sys.stderr)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
