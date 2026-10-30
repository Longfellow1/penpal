"""Mac wake from sleep (#31): permissions are re-checked and stored, silent watchers restart, sources are
rescanned without duplicate rows, and MCP capture_health does not alarm on heartbeat right after a wake.

Fakes only: temp HOME and DB, permission checks replaced by stubs. Nothing here can prompt, and no real
daemon, launchd job or watcher thread touches the machine. A "fake wake" is a direct call to
``CaptureManager.handle_wake()`` or a fake clock that jumps during the heartbeat wait.
"""
from __future__ import annotations

import time

import pytest

import penpal.capture.manager_lifecycle as manager_lifecycle
from penpal import app, permissions
from penpal.capabilities.base import HealthState
from penpal.capture.base import BaseWatcher
from penpal.capture.manager import CaptureManager
from penpal.capture.wake import LAST_WAKE_STATE_KEY, detect_wake
from penpal.config import Config
from penpal.health import capture_health as ch
from penpal.runtime import RuntimeState, RuntimeStatus
from penpal.runtime.evaluate import Issue
from penpal.permissions import PermissionSpec, PermissionStatus
from penpal.sources.scheduler import SourcesScheduler
from penpal.sources.types import DataSource, DataSourceInstance
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import get_state, insert_raw_event

G, D = PermissionStatus.GRANTED, PermissionStatus.DENIED


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    yield
    close()
    buckets.reset_cache()


class _FakeRegistry:
    """Stands in for CapabilityRegistry: monitor_all is the read-only probe the supervisor runs."""

    def __init__(self) -> None:
        self.monitor_calls = 0

    def monitor_all(self):
        self.monitor_calls += 1
        return {"accessibility_permission": HealthState(ok=True, code="ok", last_checked=time.time())}

    def select_failure(self, states, names=None):
        return None


def _install_permission_stubs(monkeypatch, status_by_key: dict, counter: dict):
    """Replace the permission specs with stubs. ``check`` is counted; ``request`` must never run."""

    def spec(key, label, pane):
        def check(k=key):
            counter[k] = counter.get(k, 0) + 1
            return status_by_key[k]

        def request():
            pytest.fail("a wake re-check must never trigger a permission prompt")

        return PermissionSpec(key=key, label=label, settings_pane=pane, check=check, request=request)

    ax = spec("accessibility", "辅助功能（Accessibility）", "x-apple.systempreferences:fake?ax")
    im = spec("input_monitoring", "输入监控（Input Monitoring）", "x-apple.systempreferences:fake?im")
    sr = spec("screen_recording", "屏幕录制（Screen Recording）", "x-apple.systempreferences:fake?sr")
    monkeypatch.setattr(permissions, "PERMISSION_SPECS", (ax, im))
    monkeypatch.setattr(permissions, "SCREEN_RECORDING_SPEC", sr)


def _keyboard_cfg() -> Config:
    cfg = Config()
    for name in ("ax_text", "ocr"):
        setattr(cfg.watchers, name, False)
    cfg.watchers.keyboard_chunk = True
    return cfg


# ── detection ──────────────────────────────────────────────────────────────────

def test_detect_wake_needs_a_jump_beyond_expected_plus_60s():
    assert detect_wake(0.5, 0.5) is False
    assert detect_wake(60.0, 0.5) is False  # 59.5 s over: not a wake
    assert detect_wake(61.6, 0.5) is True
    assert detect_wake(3600.0, 60.0) is True


def test_heartbeat_wait_sees_a_clock_jump_and_calls_handle_wake(monkeypatch):
    class _Clock:
        """A fake clock where the Mac sleeps for an hour during the first sleep slice."""

        def __init__(self) -> None:
            self.now = 1_000.0
            self.jumped = False

        def time(self) -> float:
            return self.now

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds
            if not self.jumped:
                self.jumped = True
                self.now += 3600.0

    clock = _Clock()
    monkeypatch.setattr(manager_lifecycle, "time", clock)
    manager = CaptureManager(Config())
    manager._running.set()
    wakes: list[int] = []
    manager.set_wake_handler(lambda: wakes.append(1))

    manager._wait_for_next_check()
    assert wakes == [1]
    assert get_state(LAST_WAKE_STATE_KEY)  # the wake time is recorded for the MCP grace window

    manager._wait_for_next_check()  # no further jump: no further wake
    assert wakes == [1]


# ── permissions re-checked on wake ─────────────────────────────────────────────

def test_fake_wake_reruns_permission_checks_and_updates_stored_state(monkeypatch):
    status = {"accessibility": G, "input_monitoring": G}
    counter: dict = {}
    _install_permission_stubs(monkeypatch, status, counter)
    registry = _FakeRegistry()
    scheduler = SourcesScheduler(enabled=False)
    manager = CaptureManager(Config())
    manager.set_wake_handler(lambda: app._on_mac_wake(registry, scheduler))

    before = ch.record_daemon_permissions()  # what the daemon reported before sleep
    assert before["permissions"]["input_monitoring"] == "granted"
    counter.clear()

    status["input_monitoring"] = D  # revoked while the Mac slept
    manager.handle_wake()

    assert counter == {"accessibility": 1, "input_monitoring": 1}  # the same check() calls, once each
    assert registry.monitor_calls == 1
    snapshot = ch.daemon_permission_snapshot()
    assert snapshot is not None
    assert snapshot["permissions"]["input_monitoring"] == "denied"
    assert snapshot["checked_at"] != before["checked_at"]
    assert get_state(LAST_WAKE_STATE_KEY)


