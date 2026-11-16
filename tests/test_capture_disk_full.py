"""Disk full during capture: writes stop (no crash loop, no busy retry), health says error/disk_full,
and capture resumes once a write succeeds again. Synthetic events only."""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

import penpal.capture.manager_events as event_module
from penpal.capabilities.builtin.capture_storage import DISK_FULL_CODE, CaptureStorageCapability
from penpal.capture.manager import CaptureManager
from penpal.capture.normalizer import normalize_clipboard_event
from penpal.config import Config
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal import __version__
from penpal.foundation.runtime_identity import code_fingerprint
from penpal.foundation.runtime_launchd import process_start_time
from penpal.foundation.runtime_record import Progress, RuntimeRecord, read_record

FULL = sqlite3.OperationalError("database or disk is full")


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    manager = CaptureManager(Config())
    started = datetime.now(timezone.utc).isoformat()
    manager._runtime_progress["core_loop"] = Progress(1, started)
    manager._runtime_record = RuntimeRecord(
        generation=str(uuid.uuid4()), pid=os.getpid(),
        process_start_time=process_start_time(os.getpid()) or "unknown",
        version=__version__, code_fingerprint=code_fingerprint(),
        started_at=started, written_at=started, progress=dict(manager._runtime_progress),
    )
    manager._write_runtime_record(force=True)
    yield manager
    close()
    buckets.reset_cache()


_TEXTS = [
    "kubectl get pods in staging",
    "terraform plan review notes",
    "quarterly roadmap draft outline",
    "design review checklist items",
    "release notes for the next build",
]


def _event(i: int):
    return normalize_clipboard_event(
        _TEXTS[i - 1],
        app_name="Notes",
        ts_start=f"2026-03-02T10:00:{i:02d}+00:00",
    )


def _row_count() -> int:
    return get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]


def _runtime_storage():
    record = read_record()
    assert record is not None
    return record.components["storage"]


def test_disk_full_stops_writes_and_reports_error(mgr, monkeypatch) -> None:
    real_insert = event_module.insert_raw_event
    attempts: list[int] = []
    full = {"on": True}

    def fake_insert(event):
        attempts.append(1)
        if full["on"]:
            raise FULL
        return real_insert(event)

    monkeypatch.setattr(event_module, "insert_raw_event", fake_insert)

    for i in range(1, 6):
        mgr._process_event(_event(i))  # must not raise, must not loop

    assert len(attempts) == 1, "one write attempt per probe interval, not one per event"
    assert _row_count() == 0
    mgr.report_progress("core_loop")
    mgr._write_runtime_record(force=True)
    storage = _runtime_storage()
    assert storage.state == "storage_error"
    assert storage.detail == "disk_full"

    from penpal.capabilities.builtin import capture_storage
    from penpal.runtime import RuntimeState, RuntimeStatus
    monkeypatch.setattr(capture_storage, "evaluate", lambda: RuntimeStatus(RuntimeState.RUNNING, components={"storage": storage}))

    cap = CaptureStorageCapability()
    state = cap.monitor()
    assert not state.ok and state.code == DISK_FULL_CODE
    signal = cap.diagnose(state)
    assert signal.level == "err"
    assert any(word in (signal.hint or "") for word in ("磁盘已满", "Disk is full"))  # hint follows the UI language
    assert "kubectl" not in storage.detail


def test_capture_resumes_after_space_is_back(mgr, monkeypatch) -> None:
    real_insert = event_module.insert_raw_event
    full = {"on": True}

    def fake_insert(event):
        if full["on"]:
            raise FULL
        return real_insert(event)

    monkeypatch.setattr(event_module, "insert_raw_event", fake_insert)
    mgr.DISK_FULL_PROBE_SEC = 0.0  # every event is a probe for this test

    mgr._process_event(_event(1))
    assert mgr._storage_error == "disk_full"
    mgr._write_runtime_record(force=True)
    mgr._process_event(_event(2))  # probe still fails: stays in error, no crash
    mgr.report_progress("core_loop")
    mgr._write_runtime_record(force=True)
    assert _runtime_storage().state == "storage_error"

    full["on"] = False
    mgr._process_event(_event(3))  # probe succeeds
    assert _row_count() == 1
    assert mgr._storage_error is None
    mgr.report_progress("core_loop")
    mgr._write_runtime_record(force=True)
    assert _runtime_storage().state == "running"
    recovered = CaptureStorageCapability().monitor()
    assert not recovered.ok
    assert "2" in recovered.detail


