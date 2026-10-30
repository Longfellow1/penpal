"""HUD controller: actions reach the same core functions as the CLI, secrets are shown once, revoke bites.

Temp HOME and temp DB only; nothing here touches real data, launchd, System Settings or the GUI.
"""
from __future__ import annotations

import json
import logging
import types
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from penpal.cli import daemon as cli_module
from penpal import config as config_module
from penpal import permissions
from penpal.capture import capture_control
from penpal.cli import main
from penpal.config import Config
from penpal.health import capture_health as ch
from penpal.runtime import RuntimeState, RuntimeStatus
from penpal.runtime import control
from penpal.runtime.evaluate import Issue
from penpal.foundation.runtime_record import Component
from penpal.hud.controller import HudController
from penpal.mcp import auth
from penpal.services import purge
from penpal.sources import discover
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import get_state, insert_raw_event, set_state

from tests import mcp_helpers


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    init_db(cfg.db_path_expanded)
    yield cfg
    close()


@pytest.fixture()
def hud(env):
    copied = []
    spawned = []
    c = HudController(lambda: env, copy_to_clipboard=copied.append, spawn=lambda fn: spawned.append(fn))
    c.copied, c.spawned = copied, spawned
    return c


def _event(source="clipboard", app="Notes", minutes_ago=1, text="synthetic note"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return insert_raw_event(RawEvent(source=source, event_type="x", ts_start=ts, app_name=app, content_text=text))


def _tab_rows(model):
    return [r for s in model["sections"] if s["kind"] == "list" for r in s["rows"]]


# ── snapshot reads the same state the CLI shows ─────────────────────────────────

def test_snapshot_reflects_core_state(hud, env):
    _event("clipboard")
    _event("window", app="Safari")
    capture_control.set_source_enabled("ax_text", False)
    snap = hud.snapshot(fresh=True)
    assert snap.today_records == 2 and snap.total_records == 2
    by_name = {s.name: s for s in snap.sources}
    assert by_name["clipboard"].today == 1 and by_name["clipboard"].enabled
    assert by_name["ax_text"].enabled is False
    assert {n for n, _ in snap.delete_sources} == {"clipboard", "window"}
    assert ("Notes", 1) in snap.delete_apps
    assert snap.paused is False and snap.keyboard_on is True
    assert snap.health["code"] == RuntimeState.NOT_INSTALLED.value  # empty temporary HOME has no LaunchAgent


def test_snapshot_flags_runtime_component_error_and_lost_permission(hud, env, monkeypatch):
    now = datetime.now(timezone.utc)
    runtime = {"status": RuntimeStatus(
        RuntimeState.RUNNING,
        issues=(Issue("storage", "error", "磁盘已满", "disk is full", "penpal doctor", "采集存储", "Capture storage"),),
        components={"storage": Component("storage_error", "disk_full")},
    )}
    monkeypatch.setattr("penpal.hud.snapshot.evaluate", lambda **_kwargs: runtime["status"])
    assert hud.snapshot(fresh=True).health["code"] == RuntimeState.RUNNING.value

    runtime["status"] = RuntimeStatus(RuntimeState.RUNNING)
    set_state(ch.DAEMON_PERMISSIONS_KEY, json.dumps({
        "checked_at": now.isoformat(), "pid": 1, "host_executable": "/opt/py/bin/python3.13",
        "permissions": {"accessibility": "denied", "input_monitoring": "granted"}}))
    snap = hud.snapshot(fresh=True)
    assert snap.health is None
    assert [p.key for p in snap.perm_issues] == ["accessibility"]  # ax_text is off in config, keyboard needs it
    model = hud.model(fresh=True)
    assert any(r["title"].startswith("缺少「辅助功能」") for r in _tab_rows(model))


def test_runtime_storage_component_shows_error_entry(hud, env, monkeypatch):
    status = RuntimeStatus(
        RuntimeState.RUNNING,
        issues=(Issue("storage", "error", "采集存储发生错误", "Capture storage has an error.", label_zh="采集存储", label_en="Capture storage"),),
        components={"storage": Component("storage_error", "read_only")},
    )
    monkeypatch.setattr("penpal.hud.snapshot.evaluate", lambda **_kwargs: status)
    model = hud.model(fresh=True)
    assert hud.snapshot().health["code"] == RuntimeState.RUNNING.value
    assert model["status"]["tone"] == "red"
    assert any(r["title"] == "采集存储" for r in _tab_rows(model))


def test_unreadable_database_does_not_crash_the_hud(hud, env, monkeypatch):
    monkeypatch.setattr("penpal.services.activity.source_activity", lambda since: (_ for _ in ()).throw(RuntimeError("db")))
    model = hud.model(fresh=True)
    assert model["status"]["tone"] == "red"
    assert any(r["title"] == "无法读取数据库" for r in _tab_rows(model))


# ── actions call the same core functions as the CLI ─────────────────────────────

def test_pause_resume_use_the_cli_function(hud, env, monkeypatch):
    calls = []
    spy = lambda paused: calls.append(("core", paused))
    monkeypatch.setattr(capture_control, "set_capture_paused", spy)
    monkeypatch.setattr(cli_module, "set_capture_paused", spy)
    hud.dispatch("capture.pause")
    CliRunner().invoke(main, ["pause"])
    hud.dispatch("capture.resume")
    CliRunner().invoke(main, ["resume"])
    assert calls == [("core", True)] * 2 + [("core", False)] * 2


def test_pause_persists_and_status_bar_follows(hud, env, monkeypatch):
    monkeypatch.setattr(
        "penpal.hud.snapshot.evaluate",
        lambda **_kwargs: RuntimeStatus(RuntimeState.PAUSED if capture_control.is_capture_paused() else RuntimeState.RUNNING),
    )
    m = hud.dispatch("capture.pause")
    assert get_state("status") == "paused" and m["status"]["text"] == "已暂停" and m["dim"]
    assert m["toast"]["text"].startswith("已暂停")
    m = hud.dispatch("capture.resume")
    assert get_state("status") == "running" and m["status"]["text"] != "已暂停"


def test_source_and_keyboard_toggle_match_cli_source_command(hud, env, monkeypatch):
    calls = []
    spy = lambda name, enabled: calls.append((name, enabled))
    monkeypatch.setattr(capture_control, "set_source_enabled", spy)
    monkeypatch.setattr(discover, "set_source_enabled", spy)
    hud.dispatch("source.toggle", {"name": "clipboard", "enable": False})
    CliRunner().invoke(main, ["source", "disable", "clipboard"])
    hud.dispatch("keyboard.toggle", {"enable": False})
    CliRunner().invoke(main, ["source", "disable", "keyboard_chunk"])
    hud.dispatch("source.toggle", {"name": "clipboard", "enable": True})
    CliRunner().invoke(main, ["source", "enable", "clipboard"])
    assert calls == [("clipboard", False)] * 2 + [("keyboard_chunk", False)] * 2 + [("clipboard", True)] * 2


def test_source_toggle_persists_and_rejects_unknown_names(hud, env):
    hud.dispatch("source.toggle", {"name": "clipboard", "enable": False})
    assert "clipboard" in capture_control.disabled_sources()
    assert next(r for r in _tab_rows(hud.dispatch("nav.page", {"page": "group:live"})) if r["title"] == "剪贴板")["trail"]["on"] is False
    hud.dispatch("source.toggle", {"name": "not-a-source", "enable": False})
    assert capture_control.disabled_sources() == frozenset({"clipboard"})


def test_keyboard_switch_off_stops_writes_like_the_cli(hud, env):
    assert capture_control.source_writes_allowed("keyboard_chunk")
    hud.dispatch("keyboard.toggle", {"enable": False})
    assert not capture_control.source_writes_allowed("keyboard_chunk")
    kb = _tab_rows(hud.dispatch("nav.tab", {"tab": "privacy"}))[0]
    assert kb["trail"]["on"] is False and kb["args"] == {"enable": True}


def test_chat_app_toggle_matches_cli_and_writes_config(hud, env, monkeypatch):
    calls = []
    monkeypatch.setattr(config_module, "set_chat_app_opt_in", lambda name, allowed, **kw: calls.append((name, allowed)) or (name, []))
    hud.dispatch("chat.toggle", {"name": "Slack", "allow": True})
    CliRunner().invoke(main, ["privacy", "chat-apps", "allow", "Slack"])
    assert calls == [("Slack", True)] * 2


def test_chat_app_toggle_persists_in_config_toml(hud, env):
    assert Config.load().privacy.chat_apps_opt_in == []
    hud.dispatch("chat.toggle", {"name": "Slack", "allow": True})
    assert Config.load().privacy.chat_apps_opt_in == ["Slack"]
    m = hud.dispatch("chat.toggle", {"name": "Slack", "allow": False})
    assert Config.load().privacy.chat_apps_opt_in == []
    m = hud.dispatch("chat.toggle", {"name": "Nonexistent", "allow": True})
    assert m["toast"]["text"] == "不认识这个聊天 App"


# ── grants: create / revoke / rotate ────────────────────────────────────────────

def test_grant_create_calls_cli_core_and_shows_secret_once(hud, env, caplog):
    caplog.set_level(logging.DEBUG)
    hud.dispatch("nav.tab", {"tab": "access"})
    hud.dispatch("nav.page", {"page": "host:new"})
    hud.dispatch("host.bucket", {"bucket": "Personal"})
    m = hud.dispatch("grant.create", {"host": "claude"})
    # the page now shows the one-time token and config
    assert m["view"]["page"] == "reveal" and m["title"] == "授权成功" and m["back"]["label"] == "完成"
    text = next(s for s in m["sections"] if s["kind"] == "code")["text"]
    token = next(line.strip() for line in text.splitlines() if line.strip().startswith("kp_"))
    assert "PENPAL_TOKEN" in text and "PENPAL_CLIENT" in text and "claude" in text
    # the grant is real: same scope the CLI would create for these buckets
    decision = auth.check_access(token, "claude")
    assert decision.grant is not None and set(decision.grant.buckets) == {"Work", "Personal"}
    # copy goes through the clipboard hook and does not need the secret to travel through the page
    hud.dispatch("reveal.copy")
    assert hud.copied == [text]
    # leaving the page forgets the secret everywhere: model, UI state, logs
    m = hud.dispatch("nav.back")
    assert token not in json.dumps(m, ensure_ascii=False) and hud.ui.reveal is None
    assert hud.dispatch("nav.page", {"page": "reveal"})["view"]["page"] == ""
    assert token not in caplog.text and token not in repr(hud.ui)
    assert not list(env.db_path_expanded.parent.glob("hud-state*"))
    assert token not in json.dumps(auth.list_grants())  # only the hash is stored


def test_grant_create_goes_through_core_create_grant_and_cli_text(hud, env, monkeypatch):
    seen = []
    real = auth.create_grant
    monkeypatch.setattr(auth, "create_grant", lambda *a, **k: seen.append((a, k)) or real(*a, **k))
    hud.dispatch("grant.create", {"host": "codex"})
    assert seen and seen[0][0] == ("codex",) and seen[0][1]["buckets"] == ["Work"]
    out = CliRunner().invoke(main, ["mcp", "grant", "cursor", "--host", "cursor"]).output
    model_text = next(s for s in hud.model()["sections"] if s["kind"] == "code")["text"]
    # same wording and structure as `penpal mcp grant` (token differs)
    assert "Granted client 'codex'" in model_text and "Granted client 'cursor'" in out
    assert "Revoke with: penpal mcp revoke codex" in model_text
    assert model_text.splitlines()[-1] == out.splitlines()[-1].replace("cursor", "codex")


def test_grant_create_refuses_duplicates_and_empty_scope(hud, env):
    hud.dispatch("grant.create", {"host": "claude"})
    hud.dispatch("nav.back")
    m = hud.dispatch("grant.create", {"host": "claude"})
    assert m["toast"]["text"].startswith("已经连接了") and len(auth.active_grants()) == 1
    hud.dispatch("host.bucket", {"bucket": "Work"})
    assert hud.ui.new_buckets == ()
    assert hud.dispatch("grant.create", {"host": "codex"})["toast"]["text"] == "至少选一个分类"


def test_revoke_via_confirm_dialog_refuses_the_hosts_next_mcp_call_immediately(hud, env):
    hud.dispatch("grant.create", {"host": "claude"})
    token = next(l.strip() for l in hud.model()["sections"][1]["text"].splitlines() if l.strip().startswith("kp_"))
    hud.dispatch("nav.back")
    _event(source="zsh_history", app="Terminal")  # something the host could find
    assert auth.check_access(token, "claude").grant is not None
    assert mcp_helpers.search(token, ["synthetic"])["status"] != "permission_denied"

    hud.dispatch("nav.tab", {"tab": "access"})
    hud.dispatch("nav.page", {"page": "host:claude"})
    ask = hud.dispatch("confirm.ask", {"kind": "revoke", "client_id": "claude"})
    assert ask["confirm"]["title"] == "撤销授权？" and auth.check_access(token, "claude").grant is not None  # nothing yet
    m = hud.dispatch("confirm.ok")

    assert m["confirm"] is None and m["toast"]["text"] == "已撤销授权" and m["view"]["page"] == ""
    decision = auth.check_access(token, "claude")
    assert decision.grant is None and decision.reason == "token_revoked"
    assert mcp_helpers.search(token, ["synthetic"])["status"] == "permission_denied"
    assert auth.active_grants() == []
    assert not any(r["title"] == "Claude Code" for r in _tab_rows(m))


def test_revoke_cancel_keeps_the_grant(hud, env):
    hud.dispatch("grant.create", {"host": "claude"})
    hud.dispatch("nav.back")
    hud.dispatch("confirm.ask", {"kind": "revoke", "client_id": "claude"})
    m = hud.dispatch("confirm.cancel")
    assert m["confirm"] is None and len(auth.active_grants()) == 1


def test_revoke_uses_the_cli_core_function(hud, env, monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "revoke_grants", lambda client: calls.append(client) or 1)
    hud.dispatch("grant.create", {"host": "claude"})
    hud.dispatch("nav.back")
    hud.dispatch("confirm.ask", {"kind": "revoke", "client_id": "claude"})
    hud.dispatch("confirm.ok")
    CliRunner().invoke(main, ["mcp", "revoke", "claude"])
    assert calls == ["claude", "claude"]


def test_rotate_shows_new_token_once_and_kills_the_old_one(hud, env):
    hud.dispatch("grant.create", {"host": "claude"})
    old = next(l.strip() for l in hud.model()["sections"][1]["text"].splitlines() if l.strip().startswith("kp_"))
    hud.dispatch("nav.back")
    m = hud.dispatch("grant.rotate", {"client_id": "claude"})
    assert m["title"] == "新的密钥" and m["view"]["page"] == "reveal"
    new = next(l.strip() for l in m["sections"][1]["text"].splitlines() if l.strip().startswith("kp_"))
    assert new != old
    assert auth.check_access(old, "claude").grant is None and auth.check_access(new, "claude").grant is not None
    hud.dispatch("nav.tab", {"tab": "access"})
    assert new not in json.dumps(hud.model(), ensure_ascii=False)
    assert hud.dispatch("grant.rotate", {"client_id": "ghost"})["toast"]["text"] == "没有可轮换的授权"


def test_closing_the_popover_forgets_the_secret_and_dialogs(hud, env):
    hud.dispatch("grant.create", {"host": "claude"})
    assert hud.ui.reveal is not None
    hud.popover_closed()
    assert hud.ui.reveal is None and hud.ui.page == "" and hud.ui.confirm is None


# ── delete: dry run first, then confirm ─────────────────────────────────────────

def test_delete_page_shows_dry_run_counts_then_deletes_after_confirm(hud, env):
    for _ in range(3):
        _event(source="safari_history", app="Safari")
    _event(source="clipboard", app="Notes")
    hud.dispatch("nav.tab", {"tab": "privacy"})
    m = hud.dispatch("nav.page", {"page": "delete"})
    counts = next(s for s in m["sections"] if s.get("header") == "将删除")
    assert counts["rows"][0]["value"]["text"] == "4 条"  # today: all four
    m = hud.dispatch("delete.pick", {"scope": "source:safari_history"})
    counts = next(s for s in m["sections"] if s.get("header") == "将删除")
    assert counts["rows"][0]["value"]["text"] == "3 条"
    assert m["sections"][-1]["label"] == "删除 3 条记录"
    ask = hud.dispatch("confirm.ask", {"kind": "purge"})
    assert ask["confirm"]["title"] == "删除「Safari 历史（按来源）」？"
    assert get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 4  # nothing deleted yet
    done = hud.dispatch("confirm.ok")
    assert done["toast"]["text"] == "已删除 3 条记录" and done["view"]["page"] == ""
    left = get_conn().execute("SELECT source FROM raw_events").fetchall()
    assert [r["source"] for r in left] == ["clipboard"]


def test_purge_preview_and_run_use_the_cli_functions(hud, env, monkeypatch):
    calls = []
    real = purge.run_purge
    monkeypatch.setattr(purge, "run_purge", lambda selector, dry_run=False: calls.append((selector["source"], dry_run)) or real(selector, dry_run=dry_run))
    _event(source="clipboard")
    hud.dispatch("nav.page", {"page": "delete"})
    hud.dispatch("delete.pick", {"scope": "source:clipboard"})
    hud.dispatch("confirm.ask", {"kind": "purge"})
    hud.dispatch("confirm.ok")
    _event(source="clipboard")
    CliRunner().invoke(main, ["purge", "--source", "clipboard", "--confirm"])
    dry = [c for c in calls if c[1]]
    runs = [c for c in calls if not c[1]]
    assert dry and ("clipboard", False) in runs and len(runs) == 2  # one real run from the HUD, one from the CLI


def test_purge_selector_is_the_clis(env, monkeypatch):
    import time
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        # 06:00 UTC is 14:00 in Shanghai: "today" starts at local midnight, stored as UTC (16:00 the day before).
        sel = purge.purge_selector(today=True, now=datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc))
    finally:
        monkeypatch.undo()
        time.tzset()
    assert sel["since"] == "2026-10-23T16:00:00+00:00" and sel["until"].startswith("2026-10-24T06:00:00")
    assert purge.purge_selector() is None
    assert purge.purge_selector(app="Notes")["app_name"] == "Notes"


