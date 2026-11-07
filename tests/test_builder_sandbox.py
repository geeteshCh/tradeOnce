import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arena.models import Scenario
from builder.sandbox import SandboxRunner


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
    "requires macOS sandbox-exec",
)
class SandboxRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "executor").mkdir(parents=True)
        (self.source / "executor" / "__init__.py").write_text("", encoding="utf-8")
        self.scenario = Scenario.model_validate(
            {
                "id": "sandbox-test",
                "intents": [
                    {
                        "intent_id": "intent-1",
                        "symbol": "AAPL",
                        "side": "buy",
                        "qty": 1,
                    }
                ],
                "prices": {"AAPL": 100},
            }
        )

    def write_executor(self, source: str) -> None:
        (self.source / "executor" / "run.py").write_text(source, encoding="utf-8")

    def test_generated_code_cannot_read_host_secrets_or_use_external_network(self):
        outside = self.root / "outside-secret.txt"
        outside.write_text("filesystem-secret", encoding="utf-8")
        home_secret = Path.home() / f".tradeonce-sandbox-test-{os.getpid()}"
        home_secret.write_text("home-secret", encoding="utf-8")
        self.addCleanup(home_secret.unlink, missing_ok=True)
        late_secret = self.root / "late-secret.txt"
        venv_leak = Path(sys.prefix) / f"sandbox-leak-{os.getpid()}.txt"
        self.addCleanup(venv_leak.unlink, missing_ok=True)
        repository_file = Path(__file__).resolve().parents[1] / "readme.md"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        other_port = listener.getsockname()[1]
        self.write_executor(
            f"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

probe = {{
    "gemini": os.environ.get("GEMINI_API_KEY"),
    "subq": os.environ.get("SUBQ_API_KEY"),
    "mongo": os.environ.get("MONGODB_URI"),
}}
try:
    probe["file"] = Path({str(outside)!r}).read_text()
except Exception as exc:
    probe["file"] = type(exc).__name__
try:
    probe["home"] = Path({str(home_secret)!r}).read_text()
except Exception as exc:
    probe["home"] = type(exc).__name__
try:
    probe["late"] = Path({str(late_secret)!r}).read_text()
except Exception as exc:
    probe["late"] = type(exc).__name__
try:
    probe["repository"] = Path({str(repository_file)!r}).read_text()
except Exception as exc:
    probe["repository"] = type(exc).__name__
try:
    probe["private_config"] = Path("/private/etc/hosts").read_text()
except Exception as exc:
    probe["private_config"] = type(exc).__name__
try:
    socket.create_connection(("1.1.1.1", 80), timeout=0.1)
    probe["internet"] = "connected"
except Exception as exc:
    probe["internet"] = type(exc).__name__
try:
    socket.create_connection(("127.0.0.1", {other_port}), timeout=0.1)
    probe["other_loopback"] = "connected"
except Exception as exc:
    probe["other_loopback"] = type(exc).__name__
try:
    Path({str(venv_leak)!r}).write_text("leaked")
    probe["venv_write"] = "written"
except Exception as exc:
    probe["venv_write"] = type(exc).__name__
try:
    child_pid = os.fork()
    if child_pid == 0:
        os._exit(0)
    os.waitpid(child_pid, 0)
    probe["fork"] = "spawned"
except Exception as exc:
    probe["fork"] = type(exc).__name__
try:
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    probe["subprocess"] = "spawned"
except Exception as exc:
    probe["subprocess"] = type(exc).__name__
print(json.dumps([{{"intent_id": "intent-1", "status": "failed", "probe": probe}}]))
"""
        )

        class LateSecretRunner(SandboxRunner):
            def _command(self, root: Path, base_url: str) -> list[str]:
                command = super()._command(root, base_url)
                late_secret.write_text("late-secret", encoding="utf-8")
                return command

        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "gemini-secret",
                "SUBQ_API_KEY": "subq-secret",
                "MONGODB_URI": "mongodb-secret",
            },
        ):
            trace = LateSecretRunner(timeout_s=10).run_scenario(
                self.source, self.scenario
            )

        probe = trace["executor_claims"][0]["probe"]
        self.assertIsNone(probe["gemini"])
        self.assertIsNone(probe["subq"])
        self.assertIsNone(probe["mongo"])
        self.assertEqual(probe["file"], "PermissionError")
        self.assertEqual(probe["home"], "PermissionError")
        self.assertEqual(probe["late"], "PermissionError")
        self.assertEqual(probe["repository"], "PermissionError")
        self.assertEqual(probe["private_config"], "PermissionError")
        self.assertNotEqual(probe["internet"], "connected")
        self.assertNotEqual(probe["other_loopback"], "connected")
        self.assertEqual(probe["venv_write"], "PermissionError")
        self.assertFalse(venv_leak.exists())
        self.assertEqual(probe["fork"], "PermissionError")
        self.assertEqual(probe["subprocess"], "PermissionError")

    def test_legitimate_executor_can_submit_to_local_arena(self):
        self.write_executor(
            """
import json
import os
import sys

import httpx

intent = json.load(sys.stdin)[0]
body = {
    "symbol": intent["symbol"],
    "qty": intent["qty"],
    "side": intent["side"],
    "client_order_id": intent["intent_id"],
}
response = httpx.post(os.environ["BROKER_URL"] + "/v2/orders", json=body, timeout=2)
response.raise_for_status()
order = response.json()
print(json.dumps([{
    "intent_id": intent["intent_id"],
    "status": "filled",
    "broker_order_id": order["id"],
    "filled_qty": int(float(order["filled_qty"])),
}]))
"""
        )

        trace = SandboxRunner(timeout_s=10).run_scenario(self.source, self.scenario)

        self.assertEqual(trace["exit_code"], 0, trace["stderr_tail"])
        self.assertIsNone(trace["claims_error"])
        self.assertEqual(trace["executor_claims"][0]["filled_qty"], 1)
        self.assertEqual(trace["ledger"]["positions"], {"AAPL": 1.0})

    def test_rejects_executor_source_symlinks(self):
        outside = self.root / "outside.py"
        outside.write_text("print('outside')", encoding="utf-8")
        (self.source / "executor" / "run.py").symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "symlinks"):
            SandboxRunner().run_scenario(self.source, self.scenario)


if __name__ == "__main__":
    unittest.main()
