"""Local memory selection and physical deletion."""
from __future__ import annotations

import sqlite3

from penpal.store.db import get_conn


def list_memories(*, source=None, tool=None, status="active", limit=50):
    clauses, params = [], []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if tool:
        clauses.append("source_tool = ?")
        params.append(tool)
    if status == "active":
        clauses.append("status = 'active' AND superseded_by IS NULL")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = get_conn()
    total = conn.execute("SELECT count(*) FROM memories" + where, params).fetchone()[0]
    rows = conn.execute("SELECT * FROM memories" + where + " ORDER BY created_at DESC, id DESC LIMIT ?",
                        (*params, limit)).fetchall()
    return rows, total


def memory_ids(conn: sqlite3.Connection, seed_sql: str, params=()) -> list[int]:
    rows = conn.execute(
        "WITH RECURSIVE selected(id) AS (" + seed_sql +
        " UNION SELECT m.id FROM memories m JOIN selected s ON m.superseded_by = s.id)"
        " SELECT id FROM selected", params,
    )
    return [row[0] for row in rows]


def delete_ids(conn: sqlite3.Connection, ids: list[int]) -> None:
    if ids:
        # JSON avoids SQLite's parameter limit for large imports.
        import json
        conn.execute("DELETE FROM memories WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))


def delete_memories(*, ids=(), source=None, dry_run=False) -> int:
    if bool(ids) == bool(source):
        raise ValueError("choose memory IDs or a source")
    conn = get_conn()
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if source:
            selected = memory_ids(conn, "SELECT id FROM memories WHERE source = ?", (source,))
        else:
            import json
            selected = memory_ids(conn, "SELECT id FROM memories WHERE id IN (SELECT value FROM json_each(?))",
                                  (json.dumps(ids),))
        if not dry_run:
            delete_ids(conn, selected)
        conn.commit()
        return len(selected)
    except BaseException:
        conn.rollback()
        raise
