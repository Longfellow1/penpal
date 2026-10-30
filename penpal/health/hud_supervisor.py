"""Persistent HUD supervisor policy used by the daemon periodic loop."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from penpal.foundation.hud_supervisor_state import (
    HUD_RESTART_BUDGET_PER_HOUR,
    HUD_RESTART_BUDGET_WINDOW_SEC,
    HUD_RESTART_COOLDOWN_SEC,
    parse_supervisor_time,
    persist_supervisor_state,
    read_supervisor_state,
    read_supervisor_summary as read_supervisor_summary,
    supervisor_state_path,
)
from penpal.runtime import RuntimeStatus, evaluate
from penpal.runtime.control import ControlResult, restart_hud

logger = logging.getLogger(__name__)

HUD_HUNG_THRESHOLD = 2
HUD_MAIN_THREAD_STALE_SEC = 45.0


def supervise_hud(
    status: RuntimeStatus | None = None,
    *,
    now: datetime | None = None,
    restart_control: Callable[[], ControlResult] | None = None,
    relaunch_control: Callable[[], ControlResult] | None = None,
    killer: Callable[[int, int], None] | None = None,
    is_running: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Supervise the HUD process, relaunching on crash or hang within budget."""
    checked = now or datetime.now(timezone.utc)
    checked = checked.replace(tzinfo=timezone.utc) if checked.tzinfo is None else checked.astimezone(timezone.utc)
    runtime_status = status or evaluate(now=checked)

    path = state_path or supervisor_state_path()
    state, corrupt_state = read_supervisor_state(path)
    cutoff = checked - timedelta(seconds=HUD_RESTART_BUDGET_WINDOW_SEC)
    restart_times = [
        val for raw in state["restart_times"]
        if (val := parse_supervisor_time(raw)) is not None and val > cutoff
    ]
    state["restart_times"] = [val.isoformat() for val in restart_times]

    hud = runtime_status.hud
    if hud is None:
        # Deliberate quit or optional HUD not installed: no HUD record -> do not relaunch.
        state["consecutive_hung"] = 0
        persist_supervisor_state(path, state)
        return {
            "action": "none",
            "reason": "no_hud_record",
            "consecutive_hung": 0,
            "restarts_in_window": len(restart_times),
            "budget_exhausted": False,
            "state_recovered": corrupt_state,
        }

    # Determine failure mode
    action_reason: str | None = None
    if not hud.alive:
        # Record exists, but process is dead -> crashed
        action_reason = "dead"
    else:
        tick_age = hud.main_thread_tick_age_sec
        if tick_age is not None and tick_age > HUD_MAIN_THREAD_STALE_SEC:
            state["consecutive_hung"] += 1
            if state["consecutive_hung"] >= HUD_HUNG_THRESHOLD:
                action_reason = "hung"
            else:
                persist_supervisor_state(path, state)
                return {
                    "action": "observe",
                    "reason": "consecutive_hung_threshold",
                    "consecutive_hung": state["consecutive_hung"],
                    "restarts_in_window": len(restart_times),
                    "budget_exhausted": False,
                    "state_recovered": corrupt_state,
                }
        else:
            state["consecutive_hung"] = 0
            persist_supervisor_state(path, state)
            return {
                "action": "none",
                "reason": "hud_healthy",
                "consecutive_hung": 0,
                "restarts_in_window": len(restart_times),
                "budget_exhausted": False,
                "state_recovered": corrupt_state,
            }

    base = {
        "reason": action_reason,
        "consecutive_hung": state["consecutive_hung"],
        "restarts_in_window": len(restart_times),
        "state_recovered": corrupt_state,
    }

    # Check budget
    if len(restart_times) >= HUD_RESTART_BUDGET_PER_HOUR:
        persist_supervisor_state(path, state)
        logger.info("hud_supervisor: budget exhausted count=%d", len(restart_times))
        return {**base, "action": "report_only", "reason": "restart_budget_exhausted", "budget_exhausted": True}

    cooldown_until = parse_supervisor_time(state["cooldown_until"])
    if cooldown_until is not None and checked < cooldown_until:
        persist_supervisor_state(path, state)
        logger.info("hud_supervisor: in cooldown count=%d", len(restart_times))
        return {**base, "action": "report_only", "reason": "cooldown_active", "budget_exhausted": False}

    if restart_control is not None:
        action_control = restart_control
    elif relaunch_control is not None and killer is None and is_running is None:
        action_control = relaunch_control
    else:
        restart_kwargs: dict[str, Any] = {}
        if killer is not None:
            restart_kwargs["killer"] = killer
        if is_running is not None:
            restart_kwargs["is_running"] = is_running
        if sleep is not None:
            restart_kwargs["sleep"] = sleep
        if monotonic is not None:
            restart_kwargs["monotonic"] = monotonic
        if relaunch_control is not None:
            restart_kwargs["relaunch_fn"] = relaunch_control
        if hud is not None:
            restart_kwargs["active_pid_finder"] = lambda: hud.pid if hud.alive else None
        action_control = lambda: restart_hud(**restart_kwargs)
    result = action_control()
    # Failed attempts count against the budget too, so a missing app is retried 3 times an hour, not every minute.
    restart_times.append(checked)
    state["restart_times"] = [val.isoformat() for val in restart_times]
    if len(restart_times) >= HUD_RESTART_BUDGET_PER_HOUR:
        state["cooldown_until"] = (checked + timedelta(seconds=HUD_RESTART_COOLDOWN_SEC)).isoformat()
    if result.ok:
        state["consecutive_hung"] = 0
        state["last_action"] = {
            "action": "relaunch",
            "reason": action_reason,
            "timestamp": checked.isoformat(),
        }
        logger.info("hud_supervisor: relaunch action=%s count=%d", action_reason, len(restart_times))
    else:
        logger.warning("hud_supervisor: relaunch failed reason=%s error=%s", action_reason, result.error_zh)

    persist_supervisor_state(path, state)
    return {
        **base,
        "action": "relaunch" if result.ok else "relaunch_failed",
        "budget_exhausted": len(restart_times) >= HUD_RESTART_BUDGET_PER_HOUR,
        "error_zh": result.error_zh,
        "error_en": result.error_en,
        "restarts_in_window": len(restart_times),
    }
