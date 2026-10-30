from __future__ import annotations

import queue
import time

from penpal.capture.base import BaseWatcher


class _SilentWatcher(BaseWatcher):
    """Test fixture: starts cleanly and runs forever without emitting.
    Used to simulate "silent stuck" (e.g. macOS revoked permission)."""

    name = "silent"
    HEARTBEAT_TIMEOUT_SEC = 0.2  # tight for fast tests
    MAX_HEARTBEAT_REVIVALS = 2

    def _run(self) -> None:
        # Sit and do nothing until stopped — never call emit().
        while self._running.is_set():
            time.sleep(0.02)


class _ChattyWatcher(BaseWatcher):
    """Test fixture: emits regularly so heartbeat should never trip."""

    name = "chatty"
    HEARTBEAT_TIMEOUT_SEC = 0.2

    def _run(self) -> None:
        while self._running.is_set():
            self.emit(object())  # type: ignore[arg-type]
            time.sleep(0.02)


class _NoHeartbeatWatcher(BaseWatcher):
    """Test fixture: heartbeat disabled (the default for clipboard/idle)."""

    name = "no_hb"
    HEARTBEAT_TIMEOUT_SEC = None

    def _run(self) -> None:
        while self._running.is_set():
            time.sleep(0.02)


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_silent_watcher_marked_dead_after_timeout() -> None:
    q: queue.Queue = queue.Queue()
    w = _SilentWatcher(q)
    w.start()
    try:
        assert not w.is_heartbeat_dead()  # within grace
        assert _wait(lambda: w.is_heartbeat_dead(), timeout=1.0)
    finally:
        w.stop()


def test_chatty_watcher_never_dead() -> None:
    q: queue.Queue = queue.Queue()
    w = _ChattyWatcher(q)
    w.start()
    try:
        # Sleep past two timeout windows; emit() should keep refreshing.
        time.sleep(0.5)
        assert not w.is_heartbeat_dead()
    finally:
        w.stop()


def test_disabled_heartbeat_never_dead() -> None:
    q: queue.Queue = queue.Queue()
    w = _NoHeartbeatWatcher(q)
    w.start()
    try:
        time.sleep(0.3)
        assert w.HEARTBEAT_TIMEOUT_SEC is None
        assert not w.is_heartbeat_dead()
    finally:
        w.stop()


def test_paused_watcher_not_flagged() -> None:
    """Pausing intentionally stops emissions; heartbeat must not flag it."""
    q: queue.Queue = queue.Queue()
    w = _SilentWatcher(q)
    w.start()
    try:
        w.pause()
        time.sleep(0.4)
        assert not w.is_heartbeat_dead()
    finally:
        w.resume()
        w.stop()


def test_revive_restarts_thread() -> None:
    q: queue.Queue = queue.Queue()
    w = _SilentWatcher(q)
    w.start()
    first_thread = w._thread
    try:
        assert _wait(lambda: w.is_heartbeat_dead())
        revived = w.heartbeat_revive()
        assert revived is True
        assert w._heartbeat_revival_count == 1
        assert w._thread is not first_thread  # new thread started
        assert w.is_running()
    finally:
        w.stop()


def test_revive_budget_exhaustion_marks_gave_up() -> None:
    q: queue.Queue = queue.Queue()
    w = _SilentWatcher(q)
    w.start()
    try:
        for _ in range(w.MAX_HEARTBEAT_REVIVALS):
            assert _wait(lambda: w.is_heartbeat_dead())
            assert w.heartbeat_revive() is True

        # Budget exhausted: next attempt should refuse and mark gave_up.
        assert _wait(lambda: w.is_heartbeat_dead())
        assert w.heartbeat_revive() is False
        assert w._heartbeat_gave_up is True
        # is_heartbeat_dead returns False once we've given up — to avoid
        # repeated revival attempts spamming logs.
        assert not w.is_heartbeat_dead()
        health = w.health()
        assert health["heartbeat_revivals"] == w.MAX_HEARTBEAT_REVIVALS
        assert health["heartbeat_gave_up"] is True
    finally:
        w.stop()


def test_emit_refreshes_heartbeat() -> None:
    q: queue.Queue = queue.Queue()
    w = _SilentWatcher(q)
    w.start()
    try:
        # Manually emit to refresh.
        time.sleep(0.15)
        w.emit(object())  # type: ignore[arg-type]
        # Check right after emit: a fixed sleep here exceeded the 0.2s timeout on slow CI runners.
        assert not w.is_heartbeat_dead()
        # Then go silent and trip again.
        assert _wait(lambda: w.is_heartbeat_dead())
    finally:
        w.stop()
