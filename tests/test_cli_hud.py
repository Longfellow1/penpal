from __future__ import annotations

import signal

from click.testing import CliRunner

from penpal.cli import main
from penpal.runtime import RuntimeState, RuntimeStatus
from penpal.runtime.control import ControlResult
from penpal.runtime.evaluate import HudStatus, Issue


def test_hud_invokes_hud_runner_by_default(monkeypatch, tmp_path):
    captured = {}
    pid_path = tmp_path / "hud.pid"

    def fake_load():
        return type("Cfg", (), {"db_path_expanded": object(), "log_path_expanded": object()})()

    def fake_require_db(cfg):
        captured["db_cfg"] = cfg

    def fake_run_hud(cfg):
        captured["hud_cfg"] = cfg

    monkeypatch.setattr("penpal.cli.app.get_config", fake_load)
    monkeypatch.setattr("penpal.cli.app.require_db", fake_require_db)
    monkeypatch.setattr("penpal.cli.app.run_hud", fake_run_hud)
    monkeypatch.setattr("penpal.cli.app._find_legacy_hud_pid", lambda: None)
    monkeypatch.setattr("penpal.cli.app.get_hud_pid_path", lambda: pid_path)

    result = CliRunner().invoke(main, ["hud"])

    assert result.exit_code == 0
    assert "db_cfg" in captured
    assert "hud_cfg" in captured


def test_hud_start_opens_bundle_and_reports_pid_and_never_calls_run_hud(monkeypatch):
    relaunch_called = []
    run_hud_called = []

    monkeypatch.setattr("penpal.cli.app.active_hud_pid", lambda: 4242 if relaunch_called else None)
    monkeypatch.setattr("penpal.cli.app._find_legacy_hud_pid", lambda: None)
    monkeypatch.setattr(
        "penpal.cli.app.relaunch_hud",
        lambda: (relaunch_called.append(True) or ControlResult(True, "relaunch_hud")),
    )
    monkeypatch.setattr("penpal.cli.app.wait_for_hud_ready", lambda: True)
    monkeypatch.setattr("penpal.cli.app.run_hud", lambda cfg=None: run_hud_called.append(True))

    result = CliRunner().invoke(main, ["hud", "start"])

    assert result.exit_code == 0
    assert relaunch_called == [True]
    assert run_hud_called == []
    assert "4242" in result.output
    assert ("HUD 已启动（PID 4242）。" in result.output or "HUD started (PID 4242)." in result.output)


def test_hud_start_relaunch_error_exits_one(monkeypatch):
    monkeypatch.setattr("penpal.cli.app.active_hud_pid", lambda: None)
    monkeypatch.setattr("penpal.cli.app._find_legacy_hud_pid", lambda: None)
    monkeypatch.setattr(
        "penpal.cli.app.relaunch_hud",
        lambda: ControlResult(
            False,
            "relaunch_hud",
            error_zh="未找到安装的 Penpal.app，请运行 penpal app install。",
            error_en="Penpal.app is not installed; run penpal app install.",
        ),
    )

    result = CliRunner().invoke(main, ["hud", "start"])

    assert result.exit_code == 1
    assert "penpal app install" in result.output


def test_hud_start_not_ready_in_time_exits_one(monkeypatch):
    monkeypatch.setattr("penpal.cli.app.active_hud_pid", lambda: None)
    monkeypatch.setattr("penpal.cli.app._find_legacy_hud_pid", lambda: None)
    monkeypatch.setattr("penpal.cli.app.relaunch_hud", lambda: ControlResult(True, "relaunch_hud"))
    monkeypatch.setattr("penpal.cli.app.wait_for_hud_ready", lambda: False)

    result = CliRunner().invoke(main, ["hud", "start"])

    assert result.exit_code == 1
    assert ("未就绪" in result.output or "not ready" in result.output)
    assert "penpal doctor" in result.output


def test_hud_start_already_running_does_not_relaunch(monkeypatch):
    relaunch_called = []
    monkeypatch.setattr("penpal.cli.app.active_hud_pid", lambda: 4242)
    monkeypatch.setattr(
        "penpal.cli.app.relaunch_hud",
        lambda: (relaunch_called.append(True) or ControlResult(True, "relaunch_hud")),
    )

    result = CliRunner().invoke(main, ["hud", "start"])

    assert result.exit_code == 1
    assert relaunch_called == []
    assert "4242" in result.output
    assert "already running" in result.output.lower()