def test_locked_capture_gate_retries_without_losing_event(mgr, monkeypatch):
    real_gate = event_module.source_writes_allowed
    attempts = []

    def locked_then_gate(source):
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_gate(source)

    monkeypatch.setattr(event_module, "source_writes_allowed", locked_then_gate)
    monkeypatch.setattr(event_module.time, "sleep", lambda _: None)
    mgr._queue.put(_event(1))
    mgr._drain_queue()
    assert len(attempts) == 2
    assert _row_count() == 1
    assert mgr._storage_dropped == 0


def test_locked_write_recovers_without_losing_event(mgr, monkeypatch) -> None:
    real_insert = event_module.insert_raw_event
    attempts = []

    def locked_then_write(event):
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_insert(event)

    monkeypatch.setattr(event_module, "insert_raw_event", locked_then_write)
    monkeypatch.setattr(event_module.time, "sleep", lambda _: None)
    mgr._queue.put(_event(1))
    mgr._drain_queue()
    assert len(attempts) == 3
    assert _row_count() == 1
    assert mgr._storage_error is None
    assert mgr._storage_dropped == 0


def test_locked_retry_exhausted_counts_drops(mgr, monkeypatch) -> None:
    def locked(event):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(event_module, "insert_raw_event", locked)
    monkeypatch.setattr(event_module.time, "sleep", lambda _: None)
    mgr._queue.put(_event(1))
    mgr._queue.put(_event(2))
    mgr._drain_queue()
    assert _row_count() == 0
    assert mgr._storage_error == "db_locked"
    assert mgr._storage_dropped == 2
    mgr._write_runtime_record(force=True)
    record = read_record()
    assert record.components["storage"].state == "storage_error"
    assert record.components["storage_dropped_events"].detail == "2"


def test_non_lock_write_error_is_counted_without_leaking_message(mgr, monkeypatch, caplog):
    def broken(event):
        raise sqlite3.OperationalError("synthetic sensitive text")

    monkeypatch.setattr(event_module, "insert_raw_event", broken)
    mgr._queue.put(_event(1))
    mgr._drain_queue()
    assert mgr._storage_error == "write_failed"
    assert mgr._storage_dropped == 1
    assert "synthetic sensitive text" not in caplog.text


def test_index_failure_does_not_count_saved_raw_event_as_lost(mgr, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setattr(event_module, "insert_search_doc", broken)
    mgr._queue.put(_event(1))
    mgr._drain_queue()
    assert _row_count() == 1
    assert mgr._storage_error == "write_failed"
    assert mgr._storage_dropped == 0


def test_runtime_record_stays_writable_when_database_reads_fail(mgr, monkeypatch):
    from penpal.capture import manager_runtime

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(manager_runtime, "is_capture_paused", locked)
    monkeypatch.setattr(manager_runtime, "get_state", locked)
    mgr._storage_error = "db_locked"
    mgr._storage_dropped = 3
    mgr._write_runtime_record(force=True)
    assert read_record().components["storage_dropped_events"].detail == "3"


def test_locked_control_read_keeps_queue_for_next_loop(mgr, monkeypatch):
    from penpal.capture import manager_lifecycle

    def locked():
        raise sqlite3.OperationalError("database is locked")

    mgr._queue.put(_event(1))
    mgr._running.set()
    monkeypatch.setattr(mgr, "_sync_control_state", locked)
    monkeypatch.setattr(manager_lifecycle.time, "sleep", lambda _: mgr._running.clear())
    before = mgr._runtime_progress["core_loop"].seq
    mgr._flush_loop()
    assert mgr._queue.qsize() == 1
    assert mgr._runtime_progress["core_loop"].seq == before
    assert mgr._storage_error == "db_locked"
    assert mgr._storage_dropped == 0


def test_window_session_processing_failure_is_counted(mgr, monkeypatch):
    class Watcher:
        def flush_current_session(self, **kwargs):
            return _event(1)

    def fail(_event):
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setitem(mgr._watchers, "window", Watcher())
    monkeypatch.setattr(mgr, "_process_event", fail)
    monkeypatch.setattr(mgr, "_persist_storage_state", lambda: None)
    mgr._flush_window_session(reason="watcher_stop")
    assert mgr._storage_error == "write_failed"
    assert mgr._storage_dropped == 1


def test_state_write_failure_during_disk_full_does_not_raise(mgr, monkeypatch) -> None:
    def full_write(*_args, **_kwargs):
        raise FULL

    def full_insert(event):
        raise FULL

    monkeypatch.setattr(event_module, "set_state", full_write)
    monkeypatch.setattr(event_module, "insert_raw_event", full_insert)

    mgr._process_event(_event(1))  # insert fails, state write fails too: still no exception
    assert mgr._storage_error == "disk_full"
