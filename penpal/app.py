from __future__ import annotations
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from penpal.capabilities.capture_facts import CAPTURE_FACT_CAPS as _CAPTURE_FACT_CAPS
from penpal.capabilities.registry import CapabilityRegistry, get_default_registry
from penpal.capture.browser_automation_state import clear_browser_automation_denied_browsers_on_startup
from penpal.config import Config
from penpal.foundation.runtime_logs import LAUNCHD_LOG_CHECK_INTERVAL_SEC, cap_launchd_output_logs
from penpal.foundation.runtime_identity import IdentityWatcher, RELOAD_EXIT_CODE
from penpal.i18n import T
from penpal.utils.lock import SingleInstanceLock
from penpal.utils.paths import get_daemon_lock_path, get_pid_path
from penpal.utils.logging import setup_logging, get_logger
from penpal.store.db import DatabaseSchemaTooNewError, init_db, read_schema_version
from penpal.store.migrations import SCHEMA_VERSION
from penpal.runtime.evaluate import current_identity, loaded_identity
from penpal.sources.scheduler import SourcesScheduler
from penpal.services.capability_monitor import (
    run_capability_self_check as _run_capability_self_check,
)

logger = get_logger("app")
_CAPTURE_PROBE_CAPS = {
    "accessibility_permission",
    "browser_automation",
    "appkit_runtime",
}
_COMPAT_CAPTURE_CAPS = _CAPTURE_FACT_CAPS



def _sleep_on_schema_too_new(
    config: Config,
    config_path: str | Path | None,
    lock: SingleInstanceLock,
    *,
    sleep=time.sleep,
    max_iterations: int | None = None,
) -> None:
    """Stay alive without writes when DB schema is newer than code supports.

    Avoids exit 75 hot-loops under launchd, maintains SingleInstanceLock, and watches
    for code upgrades to auto-reload once upgraded.
    """
    logger.info("daemon suspended due to newer database schema; waiting for code upgrade")

    def _shutdown(signum, frame):
        logger.info(f"Signal {signum} received while suspended on schema_too_new, shutting down")
        lock.release()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    identity_watcher = IdentityWatcher(
        loaded_identity(config, config_path=config_path),
        lambda: current_identity(config, config_path=config_path),
    )

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            sleep(10)
        except InterruptedError:
            pass

        try:
            if identity_watcher.check():
                logger.info("runtime identity changed after schema_too_new suspension; reloading daemon")
                lock.release()
                _reload_in_place()
        except Exception as exc:
            logger.warning("identity check failed during schema_too_new suspension: %s", type(exc).__name__)

        rec = read_schema_version(config.db_path_expanded)
        if rec is not None and rec <= SCHEMA_VERSION:
            logger.info("database schema became supported (schema=%d); reloading daemon", rec)
            lock.release()
            _reload_in_place()


DEFAULT_RELOAD_FLUSH_TIMEOUT_SEC: float = 10.0


def _reload_in_place() -> None:
    """Replace this process with the installed entry point after a runtime identity change.

    launchd's KeepAlive relaunch is not dependable: on macOS 26 an exited job can stay pending
    ("pended nondemand spawn = semaphore") instead of restarting. Exec keeps the PID and launchd's
    ownership. The stable entry (Homebrew's link or the ~/.local/bin wrapper) survives an upgrade
    that removed the old versioned files. If exec fails, exit 75 and let launchd or the watchdog
    relaunch.
    """
    from penpal.foundation.native_launcher import native_launcher
    from penpal.foundation.runtime_plist import resolve_penpal_executable

    try:
        from penpal.sources.sink import flush_named_entity_backfill

        flush_named_entity_backfill(timeout_sec=DEFAULT_RELOAD_FLUSH_TIMEOUT_SEC)
    except Exception as exc:
        logger.warning("flush before reload failed: %s", type(exc).__name__)

    for handler in logging.getLogger().handlers:
        handler.flush()
    try:
        # Re-enter through Penpal.app's launcher when started from it: exec'ing Python would drop the Penpal identity.
        executable = native_launcher() or str(resolve_penpal_executable())
        os.execv(executable, [executable, "serve"])
    except OSError as exc:
        logger.error("in-place reload failed (%s); exiting for launchd to relaunch", type(exc).__name__)
    sys.exit(RELOAD_EXIT_CODE)