def test_hud_stop_terminates_running_instance(monkeypatch, tmp_path):
    pid_path = tmp_path / "hud.pid"
    calls = []
    state = {"running": True}

    class FakeLock:
        def __init__(self):
            self.pid_path = pid_path

        def is_running(self):
            return state["running"]

        def get_pid(self):
            return 4242 if state["running"] else None

    monkeypatch.setattr("penpal.cli.app._hud_lock", FakeLock)
    monkeypatch.setattr("penpal.cli.app._running_hud_pids", lambda: [4242])

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == signal.SIGTERM:
            state["running"] = False

    monkeypatch.setattr("penpal.cli.app.os.kill", fake_kill)
    monkeypatch.setattr("penpal.cli.app.pid_is_running", lambda pid: state["running"])

    result = CliRunner().invoke(main, ["hud", "stop"])

    assert result.exit_code == 0
    assert (4242, signal.SIGTERM) in calls
    assert "stopped" in result.output.lower()


def test_hud_stop_finds_the_app_hud_by_its_record_before_removing_it(monkeypatch, tmp_path):
    record = {"present": True}
    calls = []
    state = {"running": True}

    class FakeLock:
        def is_running(self):
            return state["running"]

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        state["running"] = False

    monkeypatch.setattr("penpal.cli.app._hud_lock", FakeLock)
    monkeypatch.setattr("penpal.cli.app.active_hud_pid", lambda: 4242 if record["present"] else None)
    monkeypatch.setattr("penpal.cli.app.find_legacy_hud_pids", lambda: [])
    monkeypatch.setattr("penpal.foundation.hud_record.remove_hud_record", lambda: record.update(present=False))
    monkeypatch.setattr("penpal.cli.app.os.kill", fake_kill)
    monkeypatch.setattr("penpal.cli.app.pid_is_running", lambda pid: state["running"])

    result = CliRunner().invoke(main, ["hud", "stop"])

    assert result.exit_code == 0, result.output
    assert calls == [(4242, signal.SIGTERM)]
    assert record["present"] is False


def test_hud_status_reports_running_instance(monkeypatch, tmp_path):
    status = HudStatus(4242, "g", True, True, True, "2026-10-25T12:00:00+00:00", "2026-10-25T12:00:01+00:00", 1.0)
    monkeypatch.setattr("penpal.cli.app.evaluate", lambda: RuntimeStatus(RuntimeState.RUNNING, hud=status))

    result = CliRunner().invoke(main, ["hud", "status", "--plain"])

    assert result.exit_code == 0
    assert "running=True" in result.output
    assert "pid=4242" in result.output
    assert "status=running" in result.output and "status_item_visible=True" in result.output


