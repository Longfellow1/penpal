from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from penpal.sources.discoverers.sqlite import discover_sqlite_candidates


def _make_db(path: Path, table_names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for name in table_names:
            conn.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY)')
        conn.commit()
    finally:
        conn.close()


def test_discover_sqlite_candidates_confidence_and_hints(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        "penpal.sources.discoverers.sqlite.is_excluded_path",
        lambda _path: (False, ""),
    )

    high_db = home / "Library" / "Application Support" / "Cursor" / "state.vscdb"
    medium_db = home / "Library" / "Application Support" / "Warp" / "warp.sqlite"
    ignored_db = home / "Library" / "Application Support" / "Other" / "misc.db"
    fake_db = home / "Library" / "Application Support" / "Bad" / "not-sqlite.db"

    _make_db(high_db, ["messages", "chats", "events", "users"])
    _make_db(medium_db, ["commands", "metadata"])
    _make_db(ignored_db, ["settings", "flags"])
    fake_db.parent.mkdir(parents=True, exist_ok=True)
    fake_db.write_text("not sqlite", encoding="utf-8")

    candidates = discover_sqlite_candidates(exclude_paths=set())

    by_path = {candidate.path: candidate for candidate in candidates}
    assert str(high_db.resolve()) in by_path
    assert str(medium_db.resolve()) in by_path
    assert str(ignored_db.resolve()) not in by_path
    assert str(fake_db.resolve()) not in by_path

    high = by_path[str(high_db.resolve())]
    assert high.discoverer == "sqlite"
    assert high.shape == "tabular_rows"
    assert high.confidence == "high"
    assert set(high.hint_tables) == {"messages", "chats", "events"}
    assert high.hint_fields == []
    assert high.schema_signature == "chats,events,messages,users"

    medium = by_path[str(medium_db.resolve())]
    assert medium.shape == "tabular_rows"
    assert medium.confidence == "medium"
    assert medium.hint_tables == ["commands"]
    assert medium.hint_fields == []


def test_discover_sqlite_candidates_uses_column_heuristics(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        "penpal.sources.discoverers.sqlite.is_excluded_path",
        lambda _path: (False, ""),
    )

    db_path = home / "Library" / "Application Support" / "App" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE telemetry (id INTEGER PRIMARY KEY, role TEXT, message TEXT, path TEXT)")
        conn.commit()
    finally:
        conn.close()

    candidates = discover_sqlite_candidates(exclude_paths=set())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.confidence == "high"
    assert set(candidate.hint_fields) >= {"role", "message", "path"}


def test_discover_sqlite_candidates_detects_kv_json_blob(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        "penpal.sources.discoverers.sqlite.is_excluded_path",
        lambda _path: (False, ""),
    )

    db_path = home / "Library" / "Application Support" / "Cursor" / "state.vscdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value BLOB)")
        conn.commit()
    finally:
        conn.close()

    candidates = discover_sqlite_candidates(exclude_paths=set())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.shape == "kv_json_blob"
    assert candidate.confidence == "high"
    assert candidate.hint_tables == ["cursorDiskKV"]


def test_discover_sqlite_candidates_respects_excluded_paths(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    db_path = home / "Library" / "Application Support" / "App" / "history.db"
    _make_db(db_path, ["history", "metadata"])

    candidates = discover_sqlite_candidates(exclude_paths={str(db_path.resolve())})

    assert candidates == []


def test_discover_sqlite_candidates_honors_depth_limit(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    deep_db = (
        home
        / "Library"
        / "Application Support"
        / "a"
        / "b"
        / "c"
        / "d"
        / "e"
        / "f"
        / "too-deep.db"
    )
    _make_db(deep_db, ["history"])

    candidates = discover_sqlite_candidates(exclude_paths=set())

    assert candidates == []


def test_schema_probes_open_third_party_databases_read_only(tmp_path: Path) -> None:
    from penpal.sources.discoverers import sqlite as discoverer

    db = tmp_path / "app.sqlite"
    _make_db(db, ["messages"])
    before = sorted(p.name for p in tmp_path.iterdir()), db.stat().st_mtime_ns, db.read_bytes()

    assert discoverer._read_table_names(db) == ["messages"]
    assert discoverer._read_table_columns(db, ["messages"]) == {"id"}
    assert discoverer._read_table_defs(db, ["messages"])

    after = sorted(p.name for p in tmp_path.iterdir()), db.stat().st_mtime_ns, db.read_bytes()
    assert after == before, "probing must not write, journal or touch the third-party file"

    conn = discoverer._connect_read_only(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE intruder(x)")
    finally:
        conn.close()

    missing = tmp_path / "missing.sqlite"
    assert discoverer._read_table_names(missing) == []
    assert not missing.exists(), "a read-only probe must never create a database"


def test_read_only_probe_falls_back_when_the_plain_open_fails(tmp_path: Path, monkeypatch) -> None:
    from penpal.sources.discoverers import sqlite as discoverer

    db = tmp_path / "app.sqlite"
    _make_db(db, ["messages"])
    real_connect = sqlite3.connect
    seen = []

    def connect(target, *args, **kwargs):
        seen.append(target)
        if "immutable=1" in target:
            return real_connect(target, *args, **kwargs)
        # Simulate the plain read-only open failing (as it does when the -shm cannot be created).
        return real_connect(f"{(tmp_path / 'missing.sqlite').resolve().as_uri()}?mode=ro", *args, **kwargs)

    monkeypatch.setattr(discoverer.sqlite3, "connect", connect)
    assert discoverer._read_table_names(db) == ["messages"]
    assert any("immutable=1" in uri for uri in seen)
