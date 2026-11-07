"""Fail-closed macOS sandbox for running generated executors against Arena."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from arena.models import Scenario
from arena.runner import run_scenario
from arena.server import arena_server


class SandboxUnavailableError(RuntimeError):
    """Raised when the required macOS Seatbelt boundary is unavailable."""


class SandboxRunner:
    """Run generated Python with only local Arena and isolated files available."""

    def __init__(
        self,
        *,
        python_executable: Path | None = None,
        sandbox_executable: Path = Path("/usr/bin/sandbox-exec"),
        timeout_s: float = 60.0,
    ) -> None:
        self._python = Path(python_executable or sys.executable).absolute()
        self._sandbox = sandbox_executable
        self._timeout_s = timeout_s
        if sys.platform != "darwin" or not self._sandbox.is_file():
            raise SandboxUnavailableError(
                "generated executors require macOS /usr/bin/sandbox-exec"
            )
        if not self._python.is_file():
            raise SandboxUnavailableError(
                f"Python interpreter not found: {self._python}"
            )

    def run_scenario(
        self, source_dir: Path, scenario: Scenario | dict[str, Any]
    ) -> dict[str, Any]:
        """Run copied executor sources against a fresh real Arena instance."""
        validated = (
            scenario
            if isinstance(scenario, Scenario)
            else Scenario.model_validate(scenario)
        )
        with tempfile.TemporaryDirectory(
            prefix="tradeonce-sandbox-", dir="/private/tmp"
        ) as temporary:
            root = Path(temporary).resolve()
            self._copy_executor(Path(source_dir), root)
            (root / "home").mkdir()
            (root / "tmp").mkdir()
            with arena_server() as arena:
                command = self._command(root, arena.base_url)
                trace = run_scenario(
                    validated,
                    arena.base_url,
                    arena.admin_token,
                    root,
                    command,
                    timeout_s=self._timeout_s,
                )
        stderr = str(trace.get("stderr_tail") or "")
        if trace.get("exit_code") == 71 and "sandbox-exec" in stderr:
            raise SandboxUnavailableError(stderr.strip())
        return trace

    def _copy_executor(self, source_dir: Path, destination: Path) -> None:
        source = source_dir.resolve(strict=True)
        package = source / "executor"
        if not (package / "run.py").is_file():
            raise ValueError("source_dir must contain executor/run.py")
        for path in sorted(package.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"executor sources must not contain symlinks: {path}")
            if not path.is_file() or path.suffix != ".py":
                continue
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)

    def _command(self, root: Path, base_url: str) -> list[str]:
        parsed = urlparse(base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
            raise ValueError("Arena must use an explicit loopback port")
        profile = self._profile(root, parsed.port)
        site_packages = (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        clean_environment = [
            "PATH=/usr/bin:/bin",
            f"HOME={root / 'home'}",
            f"TMPDIR={root / 'tmp'}",
            "LANG=C",
            "PYTHONNOUSERSITE=1",
            "PYTHONDONTWRITEBYTECODE=1",
            "NO_PROXY=*",
            "no_proxy=*",
            f"PYTHONPATH={root}{os.pathsep}{site_packages}",
            f"BROKER_URL={base_url}",
            f"APCA_API_BASE_URL={base_url}",
            "APCA_API_KEY_ID=arena",
            "APCA_API_SECRET_KEY=arena",
        ]
        return [
            "/usr/bin/env",
            "-i",
            *clean_environment,
            str(self._sandbox),
            "-p",
            profile,
            str(self._python.resolve()),
            "-B",
            "-m",
            "executor.run",
        ]

    def _profile(self, root: Path, arena_port: int) -> str:
        environment = Path(sys.prefix).resolve()
        runtime_roots = (
            Path("/System"),
            Path("/usr/lib"),
            Path("/usr/share"),
            Path("/Library/Apple"),
            Path("/dev"),
            Path("/opt/homebrew/lib"),
            Path("/opt/homebrew/opt/openssl@3/lib").resolve(),
            Path("/private/var/db/timezone"),
            Path(sys.base_prefix).resolve(),
            environment,
            root,
        )
        read_rules = "\n".join(
            f'(allow file-read* (subpath "{_seatbelt(path)}"))'
            for path in runtime_roots
        )
        map_rules = "\n".join(
            f'(allow file-map-executable (subpath "{_seatbelt(path)}"))'
            for path in runtime_roots
        )
        framework_launcher = (
            Path(sys.base_prefix)
            / "Resources"
            / "Python.app"
            / "Contents"
            / "MacOS"
            / "Python"
        ).resolve()
        return f"""(version 1)
(deny default)
(import "dyld-support.sb")
(allow process-exec
    (literal "{_seatbelt(self._python)}")
    (literal "{_seatbelt(self._python.resolve())}")
    (literal "{_seatbelt(framework_launcher)}"))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata file-test-existence)
{read_rules}
{map_rules}
(allow file-read-data
    (literal "/dev/null")
    (literal "/dev/random")
    (literal "/dev/urandom")
    (literal "/private/etc/localtime"))
(allow file-write* (subpath "{_seatbelt(root)}"))
(allow network-outbound (remote ip "localhost:{arena_port}"))
"""


def run_sandboxed_scenario(
    source_dir: Path, scenario: Scenario | dict[str, Any]
) -> dict[str, Any]:
    """Run one generated executor using the default fail-closed sandbox."""
    return SandboxRunner().run_scenario(source_dir, scenario)


def _seatbelt(path: Path) -> str:
    """Escape a literal path for a Seatbelt string."""
    return path.as_posix().replace("\\", "\\\\").replace('"', '\\"')
