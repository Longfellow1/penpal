"""Migration v31: evidence for old codex_cli history rows (codex:history:<N>), via Codex's history.jsonl.
Synthetic fixtures only: a tmp HOME holding a fake ~/.codex/history.jsonl (Path.home monkeypatched)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from penpal.sources.plugins.codex_cli import CodexCliSource
from penpal.sources.sink import persist_semantic_event
from penpal.sources.types import DataSourceInstance
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v31 import migrate_v31
from penpal.store.migrations import MIGRATIONS, SCHEMA_VERSION, run_migrations
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event

_LINES = [
    {"session_id": "s1", "ts": 1767225600, "text": "first prompt"},
    {"session_id": "s1", "ts": 1767225660, "text": "second prompt"},
    {"session_id": "s2", "ts": 1767225720, "text": "third prompt"},
]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """tmp HOME with ~/.codex/history.jsonl; Path.home() points there, so the plugin path logic follows it."""
    root = tmp_path / "home"
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: root)
    (root / ".codex").mkdir(parents=True)
    init_db(tmp_path / "t.db")
    yield root
    close()


def _write_history(home: Path) -> Path:
    path = home / ".codex" / "history.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in _LINES), encoding="utf-8")
    return path


def _old_row(raw_ref: str, text: str, source_id: int | None = None) -> int:
    """A codex_cli row as ingested before the locator fix: raw_ref only, no sources locator."""
    n = get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    return insert_raw_event(RawEvent(
        source="codex_cli", event_type="semantic_event", ts_start="2026-01-01T00:00:00+00:00",
        content_text=text, content_hash=f"v31-{n}-{raw_ref}", source_id=source_id,
        metadata_json=json.dumps({"session_id": "s1", "raw_ref": raw_ref}), bucket="Work",
    ))


def _evidence_of(event_id: int):
    rows = get_conn().execute(
        "SELECT locator, offset_or_line, content_hash, available FROM evidence WHERE event_id = ?", (event_id,)
    ).fetchall()
    return tuple(rows[0]) if rows else None


def test_v31_backfills_history_rows_with_resolved_path_and_line(home):
    history = _write_history(home)
    rid_2 = _old_row("codex:history:2", _LINES[1]["text"])
    rid_3 = _old_row("codex:history:3", _LINES[2]["text"])

    stats = migrate_v31(get_conn())
    get_conn().commit()

    assert _evidence_of(rid_2) == (
        str(history.resolve()), 2, hashlib.sha256(_LINES[1]["text"].encode()).hexdigest(), 1,
    )
    assert _evidence_of(rid_3)[1] == 3
    assert stats == {"codex_cli": (2, 0)}


def test_v31_writes_nothing_when_history_file_is_missing(home):
    rid = _old_row("codex:history:1", "first prompt")

    stats = migrate_v31(get_conn())
    get_conn().commit()

    assert _evidence_of(rid) is None
    assert stats == {}


def test_v31_leaves_sessions_rows_and_rows_with_a_locator_alone(home):
    _write_history(home)
    sessions_rid = _old_row("codex:sessions:2026/01/01/rollout.jsonl:4", "rollout prompt")
    located = get_conn().execute(
        "INSERT INTO sources(type, locator, created_at) VALUES ('codex_cli', ?, '2026-01-01T00:00:00+00:00')",
        (str(home / "elsewhere" / "history.jsonl"),),
    ).lastrowid
    located_rid = _old_row("codex:history:1", "first prompt", source_id=located)

    migrate_v31(get_conn())
    get_conn().commit()

    assert _evidence_of(sessions_rid) is None
    assert _evidence_of(located_rid) is None


def test_v31_skips_malformed_line_numbers(home):
    _write_history(home)
    bad = [_old_row(ref, "x") for ref in ("codex:history:abc", "codex:history:0", "codex:history:", "codex:history:-1")]
    good = _old_row("codex:history:1", "first prompt")

    stats = migrate_v31(get_conn())
    get_conn().commit()

    assert all(_evidence_of(rid) is None for rid in bad)
    assert _evidence_of(good)[1] == 1
    assert stats == {"codex_cli": (1, 4)}


def test_v31_rerun_is_a_noop(home):
    _write_history(home)
    rid = _old_row("codex:history:2", _LINES[1]["text"])
    migrate_v31(get_conn())
    get_conn().commit()
    first = get_conn().execute("SELECT id, event_id, locator FROM evidence ORDER BY id").fetchall()

    stats = migrate_v31(get_conn())
    get_conn().commit()

    assert get_conn().execute("SELECT id, event_id, locator FROM evidence ORDER BY id").fetchall() == first
    assert stats == {}  # no candidate left: the history file is not even checked for rows
    assert _evidence_of(rid) is not None


def test_new_history_ingest_already_writes_the_same_locator(home):
    """Step 2 check: rows ingested now carry a sources locator and evidence at the same path and line."""
    history = _write_history(home)
    source = CodexCliSource(history_path=history)
    instance = DataSourceInstance(plugin="codex_cli", locator=str(history.resolve()), label="codex history")
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    until = datetime(2100, 1, 1, tzinfo=timezone.utc)

    events = list(source.read(instance, since, until))
    rids = [persist_semantic_event(event, async_named_entities=False) for event in events]

    assert len(rids) == len(_LINES)
    assert _evidence_of(rids[1]) == (
        str(history.resolve()), 2, hashlib.sha256(_LINES[1]["text"].encode()).hexdigest(), 1,
    )


def test_v31_is_registered_as_latest_schema_version(home):
    conn = get_conn()
    assert SCHEMA_VERSION == len(MIGRATIONS)
    conn.execute("DELETE FROM _schema_version WHERE version >= 31")  # re-run v31 and v32 on top
    conn.commit()
    run_migrations(conn)
    run_migrations(conn)
    assert conn.execute("SELECT COUNT(*) FROM _schema_version WHERE version = 31").fetchone()[0] == 1
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == SCHEMA_VERSION
