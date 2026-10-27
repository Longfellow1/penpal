"""Headless failure visibility (D33: the CLI is the minimal install, there is no UI to notice a stall).

Two consumers share this module: ``permission_report`` reads the daemon's permission self-report, while
MCP health adapts the shared runtime evaluator:

- ``penpal doctor``: ``permission_report`` lists, per enabled source, which macOS permission it needs
  and whether it is granted, plus the binaries the permission must be granted to. Read-only: it uses
  the ``check`` side of ``PermissionSpec`` and never the ``request`` side, so no prompt can appear.
- the MCP server: ``current_capture_health`` returns ``{"code", "message"}`` when capture is broken,
  else ``None``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from penpal import permissions
from penpal.permissions import PermissionStatus
from penpal.runtime import RuntimeState, evaluate, state_message
from penpal.services.capability_monitor import (
    DAEMON_PERMISSIONS_KEY,
    DAEMON_PERMISSION_MAX_AGE_SEC,
    record_daemon_permissions,
)

# Watcher (= source) -> macOS permissions it needs. keyboard_chunk also depends on AX
# (see app._AX_DEPENDENT_FACT_CAPS). OCR is off (watcher commented out in capture/manager.py), so it
# needs no Screen Recording grant and is not listed here.
SOURCE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "keyboard_chunk": ("input_monitoring", "accessibility"),
    "ax_text": ("accessibility",),
}

# ── shared permission readers ───────────────────────────────────────────────────
def _daemon_denied_permissions() -> set[str]:
    """Permissions the daemon itself reported denied (it runs under launchd, a different TCC identity
    from the process running doctor or the MCP server). Covers the capability probe for accessibility and
    the daemon's permission snapshot for every permission it checks (so a wake re-check is reflected)."""
    from penpal.capabilities.store import load_states

    denied: set[str] = set()
    state = load_states().get("accessibility_permission")
    if state is not None and not state.ok and state.code == "ax_denied":
        denied.add("accessibility")
    snapshot = daemon_permission_snapshot()
    for key, value in ((snapshot or {}).get("permissions") or {}).items():
        if value == PermissionStatus.DENIED.value:
            denied.add(key)
    return denied


def enabled_sources(cfg: Any, disabled: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Watchers that are on in config and not switched off with ``penpal sources disable``."""
    return [
        name
        for name in SOURCE_PERMISSIONS
        if bool(getattr(getattr(cfg, "watchers", None), name, False)) and name not in disabled
    ]


def required_permissions(cfg: Any, disabled: frozenset[str] | set[str] = frozenset()) -> dict[str, list[str]]:
    """permission key -> enabled sources that need it, in stable order."""
    needed: dict[str, list[str]] = {}
    for source in enabled_sources(cfg, disabled):
        for key in SOURCE_PERMISSIONS[source]:
            needed.setdefault(key, []).append(source)
    return needed


_PYTHON_BIN_RE = re.compile(r"^(?P<prefix>.*/Versions/[^/]+)/bin/python[0-9.]*$")


def grant_target(path: str) -> str:
    """The app macOS lists in Privacy panes for an interpreter path or bundle launcher.

    Homebrew/python.org builds ship ``.../Versions/3.13/bin/python3.13`` next to
    ``.../Versions/3.13/Resources/Python.app``. The TCC grant belongs to that ``.app`` bundle, not to the
    ``bin`` symlink target, so return the bundle when it exists.
    When path points to a launcher inside an .app bundle (e.g. Penpal.app/Contents/MacOS/Penpal),
    return the enclosing .app bundle path.
    """
    real = os.path.realpath(os.path.expanduser(path))
    p = Path(real)
    for parent in (p, *p.parents):
        if parent.suffix == ".app" and parent.is_dir():
            return str(parent)
    match = _PYTHON_BIN_RE.match(real)
    if match:
        bundle = Path(match.group("prefix")) / "Resources" / "Python.app"
        if bundle.is_dir():
            return str(bundle)
    return real


def executables_to_grant(host_executable: Optional[str] = None) -> list[str]:
    """Programs a user must grant a permission to, most relevant first: the interpreter the daemon reported
    (its ``host_executable``), then this interpreter. Both are mapped to their ``.app`` bundle when they have one."""
    candidates = [host_executable or "", sys.executable]
    seen: set[str] = set()
    targets: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        target = grant_target(candidate)
        if target not in seen:
            seen.add(target)
            targets.append(target)
    return targets


# ── daemon self-report (written by the daemon, read by doctor) ─────────────────

def daemon_permission_snapshot(now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Doctor side: the daemon's last self-reported permission state with ``age_sec`` added.

    ``None`` when nothing was recorded or the record is older than ``DAEMON_PERMISSION_MAX_AGE_SEC``
    (the daemon is then treated as not running).
    """
    from penpal.store.repository import get_state

    raw = get_state(DAEMON_PERMISSIONS_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("permissions"), dict):
        return None
    checked = _seconds_since(value.get("checked_at"), now or datetime.now(timezone.utc))
    if checked is None or checked > DAEMON_PERMISSION_MAX_AGE_SEC:
        return None
    value["age_sec"] = int(checked)
    return value


def _seconds_since(iso: Any, now: datetime) -> Optional[float]:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds())


