from __future__ import annotations

import os
import plistlib
import signal
import shutil
import subprocess
import sys
import time
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from penpal.foundation.runtime_plist import launchd_jobs
from penpal.health import self_heal as watchdog
from penpal.runtime import RuntimeState, evaluate
from penpal.runtime import control
from penpal.utils.paths import get_launchd_daemon_plist_path

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def _environment(home: Path, executable: Path) -> dict[str, str]:
    (home / ".penpal").mkdir(parents=True, exist_ok=True)
    (home / ".penpal" / "config.toml").write_text(
        """[app]
db_path = \"~/.penpal/penpal.db\"
log_path = \"~/.penpal/penpal.log\"
flush_interval_sec = 1

[watchers]
window = false
idle = false
clipboard = false
manual = false
keyboard_chunk = false
browser = false
browser_url = false
ax_text = false
ocr = false

[sources.scheduler]
enabled = false
""",
        encoding="utf-8",
    )
    return {
        **os.environ,
        "HOME": str(home),
        "PENPAL_ISOLATED": "1",
        "PENPAL_RUNTIME_DISABLE_LAUNCHD_PROBE": "1",
        "PENPAL_RUNTIME_RECORD_INTERVAL_SEC": "0.1",
        "PENPAL_EXECUTABLE": str(executable),
        "PYTHONPATH": str(ROOT),
    }


def _configure_parent(monkeypatch, home: Path, executable: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    monkeypatch.setenv("PENPAL_RUNTIME_DISABLE_LAUNCHD_PROBE", "1")
    monkeypatch.setenv("PENPAL_RUNTIME_RECORD_INTERVAL_SEC", "0.1")
    monkeypatch.setenv("PENPAL_EXECUTABLE", str(executable))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))


