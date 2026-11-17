"""Watcher construction, lifecycle, wake recovery, and daemon-loop orchestration."""

from __future__ import annotations

import threading
import time
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from penpal.capture import manager_runtime
from penpal.capture.manager_storage import _storage_error_code
from penpal.capture.camera_monitor import CameraMonitor
from penpal.capture.capture_control import disabled_sources, is_capture_paused
from penpal.capture.wake import LAST_WAKE_STATE_KEY, detect_wake
from penpal.foundation.runtime_record import utc_now
from penpal.store.db import init_db
from penpal.store.repository import get_state, seed_policies_from_config, set_state
from penpal.utils.logging import get_logger

logger = get_logger("manager")


def start(manager):
    """Initialize DB, seed policies, start watchers and flush loop."""
    init_db(manager.config.db_path_expanded)
    seed_policies_from_config(manager.config.policies)
    manager._policy.reload()
    # D32: startup never deletes user data. Retention was removed; history is kept until an explicit purge.

    manager._init_watchers()
    for w in manager._watchers.values():
        w.start()

    manager._running.set()
    # A persisted pause survives restarts; only a non-paused daemon sets running.
    if not is_capture_paused():
        set_state("status", "running")
    set_state("started_at", datetime.now(timezone.utc).isoformat())
    started_at = utc_now()
    manager_runtime.initialize_runtime_record(manager, started_at)
    manager._flush_thread = threading.Thread(
        target=manager._flush_loop, daemon=True, name="flush-loop"
    )
    manager._flush_thread.start()
    manager._heartbeat_thread = threading.Thread(
        target=manager._heartbeat_loop, daemon=True, name="heartbeat-loop"
    )
    manager._heartbeat_thread.start()
    logger.info("CaptureManager started")
    manager._start_camera_monitor()


def stop(manager):
    """Stop all watchers and flush remaining events."""
    if manager._camera_monitor is not None:
        try:
            manager._camera_monitor.stop()
        except Exception as exc:
            logger.error("error stopping camera monitor: %s", type(exc).__name__)
        manager._camera_monitor = None
    manager._running.clear()
    for w in manager._watchers.values():
        try:
            w.stop()
        except Exception as exc:
            logger.error("error stopping watcher: %s", type(exc).__name__)
    try:
        manager._drain_queue()
    except Exception as exc:
        logger.error("error draining queue during stop: %s", type(exc).__name__)
    try:
        manager._flush_window_session(reason="watcher_stop")
    except Exception as exc:
        logger.error("error flushing window session during stop: %s", type(exc).__name__)
    try:
        manager._aggregator.flush()
    except Exception as exc:
        logger.error("error flushing aggregator during stop: %s", type(exc).__name__)
    if manager._flush_thread:
        try:
            manager._flush_thread.join(timeout=10)
        except Exception as exc:
            logger.error("error joining flush thread: %s", type(exc).__name__)
    if manager._heartbeat_thread:
        try:
            manager._heartbeat_thread.join(timeout=5)
        except Exception as exc:
            logger.error("error joining heartbeat thread: %s", type(exc).__name__)
    try:
        from penpal.sources.sink import flush_named_entity_backfill

        flush_named_entity_backfill(timeout_sec=5.0)
    except Exception as exc:
        logger.error("error flushing named entity backfill: %s", type(exc).__name__)
    if not is_capture_paused():  # keep a persisted pause across daemon restarts
        try:
            set_state("status", "stopped")
        except Exception as exc:
            logger.error("error setting status stopped: %s", type(exc).__name__)
    logger.info("CaptureManager stopped")


def pause(manager):
    manager._paused.set()
    for w in manager._watchers.values():
        w.pause()
    set_state("status", "paused")


def resume(manager):
    manager._paused.clear()
    disabled = disabled_sources()
    for name, w in manager._watchers.items():
        if name not in disabled:  # a switched-off source stays quiet
            w.resume()
    set_state("status", "running")


def pause_watchers(manager, names: list[str]) -> None:
    for name in names:
        watcher = manager._watchers.get(name)
        if watcher is not None:
            watcher.pause()


def resume_watchers(manager, names: list[str]) -> None:
    for name in names:
        watcher = manager._watchers.get(name)
        if watcher is not None:
            watcher.resume()


