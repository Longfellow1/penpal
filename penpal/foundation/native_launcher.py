"""The Penpal.app launcher this process was started from, if any.

The launcher embeds Python so macOS attributes the process (and its privacy grants) to Penpal.app. It announces
its own path in the environment; the value is read once at import and removed, because child processes (CLI calls,
osascript) were not started by the launcher and must not believe they were.
"""
from __future__ import annotations

import os

_ENV_NAMES = ("PENPAL_NATIVE_LAUNCHER", "PENPAL_HUD_NATIVE_LAUNCHER")


def _take_from_environment() -> str | None:
    values = [os.environ.pop(name, None) for name in _ENV_NAMES]
    value = next((item for item in values if item), None)
    return os.path.realpath(value) if value else None


_LAUNCHER = _take_from_environment()


def native_launcher() -> str | None:
    """The launcher's real path while it still exists; None when started any other way."""
    return _LAUNCHER if _LAUNCHER and os.path.isfile(_LAUNCHER) else None
