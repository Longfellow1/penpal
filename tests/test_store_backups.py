"""Pre-migration backup snapshots: creation, listing and pruning (synthetic databases only)."""
from __future__ import annotations

import sqlite3
import stat

import pytest

from penpal.store.backups import list_backups, prune_backups, write_pre_migration_backup


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "penpal.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE notes (value TEXT)")
    conn.execute("INSERT INTO notes VALUES ('synthetic note')")
    conn.commit()
    yield conn, path
    conn.close()


def test_write_backup_creates_owner_only_snapshot(db):
    conn, path = db

    backup = write_pre_migration_backup(conn, 19)

    assert backup is not None
    assert backup.parent == path.parent
    assert backup.name.startswith("penpal.db.v19-") and backup.name.endswith(".bak")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    copy = sqlite3.connect(backup)
    try:
        assert copy.execute("SELECT value FROM notes").fetchone() == ("synthetic note",)
    finally:
        copy.close()


def test_write_backup_skips_empty_and_in_memory_databases(tmp_path):
    empty_path = tmp_path / "empty.db"
    empty = sqlite3.connect(empty_path)
    memory = sqlite3.connect(":memory:")
    memory.execute("CREATE TABLE notes (value TEXT)")
    try:
        assert write_pre_migration_backup(empty, 19) is None
        assert write_pre_migration_backup(memory, 19) is None
        assert list(tmp_path.glob("*.bak")) == []
    finally:
        empty.close()
        memory.close()


def test_list_and_prune_keep_only_newest_matching_backups(tmp_path):
    db_path = tmp_path / "penpal.db"
    db_path.write_bytes(b"synthetic database")
    older = tmp_path / "penpal.db.v18-20260301T000000000001Z.bak"
    newer = tmp_path / "penpal.db.v19-20260302T000000000002Z.bak"
    decoys = [
        tmp_path / "penpal.db.v19-yesterday.bak",
        tmp_path / "other.db.v20-20260303T000000000003Z.bak",
        tmp_path / "penpal.db.keep",
        tmp_path / "config.toml.bak-pre-penpal",
    ]
    for path in (older, newer, *decoys):
        path.write_bytes(b"synthetic backup")

    assert list_backups(db_path) == [newer, older]
    assert prune_backups(db_path, keep=1) == 1
    assert not older.exists() and newer.exists()
    assert all(path.exists() for path in decoys)

    assert prune_backups(db_path, keep=0) == 1
    assert not newer.exists()
    assert all(path.exists() for path in decoys)


def test_list_backups_returns_empty_for_missing_parent_and_negative_keep_is_refused(tmp_path):
    assert list_backups(tmp_path / "missing" / "penpal.db") == []
    with pytest.raises(ValueError):
        prune_backups(tmp_path / "penpal.db", keep=-1)
