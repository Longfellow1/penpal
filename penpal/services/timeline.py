from __future__ import annotations
from datetime import datetime
from typing import Optional
from penpal.services.sessionizer import sessions_for_date, sessions_for_today
from penpal.utils.dates import local_timezone


def _fmt_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    m = secs // 60
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(local_timezone()).strftime("%H:%M:%S")
    except Exception:
        return ts


def get_timeline_rows(date_str: Optional[str] = None) -> list[dict]:
    """
    Returns list of dicts with keys:
      start, end, app, title, duration, duration_secs
    """
    sessions = sessions_for_date(date_str) if date_str else sessions_for_today()
    rows = []
    for s in sessions:
        rows.append({
            "id": s["id"],
            "start": _fmt_ts(s["started_at"]),
            "end": _fmt_ts(s["ended_at"]),
            "app": s.get("app_name") or "—",
            "title": (s.get("primary_window_title") or "")[:60],
            "duration": _fmt_duration(s.get("duration_sec") or 0),
            "duration_secs": s.get("duration_sec") or 0,
        })
    return rows
