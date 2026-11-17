"""Persistence for sources, sync cursors and evidence rows (schema v20)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from penpal.store.db import get_conn
from penpal.store.locators import is_unresolvable_locator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_source(source_type: str, locator: str) -> int:
    """Return the id of the (type, locator) source, creating it on first use."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO sources(type, locator, created_at) VALUES (?,?,?) ON CONFLICT(type, locator) DO NOTHING",
        (source_type, locator, _now()),
    )
    row = conn.execute(
        "SELECT id FROM sources WHERE type = ? AND locator = ?", (source_type, locator)
    ).fetchone()
    conn.commit()
    return int(row[0])


def get_cursor(source_id: int) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT cursor FROM sync_cursors WHERE source_id = ?", (source_id,)
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        value = json.loads(row[0])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def save_cursor(source_id: int, cursor: dict[str, Any]) -> None:
    now = _now()
    conn = get_conn()
    conn.execute(
        """INSERT INTO sync_cursors(source_id, cursor, last_success, last_error)
           VALUES (?,?,?,NULL)
           ON CONFLICT(source_id) DO UPDATE SET
             cursor=excluded.cursor, last_success=excluded.last_success, last_error=NULL""",
        (source_id, json.dumps(cursor, sort_keys=True), now),
    )
    conn.execute(
        "UPDATE sources SET last_sync = ?, health = 'ok' WHERE id = ?", (now, source_id)
    )
    conn.commit()


def record_sync_error(source_id: int, message: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO sync_cursors(source_id, cursor, last_error) VALUES (?,NULL,?)
           ON CONFLICT(source_id) DO UPDATE SET last_error=excluded.last_error""",
        (source_id, message[:500]),
    )
    conn.execute("UPDATE sources SET health = 'error' WHERE id = ?", (source_id,))
    conn.commit()


def add_evidence(
    event_id: int,
    locator: str,
    offset_or_line: Optional[int],
    content_hash: Optional[str],
    *,
    conn: Optional[sqlite3.Connection] = None,
    commit: bool = True,
) -> None:
    """Idempotent: one evidence row per (event, locator). An "unresolvable://" locator (knowledgec,
    approved_sqlite: no openable original) is stored with available=0."""
    if conn is None:
        conn = get_conn()
    available = 0 if is_unresolvable_locator(locator) else 1
    conn.execute(
        """INSERT INTO evidence(event_id, locator, offset_or_line, content_hash, available, created_at)
           SELECT ?,?,?,?,?,?
           WHERE NOT EXISTS (SELECT 1 FROM evidence WHERE event_id = ? AND locator = ?)""",
        (event_id, locator, offset_or_line, content_hash, available, _now(), event_id, locator),
    )
    if commit:
        conn.commit()