def _suspend_for_new_schema(config: Config, config_path: str | Path | None,
                            lock: SingleInstanceLock, recorded_schema: int) -> None:
    logger.error(
        "database schema is newer than this daemon supports; refusing database writes "
        "(recorded=%s supported=%s)", recorded_schema, SCHEMA_VERSION,
    )
    from penpal.runtime.evaluate import write_schema_too_new_record

    try:
        write_schema_too_new_record(
            config.db_path_expanded,
            recorded_schema=recorded_schema,
            supported_schema=SCHEMA_VERSION,
            pid=os.getpid(),
        )
        _sleep_on_schema_too_new(config, config_path, lock)
    finally:
        lock.release()

def _on_mac_wake(registry: CapabilityRegistry, scheduler: SourcesScheduler) -> None:
    """After the Mac wakes: the same read-only self-check the supervisor runs (updates the stored
    permission state that doctor and MCP capture_health read), then a rescan of every source."""
    _run_capability_self_check(registry)
    scheduler.rescan_all(reason="mac_wake")


_self_check_supervisor_started = False


def _spawn_self_check_supervisor(interval_sec: float = 60.0, registry: CapabilityRegistry | None = None) -> None:
    """Re-run capability monitor every interval_sec and persist capability states."""
    global _self_check_supervisor_started
    if _self_check_supervisor_started:
        return
    _self_check_supervisor_started = True
    cap_registry = registry or get_default_registry()

    def _loop() -> None:
        while True:
            time.sleep(interval_sec)
            try:
                _run_capability_self_check(cap_registry)
            except Exception as exc:
                logger.error(f"self-check supervisor tick failed: {exc}")

    thread = threading.Thread(target=_loop, daemon=True, name="self-check-supervisor")
    thread.start()
    logger.info(f"self-check supervisor running (interval={interval_sec}s)")


