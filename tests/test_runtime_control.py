from __future__ import annotations

import plistlib
import inspect
import subprocess
from pathlib import Path

import pytest

from penpal.foundation.runtime_record import Progress
from penpal.runtime import RuntimeState, RuntimeStatus
from penpal.runtime import control


def _status(generation: str, seq: int, state: RuntimeState = RuntimeState.RUNNING) -> RuntimeStatus:
    return RuntimeStatus(state, progress={"core_loop": Progress(seq, "2026-10-25T12:00:00+00:00")}, generation=generation)


class _Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.value += duration


def _setup(monkeypatch, tmp_path, *, matching: bool):
    path = tmp_path / "com.penpal.daemon.plist"
    desired = {"Label": "com.penpal.daemon", "ProgramArguments": ["/tmp/penpal", "serve"]}
    monkeypatch.setattr(control, "get_launchd_plist_path", lambda _label: path)
    monkeypatch.setattr(control, "launchd_jobs", lambda: {control.DAEMON_LABEL: desired})
    payload = desired if matching else {**desired, "ProgramArguments": ["/tmp/old", "serve"]}
    path.write_bytes(plistlib.dumps(payload))
    return path, desired


def _completed(command, code=0, detail=""):
    return subprocess.CompletedProcess(command, code, "", detail)


def test_restart_kickstarts_matching_loaded_job_and_requires_new_progress(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, matching=True)
    calls = []
    statuses = iter((_status("generation-old", 8), _status("generation-new", 1)))

    def runner(command, **_kwargs):
        calls.append(command)
        return _completed(command)

    result = control.restart(
        "unit", runner=runner, probe=lambda _label, **_kwargs: {"loaded": True},
        evaluator=lambda: next(statuses), sleep=lambda _duration: None,
    )

    assert result.ok is True
    assert result.action == "kickstart"
    assert result.generation == "generation-new"
    assert calls == [["launchctl", "kickstart", "-k", control._target("com.penpal.daemon")]]


def test_restart_boots_out_then_retries_bootstrap_when_plist_differs(monkeypatch, tmp_path):
    path, _desired = _setup(monkeypatch, tmp_path, matching=False)
    state = {"loaded": True, "bootstrap_attempts": 0}
    calls = []
    clock = _Clock()
    statuses = iter((_status("generation-old", 14), _status("generation-new", 1)))

    def probe(_label, **_kwargs):
        return {"loaded": state["loaded"]}

    def runner(command, **_kwargs):
        calls.append(command)
        if command[1] == "bootout":
            state["loaded"] = False
            return _completed(command)
        if command[1] == "bootstrap":
            state["bootstrap_attempts"] += 1
            if state["bootstrap_attempts"] < 3:
                return _completed(command, 5, "job still tearing down")
            state["loaded"] = True
            return _completed(command)
        if command[1] == "kickstart":
            return _completed(command)
        raise AssertionError(command)

    result = control.restart(
        "upgrade", runner=runner, probe=probe, evaluator=lambda: next(statuses),
        sleep=clock.sleep, monotonic=clock.monotonic,
    )

    assert result.ok is True
    assert result.action == "bootout_bootstrap"
    assert result.generation == "generation-new"
    assert calls[0] == ["launchctl", "bootout", control._target("com.penpal.daemon")]
    assert [call for call in calls if call[1] == "bootstrap"] == [
        ["launchctl", "bootstrap", control._domain_target(), str(path)],
    ] * 3
    # bootstrap alone can leave the job pending; it is started explicitly, without -k.
    assert calls[-1] == ["launchctl", "kickstart", control._target("com.penpal.daemon")]
    assert clock.sleeps == [0.25, 0.5]


def test_restart_teardown_wait_is_bounded_and_does_not_bootstrap(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, matching=True)
    state = {"loaded": True}
    calls = []
    clock = _Clock()

    def runner(command, **_kwargs):
        calls.append(command)
        return _completed(command)

    result = control.restart(
        "plist-change", runner=runner, probe=lambda _label, **_kwargs: {"loaded": state["loaded"]},
        evaluator=lambda: _status("generation-old", 3), sleep=clock.sleep,
        monotonic=clock.monotonic, force_reload=True,
    )

    assert result.ok is False
    assert result.action == "bootout"
    assert "超时" in result.error_zh
    assert "timed out" in result.error_en.lower()
    assert clock.value <= control.TEARDOWN_TIMEOUT_SEC + control.POLL_INTERVAL_SEC
    assert all(command[1] != "bootstrap" for command in calls)


