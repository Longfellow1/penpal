import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from penpal.store.migration_v20 import migrate_v20, rebuild_events_fts  # noqa: F401
from penpal.store.migration_v21 import migrate_v21
from penpal.store.migration_v22 import migrate_v22
from penpal.store.migration_v23 import migrate_v23
from penpal.store.migration_v24 import migrate_v24
from penpal.store.migration_v26 import migrate_v26
from penpal.store.migration_v27 import migrate_v27
from penpal.store.migration_v28 import migrate_v28
from penpal.store.migration_v29 import migrate_v29
from penpal.store.migration_v30 import migrate_v30
from penpal.store.migration_v31 import migrate_v31
from penpal.store.migration_v32 import migrate_v32
from penpal.store.migration_v33 import migrate_v33
from penpal.store.migration_v34 import migrate_v34
from penpal.store.migration_v35 import migrate_v35
from penpal.store.migration_v36 import migrate_v36

LOGGER = logging.getLogger("penpal.store.migrations")

SCHEMA_VERSION = 36

MIGRATIONS = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS raw_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        event_type TEXT NOT NULL,
        ts_start TEXT NOT NULL,
        ts_end TEXT,
        app_name TEXT,
        window_title TEXT,
        process_name TEXT,
        content_text TEXT,
        content_hash TEXT,
        metadata_json TEXT,
        sensitivity_level INTEGER DEFAULT 0,
        skipped_reason TEXT,
        session_id TEXT,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        app_name TEXT,
        primary_window_title TEXT,
        duration_sec INTEGER DEFAULT 0,
        event_count INTEGER DEFAULT 0,
        summary TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS search_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref_type TEXT NOT NULL,
        ref_id TEXT NOT NULL,
        title TEXT,
        body TEXT,
        tags TEXT,
        app_name TEXT,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS search_docs_fts USING fts5(
        title,
        body,
        app_name,
        content=search_docs,
        content_rowid=id
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS search_docs_ai AFTER INSERT ON search_docs BEGIN
        INSERT INTO search_docs_fts(rowid, title, body, app_name)
        VALUES (new.id, new.title, new.body, new.app_name);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS search_docs_ad AFTER DELETE ON search_docs BEGIN
        INSERT INTO search_docs_fts(search_docs_fts, rowid, title, body, app_name)
        VALUES ('delete', old.id, old.title, old.body, old.app_name);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS search_docs_au AFTER UPDATE ON search_docs BEGIN
        INSERT INTO search_docs_fts(search_docs_fts, rowid, title, body, app_name)
        VALUES ('delete', old.id, old.title, old.body, old.app_name);
        INSERT INTO search_docs_fts(rowid, title, body, app_name)
        VALUES (new.id, new.title, new.body, new.app_name);
    END;
    """,
    """
    CREATE TABLE IF NOT EXISTS policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_type TEXT NOT NULL,
        scope_value TEXT NOT NULL,
        mode TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 100,
        config_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_raw_events_ts_start ON raw_events(ts_start);
    CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source);
    CREATE INDEX IF NOT EXISTS idx_raw_events_app_name ON raw_events(app_name);
    CREATE INDEX IF NOT EXISTS idx_raw_events_session_id ON raw_events(session_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
    CREATE INDEX IF NOT EXISTS idx_search_docs_ref ON search_docs(ref_type, ref_id);
    """,
    """
    ALTER TABLE raw_events ADD COLUMN speaker TEXT NOT NULL DEFAULT 'system';
    CREATE INDEX IF NOT EXISTS idx_raw_events_speaker ON raw_events(speaker);
    """,
    """
    ALTER TABLE raw_events ADD COLUMN semantic_weight REAL NOT NULL DEFAULT 0.5;
    CREATE INDEX IF NOT EXISTS idx_raw_events_weight ON raw_events(semantic_weight);
    UPDATE raw_events SET semantic_weight = CASE source
        WHEN 'keyboard_chunk' THEN 1.0
        WHEN 'clipboard' THEN 0.9
        WHEN 'manual' THEN 1.0
        WHEN 'browser' THEN 0.85
        WHEN 'ax_text' THEN 0.8
        WHEN 'ax_ime_commit' THEN 0.9
        WHEN 'ax_snapshot_fallback' THEN 0.5
        WHEN 'ocr_text' THEN 0.4
        WHEN 'window_focus_session' THEN 0.2
        ELSE 0.5
    END;
    """,
    # v13
    """
    ALTER TABLE raw_events ADD COLUMN user_present INTEGER NOT NULL DEFAULT 1;
    CREATE INDEX IF NOT EXISTS idx_raw_events_user_present ON raw_events(user_present);
    """,
    # v14
    """
    CREATE TABLE IF NOT EXISTS profile_identity (
        key TEXT PRIMARY KEY, value TEXT, confidence REAL, source TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS profile_entities (
        alias TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, kind TEXT, weight REAL DEFAULT 1.0
    );
    CREATE TABLE IF NOT EXISTS profile_projects (
        name TEXT PRIMARY KEY, path TEXT, summary TEXT, tech_stack TEXT, status TEXT, last_active TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_profile_entities_canonical ON profile_entities(canonical_name);
    """,
    """
    CREATE TABLE IF NOT EXISTS hourly_summaries (
        date TEXT NOT NULL,
        hour INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        PRIMARY KEY (date, hour)
    );
    CREATE INDEX IF NOT EXISTS idx_hourly_summaries_date ON hourly_summaries(date);
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_skeletons (
        date TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        generated_at TEXT NOT NULL
    );
    """,
    # v17 — raw_events idempotent insert
    # Dedupe pre-existing rows that share (source, ts_start, content_hash),
    # keeping the earliest id; then enforce a unique index so future INSERT
    # ON CONFLICT DO NOTHING blocks duplicate watcher emissions.
    # NULL content_hash is normalised to '' so two NULL-hash events with
    # identical (source, ts_start) collide as expected (SQLite treats NULL
    # as distinct in UNIQUE indexes by default).
    """
    DELETE FROM raw_events
    WHERE id NOT IN (
        SELECT MIN(id) FROM raw_events
        GROUP BY source, ts_start, IFNULL(content_hash, '')
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_events_dedup
    ON raw_events(source, ts_start, IFNULL(content_hash, ''));
    """,
    # v18 — daily_skeletons add timezone column
    """
    ALTER TABLE daily_skeletons ADD COLUMN timezone TEXT;
    """,
    # v19 — daily v3 removed hourly summaries and daily skeleton cache tables
    """
    DROP INDEX IF EXISTS idx_hourly_summaries_date;
    DROP TABLE IF EXISTS hourly_summaries;
    DROP TABLE IF EXISTS daily_skeletons;
    """,
    # v20 — V1 mainline (D20): event provenance columns, sources / cursors /
    # evidence / access grants, FTS5 over raw_events. Python migration so the
    # whole step runs inside one transaction (see migration_v20).
    migrate_v20,
    # v21 — AccessGrant scope (buckets / sources / operations / purpose / expiry)
    # + bucket backfill from per-source defaults (see migration_v21).
    migrate_v21,
    # v22 — client whitelist, token rotation marker, MCP audit log (see migration_v22).
    migrate_v22,
    # v23 — D26: capture streams queryable (bucket + secret-redaction + evidence backfill).
    migrate_v23,
    # v24 — D27: existing capture rows run through the hygiene gate (see migration_v24).
    migrate_v24,
    # v25 — owner decision 2026-10-12: keyboard chunks captured before the v24 tap fix (spaces were
    # stripped, so leftover pinyin can't be told from English) are kept but hidden from search.
    # Every keyboard row present at upgrade time predates the fixed tap. Idempotent (json_set).
    """
    UPDATE raw_events
       SET metadata_json = json_set(
               CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE '{}' END,
               '$.pre_fix_keyboard', 1)
     WHERE event_type = 'keyboard_chunk';
    """,
    # v26 — P0-12c real-data fixes (issue #53, D27 / D28): zero-width strip, system-overlay apps,
    # AI-transcript noise, input placeholders, zsh re-decode reset, browser content by website
    # (see migration_v26).
    migrate_v26,
    # v27 — browser-history plugins (chrome_history / safari_history) bucketed by website, like
    # browser_url; re-buckets Personal rows by their URL (see migration_v27).
    migrate_v27,
    # v28 — M2 (issue #57): evidence rows for existing rows of every source plugin, from stored
    # locators only (see migration_v28).
    migrate_v28,
    # v29 — M2 (issue #63): returned-result ledger + feedback verdicts for rate_result.
    migrate_v29,
    # v30 — old markdown_vault rows: evidence via vault discovery, vault name -> root (see migration_v30).
    migrate_v30,
    # v31 — old codex_cli history rows (raw_ref codex:history:<N>): evidence via the history file (see migration_v31).
    migrate_v31,
    # v32 — context label for old imported rows (repo / project / site / note path into window_title, see migration_v32).
    migrate_v32,
    # v33 — progressive-typing snapshots (ax_text): earlier states marked superseded_by the next state (see migration_v33).
    migrate_v33,
    # v34 — re-mark progressive-typing snapshots with the spaced-pinyin and same-draft-edit rules (see migration_v34).
    migrate_v34,
    migrate_v35,
    migrate_v36,
]


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    return None


def _backup_before_migration(conn: sqlite3.Connection, current: int) -> Path | None:
    """Copy an existing non-empty DB next to itself before schema changes."""
    path = _db_file(conn)
    if path is None or not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.v{current}-{stamp}.bak")
    # sqlite3 online backup API: consistent even with WAL contents pending.
    dst = sqlite3.connect(str(target))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    LOGGER.info("db backup written before migration: %s", target.name)
    return target


def run_migrations(conn: sqlite3.Connection):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()
    row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
    current = row[0] if row[0] is not None else 0
    if 0 < current < len(MIGRATIONS):
        _backup_before_migration(conn, current)
    for i, step in enumerate(MIGRATIONS, start=1):
        if i <= current:
            continue
        if callable(step):
            _apply_transactional(conn, i, step)
            continue
        conn.executescript(step.strip())
        conn.execute(
            "INSERT INTO _schema_version(version, applied_at) VALUES (?, ?)",
            (i, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()


def _apply_transactional(conn: sqlite3.Connection, version: int, step) -> None:
    """Run a Python migration atomically together with its version stamp."""
    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            step(conn)
            conn.execute(
                "INSERT INTO _schema_version(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = previous_isolation
