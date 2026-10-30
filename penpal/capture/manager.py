from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from penpal.capture import (
    manager_events,
    manager_lifecycle,
    manager_runtime,
    manager_storage,
)
from penpal.capture.aggregator import Aggregator
from penpal.capture.base import BaseWatcher
from penpal.capture.camera_monitor import CameraMonitor
from penpal.capture.fusion import CaptureFusionEngine
from penpal.capture.manager_events import _derive_speaker as _derive_speaker
from penpal.capture.normalizer import normalize_manual_event
from penpal.capture.policy import PolicyEngine
from penpal.config import Config
from penpal.foundation.runtime_record import Component, Progress, RuntimeRecord
from penpal.store.models import RawEvent


class CaptureManager:
    # While the disk is full, capture writes nothing and attempts one probe write per interval.
    DISK_FULL_PROBE_SEC: float = 60.0
    HEARTBEAT_CHECK_INTERVAL_SEC: float = 60.0

    def __init__(self, config: Config):
        self.config = config
        self._queue: queue.Queue = queue.Queue()
        self._watchers: dict[str, BaseWatcher] = {}
        self._policy = PolicyEngine()
        self._aggregator = Aggregator()
        self._fusion = CaptureFusionEngine()
        self._running = threading.Event()
        self._paused = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._camera_monitor: Optional[CameraMonitor] = None
        self._runtime_record: RuntimeRecord | None = None
        self._runtime_identity = None
        self._runtime_progress: dict[str, Progress] = {}
        self._runtime_components: dict[str, Component] = {}
        self._runtime_record_lock = threading.RLock()
        self._runtime_last_written_mono = 0.0
        self._source_counts: dict[str, int] = {}
        self._source_last_event_at: dict[str, str] = {}
        # D27: gate hits by "source:reason" (counts only, never content)
        self._gate_hits: dict[str, int] = {}
        # #26 / D27: last URL each browser app showed (app name -> URL), fed by the browser URL and tab
        # watchers. Keyboard, AX and clipboard text from that browser is checked against url_deny_hosts.
        self._browser_pages: dict[str, str] = {}
        # Disabled sources whose watchers we have already paused (#28).
        self._disabled_applied: frozenset[str] = frozenset()
        # Storage state: None = writing normally; "disk_full" = writes paused until the next probe.
        self._storage_error: Optional[str] = None
        self._storage_error_since: Optional[str] = None
        self._storage_dropped = 0
        self._storage_next_probe_mono = 0.0
        self._runtime_persist_failed = False
        self._wake_handler: Optional[Callable[[], None]] = None

    def start(self):
        """Initialize DB, seed policies, start watchers and flush loop."""
        return manager_lifecycle.start(self)

    def stop(self):
        """Stop all watchers and flush remaining events."""
        return manager_lifecycle.stop(self)

    def pause(self):
        return manager_lifecycle.pause(self)

    def resume(self):
        return manager_lifecycle.resume(self)

    def pause_watchers(self, names: list[str]) -> None:
        return manager_lifecycle.pause_watchers(self, names)

    def resume_watchers(self, names: list[str]) -> None:
        return manager_lifecycle.resume_watchers(self, names)

    def health(self) -> dict:
        return {
            "running": self._running.is_set(),
            "paused": self._paused.is_set(),
            "watchers": {name: w.health() for name, w in self._watchers.items()},
            "queue_size": self._queue.qsize(),
            "gate_hits": dict(self._gate_hits),
        }

    def save_manual(self, text: str, tags: Optional[str] = None):
        """Inject a manual save event directly."""
        event = normalize_manual_event(text=text, tags=tags)
        self._queue.put(event)

    def _init_watchers(self):
        return manager_lifecycle._init_watchers(self)

    def _start_camera_monitor(self) -> None:
        return manager_lifecycle._start_camera_monitor(self)

    def _heartbeat_loop(self) -> None:
        """Detect and revive watchers that have gone silently mute.

        BaseWatcher.is_heartbeat_dead returns True when a watcher with a
        configured HEARTBEAT_TIMEOUT_SEC hasn't emitted within the timeout
        — typically because macOS revoked permission without raising. We
        stop+start the watcher (counts toward MAX_HEARTBEAT_REVIVALS) and
        log an ERROR so the user can see permission rotation events.
        """
        return manager_lifecycle._heartbeat_loop(self)

    def _wait_for_next_check(self) -> None:
        """Sleep one heartbeat interval in 0.5 s slices and watch the wall clock for a sleep/wake jump.

        A slice that took far longer on the wall clock than the 0.5 s it asked for means the Mac slept
        in between; the wake is handled at the first slice after resume (within ~0.5 s).
        """
        return manager_lifecycle._wait_for_next_check(self)

    def set_wake_handler(self, handler: Optional[Callable[[], None]]) -> None:
        """Callback run after each detected wake, after the manager's own watcher restart.

        penpal.app wires it to the same capability self-check the supervisor runs, plus a source rescan.
        """
        return manager_lifecycle.set_wake_handler(self, handler)

    def handle_wake(self) -> None:
        """Re-check the daemon after the Mac wakes from sleep.

        Also the test seam: a test injects a fake wake by calling this directly.
        Steps: record the wake time (the MCP grace window reads it), restart watchers that stopped, persist
        the runtime heartbeat now, then run the wake handler (permission self-check and source rescan).
        """
        return manager_lifecycle.handle_wake(self)

    def _restart_watchers_after_wake(self) -> None:
        """Restart enabled, non-paused watchers whose thread is gone (sleep can leave them silent).

        Watchers paused on purpose (camera, a source switched off, a user pause) are left alone, and so are
        live threads: a running but silent watcher is still covered by the heartbeat supervisor.
        """
        return manager_lifecycle._restart_watchers_after_wake(self)

    def _flush_loop(self):
        return manager_lifecycle._flush_loop(self)

    def _sync_source_switches(self) -> None:
        """Quiet watchers of disabled sources (keyboard listener included). The write gate still applies."""
        return manager_lifecycle._sync_source_switches(self)

    def _sync_control_state(self):
        return manager_lifecycle._sync_control_state(self)

    def _drain_queue(self):
        return manager_events._drain_queue(self)

    def _is_blacklisted(self, event: RawEvent) -> bool:
        return manager_events._is_blacklisted(self, event)

    def _note_browser_page(self, event: RawEvent) -> None:
        """Remember the page a browser app is showing. Called before any drop, so the URL of an excluded
        domain is recorded too (its own event is dropped, but the text typed after it must be checked).

        The URL watcher emits only when the frontmost browser's URL changes, and it polls every few seconds,
        so a value can lag a tab switch by about one poll interval. A browser with no URL seen yet is
        unknown and is not blocked.
        """
        return manager_events._note_browser_page(self, event)

    def _current_page_url(self, event: RawEvent) -> str | None:
        return manager_events._current_page_url(self, event)

    def _count_gate_hit(self, event: RawEvent, reason: str) -> None:
        return manager_events._count_gate_hit(self, event, reason)

    def _process_event(self, event: RawEvent):
        # Write gate (#28): global pause or a switched-off source writes nothing new.
        return manager_events._process_event(self, event)

    def _current_window_session_id(self) -> str | None:
        return manager_events._current_window_session_id(self)

    def _flush_window_session(self, reason: str) -> None:
        return manager_events._flush_window_session(self, reason)

    def _sync_window_idle_state(self, event: RawEvent) -> None:
        return manager_events._sync_window_idle_state(self, event)

    def _record_runtime_event(self, event: RawEvent) -> None:
        return manager_runtime._record_runtime_event(self, event)

    def _storage_write_blocked(self) -> bool:
        return manager_storage._storage_write_blocked(self)

    def _enter_disk_full(self) -> None:
        # Roll back the failed statement so the shared connection is not left mid-transaction.
        return manager_storage._enter_disk_full(self)

    def _leave_disk_full(self) -> None:
        return manager_storage._leave_disk_full(self)

    def _persist_storage_state(self) -> None:
        # The flush loop publishes this state; a full database may not accept another write.
        return manager_storage._persist_storage_state(self)

    def _storage_status(self) -> dict:
        return manager_storage._storage_status(self)

    def report_progress(self, name: str) -> None:
        """Let a daemon loop report completion without writing the shared record."""
        return manager_runtime.report_progress(self, name)

    def set_runtime_component(self, name: str, state: str, detail: str = "") -> None:
        return manager_runtime.set_runtime_component(self, name, state, detail)

    def _component_snapshot(self) -> dict[str, Component]:
        return manager_runtime._component_snapshot(self)

    def _write_runtime_record(self, force: bool = False) -> None:
        return manager_runtime._write_runtime_record(self, force)