def _init_watchers(manager):
    cfg = manager.config.watchers
    if cfg.window:
        from penpal.capture.watchers.window import WindowWatcher

        manager._watchers["window"] = WindowWatcher(
            manager._queue,
            browser_app_names=manager.config.browser.supported_browsers,
        )
    if cfg.idle:
        from penpal.capture.watchers.idle import IdleWatcher

        manager._watchers["idle"] = IdleWatcher(
            manager._queue, manager.config.idle.threshold_sec
        )
    if cfg.clipboard:
        from penpal.capture.watchers.clipboard import ClipboardWatcher

        manager._watchers["clipboard"] = ClipboardWatcher(
            manager._queue,
            max_text_length=manager.config.clipboard.max_text_length,
            dedup_window_sec=manager.config.clipboard.dedup_window_sec,
        )
    if cfg.manual:
        from penpal.capture.watchers.manual import ManualWatcher

        manager._watchers["manual"] = ManualWatcher(manager._queue)
    if cfg.keyboard_chunk:
        from penpal.capture.watchers.keyboard_chunk import KeyboardChunkWatcher

        keyboard_cfg = getattr(manager.config, "keyboard_chunk", None)
        manager._watchers["keyboard_chunk"] = KeyboardChunkWatcher(
            manager._queue,
            silence_sec=getattr(keyboard_cfg, "silence_sec", 2.0),
            force_flush_sec=getattr(keyboard_cfg, "force_flush_sec", 20.0),
            store_text=getattr(keyboard_cfg, "store_text", True),
        )
    if cfg.ax_text:
        from penpal.capture.watchers.ax_text import AXTextWatcher

        manager._watchers["ax_text"] = AXTextWatcher(
            manager._queue,
            poll_interval_sec=manager.config.ax_text.poll_interval_sec,
        )
    if cfg.browser:
        from penpal.capture.watchers.browser import BrowserWatcher

        manager._watchers["browser"] = BrowserWatcher(
            manager._queue,
            poll_interval_sec=manager.config.browser.poll_interval_sec,
            supported_browsers=manager.config.browser.supported_browsers,
        )
    if cfg.browser_url:
        from penpal.capture.watchers.browser_url import BrowserUrlWatcher

        manager._watchers["browser_url"] = BrowserUrlWatcher(
            manager._queue,
            poll_interval_sec=manager.config.browser_url.poll_interval_sec,
            supported_browsers=manager.config.browser_url.supported_browsers,
            emit_on_url_change_only=manager.config.browser_url.emit_on_url_change_only,
        )
    if manager.config.browser_history.enabled:
        from penpal.capture.watchers.browser_history import BrowserHistoryWatcher

        manager._watchers["browser_history"] = BrowserHistoryWatcher(
            manager._queue,
            poll_interval_sec=manager.config.browser_history.poll_interval_sec,
            enabled=manager.config.browser_history.enabled,
            copy_to_cache=manager.config.browser_history.copy_to_cache,
            browsers=manager.config.browser_history.browsers,
        )


def _start_camera_monitor(manager) -> None:
    if not manager.config.privacy.camera_scene_pause:
        return
    watched_names = [name for name in ("ax_text",) if name in manager._watchers]
    if not watched_names:
        return

    def on_change(in_use: bool) -> None:
        if in_use:
            logger.info("Camera in use, pausing ax_text")
            manager.pause_watchers(watched_names)
        else:
            logger.info("Camera released, resuming ax_text")
            manager.resume_watchers(watched_names)

    manager._camera_monitor = CameraMonitor(on_change=on_change)
    manager._camera_monitor.start()


