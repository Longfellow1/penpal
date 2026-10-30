from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from penpal.store import migration_v20
from penpal.store.migrations import MIGRATIONS, SCHEMA_VERSION, run_migrations


def _build_v19_db(path: Path) -> None:
    """Synthetic legacy DB: apply exactly the first 19 migrations, add rows."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    for i, sql in enumerate(MIGRATIONS[:19], start=1):
        conn.executescript(sql.strip())
        conn.execute("INSERT INTO _schema_version VALUES (?, 'legacy')", (i,))
    conn.execute(
        "INSERT INTO raw_events(source,event_type,ts_start,content_text,content_hash,created_at) "
        "VALUES ('clipboard','clipboard_copy','2026-01-02T03:04:05+00:00','legacy pelican note','h1','2026-01-02T03:04:06+00:00')"
    )
    conn.execute(
        "INSERT INTO raw_events(source,event_type,ts_start,window_title,content_hash,created_at) "
        "VALUES ('window','window_focus','2026-01-02T04:00:00+00:00','Terminal','h2','2026-01-02T04:00:01+00:00')"
    )
    conn.commit()
    conn.close()


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def test_schema_version_is_21() -> None:
    assert SCHEMA_VERSION == len(MIGRATIONS)


def test_v20_upgrades_v19_in_place_and_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "penpal.db"
    _build_v19_db(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert _version(conn) == 19
    run_migrations(conn)

    assert _version(conn) == SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_events)")}
    assert {"source_id", "external_id", "bucket", "kind"} <= cols
    assert {"sources", "sync_cursors", "evidence", "access_grants", "raw_events_fts"} <= _tables(conn)
    # legacy rows survive, kind backfilled from event_type
    rows = conn.execute("SELECT event_type, kind FROM raw_events ORDER BY id").fetchall()
    assert [(r["event_type"], r["kind"]) for r in rows] == [
        ("clipboard_copy", "clipboard_copy"),
        ("window_focus", "window_focus"),
    ]
    # legacy text is searchable (FTS built during migration)
    hit = conn.execute("SELECT rowid FROM raw_events_fts WHERE raw_events_fts MATCH '\"pelican\"'").fetchall()
    assert len(hit) == 1
    # triggers keep FTS in sync afterwards
    conn.execute(
        "INSERT INTO raw_events(source,event_type,ts_start,content_text,created_at) "
        "VALUES ('manual','manual_save','2026-02-01T00:00:00+00:00','fresh axolotl text','2026-02-01T00:00:00+00:00')"
    )
    assert conn.execute("SELECT count(*) FROM raw_events_fts WHERE raw_events_fts MATCH '\"axolotl\"'").fetchone()[0] == 1
    conn.execute("DELETE FROM raw_events WHERE source='manual'")
    assert conn.execute("SELECT count(*) FROM raw_events_fts WHERE raw_events_fts MATCH '\"axolotl\"'").fetchone()[0] == 0
    conn.close()


def test_v20_unique_external_id_per_source(tmp_path: Path) -> None:
    db = tmp_path / "penpal.db"
    conn = sqlite3.connect(db)
    run_migrations(conn)
    ins = (
        "INSERT INTO raw_events(source,event_type,ts_start,content_hash,created_at,source_id,external_id) "
        "VALUES ('s','e',?,?,'t',?,?)"
    )
    conn.execute(ins, ("2026-01-01T00:00:00+00:00", "a", 1, "ext-1"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, ("2026-01-01T00:00:01+00:00", "b", 1, "ext-1"))
    conn.execute(ins, ("2026-01-01T00:00:02+00:00", "c", 2, "ext-1"))  # other source: fine
    conn.execute(ins, ("2026-01-01T00:00:03+00:00", "d", None, None))  # no external id: fine
    conn.execute(ins, ("2026-01-01T00:00:04+00:00", "e", None, None))
    conn.close()


def test_v20_rerun_is_noop_and_makes_no_second_backup(tmp_path: Path) -> None:
    db = tmp_path / "penpal.db"
    _build_v19_db(db)
    conn = sqlite3.connect(db)
    run_migrations(conn)
    backups = sorted(tmp_path.glob("penpal.db.*.bak"))
    versions = conn.execute("SELECT count(*) FROM _schema_version").fetchone()[0]
    count = conn.execute("SELECT count(*) FROM raw_events").fetchone()[0]

    run_migrations(conn)
    run_migrations(sqlite3.connect(db))

    assert sorted(tmp_path.glob("penpal.db.*.bak")) == backups
    assert conn.execute("SELECT count(*) FROM _schema_version").fetchone()[0] == versions
    assert conn.execute("SELECT count(*) FROM raw_events").fetchone()[0] == count
    conn.close()


def test_v20_is_idempotent_on_partially_applied_schema(tmp_path: Path) -> None:
    """A DB that already has some v20 objects (e.g. restored mid-way) still migrates."""
    db = tmp_path / "penpal.db"
    _build_v19_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE raw_events ADD COLUMN bucket TEXT")
    conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, "
                 "locator TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT, health TEXT, "
                 "created_at TEXT NOT NULL, UNIQUE(type, locator))")
    conn.commit()
    run_migrations(conn)
    assert _version(conn) == SCHEMA_VERSION
    conn.close()


def test_v20_backup_created_before_migrating_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "penpal.db"
    _build_v19_db(db)
    conn = sqlite3.connect(db)
    run_migrations(conn)
    conn.close()

    backups = list(tmp_path.glob("penpal.db.v19-*.bak"))
    assert len(backups) == 1
    b = sqlite3.connect(backups[0])
    assert _version(b) == 19  # pristine pre-migration copy
    assert b.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 2
    assert "sources" not in _tables(b)
    b.close()


def test_fresh_db_gets_no_backup(tmp_path: Path) -> None:
    db = tmp_path / "penpal.db"
    conn = sqlite3.connect(db)
    run_migrations(conn)
    conn.close()
    assert list(tmp_path.glob("*.bak")) == []


def test_v20_failure_rolls_back_to_v19_with_data_intact(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "penpal.db"
    _build_v19_db(db)

    def boom(_conn):
        raise RuntimeError("injected failure")

    # fails after all DDL + backfill have executed inside the transaction
    monkeypatch.setattr(migration_v20, "rebuild_events_fts", boom)
    conn = sqlite3.connect(db)
    with pytest.raises(RuntimeError, match="injected"):
        run_migrations(conn)
    conn.close()

    check = sqlite3.connect(db)
    assert _version(check) == 19
    cols = {r[1] for r in check.execute("PRAGMA table_info(raw_events)")}
    assert not ({"source_id", "external_id", "bucket", "kind"} & cols)
    assert not ({"sources", "sync_cursors", "evidence", "access_grants", "raw_events_fts"} & _tables(check))
    assert check.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 2
    check.close()

    # retry without the fault succeeds
    monkeypatch.undo()
    conn = sqlite3.connect(db)
    run_migrations(conn)
    assert _version(conn) == SCHEMA_VERSION
    conn.close()