def test_confirm_purge_needs_the_delete_page(hud, env):
    _event()
    hud.dispatch("confirm.ask", {"kind": "purge"})  # not on the delete page: ignored
    assert hud.ui.confirm is None
    assert get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1


# ── recovery actions ────────────────────────────────────────────────────────────

def test_daemon_restart_uses_runtime_control(hud, env, monkeypatch):
    runs = []
    monkeypatch.setattr(control, "restart", lambda reason: runs.append(reason))
    m = hud.dispatch("daemon.restart")
    assert m["toast"]["text"].startswith("正在重启后台") and len(hud.spawned) == 1
    hud.spawned[0]()
    assert runs == ["hud"]


def test_open_settings_only_opens_known_panes(hud, env, monkeypatch):
    opened = []
    monkeypatch.setattr(permissions, "open_settings_url", opened.append)
    now = datetime.now(timezone.utc)
    set_state(ch.DAEMON_PERMISSIONS_KEY, json.dumps({
        "checked_at": now.isoformat(), "pid": 1, "host_executable": "/opt/py/bin/python3.13",
        "permissions": {"accessibility": "denied"}}))
    hud.snapshot(fresh=True)
    hud.dispatch("settings.open", {"pane": "full_disk"})
    hud.dispatch("settings.open", {"pane": "accessibility"})
    hud.dispatch("settings.open", {"pane": "input_monitoring"})  # not flagged: refused
    hud.dispatch("settings.open", {"pane": "file:///etc/passwd"})
    assert opened == [permissions.FULL_DISK_ACCESS_PANE, permissions.spec_by_key("accessibility").settings_pane]