def _heartbeat_loop(manager) -> None:
    """Detect and revive watchers that have gone silently mute.

    BaseWatcher.is_heartbeat_dead returns True when a watcher with a
    configured HEARTBEAT_TIMEOUT_SEC hasn't emitted within the timeout
    — typically because macOS revoked permission without raising. We
    stop+start the watcher (counts toward MAX_HEARTBEAT_REVIVALS) and
    log an ERROR so the user can see permission rotation events.
    """
    # Sleep first so quick pytest scenarios don't race startup.
    manager._wait_for_next_check()
    while manager._running.is_set():
        try:
            if not manager._paused.is_set():
                for name, watcher in list(manager._watchers.items()):
                    try:
                        if watcher.is_heartbeat_dead():
                            watcher.heartbeat_revive()
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.error("heartbeat check for %s failed: %s", name, exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("heartbeat loop error: %s", exc)
        manager._wait_for_next_check()


def _wait_for_next_check(manager) -> None:
    """Sleep one heartbeat interval in 0.5 s slices and watch the wall clock for a sleep/wake jump.

    A slice that took far longer on the wall clock than the 0.5 s it asked for means the Mac slept
    in between; the wake is handled at the first slice after resume (within ~0.5 s).
    """
    slept = 0.0
    while manager._running.is_set() and slept < manager.HEARTBEAT_CHECK_INTERVAL_SEC:
        wall_before = time.time()
        time.sleep(0.5)
        slept += 0.5
        if detect_wake(time.time() - wall_before, 0.5):
            manager.handle_wake()


def set_wake_handler(manager, handler: Optional[Callable[[], None]]) -> None:
    """Callback run after each detected wake, after the manager's own watcher restart.

    penpal.app wires it to the same capability self-check the supervisor runs, plus a source rescan.
    """
    manager._wake_handler = handler


def handle_wake(manager) -> None:
    """Re-check the daemon after the Mac wakes from sleep.

    Also the test seam: a test injects a fake wake by calling this directly.
    Steps: record the wake time (the MCP grace window reads it), restart watchers that stopped, persist
    the runtime heartbeat now, then run the wake handler (permission self-check and source rescan).
    """
    logger.info("Mac woke from sleep: re-checking watchers, permissions and sources")
    try:
        set_state(LAST_WAKE_STATE_KEY, datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        logger.error("recording wake time failed: %s", exc)
    manager._restart_watchers_after_wake()
    handler = manager._wake_handler
    if handler is not None:
        try:
            handler()
        except Exception as exc:
            logger.error("wake handler failed: %s", exc)


def _restart_watchers_after_wake(manager) -> None:
    """Restart enabled, non-paused watchers whose thread is gone (sleep can leave them silent).

    Watchers paused on purpose (camera, a source switched off, a user pause) are left alone, and so are
    live threads: a running but silent watcher is still covered by the heartbeat supervisor.
    """
    if manager._paused.is_set():
        return
    disabled = disabled_sources()
    for name, watcher in list(manager._watchers.items()):
        if name in disabled or watcher.is_paused() or watcher.is_running():
            continue
        try:
            watcher.start()
            logger.info("watcher %s restarted after wake", name)
        except Exception as exc:
            logger.error("watcher %s restart after wake failed: %s", name, exc)


def _flush_loop(manager):
    while manager._running.is_set():
        try:
            manager._sync_control_state()
            manager._sync_source_switches()
        except sqlite3.Error as exc:
            manager._enter_storage_error(_storage_error_code(exc))
            manager._storage_next_probe_mono = 0.0
            time.sleep(manager.config.app.flush_interval_sec)
            continue
        # Always drain: while paused or source-disabled, _process_event drops events at write time,
        # so nothing queued during a pause is replayed after resume.
        manager._drain_queue()
        manager.report_progress("core_loop")
        try:
            manager._write_runtime_record()
        except (
            Exception
        ) as exc:  # e.g. disk full: keep the flush loop alive, report once
            if not manager._runtime_persist_failed:
                logger.error(
                    "capture runtime state not persisted: %s", type(exc).__name__
                )
            manager._runtime_persist_failed = True
        else:
            manager._runtime_persist_failed = False
        time.sleep(manager.config.app.flush_interval_sec)


def _sync_source_switches(manager) -> None:
    """Quiet watchers of disabled sources (keyboard listener included). The write gate still applies."""
    disabled = disabled_sources()
    for name in disabled:
        watcher = manager._watchers.get(name)
        if watcher is not None:
            watcher.pause()  # idempotent; re-applied every sync in case a watcher was restarted
    if not manager._paused.is_set():
        for name in manager._disabled_applied - disabled:
            watcher = manager._watchers.get(name)
            if watcher is not None:
                watcher.resume()
    manager._disabled_applied = disabled


def _sync_control_state(manager):
    desired = get_state("status") or "running"
    paused_until = get_state("paused_until") or ""
    if desired == "paused" and paused_until:
        try:
            if time.time() >= float(paused_until):
                manager.resume()
                set_state("paused_until", "")
                return
        except Exception:
            set_state("paused_until", "")
    if desired == "paused" and not manager._paused.is_set():
        manager.pause()
    elif desired == "running" and manager._paused.is_set():
        manager.resume()
