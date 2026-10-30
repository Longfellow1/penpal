from __future__ import annotations
from datetime import date
from penpal.store.repository import get_sessions


def sessions_for_date(date_str: str) -> list[dict]:
    """Return sessions for YYYY-MM-DD."""
    return get_sessions(date_str=date_str)


def sessions_for_today() -> list[dict]:
    today = date.today().isoformat()
    return sessions_for_date(today)


def recent_sessions(limit: int = 20) -> list[dict]:
    return get_sessions(limit=limit)
