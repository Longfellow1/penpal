from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from penpal.app import _reload_in_place
from penpal.sources.scheduler import SourcesScheduler
from penpal.sources.sink import (
    flush_named_entity_backfill,
    persist_semantic_event,
)
from penpal.sources.types import SemanticEvent
from penpal.store.db import close, get_conn, init_db


def _make_event(
    intent: str,
    *,
    evidence_locator: str | None = None,
    source: str = "claude_code",
    metadata: dict[str, Any] | None = None,
) -> SemanticEvent:
    return SemanticEvent(
        time=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        source=source,
        actor="user",
        intent=intent,
        artifact="claude:session:test-session",
        raw_ref="claude:ref:1",
        privacy_tier="green",
        metadata=metadata or {"session_id": "test-session"},
        evidence_locator=evidence_locator,
        evidence_line=42 if evidence_locator else None,
    )


def test_import_atomic_failure_injected_in_evidence_rolls_back_event_and_fts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "penpal.db"
    close()
    init_db(db_path)

    monkeypatch.setattr("penpal.sources.sink.extract_named_entities", lambda event: [])

    def _fail_add_evidence(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected evidence write failure")

    monkeypatch.setattr("penpal.sources.sink.add_evidence", _fail_add_evidence)

    intent_text = "atomic_rollback_canary_phrase"
    event = _make_event(intent_text, evidence_locator="/tmp/test_source_file.jsonl")

    with pytest.raises(RuntimeError, match="injected evidence write failure"):
        persist_semantic_event(event)

    conn = get_conn()
    raw_count = conn.execute(
        "SELECT COUNT(*) FROM raw_events WHERE content_text LIKE ?", (f"%{intent_text}%",)
    ).fetchone()[0]
    assert raw_count == 0

    fts_count = conn.execute(
        "SELECT COUNT(*) FROM raw_events_fts WHERE raw_events_fts MATCH ?", (f'"{intent_text}"',)
    ).fetchone()[0]
    assert fts_count == 0

    evidence_count = conn.execute(
        "SELECT COUNT(*) FROM evidence WHERE locator = ?", ("/tmp/test_source_file.jsonl",)
    ).fetchone()[0]
    assert evidence_count == 0

    close()


def test_import_atomic_success_persists_both_event_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "penpal.db"
    close()
    init_db(db_path)

    monkeypatch.setattr("penpal.sources.sink.extract_named_entities", lambda event: [])

    intent_text = "atomic_success_canary_phrase"
    evidence_path = "/tmp/test_source_file_success.jsonl"
    event = _make_event(intent_text, evidence_locator=evidence_path)

    row_id = persist_semantic_event(event, async_named_entities=False)
    assert row_id > 0

    conn = get_conn()
    row = conn.execute("SELECT id, content_text FROM raw_events WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    assert row["content_text"] == intent_text

    evidence_row = conn.execute(
        "SELECT event_id, locator, offset_or_line FROM evidence WHERE event_id = ?", (row_id,)
    ).fetchone()
    assert evidence_row is not None
    assert evidence_row["locator"] == evidence_path
    assert evidence_row["offset_or_line"] == 42

    fts_count = conn.execute(
        "SELECT COUNT(*) FROM raw_events_fts WHERE raw_events_fts MATCH ?", (f'"{intent_text}"',)
    ).fetchone()[0]
    assert fts_count == 1

    close()


def test_pending_backfill_is_flushed_by_stop_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "penpal.db"
    close()
    init_db(db_path)

    monkeypatch.setattr(
        "penpal.sources.sink.extract_named_entities",
        lambda event: ["atomic_entity", "flush_test"],
    )

    event = _make_event("backfill flush test event")
    row_id = persist_semantic_event(event, async_named_entities=True)
    assert row_id > 0

    scheduler = SourcesScheduler(enabled=False)
    scheduler.stop()

    conn = get_conn()
    row = conn.execute("SELECT metadata_json FROM raw_events WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    payload = json.loads(row["metadata_json"] or "{}")
    named_entities = payload.get("entities", {}).get("named_entities", [])
    assert named_entities == ["atomic_entity", "flush_test"]

    close()


def test_hung_backfill_does_not_block_stop_beyond_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "penpal.db"
    close()
    init_db(db_path)

    hang_event = threading.Event()

    def _hung_backfill(row_id: int, event: SemanticEvent) -> None:
        hang_event.wait(timeout=5.0)

    monkeypatch.setattr("penpal.sources.sink._backfill_named_entities", _hung_backfill)

    event = _make_event("hung backfill test event")
    row_id = persist_semantic_event(event, async_named_entities=True)
    assert row_id > 0

    scheduler = SourcesScheduler(enabled=False)
    try:
        t0 = time.monotonic()
        scheduler.stop(timeout_sec=0.15)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
    finally:
        hang_event.set()
        flush_named_entity_backfill(timeout_sec=1.0)
        close()


def test_reload_path_calls_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "penpal.db"
    close()
    init_db(db_path)

    monkeypatch.setattr(
        "penpal.sources.sink.extract_named_entities",
        lambda event: ["reload_entity"],
    )

    event = _make_event("reload flush test event")
    row_id = persist_semantic_event(event, async_named_entities=True)
    assert row_id > 0

    exec_called = False

    def _fake_execv(executable: str, args: list[str]) -> None:
        nonlocal exec_called
        exec_called = True
        raise SystemExit(0)

    monkeypatch.setattr("os.execv", _fake_execv)

    with pytest.raises(SystemExit):
        _reload_in_place()

    assert exec_called

    conn = get_conn()
    row = conn.execute("SELECT metadata_json FROM raw_events WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    payload = json.loads(row["metadata_json"] or "{}")
    named_entities = payload.get("entities", {}).get("named_entities", [])
    assert named_entities == ["reload_entity"]

    close()


def test_pooled_thread_reconnects_after_db_switch(tmp_path: Path) -> None:
    close()
    init_db(tmp_path / "first.db")
    seen: list[str] = []

    def worker_path() -> None:
        rows = get_conn().execute("PRAGMA database_list").fetchall()
        seen.append(Path(next(r["file"] for r in rows if r["name"] == "main")).name)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker_path).result()
        init_db(tmp_path / "second.db")
        pool.submit(worker_path).result()
    assert seen == ["first.db", "second.db"]
    close()


def test_stop_waits_for_running_scan_within_timeout() -> None:
    from concurrent.futures import ThreadPoolExecutor

    finished = threading.Event()

    def slow_scan() -> None:
        time.sleep(0.2)
        finished.set()

    scheduler = SourcesScheduler(enabled=False)
    scheduler._executor = ThreadPoolExecutor(max_workers=1)
    scheduler._running.set()
    future = scheduler._executor.submit(slow_scan)
    scheduler._in_flight_tasks[future] = "synthetic"
    scheduler.stop(timeout_sec=5.0)
    assert finished.is_set()


def test_scan_cancelled_by_stop_releases_its_claim() -> None:
    from concurrent.futures import ThreadPoolExecutor

    gate = threading.Event()
    scheduler = SourcesScheduler(enabled=False)
    scheduler._executor = ThreadPoolExecutor(max_workers=1)
    scheduler._running.set()
    blocker = scheduler._executor.submit(gate.wait, 2.0)
    scheduler._in_flight_tasks[blocker] = "busy"
    with scheduler._lock:
        scheduler._in_flight.add("queued")
    queued = scheduler._executor.submit(lambda: None)
    scheduler._in_flight_tasks[queued] = "queued"
    queued.add_done_callback(scheduler._on_scan_done)
    try:
        scheduler.stop(timeout_sec=0.1)
    finally:
        gate.set()
    assert queued.cancelled()
    assert "queued" not in scheduler._in_flight
