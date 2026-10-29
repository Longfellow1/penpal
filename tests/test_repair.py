"""Tests for penpal.services.repair and penpal doctor --fix."""
from __future__ import annotations

import types
from datetime import datetime, timezone
import pytest
from click.testing import CliRunner

from penpal import cli
from penpal.runtime.control import ControlResult
from penpal.runtime.evaluate import Issue, RuntimeState, RuntimeStatus
from penpal.services.repair import RepairResult, is_fixable_issue, repair


class FakeControl:
    def __init__(self, restart_ok: bool = True, relaunch_ok: bool = True):
        self.restart_ok = restart_ok
        self.relaunch_ok = relaunch_ok
        self.restarts = []
        self.relaunches = 0
        self.hud_restarts = 0
        self.resumes = 0

    def restart(self, reason: str = "repair") -> ControlResult:
        self.restarts.append(reason)
        if self.restart_ok:
            return ControlResult(True, "restart", generation="gen-new")
        return ControlResult(False, "restart", error_zh="重启失败", error_en="restart failed")

    def relaunch_hud(self) -> ControlResult:
        self.relaunches += 1
        if self.relaunch_ok:
            return ControlResult(True, "relaunch_hud")
        return ControlResult(False, "relaunch_hud", error_zh="启动 HUD 失败", error_en="relaunch failed")

    def restart_hud(self) -> ControlResult:
        self.hud_restarts += 1
        if self.relaunch_ok:
            return ControlResult(True, "restart_hud")
        return ControlResult(False, "restart_hud", error_zh="启动 HUD 失败", error_en="relaunch failed")

    def resume(self) -> None:
        self.resumes += 1


def test_repair_daemon_not_running():
    control = FakeControl(restart_ok=True)
    res = repair("daemon.not_running", control=control)
    assert res.attempted and res.fixed
    assert res.issue_code == "daemon.not_running"
    assert "已成功重启" in res.message_zh
    assert control.restarts == ["repair"]


def test_repair_daemon_hung():
    control = FakeControl(restart_ok=True)
    res = repair("daemon.hung", control=control)
    assert res.attempted and res.fixed
    assert "已成功重启" in res.message_zh


def test_repair_daemon_outdated():
    control = FakeControl(restart_ok=True)
    res = repair("daemon.outdated", control=control)
    assert res.attempted and res.fixed
    assert "已成功重启" in res.message_zh


def test_repair_daemon_restart_failure():
    control = FakeControl(restart_ok=False)
    res = repair("daemon.hung", control=control)
    assert res.attempted and not res.fixed
    assert "重启失败" in res.message_zh


def test_repair_paused():
    control = FakeControl()
    res = repair("daemon.paused", control=control)
    assert res.attempted and res.fixed
    assert control.resumes == 1
    assert "已成功恢复" in res.message_zh


def test_repair_hud_dead():
    control = FakeControl(relaunch_ok=True)
    res = repair("hud.dead", control=control)
    assert res.attempted and res.fixed
    assert control.hud_restarts == 1
    assert "HUD 已重新启动" in res.message_zh


def test_repair_hud_hung():
    control = FakeControl(relaunch_ok=True)
    res = repair("hud.hung", control=control)
    assert res.attempted and res.fixed
    assert control.hud_restarts == 1


def test_repair_hud_relaunch_failure():
    control = FakeControl(relaunch_ok=False)
    res = repair("hud.hung", control=control)
    assert res.attempted and not res.fixed
    assert "启动 HUD 失败" in res.message_zh


def test_repair_hud_icon_hidden():
    opened = []
    res = repair("hud.icon_hidden", opener=opened.append)
    assert res.attempted and res.fixed
    assert len(opened) == 1
    assert "ControlCenter" in opened[0]
    assert "已打开系统设置「控制中心」" in res.message_zh


