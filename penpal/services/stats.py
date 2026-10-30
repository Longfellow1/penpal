from __future__ import annotations
from datetime import datetime, timedelta, timezone
from penpal.store.db import get_conn


def get_stats(days: int = 7) -> dict:
    """Aggregate stats for last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()

    # Total sessions
    total_sessions = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (since,)
    ).fetchone()[0]

    # Total active seconds
    total_secs = conn.execute(
        "SELECT COALESCE(SUM(duration_sec), 0) FROM sessions WHERE started_at >= ?",
        (since,),
    ).fetchone()[0]

    # App distribution (top 10 by total duration)
    app_rows = conn.execute(
        """SELECT app_name, SUM(duration_sec) as total_sec
           FROM sessions
           WHERE started_at >= ? AND app_name IS NOT NULL
           GROUP BY app_name
           ORDER BY total_sec DESC
           LIMIT 10""",
        (since,),
    ).fetchall()
    app_distribution = [{"app": r[0], "duration_sec": r[1]} for r in app_rows]

    # Clipboard count
    clipboard_count = conn.execute(
        "SELECT COUNT(*) FROM raw_events WHERE source='clipboard' AND ts_start >= ?",
        (since,),
    ).fetchone()[0]

    # Manual saves
    manual_count = conn.execute(
        "SELECT COUNT(*) FROM raw_events WHERE source='manual' AND ts_start >= ?",
        (since,),
    ).fetchone()[0]

    # Active days
    active_days = conn.execute(
        "SELECT COUNT(DISTINCT substr(started_at, 1, 10)) FROM sessions WHERE started_at >= ?",
        (since,),
    ).fetchone()[0]

    def fmt_duration(secs: int) -> str:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m}m"

    return {
        "days": days,
        "total_sessions": total_sessions,
        "total_active_secs": total_secs,
        "total_active_human": fmt_duration(total_secs),
        "active_days": active_days,
        "app_distribution": app_distribution,
        "clipboard_count": clipboard_count,
        "manual_count": manual_count,
    }
