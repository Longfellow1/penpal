"""Run the shared capability check and persist its health snapshot."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from penpal.capabilities.base import HealthState
from penpal.foundation.native_launcher import native_launcher
from penpal.capabilities.capture_facts import CAPTURE_FACT_CAPS
from penpal.capabilities.registry import CapabilityRegistry
from penpal.capabilities.store import save_states as save_capability_states
from penpal.permissions import PermissionStatus
from penpal import permissions
from penpal.utils.logging import get_logger

logger = get_logger("app")
_AX_DEPENDENT_FACT_CAPS = {"ax_text_watcher", "keyboard_chunk_watcher"}
DAEMON_PERMISSIONS_KEY = "daemon_permissions"
DAEMON_PERMISSION_MAX_AGE_SEC = 150.0


def run_capability_self_check(registry: CapabilityRegistry) -> None:
    """Probe capabilities and persist the daemon's view of their state."""
    from penpal.store.repository import set_state

    states = registry.monitor_all()
    states = _apply_ax_denied_fact_overrides(states)
    save_capability_states(states)

    try:
        record_daemon_permissions()
    except Exception as exc:
        logger.warning("daemon permission self-report failed: %s", exc)

    capture_failure = registry.select_failure(states, names=CAPTURE_FACT_CAPS)
    set_state("capture_error_code", "" if capture_failure is None else capture_failure.state.code)


def record_daemon_permissions() -> dict[str, Any]:
    """Persist read-only permission checks performed under the daemon's own process identity."""
    from penpal.store.repository import set_state

    results: dict[str, str] = {}
    for spec in permissions.PERMISSION_SPECS:
        try:
            results[spec.key] = spec.check().value
        except Exception:
            results[spec.key] = PermissionStatus.UNAVAILABLE.value
    snapshot = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "host_executable": native_launcher() or os.path.realpath(sys.executable),
        "permissions": results,
    }
    set_state(DAEMON_PERMISSIONS_KEY, json.dumps(snapshot, ensure_ascii=False))
    return snapshot


def _apply_ax_denied_fact_overrides(states: dict[str, HealthState]) -> dict[str, HealthState]:
    """Force AX-dependent watcher facts false when accessibility is denied."""
    accessibility_state = states.get("accessibility_permission")
    if accessibility_state is None or accessibility_state.ok or accessibility_state.code != "ax_denied":
        return states

    now = time.time()
    overridden = dict(states)
    for capability_name in _AX_DEPENDENT_FACT_CAPS:
        if capability_name not in overridden:
            continue
        overridden[capability_name] = HealthState(
            ok=False,
            code="ax_denied",
            last_checked=now,
            detail="forced by accessibility_permission=ax_denied",
        )
    return overridden