def run(config: Optional[Config] = None, *, config_path: str | Path | None = None):
    """
    Main daemon execution: starts CaptureManager, blocks until SIGTERM/SIGINT.
    Runs in the foreground under launchd or directly for diagnostics.
    """
    if config is None:
        config = Config.load()

    setup_logging(config.log_path_expanded)
    logger.info("Penpal daemon starting")
    for path, exc in cap_launchd_output_logs():
        logger.warning("could not cap launchd output log %s: %s", path.name, type(exc).__name__)

    lock = SingleInstanceLock(get_daemon_lock_path(), get_pid_path())
    if not lock.acquire():
        pid = lock.get_pid() or "unknown"
        message = T(
            f"Penpal 后台服务已经运行（PID {pid}）。",
            f"Penpal daemon is already running (PID {pid}).",
        )
        sys.stderr.write(message + "\n")
        sys.exit(73)

    recorded_schema = read_schema_version(config.db_path_expanded)
    if recorded_schema is not None and recorded_schema > SCHEMA_VERSION:
        _suspend_for_new_schema(config, config_path, lock, recorded_schema)
        return

    try:
        from penpal.permissions import trigger_required_prompts
        required_permission_keys = set()
        if config.watchers.keyboard_chunk:
            required_permission_keys.update({"input_monitoring", "accessibility"})
        if config.watchers.ax_text:
            required_permission_keys.add("accessibility")
        prompt_results = trigger_required_prompts(required_permission_keys)
        logger.info("permission prompt results: %s", prompt_results)
    except Exception as exc:
        logger.warning("permission prompt trigger failed: %s", exc)

    try:
        init_db(config.db_path_expanded)
    except DatabaseSchemaTooNewError as exc:
        _suspend_for_new_schema(config, config_path, lock, exc.recorded)
        return
    cleared_denied = clear_browser_automation_denied_browsers_on_startup()
    logger.info(
        "daemon startup reset browser automation denied cache (cleared=%d, browsers=%s)",
        len(cleared_denied),
        ", ".join(cleared_denied) if cleared_denied else "-",
    )
    registry = get_default_registry()
    _run_capability_self_check(registry)
    _spawn_self_check_supervisor(registry=registry)
    scheduler_cfg = config.sources.scheduler
    scheduler = SourcesScheduler(
        enabled=scheduler_cfg.enabled,
        poll_interval_sec=scheduler_cfg.poll_interval_sec,
        debounce_sec=scheduler_cfg.debounce_sec,
        fast_poll_interval_sec=scheduler_cfg.fast_poll_interval_sec,
    )

    from penpal.capture.manager import CaptureManager

    manager = CaptureManager(config)
    manager._runtime_identity = loaded_identity(config, config_path=config_path)
    manager.set_runtime_component("sources_scheduler", "running" if scheduler.enabled else "disabled")
    scheduler.set_progress_callback(lambda: manager.report_progress("sources_scheduler"))
    manager.set_wake_handler(lambda: _on_mac_wake(registry, scheduler))

    def _shutdown(signum, frame):
        logger.info(f"Signal {signum} received, shutting down")
        try:
            scheduler.stop()
        except Exception as exc:
            logger.error("error stopping sources scheduler: %s", type(exc).__name__)
        try:
            manager.stop()
        except Exception as exc:
            logger.error("error stopping capture manager: %s", type(exc).__name__)
        finally:
            lock.release()
            sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        manager.start()
        logger.info("Penpal daemon running")

        scheduler.start()

        identity_watcher = IdentityWatcher(
            manager._runtime_identity,
            lambda: current_identity(config, config_path=config_path),
        )
        next_log_cap = time.monotonic() + LAUNCHD_LOG_CHECK_INTERVAL_SEC
        next_hud_supervision = time.monotonic() + 60.0

        while True:
            now_monotonic = time.monotonic()
            if now_monotonic >= next_log_cap:
                for path, exc in cap_launchd_output_logs():
                    logger.warning("could not cap launchd output log %s: %s", path.name, type(exc).__name__)
                next_log_cap = now_monotonic + LAUNCHD_LOG_CHECK_INTERVAL_SEC
            if now_monotonic >= next_hud_supervision:
                try:
                    from penpal.health.hud_supervisor import supervise_hud

                    supervise_hud()
                except Exception as exc:
                    logger.warning("HUD supervision failed: %s", type(exc).__name__)
                next_hud_supervision = now_monotonic + 60.0
            try:
                identity_changed = identity_watcher.check()
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("runtime identity check failed: %s", type(exc).__name__)
                identity_changed = False
            if identity_changed:
                logger.info("runtime identity changed; flushing and reloading daemon")
                try:
                    scheduler.stop()
                except Exception as exc:
                    logger.error("error stopping sources scheduler during reload: %s", type(exc).__name__)
                try:
                    manager.stop()
                except DatabaseSchemaTooNewError:
                    logger.error("newer database schema prevents the old daemon from flushing")
                except Exception as exc:
                    logger.error("error stopping capture manager during reload: %s", type(exc).__name__)
                try:
                    manager._write_runtime_record(force=True)
                except Exception as exc:
                    logger.error("error writing runtime record during reload: %s", type(exc).__name__)
                lock.release()
                _reload_in_place()
            time.sleep(1)
    except Exception as e:
        logger.error("Fatal error: %s", type(e).__name__)
        try:
            scheduler.stop()
        except Exception as stop_exc:
            logger.error("Error stopping sources scheduler after fatal error: %s", type(stop_exc).__name__)
        try:
            manager.stop()
        except Exception as stop_exc:
            logger.error("Error stopping capture manager after fatal error: %s", type(stop_exc).__name__)
        lock.release()
        sys.exit(1)

    finally:
        lock.release()
