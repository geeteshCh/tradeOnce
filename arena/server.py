"""Start an Arena in a background thread. Each instance has its own state, so run one per parallel candidate."""

from __future__ import annotations

import secrets
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import httpx
import uvicorn

from .broker import create_app


@dataclass
class ArenaHandle:
    base_url: str
    admin_token: str
    app: object

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"X-Arena-Admin": self.admin_token}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def arena_server(port: int | None = None, admin_token: str | None = None) -> Iterator[ArenaHandle]:
    port = port or _free_port()
    admin_token = admin_token or secrets.token_hex(16)
    app = create_app(admin_token)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/_arena/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("arena did not start")
    try:
        yield ArenaHandle(base_url, admin_token, app)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
