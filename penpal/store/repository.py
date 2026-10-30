import sqlite3
import json
from datetime import datetime, timezone
from typing import Optional
from penpal.foundation.desensitizer import redact_secrets
from penpal.store.capture_events import CAPTURE_SEARCHABLE_EVENT_TYPES, penpal_locator
from penpal.store.db import get_conn
from penpal.utils.text_filters import strip_invisible
from penpal.store.models import RawEvent, Session, SearchDoc, Policy
from penpal.store.snapshots import mark_previous_snapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── RawEvent ─────────────────────────────────────────────────────────────────

def insert_raw_event(e: RawEvent) -> int:
    """Idempotent insert.

    A unique index on (source, ts_start, IFNULL(content_hash,'')) blocks
    duplicate emissions from the same watcher; since v20 a second unique
    index on (source_id, external_id) blocks re-imports of the same
    upstream record. On conflict the existing
    row's id is returned so callers keep working with a stable id even
    after a retry. NULL content_hash is normalised to '' so two
    NULL-hash events with identical (source, ts_start) collide.
    """
    conn = get_conn()
    # Credentials must not reach the DB / FTS index / MCP hosts (D26). Idempotent; capture keeps its
    # own PII filters upstream, this is the secret-class backstop for every writer.
    e.content_text = redact_secrets(strip_invisible(e.content_text))
    e.window_title = redact_secrets(strip_invisible(e.window_title))
    if e.bucket is None:
        # P0 bucket assignment (D23): source default + app rules from the policies table.
        from penpal.store.buckets import resolve_for_event

        e.bucket = resolve_for_event(
            conn, e.source, e.app_name, e.process_name, metadata_json=e.metadata_json, ts_start=e.ts_start,
            window_title=e.window_title,
        )
    cur = conn.execute(
        """INSERT INTO raw_events
           (source, event_type, ts_start, ts_end, app_name, window_title,
            process_name, content_text, content_hash, metadata_json,
            sensitivity_level, skipped_reason, session_id, speaker, semantic_weight, user_present, created_at,
            source_id, external_id, bucket, kind)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT DO NOTHING""",
        (e.source, e.event_type, e.ts_start, e.ts_end, e.app_name,
         e.window_title, e.process_name, e.content_text, e.content_hash,
         e.metadata_json, e.sensitivity_level, e.skipped_reason,
         e.session_id, e.speaker, e.semantic_weight, e.user_present, e.created_at,
         e.source_id, e.external_id, e.bucket, e.kind),
    )
    if cur.rowcount > 0 and e.event_type in CAPTURE_SEARCHABLE_EVENT_TYPES:
        # Penpal itself is the original record: evidence points at the event, always available.
        conn.execute(
            """INSERT INTO evidence(event_id, locator, offset_or_line, content_hash, available, created_at)
               VALUES (?,?,?,?,1,?)""",
            (cur.lastrowid, penpal_locator(cur.lastrowid), None, e.content_hash, _now()),
        )
    if cur.rowcount > 0:
        # Progressive-typing snapshots: the previous state of the same field is superseded (v33 rule).
        mark_previous_snapshot(conn, cur.lastrowid, e.source, e.app_name, e.ts_start, e.content_text)
    conn.commit()
    if cur.rowcount > 0:
        return cur.lastrowid
    if e.external_id is not None:
        row = conn.execute(
            "SELECT id FROM raw_events WHERE source_id IS ? AND external_id = ?",
            (e.source_id, e.external_id),
        ).fetchone()
        if row:
            return row[0]
    row = conn.execute(
        """SELECT id FROM raw_events
           WHERE source=? AND ts_start=? AND IFNULL(content_hash,'')=IFNULL(?,'')""",
        (e.source, e.ts_start, e.content_hash),
    ).fetchone()
    if not row:
        return 0
    if e.source_id is not None or e.external_id is not None or e.bucket or e.kind:
        # Legacy row hit by the dedup index: fill missing provenance, never overwrite.
        # (A same-key row already owning this external_id is left alone.)
        try:
            conn.execute(
                """UPDATE raw_events SET
                     source_id   = COALESCE(source_id, ?),
                     external_id = COALESCE(external_id, ?),
                     bucket      = COALESCE(bucket, ?),
                     kind        = COALESCE(kind, ?)
                   WHERE id = ?""",
                (e.source_id, e.external_id, e.bucket, e.kind, row[0]),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
    return row[0]


def query_raw_events(
    source: Optional[str] = None,
    app_name: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    conn = get_conn()
    clauses, params = [], []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if app_name:
        clauses.append("app_name LIKE ?")
        params.append(f"%{app_name}%")
    if since:
        clauses.append("ts_start >= ?")
        params.append(since)
    if until:
        clauses.append("ts_start <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM raw_events {where} ORDER BY ts_start DESC LIMIT ?",
        (*params, limit)
    ).fetchall()
    return [dict(r) for r in rows]


PURGE_COUNT_KEYS = (
    "raw_events", "raw_events_fts", "evidence", "mcp_returned_results", "mcp_feedback",
    "search_docs", "sessions",
)


def purge_raw_events(
    since: Optional[str] = None,
    until: Optional[str] = None,
    app_name: Optional[str] = None,
    source: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete the raw_events matching the selector AND everything derived from them, in one transaction.

    Selector: time range (ts_start, inclusive), app_name (exact, case-insensitive), source (exact).
    At least one must be given; an empty selector is refused rather than meaning "everything".
    Returns per-table row counts. dry_run=True counts and rolls back.

    Derived data handled:
      * raw_events_fts        kept in sync by the raw_events AFTER DELETE trigger (verified in tests)
      * evidence              rows whose event_id is deleted (explicit: FK cascade is not enabled on the
                              app connection)
      * mcp_returned_results  rows for a deleted event or evidence id
      * mcp_feedback          rows for a deleted event or evidence id (their note / query text goes too)
      * search_docs           clipboard / manual docs whose ref_id is a deleted event id
      * sessions              sessions whose events were all deleted are removed; a session that keeps
                              other events is rebuilt from what is left (count, title) and its summary
                              cleared, so no deleted text survives in it
    """
    clauses, params = [], []
    if since:
        clauses.append("ts_start >= ?")
        params.append(since)
    if until:
        clauses.append("ts_start <= ?")
        params.append(until)
    if app_name is not None:
        clauses.append("lower(app_name) = lower(?)")
        params.append(app_name)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if not clauses:
        raise ValueError("purge needs at least one selector: a time range, app_name or source")

    conn = get_conn()
    if conn.in_transaction:
        conn.commit()
    # Temp tables are created outside the transaction: a dry-run ROLLBACK would otherwise drop them.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_event_ids (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_evidence_ids (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_session_ids (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_partial_session_ids (id TEXT PRIMARY KEY)")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM purge_event_ids")
        conn.execute("DELETE FROM purge_evidence_ids")
        conn.execute("DELETE FROM purge_session_ids")
        conn.execute("DELETE FROM purge_partial_session_ids")
        conn.execute(
            f"INSERT INTO purge_event_ids SELECT id FROM raw_events WHERE {' AND '.join(clauses)}", params
        )
        conn.execute(
            "INSERT INTO purge_evidence_ids SELECT id FROM evidence WHERE event_id IN (SELECT id FROM purge_event_ids)"
        )
        # Sessions that lose every event. A session that keeps events outside the selector stays.
        conn.execute(
            """INSERT INTO purge_session_ids SELECT s.id FROM sessions s
               WHERE s.id IN (SELECT session_id FROM raw_events
                              WHERE id IN (SELECT id FROM purge_event_ids) AND session_id IS NOT NULL)
                 AND NOT EXISTS (SELECT 1 FROM raw_events r
                                 WHERE r.session_id = s.id AND r.id NOT IN (SELECT id FROM purge_event_ids))"""
        )
        conn.execute(
            """INSERT INTO purge_partial_session_ids SELECT DISTINCT session_id FROM raw_events
               WHERE id IN (SELECT id FROM purge_event_ids) AND session_id IS NOT NULL
                 AND session_id NOT IN (SELECT id FROM purge_session_ids)"""
        )

        ev = "event_id IN (SELECT id FROM purge_event_ids) OR evidence_id IN (SELECT id FROM purge_evidence_ids)"
        doc = "ref_type IN ('clipboard', 'manual') AND ref_id IN (SELECT CAST(id AS TEXT) FROM purge_event_ids)"
        targets = {
            "raw_events": ("SELECT COUNT(*) FROM purge_event_ids", None),
            "raw_events_fts": ("SELECT COUNT(*) FROM purge_event_ids", None),
            "evidence": ("SELECT COUNT(*) FROM purge_evidence_ids", None),
            "mcp_returned_results": (f"SELECT COUNT(*) FROM mcp_returned_results WHERE {ev}",
                                     f"DELETE FROM mcp_returned_results WHERE {ev}"),
            "mcp_feedback": (f"SELECT COUNT(*) FROM mcp_feedback WHERE {ev}",
                             f"DELETE FROM mcp_feedback WHERE {ev}"),
            "search_docs": (f"SELECT COUNT(*) FROM search_docs WHERE {doc}",
                            f"DELETE FROM search_docs WHERE {doc}"),
            "sessions": ("SELECT COUNT(*) FROM purge_session_ids",
                         "DELETE FROM sessions WHERE id IN (SELECT id FROM purge_session_ids)"),
        }
        counts = {name: conn.execute(count_sql).fetchone()[0] for name, (count_sql, _) in targets.items()}
        if dry_run:
            conn.execute("ROLLBACK")
            return counts

        # Children first. raw_events last: its AFTER DELETE trigger removes the FTS rows.
        for name in ("mcp_returned_results", "mcp_feedback", "search_docs", "sessions"):
            conn.execute(targets[name][1])
        conn.execute("DELETE FROM evidence WHERE id IN (SELECT id FROM purge_evidence_ids)")
        conn.execute("DELETE FROM raw_events WHERE id IN (SELECT id FROM purge_event_ids)")
        conn.execute(
            """UPDATE sessions SET
                 event_count = (SELECT COUNT(*) FROM raw_events r WHERE r.session_id = sessions.id),
                 primary_window_title = (SELECT r.window_title FROM raw_events r
                                         WHERE r.session_id = sessions.id AND r.window_title IS NOT NULL
                                         GROUP BY r.window_title ORDER BY COUNT(*) DESC LIMIT 1),
                 summary = NULL
               WHERE id IN (SELECT id FROM purge_partial_session_ids)"""
        )
        conn.execute("COMMIT")
        return counts
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        # Temp tables only hold ids for this call; clear them so nothing lingers on the connection.
        conn.execute("DELETE FROM purge_event_ids")
        conn.execute("DELETE FROM purge_evidence_ids")
        conn.execute("DELETE FROM purge_session_ids")
        conn.execute("DELETE FROM purge_partial_session_ids")
        conn.commit()


# ── Session ───────────────────────────────────────────────────────────────────

def upsert_session(s: Session):
    conn = get_conn()
    conn.execute(
        """INSERT INTO sessions
           (id, started_at, ended_at, app_name, primary_window_title,
            duration_sec, event_count, summary, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             ended_at=excluded.ended_at,
             duration_sec=excluded.duration_sec,
             event_count=excluded.event_count,
             summary=excluded.summary,
             updated_at=excluded.updated_at""",
        (s.id, s.started_at, s.ended_at, s.app_name, s.primary_window_title,
         s.duration_sec, s.event_count, s.summary, s.created_at, s.updated_at),
    )
    conn.commit()


def get_sessions(date_str: Optional[str] = None, limit: int = 200) -> list[dict]:
    """date_str format: YYYY-MM-DD"""
    conn = get_conn()
    if date_str:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE started_at LIKE ? ORDER BY started_at ASC LIMIT ?",
            (f"{date_str}%", limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_by_id(session_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


# ── SearchDoc ─────────────────────────────────────────────────────────────────

def insert_search_doc(doc: SearchDoc) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO search_docs (ref_type, ref_id, title, body, tags, app_name, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (doc.ref_type, doc.ref_id, doc.title, doc.body, doc.tags, doc.app_name, doc.created_at),
    )
    conn.commit()
    return cur.lastrowid


def search_docs_fts(query: str, app_name: Optional[str] = None, limit: int = 50) -> list[dict]:
    conn = get_conn()
    sql = """
        SELECT sd.*, bm25(search_docs_fts) as score
        FROM search_docs sd
        JOIN search_docs_fts ON search_docs_fts.rowid = sd.id
        WHERE search_docs_fts MATCH ?
    """
    params = [query]
    if app_name:
        sql += " AND sd.app_name LIKE ?"
        params.append(f"%{app_name}%")
    sql += " ORDER BY score, sd.created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Policy ────────────────────────────────────────────────────────────────────

def get_all_policies() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM policies WHERE enabled=1 ORDER BY priority ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def insert_policy(p: Policy) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO policies (scope_type, scope_value, mode, enabled, priority, config_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (p.scope_type, p.scope_value, p.mode, int(p.enabled), p.priority,
         p.config_json, p.created_at, p.updated_at),
    )
    conn.commit()
    return cur.lastrowid


def seed_policies_from_config(policy_configs: list):
    """Insert policies from config if not already seeded."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0]
    if count > 0:
        return  # already seeded
    for pc in policy_configs:
        bucket = getattr(pc, "bucket", None)
        if bucket:
            from penpal.store.buckets import BUCKET_MODE, normalize_bucket

            p = Policy(
                scope_type=pc.scope_type,
                scope_value=pc.scope_value,
                mode=BUCKET_MODE,
                config_json=json.dumps({"bucket": normalize_bucket(bucket)}),
                enabled=pc.enabled,
                priority=pc.priority,
            )
        else:
            p = Policy(
                scope_type=pc.scope_type,
                scope_value=pc.scope_value,
                mode=pc.mode,
                enabled=pc.enabled,
                priority=pc.priority,
            )
        insert_policy(p)


# ── AppState ──────────────────────────────────────────────────────────────────

def set_state(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO app_state(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, _now()),
    )
    conn.commit()


def get_state(key: str) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


