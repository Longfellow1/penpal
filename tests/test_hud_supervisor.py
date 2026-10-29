"""Tests for penpal.health.hud_supervisor and penpal.runtime.control.relaunch_hud."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from penpal.health.hud_supervisor import (
    HUD_MAIN_THREAD_STALE_SEC,
    read_supervisor_summary,
    supervise_hud,
)
from penpal.runtime.control import ControlResult, relaunch_hud
from penpal.runtime.evaluate import HudStatus, RuntimeState, RuntimeStatus

NOW = datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)


def _make_status(*, hud_alive=True, tick_age=5.0, hud_present=True):
    if not hud_present:
        return RuntimeStatus(RuntimeState.RUNNING, pid=123, hud=None)
    hud = HudStatus(
        pid=5555,
        generation="gen-1",
        alive=hud_alive,
        status_item_created=True,
        status_item_visible=True,
        last_model_built_at=NOW.isoformat(),
        last_main_thread_tick_at=NOW.isoformat(),
        main_thread_tick_age_sec=tick_age,
    )
    return RuntimeStatus(RuntimeState.RUNNING, pid=123, hud=hud)


def test_supervisor_deliberate_quit_no_hud_record(tmp_path):
    state_file = tmp_path / "hud_supervisor.json"
    status = _make_status(hud_present=False)
    relaunch_called = []

    res = supervise_hud(
        status,
        now=NOW,
        relaunch_control=lambda: (relaunch_called.append(1) or ControlResult(True, "relaunch")),
        state_path=state_file,
    )
    assert res["action"] == "none"
    assert res["reason"] == "no_hud_record"
    assert relaunch_called == []


def test_hud_supervisor_state_foundation(tmp_path):
    import json
    from penpal.foundation.hud_supervisor_state import (
        read_supervisor_summary as foundation_summary,
        supervisor_state_path as foundation_state_path,
    )

    state_file = tmp_path / "hud_supervisor.json"
    data = {
        "restart_times": [
            (NOW - timedelta(minutes=10)).isoformat(),
            (NOW - timedelta(hours=2)).isoformat(),  # out of window
        ],
    }
    state_file.write_text(json.dumps(data), encoding="utf-8")

    zh, en = foundation_summary(state_file, now=NOW)
    assert "已自动重开 1 次" in zh
    assert "automatically relaunched 1 time(s)" in en
    assert foundation_state_path().name == "hud_supervisor.json"



def test_supervisor_dead_triggers_relaunch(tmp_path):
    state_file = tmp_path / "hud_supervisor.json"
    status = _make_status(hud_alive=False)
    relaunch_called = []

    res = supervise_hud(
        status,
        now=NOW,
        relaunch_control=lambda: (relaunch_called.append(1) or ControlResult(True, "relaunch")),
        state_path=state_file,
    )
    assert res["action"] == "relaunch"
    assert res["reason"] == "dead"
    assert len(relaunch_called) == 1
    assert res["restarts_in_window"] == 1


def test_supervisor_hung_once_observes_only(tmp_path):
    state_file = tmp_path / "hud_supervisor.json"
    status = _make_status(hud_alive=True, tick_age=HUD_MAIN_THREAD_STALE_SEC + 10)
    relaunch_called = []

    res = supervise_hud(
        status,
        now=NOW,
        relaunch_control=lambda: (relaunch_called.append(1) or ControlResult(True, "relaunch")),
        state_path=state_file,
    )
    assert res["action"] == "observe"
    assert res["reason"] == "consecutive_hung_threshold"
    assert res["consecutive_hung"] == 1
    assert relaunch_called == []


def test_supervisor_hung_twice_kills_and_relaunches(tmp_path):
    state_file = tmp_path / "hud_supervisor.json"
    status = _make_status(hud_alive=True, tick_age=HUD_MAIN_THREAD_STALE_SEC + 10)
    relaunch_called = []
    killed = []

    # Check 1: hung once
    supervise_hud(
        status,
        now=NOW,
        relaunch_control=lambda: (relaunch_called.append(1) or ControlResult(True, "relaunch")),
        state_path=state_file,
    )
    assert relaunch_called == []

    # Check 2: hung twice -> kill + relaunch
    res2 = supervise_hud(
        status,
        now=NOW + timedelta(seconds=60),
        relaunch_control=lambda: (relaunch_called.append(1) or ControlResult(True, "relaunch")),
        killer=lambda pid, sig: killed.append((pid, sig)),
        is_running=lambda _pid: False,
        state_path=state_file,
    )
    assert res2["action"] == "relaunch"
    assert res2["reason"] == "hung"
    assert len(relaunch_called) == 1
    assert len(killed) >= 1
    assert killed[0][0] == 5555


def test_supervisor_budget_exhaustion_and_cooldown(tmp_path):
    state_file = tmp_path / "hud_supervisor.json"
    status = _make_status(hud_alive=False)
    relaunch_count = 0

    def fake_relaunch():
        nonlocal relaunch_count
        relaunch_count += 1
        return ControlResult(True, "relaunch")

    # 3 relaunches within 1 hour
    for i in range(3):
        t = NOW + timedelta(minutes=i * 5)
        res = supervise_hud(status, now=t, relaunch_control=fake_relaunch, state_path=state_file)
        assert res["action"] == "relaunch"
    assert relaunch_count == 3

    # 4th check within 1 hour -> budget exhausted, cooldown active
    t4 = NOW + timedelta(minutes=20)
    res4 = supervise_hud(status, now=t4, relaunch_control=fake_relaunch, state_path=state_file)
    assert res4["action"] == "report_only"
    assert res4["reason"] in ("restart_budget_exhausted", "cooldown_active")
    assert relaunch_count == 3  # Not incremented


def test_supervisor_summary_formatting(tmp_path):
    state_file = tmp_path / "hud_supervisor.json"
    status = _make_status(hud_alive=False)

    supervise_hud(
        status,
        now=NOW,
        relaunch_control=lambda: ControlResult(True, "relaunch"),
        state_path=state_file,
    )
    summary_zh, summary_en = read_supervisor_summary(state_file, now=NOW + timedelta(minutes=1))
    assert "已自动重开 1 次" in summary_zh
    assert "automatically relaunched 1 time(s)" in summary_en


def test_relaunch_hud_isolated_environment(monkeypatch):
    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    res = relaunch_hud()
    assert not res.ok
    assert "隔离环境" in res.error_zh


def test_relaunch_hud_no_app(monkeypatch):
    monkeypatch.delenv("PENPAL_ISOLATED", raising=False)
    monkeypatch.setattr("penpal.runtime.control.sys.platform", "darwin")
    res = relaunch_hud(app_finder=lambda: None)
    assert not res.ok
    assert "未找到安装的 Penpal.app" in res.error_zh


def test_relaunch_hud_executes_open(monkeypatch, tmp_path):
    monkeypatch.delenv("PENPAL_ISOLATED", raising=False)
    monkeypatch.setattr("penpal.runtime.control.sys.platform", "darwin")
    fake_app = tmp_path / "Penpal.app"
    fake_app.mkdir()
    commands = []

    def fake_runner(cmd, **_kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    res = relaunch_hud(app_finder=lambda: fake_app, runner=fake_runner)
    assert res.ok
    assert commands == [["/usr/bin/open", "-a", str(fake_app)]]


def test_failed_relaunch_counts_against_budget(tmp_path):


    calls = []

    def failing() -> ControlResult:
        calls.append(1)
        return ControlResult(False, "relaunch_hud", error_zh="未找到安装的 Penpal.app", error_en="not installed")

    status = _make_status(hud_alive=False)
    start = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    for minute in range(10):
        supervise_hud(status, now=start + timedelta(minutes=minute), restart_control=failing, state_path=tmp_path / "s.json")
    assert len(calls) == 3
