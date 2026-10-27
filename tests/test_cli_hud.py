from __future__ import annotations

import signal

from click.testing import CliRunner

from penpal.cli import main
from penpal.runtime import RuntimeState, RuntimeStatus
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


def test_hud_start_invokes_hud_runner(monkeypatch, tmp_path):
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

    result = CliRunner().invoke(main, ["hud", "start"])

    assert result.exit_code == 0
    assert "db_cfg" in captured
    assert "hud_cfg" in captured


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

    result = CliRunner().invoke(main, ["hud", "stop"])

    assert result.exit_code == 0
    assert (4242, signal.SIGTERM) in calls
    assert "stopped" in result.output.lower()


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