def test_permission_revoked_during_sleep_shows_as_missing_after_wake(monkeypatch):
    status = {"accessibility": G, "input_monitoring": G}
    _install_permission_stubs(monkeypatch, status, {})
    registry = _FakeRegistry()
    manager = CaptureManager(Config())
    manager._running.set()  # the daemon is up (the heartbeat loop that calls handle_wake runs only then)
    manager.set_wake_handler(lambda: app._on_mac_wake(registry, SourcesScheduler(enabled=False)))
    cfg = _keyboard_cfg()
    runtime = {"status": RuntimeStatus(RuntimeState.RUNNING)}
    monkeypatch.setattr(ch, "evaluate", lambda **_kwargs: runtime["status"])
    assert ch.compute_capture_health(cfg) is None

    status["input_monitoring"] = D
    manager.handle_wake()

    findings = {f.key: f for f in ch.permission_report(cfg)}
    assert findings["input_monitoring"].granted is False
    assert findings["input_monitoring"].status == "missing"
    assert findings["input_monitoring"].checked_by == "daemon"

    runtime["status"] = RuntimeStatus(
        RuntimeState.RUNNING,
        issues=(Issue("permission.input_monitoring", "warning", "keyboard_chunk 缺少输入监控权限。", "keyboard_chunk is missing input monitoring permission", "penpal doctor"),),
    )
    health = ch.compute_capture_health(cfg)
    assert health is not None and health["code"] == RuntimeState.RUNNING.value
    assert "keyboard_chunk" in health["message"]


# ── silent watchers restarted ──────────────────────────────────────────────────

class _IdleWatcher(BaseWatcher):
    name = "fake_idle"

    def _run(self):
        while self._running.is_set():
            time.sleep(0.01)


def test_fake_wake_restarts_dead_watchers_but_not_paused_or_disabled(monkeypatch):
    manager = CaptureManager(Config())
    dead = _IdleWatcher(manager._queue)
    paused = _IdleWatcher(manager._queue)
    paused.name = "fake_paused"
    paused.pause()
    manager._watchers = {"fake_idle": dead, "fake_paused": paused}
    assert not dead.is_running()

    manager.handle_wake()
    try:
        assert dead.is_running()
        assert not paused.is_running()
    finally:
        dead.stop()


# ── sources rescanned, no duplicate rows ───────────────────────────────────────

class _Src(DataSource):
    privacy_tier = "green"

    def __init__(self, name: str) -> None:
        self.name = name
        self.liveness = "always"
        self.app_hints = ()

    def discover(self) -> list[DataSourceInstance]:
        return []

    def read(self, instance, since, until):
        return iter(())


def _raw_event_count() -> int:
    return int(get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])


def test_wake_rescan_plus_reimport_adds_no_duplicate_rows():
    reads: list[str] = []

    def fake_read_all(since, until, *, source, persist_raw_events):
        # The same upstream records every time: first read, then the post-wake rescan re-imports them.
        reads.append(source)
        for n in range(3):
            insert_raw_event(RawEvent(
                source="clipboard",
                event_type="clipboard_copy",
                ts_start="2026-10-22T09:00:0%dZ" % n,
                content_text=f"record {n}",
                content_hash=f"hash-{n}",
                external_id=f"ext-{n}",
            ))
        return iter(())

    scheduler = SourcesScheduler(
        enabled=True,
        poll_interval_sec=600,
        debounce_sec=0,
        list_sources_fn=lambda: [_Src("fake_source")],
        read_all_fn=fake_read_all,
        event_source_factory=lambda: None,
        history_allowed_fn=lambda: True,  # this test is about wake re-scan, not the onboarding gate (#32)
    )
    scheduler.start()
    try:
        deadline = time.monotonic() + 2.0
        while not reads and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        rows_after_first_read = _raw_event_count()
        assert rows_after_first_read == 3

        before_wake_reads = len(reads)
        scheduler.rescan_all(reason="mac_wake")
        deadline = time.monotonic() + 2.0
        while len(reads) == before_wake_reads and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        assert len(reads) == before_wake_reads + 1
        assert _raw_event_count() == rows_after_first_read
    finally:
        scheduler.stop()


# ── MCP capture_health liveness follows core-loop progress, not emitted events ──

@pytest.mark.parametrize("quiet_hours", [1, 24, 720])
def test_quiet_event_stream_is_healthy_when_core_loop_progresses(monkeypatch, quiet_hours):
    del quiet_hours  # event age is deliberately not part of runtime liveness
    monkeypatch.setattr(ch, "evaluate", lambda **_kwargs: RuntimeStatus(RuntimeState.RUNNING))
    assert ch.compute_capture_health(_keyboard_cfg()) is None