# ── robustness ──────────────────────────────────────────────────────────────────

def test_unknown_or_malformed_actions_never_raise(hud, env):
    for action, args in [("nope", {}), (None, None), ("nav.tab", {"tab": "zzz"}), ("nav.page", {"page": 5}), ("source.toggle", None),
                         ("grant.create", {"host": "evil"}), ("confirm.ok", {}), ("reveal.copy", {}), ("delete.pick", {"scope": "x"})]:
        m = hud.dispatch(action, args)
        assert m["title"] == "概览"


def test_core_failure_becomes_a_toast(hud, env, monkeypatch):
    monkeypatch.setattr(capture_control, "set_capture_paused", lambda paused: (_ for _ in ()).throw(RuntimeError("secret detail")))
    m = hud.dispatch("capture.pause")
    assert m["toast"]["text"] == "操作没有成功，详情见 penpal doctor" and "secret detail" not in json.dumps(m, ensure_ascii=False)


def test_toast_ids_increase_so_repeated_messages_show_again(hud, env):
    a = hud.dispatch("capture.pause")["toast"]["id"]
    b = hud.dispatch("capture.resume")["toast"]["id"]
    assert b > a


def test_navigation_state_machine(hud, env):
    assert hud.model()["view"] == {"tab": "overview", "page": ""}
    assert hud.dispatch("nav.page", {"page": "group:live"})["back"]["label"] == "概览"
    assert hud.dispatch("nav.back")["view"]["page"] == ""
    hud.dispatch("nav.page", {"page": "group:live"})
    assert hud.dispatch("nav.tab", {"tab": "access"})["view"] == {"tab": "access", "page": ""}  # switching tabs leaves the 2nd level


