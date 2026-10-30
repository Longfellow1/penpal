
"""Test migration v12: semantic_weight column and per-source defaults."""
import sqlite3
import tempfile
from pathlib import Path

from penpal.store.db import init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.capture.normalizer import _semantic_weight_for


def test_semantic_weight_column_exists_after_migration():
    """Test that v12 creates the semantic_weight column and index."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        conn = sqlite3.connect(str(db_path))
        # Reset module-level DB connection after init_db
        from penpal.store import db
        db._db_conn = None
        conn.row_factory = sqlite3.Row

        # Check column exists
        info = conn.execute("PRAGMA table_info(raw_events)").fetchall()
        col_names = [row["name"] for row in info]
        assert "semantic_weight" in col_names, "semantic_weight column not found"

        # Check column type and default
        semantic_weight_col = next(r for r in info if r["name"] == "semantic_weight")
        assert semantic_weight_col["type"] == "REAL", f"Expected REAL, got {semantic_weight_col['type']}"

        # Check index exists
        indexes = conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_raw_events_weight'").fetchall()
        assert len(indexes) > 0, "Index idx_raw_events_weight not found"

        conn.close()


def test_backfill_weights_per_source():
    """Test that pre-v12 rows with known sources get correct weights on migration."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"

        # Create old schema (v11) manually
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        # Bootstrap schema tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _schema_version (
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL
            )
        """)
        conn.commit()

        # Insert v1-v11 migrations manually to set schema version to 11
        from penpal.store.migrations import MIGRATIONS
        from datetime import datetime, timezone
        for i, sql in enumerate(MIGRATIONS[:11], start=1):
            conn.executescript(sql.strip())
            conn.execute(
                "INSERT INTO _schema_version(version, applied_at) VALUES (?, ?)",
                (i, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()

        # Insert test events with various sources (before v12 migration)
        test_data = [
            ("ax_text", 0.8),
            ("clipboard", 0.9),
            ("manual", 1.0),
            ("browser", 0.85),
            ("ax_text", 0.8),
            ("ax_ime_commit", 0.9),
            ("ax_snapshot_fallback", 0.5),
            ("ocr_text", 0.4),
            ("window_focus_session", 0.2),
            ("idle", 0.5),  # unknown source -> default
        ]

        for source, _expected_weight in test_data:
            conn.execute(
                """INSERT INTO raw_events
                   (source, event_type, ts_start, speaker, created_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (source, "test_event", "2026-01-01T00:00:00Z", "system")
            )
        conn.commit()

        # Now apply v12 migration

        # Manually apply v12
        v12_sql = MIGRATIONS[11]
        conn.executescript(v12_sql.strip())
        conn.execute(
            "INSERT INTO _schema_version(version, applied_at) VALUES (?, ?)",
            (12, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()

        # Verify weights
        for source, expected_weight in test_data:
            row = conn.execute(
                "SELECT semantic_weight FROM raw_events WHERE source = ? LIMIT 1",
                (source,)
            ).fetchone()
            assert row is not None, f"Row with source={source} not found"
            actual_weight = row[0]
            assert abs(actual_weight - expected_weight) < 0.001, \
                f"Source {source}: expected {expected_weight}, got {actual_weight}"

        conn.close()


def test_semantic_weight_helper():
    """Test the _semantic_weight_for() helper function."""
    test_cases = [
        ("ax_text", 0.8),
        ("clipboard", 0.9),
        ("manual", 1.0),
        ("browser", 0.85),
        ("ax_text", 0.8),
        ("ocr_text", 0.4),
        ("window_focus_session", 0.2),
        ("unknown_source", 0.5),
        ("ax_ime_commit", 0.5),  # no producer any more: falls to the default
        ("window", 0.5),
        ("idle", 0.5),
    ]

    for source, expected_weight in test_cases:
        actual_weight = _semantic_weight_for(source)
        assert abs(actual_weight - expected_weight) < 0.001, \
            f"_semantic_weight_for('{source}'): expected {expected_weight}, got {actual_weight}"


def test_new_raw_event_writes_include_semantic_weight():
    """Test that newly inserted raw_events have semantic_weight set."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        # Reset module-level DB connection after init_db
        from penpal.store import db
        db._db_conn = None

        # Insert a raw event via insert_raw_event
        event = RawEvent(
            source="ax_text",
            event_type="ax_text_capture",
            ts_start="2026-01-01T00:00:00Z",
            content_text="hello",
            semantic_weight=_semantic_weight_for("ax_text"),
        )
        event_id = insert_raw_event(event)

        # Verify semantic_weight was persisted
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT semantic_weight FROM raw_events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None
        assert abs(row["semantic_weight"] - 0.8) < 0.001

        conn.close()