def test_hud_stop_falls_back_to_legacy_process_lookup(monkeypatch, tmp_path):
    pid_path = tmp_path / "hud.pid"
    calls = []

    monkeypatch.setattr("penpal.cli.app.get_hud_pid_path", lambda: pid_path)
    monkeypatch.setattr("penpal.cli.app._running_hud_pids", lambda: [5151])

    def fake_kill(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr("penpal.cli.app.os.kill", fake_kill)
    monkeypatch.setattr("penpal.cli.app.pid_is_running", lambda pid: False)

    result = CliRunner().invoke(main, ["hud", "stop"])

    assert result.exit_code == 0
    assert (5151, signal.SIGTERM) in calls


def test_hud_status_does_not_report_normal_when_status_item_is_hidden(monkeypatch):
    status = HudStatus(5151, "g", True, True, False, None, "2026-10-25T12:00:00+00:00", 1.0)
    issue = Issue("hud", "error", "菜单栏图标被系统隐藏：系统设置 → 菜单栏 → 打开 Penpal。", "hidden")
    monkeypatch.setattr(
        "penpal.cli.app.evaluate",
        lambda: RuntimeStatus(RuntimeState.RUNNING, issues=(issue,), hud=status),
    )

    result = CliRunner().invoke(main, ["hud", "status", "--plain"])

    assert result.exit_code == 0
    assert "running=True" in result.output
    assert "status=issue" in result.output
    assert "status_item_visible=False" in result.output
    assert "菜单栏图标被系统隐藏" in result.output


def test_find_legacy_hud_pids_detects_variants_and_excludes_subcommands_and_valid_current():
    from subprocess import CompletedProcess
    from penpal.foundation.hud_record import HudRecord
    from penpal.foundation.runtime_identity import RuntimeIdentity
    from penpal.runtime.control import find_legacy_hud_pids

    commands = {
        101: "/opt/homebrew/bin/penpal hud",
        102: "/opt/homebrew/bin/keypulse hud",
        103: "/Users/alice/.local/venv/bin/python -m penpal.cli hud",
        104: "/opt/homebrew/bin/penpal hud status",
        105: "/opt/homebrew/bin/penpal hud stop",
        106: "/opt/homebrew/bin/penpal hud close",
        107: "/opt/homebrew/bin/penpal hud start",
        108: "/opt/homebrew/bin/penpal hud",
        109: "/opt/homebrew/bin/penpal hud",
        110: "/opt/homebrew/bin/penpal hud",
    }

    def fake_runner(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return CompletedProcess(args=cmd, returncode=0, stdout="\n".join(str(p) for p in commands.keys()))
        if cmd[0] == "ps":
            pid = int(cmd[2])
            return CompletedProcess(args=cmd, returncode=0, stdout=commands.get(pid, ""))
        raise AssertionError(f"Unexpected command: {cmd}")

    ident = RuntimeIdentity("code", "0.3.0", "cfg", 34, 34)
    current_record = HudRecord(
        pid=109, process_start_time="t109", generation="g1", identity=ident,
        last_model_built_at=None, last_main_thread_tick_at=None, status_item_created=True, status_item_visible=True,
    )
    start_times = {
        101: "t101",
        102: "t102",
        103: "t103",
        108: "t108",
        109: "t109",
        110: "t_reused",
    }

    pids = find_legacy_hud_pids(
        runner=fake_runner,
        hud_record_reader=lambda: current_record,
        process_start_reader=lambda pid: start_times.get(pid),
        current_pid=108,
    )

    assert pids == [101, 102, 103, 110]


def test_start_hud_refuses_when_legacy_hud_running(monkeypatch):
    class FakeLock:
        def acquire(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr("penpal.cli.app.get_config", lambda: None)
    monkeypatch.setattr("penpal.cli.app._hud_lock", FakeLock)
    monkeypatch.setattr("penpal.cli.app.find_legacy_hud_pids", lambda: [9999])

    result = CliRunner().invoke(main, ["hud", "start"])
    assert result.exit_code == 1
    assert "9999" in result.output
    assert "already running" in result.output.lower()



def test_running_hud_pids_includes_the_current_hud_from_its_record(monkeypatch):
    # The current HUD's command line is "<bundle>/Contents/MacOS/Penpal", which the legacy finder never matches.
    from penpal.cli import app as app_cli

    monkeypatch.setattr("penpal.cli.app.active_hud_pid", lambda: 4242)
    monkeypatch.setattr("penpal.cli.app.find_legacy_hud_pids", lambda: [5151, 4242])

    assert app_cli._running_hud_pids() == [4242, 5151]


def test_hud_stop_reports_stopped_only_after_pid_is_gone(monkeypatch):
    class FakeLock:
        def is_running(self):
            return False

    monkeypatch.setattr("penpal.cli.app._hud_lock", FakeLock)
    monkeypatch.setattr("penpal.cli.app._running_hud_pids", lambda: [7777])
    monkeypatch.setattr("penpal.cli.app.os.kill", lambda pid, sig: None)

    alive_states = [True, True, False]

    def fake_pid_is_running(pid):
        assert pid == 7777
        if alive_states:
            return alive_states.pop(0)
        return False

    sleep_calls = []
    monkeypatch.setattr("penpal.cli.app.pid_is_running", fake_pid_is_running)
    monkeypatch.setattr("penpal.cli.app.time.sleep", lambda s: sleep_calls.append(s))

    result = CliRunner().invoke(main, ["hud", "stop"])

    assert result.exit_code == 0
    assert "hud stopped" in result.output.lower()
    assert len(sleep_calls) == 3


def test_hud_stop_reports_fallback_when_pid_does_not_exit(monkeypatch):
    class FakeLock:
        def is_running(self):
            return False

    monkeypatch.setattr("penpal.cli.app._hud_lock", FakeLock)
    monkeypatch.setattr("penpal.cli.app._running_hud_pids", lambda: [7777])
    monkeypatch.setattr("penpal.cli.app.os.kill", lambda pid, sig: None)

    sleep_calls = []
    monkeypatch.setattr("penpal.cli.app.pid_is_running", lambda pid: True)
    monkeypatch.setattr("penpal.cli.app.time.sleep", lambda s: sleep_calls.append(s))

    result = CliRunner().invoke(main, ["hud", "stop"])

    assert result.exit_code == 0
    assert len(sleep_calls) == 50
    assert "stop signal sent" in result.output.lower()