def test_recheck_panel_action_updates_status_subtext(hud, env, monkeypatch):

    m = hud.dispatch("health.recheck")
    assert "已重新检测" in m["status"]["sub"]
    assert "1 个问题" in m["status"]["sub"]

    # When no alerts are present, it shows 正常
    monkeypatch.setattr("penpal.hud.viewmodel.alerts", lambda _snap: [])
    m2 = hud.dispatch("health.recheck")
    assert "已重新检测" in m2["status"]["sub"]
    assert "正常" in m2["status"]["sub"]


def test_repair_issue_action_dispatches_repair_and_rechecks(hud, env, monkeypatch):
    from penpal.services.repair import RepairResult

    repaired = []
    monkeypatch.setattr(
        "penpal.services.repair.repair",
        lambda code: (repaired.append(code) or RepairResult(code, True, True, "已成功重启", "restarted")),
    )
    m = hud.dispatch("issue.repair", {"issue_code": "daemon.hung"})
    assert repaired == ["daemon.hung"]
    assert m["toast"]["text"] == "已成功重启"
    assert "已重新检测" in m["status"]["sub"]


def test_worker_does_not_monkeypatch_controller(env):
    from penpal.hud.controller import HudModelWorker

    ctrl = HudController(lambda: env)
    assert getattr(ctrl, "_push_model", None) is None

    HudModelWorker(
        ctrl,
        main_dispatcher=lambda fn: fn(),
        apply_script=lambda _s, _b: None,
    )
    # The worker must NOT monkeypatch ctrl._push_model
    assert getattr(ctrl, "_push_model", None) is None


