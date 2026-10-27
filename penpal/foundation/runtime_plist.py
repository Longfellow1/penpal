"""Canonical LaunchAgent payloads written by Penpal installation commands."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from penpal.foundation.runtime_logs import (
    DAEMON_STDERR_NAME,
    DAEMON_STDOUT_NAME,
    HEALTHCHECK_STDERR_NAME,
    HEALTHCHECK_STDOUT_NAME,
)
from penpal.utils.env import get_env_compat
from penpal.utils.paths import get_logs_dir


def resolve_penpal_executable() -> Path:
    override = (get_env_compat("PENPAL_EXECUTABLE", "") or "").strip()
    candidates = [override, shutil.which("penpal") or ""]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            resolved = shutil.which(str(path))
            path = Path(resolved) if resolved else path
        if path.exists():
            # Keep Homebrew's stable opt symlink instead of pinning a versioned Cellar path.
            return path.absolute()
    raise FileNotFoundError(
        "Could not find the installed `penpal` executable. Install with Homebrew first, "
        "or set PENPAL_EXECUTABLE=/absolute/path/to/penpal."
    )


def launchd_jobs(
    executable: Path | None = None,
    *,
    launcher_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    executable = executable or resolve_penpal_executable()
    daemon_executable = launcher_path if launcher_path is not None else executable
    logs = get_logs_dir()
    env = {
        "PATH": ":".join(dict.fromkeys((
            str(executable.parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
            "/bin", "/usr/sbin", "/sbin",
        )))
    }
    return {
        "com.penpal.daemon": {
            "Label": "com.penpal.daemon",
            "ProgramArguments": [str(daemon_executable), "serve"],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardOutPath": str(logs / DAEMON_STDOUT_NAME),
            "StandardErrorPath": str(logs / DAEMON_STDERR_NAME),
            "EnvironmentVariables": env,
        },
        "com.penpal.healthcheck": {
            "Label": "com.penpal.healthcheck",
            "ProgramArguments": [str(executable), "healthcheck"],
            "StartInterval": 600,
            "RunAtLoad": True,
            "StandardOutPath": str(logs / HEALTHCHECK_STDOUT_NAME),
            "StandardErrorPath": str(logs / HEALTHCHECK_STDERR_NAME),
            "EnvironmentVariables": env,
        },
    }