def test_repair_permission_issues():
    opened = []
    for code, expected_fragment in [
        ("permission.accessibility.missing_permission", "Privacy_Accessibility"),
        ("permission.input_monitoring.missing_permission", "Privacy_ListenEvent"),
        ("permission.screen_recording.missing_permission", "Privacy_ScreenCapture"),
    ]:
        opened.clear()
        res = repair(code, opener=opened.append)
        assert res.attempted and res.fixed, code
        assert len(opened) == 1 and expected_fragment in opened[0], (code, opened)
        assert "已打开系统设置对应权限面板" in res.message_zh


def test_repair_from_issue_object():
    control = FakeControl()
    issue = Issue("daemon", "error", "后台服务核心循环没有推进。", "Core loop hung", "penpal restart", code="daemon.hung")
    res = repair(issue, control=control)
    assert res.attempted and res.fixed
    assert control.restarts == ["repair"]


def test_repair_unfixable_issue():
    issue = Issue("storage", "error", "磁盘已满，采集暂停写入。", "Disk full", code="storage.storage_error")
    res = repair(issue)
    assert not res.attempted and not res.fixed
    assert "需要手动处理" in res.message_zh
    assert res.guidance == "磁盘已满，采集暂停写入。"


def test_is_fixable_issue():
    assert is_fixable_issue("daemon.not_running")
    assert is_fixable_issue("daemon.hung")
    assert is_fixable_issue("daemon.outdated")
    assert is_fixable_issue("daemon.paused")
    assert is_fixable_issue("hud.dead")
    assert is_fixable_issue("hud.hung")
    assert is_fixable_issue("hud.icon_hidden")
    assert is_fixable_issue("permission.accessibility.missing_permission")
    assert not is_fixable_issue("not_running")
    assert not is_fixable_issue("storage.storage_error")
    assert not is_fixable_issue("unknown_issue")


def test_doctor_fix_before_after_summary(monkeypatch):
    states = [
        RuntimeStatus(RuntimeState.HUNG, issues=(Issue("daemon", "error", "后台无响应", "Hung", code="daemon.hung"),)),
        RuntimeStatus(RuntimeState.RUNNING, issues=()),
    ]
    calls = []

    def fake_evaluate(*_args, **_kwargs):
        if calls:
            return states[1]
        calls.append(1)
        return states[0]

    control = FakeControl(restart_ok=True)
    monkeypatch.setattr("penpal.cli.doctor.evaluate", fake_evaluate)
    monkeypatch.setattr("penpal.services.repair.default_restart", control.restart)

    # Plain output test
    res = CliRunner().invoke(cli.main, ["doctor", "--fix", "--plain"])
    assert res.exit_code == 0
    assert "before: state=hung issues=1" in res.output
    assert "repair daemon.hung: FIXED" in res.output
    assert "after: state=running issues=0" in res.output

    # JSON output test
    calls.clear()
    res_json = CliRunner().invoke(cli.main, ["doctor", "--fix", "--json"])
    assert res_json.exit_code == 0
    import json
    data = json.loads(res_json.output)
    assert data["before"]["state"] == "hung"
    assert data["after"]["state"] == "running"
    assert len(data["repairs"]) == 1
    assert data["repairs"][0]["fixed"] is True


def test_doctor_fix_leaves_a_deliberate_pause_alone(monkeypatch):
    paused = RuntimeStatus(RuntimeState.PAUSED, issues=(Issue("daemon", "info", "采集已暂停", "Paused", code="daemon.paused"),))
    monkeypatch.setattr("penpal.cli.doctor.evaluate", lambda *_a, **_k: paused)
    resumed = []
    monkeypatch.setattr("penpal.services.repair.set_capture_paused", resumed.append)

    res = CliRunner().invoke(cli.main, ["doctor", "--fix", "--plain"])
    assert res.exit_code == 0
    assert "repair daemon.paused" not in res.output
    assert resumed == []