def test_recheck_and_repair_use_injected_clock(env):
    fixed_dt = datetime(2026, 10, 29, 14, 25, 36, tzinfo=timezone.utc)
    expected_local_str = fixed_dt.astimezone().strftime("%H:%M:%S")

    ctrl = HudController(lambda: env, clock=lambda: fixed_dt)
    ctrl.dispatch("health.recheck")
    assert ctrl.ui.rechecked_at == expected_local_str


def test_hud_quit_action_calls_injected_callback_once_and_touches_nothing_else(env):
    quit_calls = []
    controller = HudController(
        lambda: env,
        quit_hud=lambda: quit_calls.append(1),
    )
    initial_ui = controller.ui
    initial_model = controller.model()

    model = controller.dispatch("hud.quit")

    assert quit_calls == [1]
    assert controller.ui == initial_ui
    assert model["view"] == initial_model["view"]
    assert model["confirm"] is None
    assert model["toast"] is None




def _wait(event, seconds=5.0):
    assert event.wait(seconds), "timed out"


def test_background_scan_runs_off_the_caller_thread_and_reports_on_the_dispatcher():
    import threading
    from penpal.hud.controller import BackgroundScan

    delivered, done = [], threading.Event()
    scan = BackgroundScan(main_dispatcher=lambda fn: (fn(), done.set()))
    assert scan.run("count", lambda: threading.current_thread().name, delivered.append) is True
    _wait(done)
    assert delivered and delivered[0].startswith("penpal-hud-scan")