class _ProcessHarness:
    def __init__(self, home: Path, executable: Path):
        self.home = home
        self.executable = executable
        self.env = _environment(home, executable)
        self.processes: list[subprocess.Popen] = []
        _MANAGED["harness"] = self
        self.spawn()

    def spawn(self) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", "from penpal.cli import main; main()", "serve"],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process

    @property
    def current(self) -> subprocess.Popen:
        return self.processes[-1]

    def kill_current(self, sig: int = signal.SIGKILL) -> None:
        if self.current.poll() is None:
            self.current.send_signal(sig)
            self.current.wait(timeout=5)

    def stop(self) -> None:
        _MANAGED["harness"] = None
        for process in reversed(self.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()


# A cap, not a sleep: passing runs return as soon as the condition holds; cold CI runners need the headroom.
def _wait_for(predicate, *, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    return predicate()


def _running_status():
    status = _evaluate_real()
    return status if status.state == RuntimeState.RUNNING else None


# The process under test stands in for the launchd-managed job: the watchdog only acts on managed jobs.
_MANAGED: dict[str, "_ProcessHarness | None"] = {"harness": None}


def _fake_launchd_probe() -> dict:
    harness = _MANAGED["harness"]
    if harness is None:
        return {"loaded": False}
    alive = harness.current.poll() is None
    return {"loaded": True, "pid": harness.current.pid if alive else None}


def _evaluate_real():
    return evaluate(paused_reader=lambda: False, launchd_probe=_fake_launchd_probe)


def _status_for(state: RuntimeState):
    status = _evaluate_real()
    return status if status.state == state else None


def _diagnostic(harness: _ProcessHarness) -> str:
    log_path = harness.home / ".penpal" / "penpal.log"
    log = log_path.read_text(encoding="utf-8")[-2500:] if log_path.exists() else "no daemon log"
    stderr = ""
    if harness.current.poll() is not None and harness.current.stderr is not None:
        stderr = harness.current.stderr.read()[-1200:]
    return f"process_exit={harness.current.poll()}\nlog={log}\nstderr={stderr}"


def _write_daemon_plist(monkeypatch, executable: Path) -> Path:
    monkeypatch.setenv("PENPAL_EXECUTABLE", str(executable))
    path = get_launchd_daemon_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = launchd_jobs(executable)["com.penpal.daemon"]
    path.write_bytes(plistlib.dumps(payload, sort_keys=False))
    return path


@pytest.mark.parametrize("change", ["code", "config"])
def test_daemon_identity_change_flushes_and_exits_for_launchd_reload(tmp_path, change):
    home = tmp_path / "home"
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    env = _environment(home, executable)
    env["PENPAL_TEST_IDENTITY_INTERVAL_SEC"] = "0.2"
    fingerprint_root = tmp_path / "installed" / "penpal"
    shutil.copytree(ROOT / "penpal", fingerprint_root)
    env["PENPAL_TEST_FINGERPRINT_ROOT"] = str(fingerprint_root)

    process = subprocess.Popen(
        [sys.executable, "-c", "from penpal.cli import main; main()", "serve"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        record_path = home / ".penpal" / "run" / "daemon.json"
        assert _wait_for(lambda: record_path.exists(), timeout=30), "daemon did not write its start record"
        if change == "code":
            (fingerprint_root / "upgrade_marker.py").write_text("UPGRADE = 1\n", encoding="utf-8")
        else:
            config_path = home / ".penpal" / "config.toml"
            config = config_path.read_text(encoding="utf-8")
            config_path.write_text(config.replace("flush_interval_sec = 1", "flush_interval_sec = 2"), encoding="utf-8")
        assert process.wait(timeout=10) == 75
        assert _wait_for(lambda: (home / ".penpal" / "run" / "daemon.json").stat().st_size > 0)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()


def test_daemon_plist_restarts_only_unsuccessful_exits(monkeypatch, tmp_path):
    executable = tmp_path / "opt" / "penpal" / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    payload = launchd_jobs(executable)["com.penpal.daemon"]

    assert payload["ProgramArguments"] == [str(executable), "serve"]
    assert payload["KeepAlive"] == {"SuccessfulExit": False}


def _fake_restart(harness: _ProcessHarness, calls: list[list[str]]):
    # Models real launchd: bootout stops the job, bootstrap only loads it (macOS can leave RunAtLoad as a
    # pending "speculative" spawn), and a plain kickstart starts it unless it is already running.
    loaded = {"value": True}

    def running() -> bool:
        return bool(harness.processes) and harness.current.poll() is None

    def runner(command, **_kwargs):
        calls.append(command)
        if command[1] == "kickstart" and "-k" in command:
            harness.kill_current()
            harness.spawn()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "kickstart":
            if loaded["value"] and not running():
                harness.spawn()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "bootout":
            loaded["value"] = False
            if running():
                harness.kill_current(signal.SIGTERM)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "bootstrap":
            loaded["value"] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    def probe(_label, **_kwargs):
        return {"loaded": loaded["value"]}

    def restart(reason):
        return control.restart(reason, runner=runner, probe=probe, evaluator=_evaluate_real)

    return restart, calls


@pytest.mark.integration
def test_r2_sigstop_watchdog_restarts_into_new_generation_with_progress(monkeypatch, tmp_path):
    evaluator_module = importlib.import_module("penpal.runtime.evaluate")

    monkeypatch.setattr(evaluator_module, "HUNG_AFTER_SEC", 3)
    monkeypatch.setattr(evaluator_module, "STARTING_GRACE_SEC", 0)
    home = tmp_path / "home"
    executable = tmp_path / "bin" / "penpal"
    _configure_parent(monkeypatch, home, executable)
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    harness = _ProcessHarness(home, executable)
    try:
        _write_daemon_plist(monkeypatch, executable)
        initial_status = _wait_for(_running_status)
        assert initial_status, _diagnostic(harness)
        initial = initial_status.generation

        harness.current.send_signal(signal.SIGSTOP)
        hung = _wait_for(lambda: _status_for(RuntimeState.HUNG))
        assert hung is not None, f"state_after_sigstop={_evaluate_real()!r}"

        calls: list[list[str]] = []
        restart_control, _ = _fake_restart(harness, calls)
        state_path = home / ".penpal" / "run" / "watchdog.json"
        observed = watchdog.run_watchdog(hung, now=BASE, restart_control=restart_control, state_path=state_path)
        assert observed["action"] == "observe"
        recovered = watchdog.run_watchdog(
            hung, now=BASE + timedelta(seconds=1), restart_control=restart_control, state_path=state_path,
        )

        assert recovered["action"] == "restart"
        assert calls[0][1:3] == ["kickstart", "-k"]
        running = _wait_for(_running_status)
        assert running is not None, _diagnostic(harness)
        assert running.generation != initial
        assert running.progress["core_loop"].seq > 0
    finally:
        harness.stop()


@pytest.mark.integration
def test_r3_sigkill_restarts_and_budget_exhaustion_stops_further_processes(monkeypatch, tmp_path):
    home = tmp_path / "home"
    executable = tmp_path / "bin" / "penpal"
    _configure_parent(monkeypatch, home, executable)
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    harness = _ProcessHarness(home, executable)
    try:
        _write_daemon_plist(monkeypatch, executable)
        initial_status = _wait_for(_running_status)
        assert initial_status, _diagnostic(harness)
        initial = initial_status.generation
        restart_control, calls = _fake_restart(harness, [])
        state_path = home / ".penpal" / "run" / "watchdog.json"

        for attempt in range(watchdog.RESTART_BUDGET_PER_HOUR):
            harness.kill_current()
            dead = _wait_for(lambda: _status_for(RuntimeState.NOT_RUNNING))
            assert dead is not None
            at = BASE + timedelta(minutes=16 * attempt)
            first = watchdog.run_watchdog(dead, now=at, restart_control=restart_control, state_path=state_path)
            assert first["action"] == "observe"
            result = watchdog.run_watchdog(
                dead, now=at + timedelta(seconds=1), restart_control=restart_control, state_path=state_path,
            )
            assert result["action"] == "restart"
            alive = _wait_for(_running_status)
            assert alive is not None, _diagnostic(harness)
            assert alive.generation != dead.generation
            assert alive.progress["core_loop"].seq > 0

        harness.kill_current()
        dead = _wait_for(lambda: _status_for(RuntimeState.NOT_RUNNING))
        assert dead is not None
        exhausted = watchdog.run_watchdog(
            dead, now=BASE + timedelta(minutes=48), restart_control=restart_control, state_path=state_path,
        )
        assert exhausted["budget_exhausted"] is True
        assert exhausted["reason"] == "restart_budget_exhausted"
        assert len(calls) == watchdog.RESTART_BUDGET_PER_HOUR
        assert len(harness.processes) == watchdog.RESTART_BUDGET_PER_HOUR + 1
    finally:
        harness.stop()


@pytest.mark.integration
def test_serve_rejects_second_real_process_with_distinct_exit_code(monkeypatch, tmp_path):
    home = tmp_path / "home"
    executable = tmp_path / "bin" / "penpal"
    _configure_parent(monkeypatch, home, executable)
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    harness = _ProcessHarness(home, executable)
    try:
        running = _wait_for(_running_status)
        assert running is not None, _diagnostic(harness)
        second = subprocess.run(
            [sys.executable, "-c", "from penpal.cli import main; main()", "serve"],
            cwd=ROOT,
            env=harness.env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert second.returncode == 73
        assert "already running" in second.stderr.lower() or "已经运行" in second.stderr
    finally:
        harness.stop()