def test_restart_hud_kills_live_pid_before_relaunch():
    from penpal.runtime.control import restart_hud
    import signal

    signals_sent = []
    # Process stays alive until SIGKILL
    running = [True]

    def fake_killer(pid, sig):
        signals_sent.append((pid, sig))
        if sig == signal.SIGKILL:
            running[0] = False

    time_counter = [0.0]

    def fake_monotonic():
        time_counter[0] += 2.0
        return time_counter[0]

    relaunched = []

    def fake_relaunch():
        relaunched.append(True)
        return ControlResult(True, "relaunch_hud")

    res = restart_hud(
        active_pid_finder=lambda: 9999,
        killer=fake_killer,
        is_running=lambda _pid: running[0],
        sleep=lambda _s: None,
        monotonic=fake_monotonic,
        kill_timeout_sec=3.0,
        relaunch_fn=fake_relaunch,
    )
    assert res.ok
    assert (9999, signal.SIGTERM) in signals_sent
    assert (9999, signal.SIGKILL) in signals_sent
    assert relaunched == [True]


def test_repair_hud_hung_calls_restart_hud():
    class FakeControlWithRestartHud:
        def __init__(self):
            self.restart_hud_calls = 0

        def restart_hud(self) -> ControlResult:
            self.restart_hud_calls += 1
            return ControlResult(True, "restart_hud")

    ctrl = FakeControlWithRestartHud()
    res = repair("hud.hung", control=ctrl)
    assert res.attempted and res.fixed
    assert ctrl.restart_hud_calls == 1


def test_evaluate_emits_hud_dead_issue_when_crashed(monkeypatch):
    import sys
    from penpal.foundation.hud_record import HudRecord
    ev_mod = sys.modules["penpal.runtime.evaluate"]

    record = HudRecord(
        pid=7777,
        process_start_time="1000.0",
        generation="gen-dead",
        identity=ev_mod.RuntimeIdentity("fp", "0.4.0", "cfg", 1, 1),
        last_model_built_at=datetime.now(timezone.utc).isoformat(),
        last_main_thread_tick_at=datetime.now(timezone.utc).isoformat(),
        status_item_created=True,
        status_item_visible=True,
    )
    monkeypatch.setattr(ev_mod, "is_installed", lambda: True)
    monkeypatch.setattr(ev_mod, "process_start_time", lambda _pid: None)  # dead pid

    runtime = ev_mod.evaluate(
        record_reader=lambda: None,
        launchd_probe=lambda: {"loaded": False, "pid": None, "last_exit": None},
        hud_record_reader=lambda: record,
    )
    assert runtime.hud is not None
    assert runtime.hud.alive is False
    hud_issues = [i for i in runtime.issues if i.component == "hud"]
    assert len(hud_issues) == 1
    issue = hud_issues[0]
    assert issue.code == "hud.dead"
    assert issue.severity == "warning"
    assert "1 分钟" in issue.message_zh
    assert "doctor --fix" in issue.message_zh


def test_canonical_issue_codes_and_mapping_table_consistency():
    control = FakeControl(restart_ok=True, relaunch_ok=True)
    control.restart_hud = lambda: ControlResult(True, "restart_hud")

    canonical_fixables = [
        "daemon.not_running",
        "daemon.hung",
        "daemon.outdated",
        "daemon.paused",
        "hud.dead",
        "hud.hung",
        "hud.icon_hidden",
        "permission.accessibility.missing_permission",
    ]
    for code in canonical_fixables:
        assert is_fixable_issue(code), f"expected {code} to be fixable"
        res = repair(code, control=control, opener=lambda _u: None)
        assert res.attempted and res.fixed, f"expected repair({code}) to succeed"

    # Deprecated aliases must be removed
    deprecated_aliases = ["not_running", "hung", "outdated", "paused", "hud.hidden", "permission.accessibility"]
    for code in deprecated_aliases:
        assert not is_fixable_issue(code), f"alias {code} should not be recognized"
        res = repair(code, control=control, opener=lambda _u: None)
        assert not res.attempted and not res.fixed

    # Issue object without code must not be text-matched
    issue_no_code = Issue("daemon", "error", "后台无响应", "Daemon hung", "penpal restart")
    assert not is_fixable_issue(issue_no_code)
    res_no_code = repair(issue_no_code, control=control)
    assert not res_no_code.attempted



