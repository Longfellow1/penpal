"""Single source of truth for HUD supervisor state storage and summary."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from penpal.utils.atomic_io import atomic_write_text
from penpal.utils.paths import get_run_dir

HUD_RESTART_BUDGET_PER_HOUR = 3
HUD_RESTART_BUDGET_WINDOW_SEC = 60 * 60
HUD_RESTART_COOLDOWN_SEC = 15 * 60


def supervisor_state_path() -> Path:
    return get_run_dir() / "hud_supervisor.json"


def _empty_state() -> dict[str, Any]:
    return {
        "consecutive_hung": 0,
        "cooldown_until": "",
        "restart_times": [],
        "last_action": {},
    }


def parse_supervisor_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def read_supervisor_state(path: Path) -> tuple[dict[str, Any], bool]:
    """Read the supervisor state file. Returns (state_dict, was_corrupt)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state(), False
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_state(), True
    if not isinstance(raw, dict):
        return _empty_state(), True
    hung = raw.get("consecutive_hung", 0)
    cooldown = raw.get("cooldown_until", "")
    restart_times = raw.get("restart_times", [])
    last_action = raw.get("last_action", {})
    if (
        not isinstance(hung, int) or hung < 0
        or not isinstance(cooldown, str)
        or not isinstance(restart_times, list)
        or not isinstance(last_action, dict)
    ):
        return _empty_state(), True
    if any(not isinstance(item, str) for item in restart_times):
        return _empty_state(), True
    return {
        "consecutive_hung": hung,
        "cooldown_until": cooldown,
        "restart_times": restart_times,
        "last_action": last_action,
    }, False


def persist_supervisor_state(path: Path, state: dict[str, Any]) -> None:
    rendered = json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n"
    atomic_write_text(path, rendered)


def read_supervisor_summary(
    state_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Read the supervisor's recent action summary: (zh, en)."""
    path = state_path or supervisor_state_path()
    state, corrupt = read_supervisor_state(path)
    if corrupt or not state.get("restart_times"):
        return "", ""
    checked = now or datetime.now(timezone.utc)
    cutoff = checked - timedelta(seconds=HUD_RESTART_BUDGET_WINDOW_SEC)
    valid_times = [
        val for raw in state["restart_times"]
        if (val := parse_supervisor_time(raw)) is not None and val > cutoff
    ]
    if not valid_times:
        return "", ""
    last_val = max(valid_times)
    time_str = last_val.astimezone().strftime("%H:%M")
    count = len(valid_times)
    return (
        f"已自动重开 {count} 次（{time_str}）",
        f"automatically relaunched {count} time(s) ({time_str})",
    )