def test_restart_reports_bootstrap_failure_after_three_attempts(monkeypatch, tmp_path):
    path, _desired = _setup(monkeypatch, tmp_path, matching=False)
    calls = []
    clock = _Clock()

    def runner(command, **_kwargs):
        calls.append(command)
        return _completed(command, 5, "bootstrap denied")

    result = control.restart(
        "manual", runner=runner, probe=lambda _label, **_kwargs: {"loaded": False},
        evaluator=lambda: _status("generation-old", 4), sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    bootstraps = [command for command in calls if command[1] == "bootstrap"]
    assert result.ok is False
    assert result.action == "bootstrap"
    assert len(bootstraps) == control.BOOTSTRAP_ATTEMPTS
    assert str(path) in bootstraps[0]
    assert result.error_zh and result.error_en
    assert "bootstrap denied" in result.error_en


def test_start_refuses_to_bootstrap_over_an_unmanaged_live_daemon(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, matching=True)
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return _completed(command)

    live_unmanaged = RuntimeStatus(
        RuntimeState.RUNNING,
        progress={"core_loop": _status("generation-live", 1).progress["core_loop"]},
        pid=4242,
        generation="generation-live",
        managed=False,
    )
    result = control.start(
        runner=runner,
        probe=lambda _label, **_kwargs: {"loaded": False},
        evaluator=lambda: live_unmanaged,
    )

    assert result.ok is False
    assert result.action == "not_managed"
    assert result.generation == "generation-live"
    assert result.error_zh and result.error_en
    assert calls == []


def test_load_job_starts_the_job_unless_it_is_only_a_login_item(tmp_path):
    cases = (
        (False, True, ["bootstrap", "kickstart"]),
        (False, False, ["bootstrap"]),
        # Loaded but left pending by an earlier bootstrap: its StartInterval never fires until it is started.
        (True, True, ["kickstart"]),
        (True, False, []),
    )
    for loaded, spawn, expected in cases:
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return _completed(command)

        assert control.load_job(
            "com.penpal.healthcheck", tmp_path / "com.penpal.healthcheck.plist",
            runner=runner, probe=lambda _label, **_kwargs: {"loaded": loaded}, spawn=spawn,
        )
        assert [command[1] for command in calls] == expected


def test_load_job_replace_reloads_job_when_already_loaded(tmp_path):
    path = tmp_path / "com.penpal.healthcheck.plist"
    path.write_text("plist")
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return _completed(command)

    # replace=True on loaded job: issues bootout, then bootstrap, then kickstart
    assert control.load_job(
        "com.penpal.healthcheck", path, runner=runner,
        probe=lambda _label, **_kwargs: {"loaded": True},
        spawn=True, replace=True,
    )
    assert [command[1] for command in calls] == ["bootout", "bootstrap", "kickstart"]

    # replace=False on loaded job: issues only kickstart (existing behavior)
    calls.clear()
    assert control.load_job(
        "com.penpal.healthcheck", path, runner=runner,
        probe=lambda _label, **_kwargs: {"loaded": True},
        spawn=True, replace=False,
    )
    assert [command[1] for command in calls] == ["kickstart"]


def test_run_returns_failure_on_timeout_expired():
    def runner(command, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=15.0)

    ok, message = control._run(["launchctl", "kickstart", "gui/501/com.penpal.healthcheck"], runner=runner)
    assert ok is False
    assert "timed out" in message.lower()
    assert message.endswith("launchctl kickstart gui/501/com.penpal.healthcheck")


def test_default_runner_bounds_launchctl_call(monkeypatch):
    captured_kwargs = {}

    def fake_run(command, **kwargs):
        captured_kwargs.update(kwargs)
        return _completed(command)

    monkeypatch.delenv("PENPAL_ISOLATED", raising=False)
    monkeypatch.setattr(control.sys, "platform", "darwin")
    monkeypatch.setattr(control.subprocess, "run", fake_run)

    control._default_runner(["launchctl", "print", "gui/501/com.penpal.daemon"])
    assert captured_kwargs.get("timeout") == control.LAUNCHCTL_TIMEOUT_SEC


def test_probe_label_bounds_call_and_handles_timeout(monkeypatch):
    monkeypatch.delenv("PENPAL_RUNTIME_DISABLE_LAUNCHD_PROBE", raising=False)
    captured = {}

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=15.0)

    result = control.probe_label("com.penpal.daemon", runner=timeout_runner)
    assert result == {"loaded": False, "pid": None, "last_exit": None}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return _completed(command)

    # The default runner carries the bound, so every caller of probe_label gets it.
    from penpal.foundation import runtime_launchd

    monkeypatch.setattr(runtime_launchd.subprocess, "run", fake_run)
    runtime_launchd.run_launchctl(["launchctl", "print", "gui/501/com.penpal.daemon"])
    assert captured.get("timeout") == control.LAUNCHCTL_TIMEOUT_SEC
    assert inspect.signature(control.probe_label).parameters["runner"].default is runtime_launchd.run_launchctl


def test_stop_legacy_huds_terminates_then_kills_a_hud_that_ignores_sigterm():
    import signal

    signals: list[tuple[int, int]] = []
    clock = iter(range(100))

    stopped = control.stop_legacy_huds(
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="41\n" if command[0] == "pgrep" else "/usr/local/bin/penpal hud\n", stderr=""
        ),
        killer=lambda pid, sig: signals.append((pid, sig)),
        is_running=lambda pid: True,
        active_pid_finder=lambda: None,
        current_pid=1,
        sleep=lambda _seconds: None,
        monotonic=lambda: float(next(clock)),
    )

    assert stopped == [41]
    assert signals == [(41, signal.SIGTERM), (41, signal.SIGKILL)]


def test_find_legacy_hud_pids_through_the_default_process_runner(monkeypatch):
    # The real runner owns the subprocess options; a caller passing them again once raised TypeError, which a broad
    # except turned into "no legacy HUD" (caught by the upgrade drill, not by fake runners).
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = "41\n" if command[0] == "pgrep" else "/usr/local/bin/penpal hud\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.delenv("PENPAL_ISOLATED", raising=False)
    monkeypatch.setattr(control.sys, "platform", "darwin")
    monkeypatch.setattr(control.subprocess, "run", fake_run)

    assert control.find_legacy_hud_pids(active_pid_finder=lambda: None, current_pid=1) == [41]
    assert calls == [["pgrep", "-f", "hud"], ["ps", "-p", "41", "-o", "command="]]
