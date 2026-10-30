from __future__ import annotations


from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event


def _row_count() -> int:
    return get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]


def _make_event(
    source: str = "manual",
    ts_start: str = "2026-04-30T01:23:45+00:00",
    content_hash: str | None = "abc123",
    content_text: str | None = "hello",
) -> RawEvent:
    return RawEvent(
        source=source,
        event_type="manual_save",
        ts_start=ts_start,
        ts_end=ts_start,
        content_text=content_text,
        content_hash=content_hash,
        created_at=ts_start,
    )


def test_insert_raw_event_idempotent_returns_same_id(tmp_path) -> None:
    close()
    init_db(tmp_path / "penpal.db")
    try:
        event = _make_event()

        first = insert_raw_event(event)
        second = insert_raw_event(event)

        assert first > 0
        assert first == second
        assert _row_count() == 1
    finally:
        close()


def test_insert_raw_event_different_content_hash_inserted(tmp_path) -> None:
    """Same (source, ts_start) but different content_hash → both kept."""
    close()
    init_db(tmp_path / "penpal.db")
    try:
        first = insert_raw_event(_make_event(content_hash="hash-a", content_text="a"))
        second = insert_raw_event(_make_event(content_hash="hash-b", content_text="b"))

        assert first != second
        assert _row_count() == 2
    finally:
        close()


def test_insert_raw_event_null_content_hash_dedupes(tmp_path) -> None:
    """Two events with content_hash=NULL and same (source, ts_start) collide."""
    close()
    init_db(tmp_path / "penpal.db")
    try:
        ev = _make_event(content_hash=None, content_text=None)

        first = insert_raw_event(ev)
        second = insert_raw_event(ev)

        assert first == second
        assert _row_count() == 1
    finally:
        close()


def test_insert_raw_event_different_ts_start_inserted(tmp_path) -> None:
    close()
    init_db(tmp_path / "penpal.db")
    try:
        first = insert_raw_event(_make_event(ts_start="2026-04-30T01:00:00+00:00"))
        second = insert_raw_event(_make_event(ts_start="2026-04-30T02:00:00+00:00"))

        assert first != second
        assert _row_count() == 2
    finally:
        close()


def test_migration_dedupes_existing_rows(tmp_path, monkeypatch) -> None:
    """Pre-v17 DBs may already contain duplicates; the migration must
    drop them before adding the unique index, otherwise the CREATE
    UNIQUE INDEX statement fails.

    Strategy: pretend v17 is the latest version (so MAX(version) check
    in run_migrations honors a deletion of v17), bring the DB up via
    normal migration path, seed duplicates, then re-init to apply v17.
    """
    import penpal.store.migrations as _m
    monkeypatch.setattr(_m, "MIGRATIONS", _m.MIGRATIONS[:17])
    monkeypatch.setattr(_m, "SCHEMA_VERSION", 17)

    db_path = tmp_path / "legacy.db"

    close()
    # First init: brings DB to current SCHEMA_VERSION; we then rewind it
    # to 16 to simulate a pre-v17 install.
    init_db(db_path)
    conn = get_conn()
    conn.execute("DELETE FROM _schema_version WHERE version = 17")
    conn.execute("DROP INDEX IF EXISTS idx_raw_events_dedup")
    conn.executemany(
        "INSERT INTO raw_events(source, event_type, ts_start, content_hash, created_at) VALUES (?,?,?,?,?)",
        [
            ("manual", "manual_save", "2026-04-30T01:00:00+00:00", "h", "c1"),
            ("manual", "manual_save", "2026-04-30T01:00:00+00:00", "h", "c2"),  # duplicate
            ("manual", "manual_save", "2026-04-30T01:00:00+00:00", "h", "c3"),  # duplicate
            ("manual", "manual_save", "2026-04-30T02:00:00+00:00", None, "c4"),
            ("manual", "manual_save", "2026-04-30T02:00:00+00:00", None, "c5"),  # NULL → ''
        ],
    )
    conn.commit()
    pre_count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    assert pre_count == 5
    close()

    # Second init: the migration runner sees current=16 and applies v17.
    init_db(db_path)
    try:
        rows = get_conn().execute(
            "SELECT ts_start, content_hash FROM raw_events ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["ts_start"] == "2026-04-30T01:00:00+00:00"
        assert rows[1]["ts_start"] == "2026-04-30T02:00:00+00:00"
    finally:
        close()