# ── doctor ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PermissionFinding:
    key: str
    label: str
    sources: tuple[str, ...]
    granted: bool
    status: str  # "granted" | "missing" | "unavailable"
    settings_pane: str
    executables: tuple[str, ...]
    checked_by: str = "terminal"  # "daemon" (background service's own report) | "terminal" (fallback)
    age_sec: Optional[int] = None  # daemon report age; None for the terminal fallback


def permission_report(
    cfg: Any,
    *,
    disabled: frozenset[str] | set[str] | None = None,
    daemon_denied: set[str] | None = None,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> list[PermissionFinding]:
    """Granted / missing per permission needed by an enabled source. Never prompts.

    Prefers the daemon's own recent report. Without one (daemon stopped, or never ran with this build),
    falls back to checking the current process, which is the user's terminal, not the background service.
    """
    if disabled is None:
        try:
            from penpal.capture.capture_control import disabled_sources

            disabled = disabled_sources()
        except Exception:
            disabled = frozenset()
    if daemon_denied is None:
        try:
            daemon_denied = _daemon_denied_permissions()
        except Exception:
            daemon_denied = set()
    if snapshot is None:
        try:
            snapshot = daemon_permission_snapshot()
        except Exception:
            snapshot = None
    daemon_perms: Mapping[str, Any] = (snapshot or {}).get("permissions") or {}
    exes = tuple(executables_to_grant((snapshot or {}).get("host_executable")))
    age = (snapshot or {}).get("age_sec")
    findings: list[PermissionFinding] = []
    for key, sources in required_permissions(cfg, disabled).items():
        spec = permissions.spec_by_key(key)
        if spec is None:
            continue
        if key in daemon_perms:
            current = _status_from_value(daemon_perms[key])
            checked_by, age_sec = "daemon", int(age) if isinstance(age, (int, float)) else None
        else:
            try:
                current = spec.check()
            except Exception:
                current = PermissionStatus.UNAVAILABLE
            checked_by, age_sec = "terminal", None
        granted = current == PermissionStatus.GRANTED and key not in daemon_denied
        status = "granted" if granted else ("unavailable" if current == PermissionStatus.UNAVAILABLE else "missing")
        findings.append(
            PermissionFinding(
                key=key,
                label=spec.label,
                sources=tuple(sources),
                granted=granted,
                status=status,
                settings_pane=spec.settings_pane,
                executables=exes,
                checked_by=checked_by,
                age_sec=age_sec,
            )
        )
    return findings


def _status_from_value(value: Any) -> PermissionStatus:
    try:
        return PermissionStatus(value)
    except ValueError:
        return PermissionStatus.UNAVAILABLE


# ── MCP capture_health ──────────────────────────────────────────────────────────


def compute_capture_health(cfg: Any = None, *, now: Optional[datetime] = None) -> Optional[dict[str, str]]:
    """Adapt the shared state to MCP's existing ``{code, message}`` response shape."""
    status = evaluate(now=now)
    if status.state == RuntimeState.RUNNING and not status.issues:
        return None
    return {"code": status.code, "message": state_message(status, language="en")}


def current_capture_health() -> Optional[dict[str, str]]:
    """Read the same current runtime state as CLI, HUD and healthcheck; never raises."""
    try:
        return compute_capture_health()
    except Exception:
        return None
