"""Read-only probe for Penpal's LaunchAgent."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import ctypes
from typing import Callable

from penpal.utils.paths import get_launchd_daemon_plist_path

_PID_RE = re.compile(r"\bpid\s*=\s*(\d+)", re.IGNORECASE)
_LAST_EXIT_RE = re.compile(r"\blast exit code\s*=\s*(-?\d+)", re.IGNORECASE)
# Every launchctl call is bounded: launchctl can block indefinitely on a job whose program vanished
# (observed: kickstart of a healthcheck still pointing at a deleted Homebrew keg).
LAUNCHCTL_TIMEOUT_SEC = 15.0


def run_launchctl(command: list[str], **kwargs):
    kwargs.setdefault("timeout", LAUNCHCTL_TIMEOUT_SEC)
    return subprocess.run(command, **kwargs)


_DEFAULT_RUNNER = run_launchctl


def is_installed() -> bool:
    return get_launchd_daemon_plist_path().is_file()


def probe_label(label: str, *, runner: Callable = run_launchctl) -> dict[str, int | bool | None]:
    """Return launchd's current view for a label; tests inject the subprocess runner."""
    if os.getenv("PENPAL_RUNTIME_DISABLE_LAUNCHD_PROBE") == "1":
        return {"loaded": False, "pid": None, "last_exit": None}
    if os.getenv("PENPAL_ISOLATED") == "1" and runner is _DEFAULT_RUNNER:
        return {"loaded": False, "pid": None, "last_exit": None}
    if os.uname().sysname != "Darwin":
        return {"loaded": False, "pid": None, "last_exit": None}
    target = f"gui/{os.getuid()}/{label}"
    try:
        result = runner(["launchctl", "print", target], check=False, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": False, "pid": None, "last_exit": None}
    output = "\n".join(filter(None, (result.stdout or "", result.stderr or "")))
    pid_match = _PID_RE.search(output)
    exit_match = _LAST_EXIT_RE.search(output)
    return {
        "loaded": result.returncode == 0,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit": int(exit_match.group(1)) if exit_match else None,
    }


def probe_daemon(*, runner: Callable = subprocess.run) -> dict[str, int | bool | None]:
    return probe_label("com.penpal.daemon", runner=runner)


def process_start_time(pid: int, *, runner: Callable = subprocess.run) -> str | None:
    """Return a stable process start value so a reused PID cannot inherit a record."""
    if sys.platform == "darwin":
        try:
            # A numeric kernel timestamp avoids ps text parsing and remains usable in the sandbox.
            class ProcBsdInfo(ctypes.Structure):
                _fields_ = [
                    ("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                    ("xstatus", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                    ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32),
                    ("gid", ctypes.c_uint32), ("ruid", ctypes.c_uint32),
                    ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
                    ("svgid", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
                    ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                    ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
                    ("pjobc", ctypes.c_uint32), ("tty_device", ctypes.c_uint32),
                    ("tty_pgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
                    ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64),
                ]

            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            proc_pidinfo.restype = ctypes.c_int
            info = ProcBsdInfo()
            size = ctypes.sizeof(info)
            if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) >= size and info.pid == pid:
                return f"{info.start_sec}.{info.start_usec:06d}"
        except (AttributeError, OSError):
            pass
        return None
    try:
        result = runner(["ps", "-o", "lstart=", "-p", str(pid)], check=False, capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None