def test_background_scan_skips_a_scan_that_is_still_running():
    import threading
    from penpal.hud.controller import BackgroundScan

    release, done = threading.Event(), threading.Event()
    scan = BackgroundScan(main_dispatcher=lambda fn: (fn(), done.set()))
    assert scan.run("count", lambda: release.wait(5), lambda _r: None) is True
    assert scan.run("count", lambda: None, lambda _r: None) is False
    release.set()
    _wait(done)
    assert scan.run("count", lambda: None, lambda _r: None) is True


def test_background_scan_logs_a_failure_and_skips_the_callback(caplog):
    import threading
    from penpal.hud import controller as hud_controller

    logged = threading.Event()

    class _Handler(logging.Handler):
        def emit(self, record):
            if "hud scan count failed" in record.getMessage():
                logged.set()

    handler = _Handler()
    hud_controller.LOGGER.addHandler(handler)
    try:
        delivered = []
        scan = hud_controller.BackgroundScan(main_dispatcher=lambda fn: fn())
        scan.run("count", lambda: 1 / 0, delivered.append)
        _wait(logged)
        assert delivered == []
    finally:
        hud_controller.LOGGER.removeHandler(handler)


def test_worker_logs_a_turn_that_holds_the_panel(monkeypatch):
    import threading
    from penpal.hud import controller as hud_controller

    monkeypatch.setattr(hud_controller, "SLOW_WORKER_SEC", 0.0)
    messages, logged = [], threading.Event()

    class _Handler(logging.Handler):
        def emit(self, record):
            if "hud worker busy" in record.getMessage():
                messages.append(record.getMessage())
                logged.set()

    handler = _Handler()
    hud_controller.LOGGER.addHandler(handler)
    try:
        def count_candidates():
            return 3

        worker = hud_controller.HudModelWorker(
            types.SimpleNamespace(model=lambda fresh=False: {"tabs": []}),
            main_dispatcher=lambda fn: fn(),
            apply_script=lambda _s, _b: None,
        )
        worker.submit(count_candidates)
        _wait(logged)
        assert "count_candidates" in messages[0]
    finally:
        hud_controller.LOGGER.removeHandler(handler)
