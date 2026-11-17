from __future__ import annotations

import threading
import time
from concurrent.futures import ALL_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator, Protocol

from penpal.capture.capture_control import source_writes_allowed
from penpal.capture.workspace_events import NSWorkspaceEventSource
from penpal.sources.history_gate import historical_imports_allowed
from penpal.sources.registry import list_sources, read_all
from penpal.sources.types import DataSource, SemanticEvent
from penpal.utils.logging import get_logger

LOGGER = get_logger("sources.scheduler")
DEFAULT_STOP_TIMEOUT_SEC: float = 10.0


def _normalize_app_token(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _expand_app_tokens(value: str) -> set[str]:
    normalized = _normalize_app_token(value)
    if not normalized:
        return set()
    tokens = {normalized, normalized.replace(" ", "")}
    if "." in normalized:
        tail = normalized.rsplit(".", 1)[-1]
        if tail:
            tokens.add(tail)
    return {token for token in tokens if token}


class WorkspaceEventSource(Protocol):
    def start(
        self,
        *,
        on_app_launch: Callable[[str, str], None],
        on_app_quit: Callable[[str, str], None],
        on_unlock: Callable[[], None],
    ) -> None:
        ...

    def stop(self) -> None:
        ...


def _default_event_source() -> WorkspaceEventSource:
    return NSWorkspaceEventSource()


class SourcesScheduler:
    def __init__(
        self,
        *,
        enabled: bool = True,
        poll_interval_sec: int = 600,
        debounce_sec: int = 300,
        fast_poll_interval_sec: int = 120,
        list_sources_fn: Callable[[], list[DataSource]] = list_sources,
        read_all_fn: Callable[..., Iterator[SemanticEvent]] = read_all,
        history_allowed_fn: Callable[[], bool] = historical_imports_allowed,
        event_source_factory: Callable[[], WorkspaceEventSource | None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        progress_callback: Callable[[], None] | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._poll_interval_sec = max(float(poll_interval_sec), 0.05)
        self._fast_poll_interval_sec = max(float(fast_poll_interval_sec), 0.05)
        self._debounce_sec = max(float(debounce_sec), 0.0)
        self._list_sources_fn = list_sources_fn
        self._read_all_fn = read_all_fn
        self._history_allowed_fn = history_allowed_fn
        self._event_source_factory = event_source_factory or (_default_event_source)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._monotonic_fn = monotonic_fn
        self._progress_callback = progress_callback or (lambda: None)

        self._running = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._executor: ThreadPoolExecutor | None = None
        self._always_thread: threading.Thread | None = None
        self._event_source: WorkspaceEventSource | None = None

        self._all_plugins: set[str] = set()
        self._always_plugins: set[str] = set()
        self._fast_plugins: set[str] = set()
        self._in_flight: set[str] = set()
        self._in_flight_tasks: dict[Future, str] = {}
        self._unlock_plugins: set[str] = set()
        self._launch_routes: dict[str, set[str]] = {}
        self._quit_routes: dict[str, set[str]] = {}

        self._last_trigger_mono: dict[str, float] = {}
        self._last_success_utc: dict[str, datetime] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled:
            LOGGER.info("sources scheduler disabled by config")
            return
        if self._running.is_set():
            return

        self._rebuild_routes()
        self._in_flight_tasks.clear()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sources-scan")
        self._stop_event.clear()
        self._running.set()

        self._always_thread = threading.Thread(
            target=self._always_loop,
            daemon=True,
            name="sources-always",
        )
        self._always_thread.start()

        if self._launch_routes or self._quit_routes or self._unlock_plugins:
            try:
                self._event_source = self._event_source_factory()
                if self._event_source is not None:
                    self._event_source.start(
                        on_app_launch=self._on_app_launch,
                        on_app_quit=self._on_app_quit,
                        on_unlock=self._on_unlock,
                    )
            except Exception:
                LOGGER.exception("failed to start NSWorkspace event source; event-triggered scans disabled")
                self._event_source = None

        LOGGER.info(
            "sources scheduler started enabled=%s poll_interval_sec=%s debounce_sec=%s fast_poll_interval_sec=%s always=%s app_running_routes=%s after_quit_routes=%s after_unlock=%s",
            self._enabled,
            self._poll_interval_sec,
            self._debounce_sec,
            self._fast_poll_interval_sec,
            sorted(self._always_plugins),
            {k: sorted(v) for k, v in self._launch_routes.items()},
            {k: sorted(v) for k, v in self._quit_routes.items()},
            sorted(self._unlock_plugins),
        )

    def stop(self, timeout_sec: float = DEFAULT_STOP_TIMEOUT_SEC) -> None:
        start_time = self._monotonic_fn()
        total_timeout = max(0.0, float(timeout_sec))

        def _remaining() -> float:
            elapsed = self._monotonic_fn() - start_time
            return max(0.0, total_timeout - elapsed)

        if self._running.is_set() or self._executor is not None:
            self._running.clear()
            self._stop_event.set()

            if self._event_source is not None:
                try:
                    self._event_source.stop()
                except Exception:
                    LOGGER.exception("failed to stop NSWorkspace event source")
                finally:
                    self._event_source = None

            if self._always_thread is not None:
                try:
                    self._always_thread.join(timeout=min(2.0, _remaining()))
                except Exception:
                    LOGGER.exception("failed to join always thread")
                finally:
                    self._always_thread = None

            with self._lock:
                tasks = dict(self._in_flight_tasks)
            if tasks:
                done, not_done = wait(list(tasks.keys()), timeout=_remaining(), return_when=ALL_COMPLETED)
                if not_done:
                    for f in not_done:
                        plugin_name = tasks.get(f, "unknown")
                        LOGGER.warning("sync task did not finish: plugin=%s", plugin_name)
                for f in done:
                    try:
                        f.result()
                    except Exception as exc:
                        LOGGER.warning("sync task failed: %s", type(exc).__name__)

            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    LOGGER.exception("failed to shutdown sources scan executor")
                finally:
                    self._executor = None

        try:
            from penpal.sources.sink import flush_named_entity_backfill

            flush_named_entity_backfill(timeout_sec=_remaining())
        except Exception as exc:
            LOGGER.warning("failed to flush named entity backfill: %s", type(exc).__name__)

        LOGGER.info("sources scheduler stopped")

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        self._progress_callback = callback

    def rescan_all(self, *, reason: str) -> None:
        """Re-read every known source once (used after a Mac wake).

        Nothing here resets cursors: each plugin's own cursor or since-window decides what is new, and the
        unique indexes on raw_events make re-reading an already stored record a no-op.
        """
        for plugin_name in sorted(self._all_plugins):
            self._schedule_scan(plugin_name, reason=reason)

    def _rebuild_routes(self) -> None:
        self._all_plugins.clear()
        self._always_plugins.clear()
        self._fast_plugins.clear()
        self._unlock_plugins.clear()
        self._launch_routes.clear()
        self._quit_routes.clear()

        try:
            plugins = self._list_sources_fn()
        except Exception:
            LOGGER.exception("failed to list sources for scheduler")
            plugins = []

        for plugin in plugins:
            name = str(getattr(plugin, "name", "") or "")
            if not name:
                continue
            self._all_plugins.add(name)
            if getattr(plugin, "fast_poll", False):
                self._fast_plugins.add(name)
            liveness = str(getattr(plugin, "liveness", "") or "").strip()
            app_hints = tuple(getattr(plugin, "app_hints", ()) or ())

            if liveness == "always":
                self._always_plugins.add(name)
                continue

            if liveness == "after_unlock":
                self._unlock_plugins.add(name)
                continue

            if liveness not in {"app_running", "after_app_quit"}:
                continue

            route_map = self._launch_routes if liveness == "app_running" else self._quit_routes
            for hint in app_hints:
                for token in _expand_app_tokens(str(hint)):
                    route_map.setdefault(token, set()).add(name)

    def _plugin_interval(self, plugin_name: str) -> float:
        return self._fast_poll_interval_sec if plugin_name in self._fast_plugins else self._poll_interval_sec

    def _always_loop(self) -> None:
        next_due: dict[str, float] = {}
        while self._running.is_set():
            for plugin_name in sorted(self._always_plugins):
                if not self._running.is_set():
                    break
                now = self._monotonic_fn()
                if now >= next_due.get(plugin_name, now):
                    self._schedule_scan(plugin_name, reason="always_interval")
                    next_due[plugin_name] = self._monotonic_fn() + self._plugin_interval(plugin_name)

            delay = min(next_due.values(), default=self._monotonic_fn() + 10.0) - self._monotonic_fn()
            if self._stop_event.wait(timeout=max(delay, 0.0)):
                break
            self._progress_callback()

    def _on_app_launch(self, bundle_id: str, process_name: str) -> None:
        matched = self._match_plugins(self._launch_routes, bundle_id=bundle_id, process_name=process_name)
        for plugin_name in sorted(matched):
            self._schedule_scan(
                plugin_name,
                reason=f"app_launch:{bundle_id or '-'}:{process_name or '-'}",
            )

    def _on_app_quit(self, bundle_id: str, process_name: str) -> None:
        matched = self._match_plugins(self._quit_routes, bundle_id=bundle_id, process_name=process_name)
        for plugin_name in sorted(matched):
            self._schedule_scan(
                plugin_name,
                reason=f"app_quit:{bundle_id or '-'}:{process_name or '-'}",
            )

    def _on_unlock(self) -> None:
        for plugin_name in sorted(self._unlock_plugins):
            self._schedule_scan(plugin_name, reason="after_unlock")

    def _match_plugins(
        self,
        route_map: dict[str, set[str]],
        *,
        bundle_id: str,
        process_name: str,
    ) -> set[str]:
        tokens: set[str] = set()
        for candidate in (bundle_id, process_name):
            tokens.update(_expand_app_tokens(candidate))

        matched: set[str] = set()
        for token in tokens:
            matched.update(route_map.get(token, set()))
        return matched

    def _schedule_scan(self, plugin_name: str, *, reason: str) -> None:
        if not self._running.is_set():
            return
        if self._writes_blocked(plugin_name):
            # No scan while paused or switched off, so the since-window keeps growing and resumes after re-enable.
            LOGGER.debug("sources scheduler skip plugin=%s reason=%s (paused or disabled)", plugin_name, reason)
            return
        executor = self._executor
        if executor is None:
            return
        if not self._claim_scan(plugin_name, reason=reason):
            return
        try:
            future = executor.submit(self._scan_plugin, plugin_name, reason)
            if future is not None:
                with self._lock:
                    self._in_flight_tasks[future] = plugin_name
                if hasattr(future, "add_done_callback"):
                    future.add_done_callback(self._on_scan_done)
        except Exception:
            with self._lock:
                self._in_flight.discard(plugin_name)
            raise

    def _on_scan_done(self, future: Future) -> None:
        with self._lock:
            self._in_flight_tasks.pop(future, None)

    @staticmethod
    def _writes_blocked(plugin_name: str) -> bool:
        try:
            return not source_writes_allowed(plugin_name)
        except Exception:
            # read_all re-checks and fails closed, so a state read error here only skips the optimisation.
            LOGGER.exception("capture switch read failed plugin=%s", plugin_name)
            return False

    def _claim_scan(self, plugin_name: str, *, reason: str) -> bool:
        now_mono = self._monotonic_fn()
        with self._lock:
            if plugin_name in self._in_flight:
                LOGGER.debug("sources scheduler in-flight skip plugin=%s reason=%s", plugin_name, reason)
                return False
            last_mono = self._last_trigger_mono.get(plugin_name)
            if last_mono is not None and (now_mono - last_mono) < min(self._debounce_sec, self._plugin_interval(plugin_name)):
                LOGGER.debug("sources scheduler debounce skip plugin=%s reason=%s", plugin_name, reason)
                return False
            self._in_flight.add(plugin_name)
            self._last_trigger_mono[plugin_name] = now_mono
        return True

    def _scan_plugin(self, plugin_name: str, reason: str) -> None:
        try:
            # Historical sources stay off until onboarding (or an existing install is detected). Live capture is a
            # separate watcher path and never goes through here (#32, D26).
            if not self._history_allowed_fn():
                LOGGER.debug("sources scheduler skip plugin=%s reason=%s (waiting for onboarding)", plugin_name, reason)
                return
            now = self._as_utc(self._now_fn())
            with self._lock:
                since = self._last_success_utc.get(plugin_name)
            if since is None:
                since = now - timedelta(seconds=self._poll_interval_sec)

            events = 0
            try:
                for _ in self._read_all_fn(
                    since,
                    now,
                    source=plugin_name,
                    persist_raw_events=True,
                ):
                    events += 1
                with self._lock:
                    self._last_success_utc[plugin_name] = now
                LOGGER.info(
                    "sources scheduler scan done plugin=%s reason=%s events=%s since=%s until=%s",
                    plugin_name,
                    reason,
                    events,
                    since.isoformat(),
                    now.isoformat(),
                )
            except Exception:
                LOGGER.exception("sources scheduler scan failed plugin=%s reason=%s", plugin_name, reason)
        finally:
            with self._lock:
                self._in_flight.discard(plugin_name)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


__all__ = ["SourcesScheduler", "WorkspaceEventSource"]
