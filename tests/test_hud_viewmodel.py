"""HUD v3 view-model: every page of the approved interaction draft, plus the AT09 failure entries.

Pure data in, pure data out: synthetic snapshots only, no database, no AppKit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from penpal.hud.snapshot import AuditRow, HostInfo, PermIssue, Snapshot, SourceInfo
from penpal.hud.viewmodel import UiState, Reveal, build_model, delete_options
from penpal.i18n import T
from penpal.runtime import RuntimeState, RuntimeStatus
from penpal.runtime.evaluate import Issue
from penpal.foundation.runtime_record import Component

NOW = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _sources(**over) -> tuple[SourceInfo, ...]:
    base = [
        SourceInfo("window", "前台窗口", "live", "capture", True, last_ts=_ago(seconds=20), today=412),
        SourceInfo("clipboard", "剪贴板", "live", "capture", True, last_ts=_ago(minutes=5), today=38),
        SourceInfo("keyboard_chunk", "键盘片段", "live", "capture", True, last_ts=_ago(minutes=2), today=9),
        SourceInfo("ax_text", "当前看到的正文", "live", "capture", False),
        SourceInfo("chrome_history", "Chrome 历史", "browser", "plugin", True, last_ts=_ago(minutes=10), today=96),
        SourceInfo("safari_history", "Safari 历史", "browser", "plugin", True, last_ts=_ago(hours=1), today=12),
        SourceInfo("claude_code", "Claude Code", "ai", "plugin", True, last_ts=_ago(minutes=3), today=210),
    ]
    out = []
    for s in base:
        fields = over.get(s.name)
        out.append(SourceInfo(**{**s.__dict__, **fields}) if fields else s)
    return tuple(out)


def snap(**over) -> Snapshot:
    base = dict(
        now=NOW, last_write_ts=_ago(minutes=2), today_records=1284, sources=_sources(),
        hosts=(
            HostInfo("claude", ("Work", "Personal"), None, ("read",), today=24, week=132, denied=3, last_ts=_ago(minutes=2)),
            HostInfo("codex", ("Work",), ("claude_code",), ("read",), today=0, week=61, denied=0, last_ts=_ago(hours=1)),
        ),
        audit=(
            AuditRow(_ago(minutes=2), "claude", "search_context", True, "ok", 5),
            AuditRow(_ago(minutes=3), "claude", "get_evidence", True, "ok", 1),
            AuditRow(_ago(hours=3), "codex", "search_context", True, "ok", 0),
            AuditRow(_ago(hours=4), "unknown", "search_context", False, "token_unknown", None),
        ),
        ai_queries_today=36, keyboard_on=True,
        chat_apps=(("WeChat", "微信", False), ("Slack", "Slack", True)),
        exclusions=(("app", "1Password"), ("content", "password")),
        total_records=382000, db_bytes=1_500_000_000, disk_free_bytes=86 << 30,
        delete_sources=(("safari_history", 128),), delete_apps=(("Notes", 40),),
    )
    health = over.pop("health", None)
    if "runtime_status" not in over:
        code = (health or {}).get("code")
        states = {
            "daemon_not_installed": RuntimeState.NOT_INSTALLED,
            "daemon_not_running": RuntimeState.NOT_RUNNING,
            "hung": RuntimeState.HUNG,
            "outdated": RuntimeState.OUTDATED,
            "paused": RuntimeState.PAUSED,
        }
        state = states.get(code, RuntimeState.PAUSED if over.get("paused") else RuntimeState.RUNNING)
        components = {}
        issues = ()
        if code in {"disk_full", "storage_error"}:
            components["storage"] = Component("storage_error", "disk_full" if code == "disk_full" else "io_error")
            issues = (Issue("storage", "error", "磁盘空间已满" if code == "disk_full" else "采集存储发生错误", "disk is full" if code == "disk_full" else "Capture storage has an error.", label_zh="采集存储", label_en="Capture storage"),)
        over["runtime_status"] = RuntimeStatus(state, issues, components)
    base.update(over)
    return Snapshot(**base)


def titles(section):
    return [r["title"] for r in section["rows"]]


def all_rows(model):
    return [r for s in model["sections"] if s["kind"] == "list" for r in s["rows"]]


def row_by_title(model, title):
    return next(r for r in all_rows(model) if r["title"] == title)


# ── 概览 ────────────────────────────────────────────────────────────────────────

def test_overview_normal():
    m = build_model(snap(), UiState())
    assert m["title"] == "概览" and m["back"] is None and not m["dim"]
    assert m["status"] == {"code": "running", "tone": "ok", "text": "正在记录", "sub": "一切正常 · 2 分钟前写入"}
    assert m["pause"] == {"label": "暂停", "action": "capture.pause"}
    assert [t["label"] for t in m["tabs"]] == ["概览", "AI 访问", "设置"]
    assert all(t["badge"] is None for t in m["tabs"])
    stats = next(s for s in m["sections"] if s["kind"] == "stats")
    assert [(x["v"], x["k"]) for x in stats["stats"]] == [("1,284", "新增记录"), ("36", "AI 查询"), ("2", "AI 已连接")]
    groups = next(s for s in m["sections"] if s.get("header") == "数据来源")
    assert titles(groups) == ["实时采集", "浏览器", "AI 对话"]
    live = groups["rows"][0]
    assert live["value"] == {"text": "3 开 · 1 关", "tone": None} and live["trail"] == {"type": "nav"}  # summary like the draft
    assert live["dot"] == "ok" and live["action"] == "nav.page" and live["args"] == {"page": "group:live"}
    assert not any(s.get("header") == "需要处理" for s in m["sections"])


def test_overview_source_needs_attention():
    srcs = _sources(safari_history={"unavailable_reason": "permission_denied", "last_ts": _ago(days=2)})
    m = build_model(snap(sources=srcs), UiState())
    assert m["status"] == {"code": "running", "tone": "amber", "text": "正在记录", "sub": "1 个问题需要处理"}
    assert next(t for t in m["tabs"] if t["id"] == "overview")["badge"] == "amber"
    todo = next(s for s in m["sections"] if s.get("header") == "需要处理")
    r = todo["rows"][0]
    assert r["title"] == "Safari 历史 不可用" and r["dot"] == "amber" and "完全磁盘访问" in r["sub"]
    assert r["args"] == {"page": "group:browser"}
    browser = row_by_title(m, "浏览器")
    assert browser["dot"] == "amber" and browser["value"]["text"] == "1 个需处理"


def test_overview_stopped_recording_daemon_and_disk():
    m = build_model(snap(health={"code": "daemon_not_running", "message": "x"}), UiState())
    assert m["status"]["tone"] == "red" and m["status"]["text"] == "未运行"
    assert next(t for t in m["tabs"] if t["id"] == "overview")["badge"] == "red"
    groups = next(s for s in m["sections"] if s.get("header") == "数据来源")
    assert all(r["value"]["text"] == "已停止" and r["dot"] == "red" for r in groups["rows"])


def test_overview_paused_hides_alerts_and_dims():
    m = build_model(snap(paused=True, health={"code": "disk_full", "message": "x"}), UiState())
    assert m["status"] == {"code": "paused", "tone": "grey", "text": "已暂停", "sub": "重启后仍保持暂停 · 不会补记"}
    assert m["pause"] == {"label": "继续记录", "action": "capture.resume"} and m["dim"]
    assert not any(s.get("header") == "需要处理" for s in m["sections"])
    assert all(t["badge"] is None for t in m["tabs"])


# ── AT09: specific fix entries ──────────────────────────────────────────────────

def _alert_rows(model):
    return next(s for s in model["sections"] if s.get("header") == "需要处理")["rows"]


def test_at09_daemon_dead_offers_restart():
    for code in ("daemon_not_running", "hung"):
        m = build_model(snap(health={"code": code, "message": "x"}), UiState())
        r = _alert_rows(m)[0]
        assert r["dot"] == "red" and r["action"] == "daemon.restart"
        assert r["value"]["text"] == "修复"
    assert _alert_rows(build_model(snap(health={"code": "daemon_not_running", "message": "x"}), UiState()))[0]["title"] == "未运行"
    assert _alert_rows(build_model(snap(health={"code": "hung", "message": "x"}), UiState()))[0]["title"] == "后台无响应"


def test_at09_permission_revoked_names_permission_and_pane():
    issue = PermIssue("accessibility", "辅助功能（Accessibility）", ("ax_text", "keyboard_chunk"), ("/opt/py/Python.app",))
    s = snap(perm_issues=(issue,), health={"code": "permission_missing", "message": "x"},
             sources=_sources(ax_text={"enabled": True, "missing_permissions": ("accessibility",)},
                              keyboard_chunk={"missing_permissions": ("accessibility",)}))
    m = build_model(s, UiState())
    r = _alert_rows(m)[0]
    assert r["title"] == "缺少「辅助功能」权限" and r["dot"] == "amber"
    assert "当前看到的正文" in r["sub"] and "键盘片段" in r["sub"]
    assert r["args"] == {"page": "perm:accessibility"}
    page = build_model(s, UiState(page="perm:accessibility"))
    assert page["title"] == "辅助功能" and page["back"] == {"label": "概览", "action": "nav.back"}
    btn = next(x for x in page["sections"] if x["kind"] == "button")
    assert btn["label"] == "打开系统设置 › 辅助功能"
    assert (btn["action"], btn["args"]) == ("settings.open", {"pane": "accessibility"})
    rows = all_rows(page)
    assert any(r["title"] == "Python.app" and r["sub"] == "/opt/py/Python.app" for r in rows)
    # the group page of an affected source also carries the fix button
    group = build_model(s, UiState(page="group:live"))
    assert any(x["kind"] == "button" and x["args"] == {"pane": "accessibility"} for x in group["sections"])


def test_at09_storage_errors_disk_full_and_read_only():
    full = build_model(snap(health={"code": "disk_full", "message": "x"}, disk_free_bytes=0), UiState())
    assert full["status"]["tone"] == "red"
    ro = build_model(snap(health={"code": "storage_error", "message": "x"}), UiState())
    assert _alert_rows(ro)[0]["title"] == "采集存储" and _alert_rows(ro)[0]["dot"] == "red"
    unreadable = build_model(snap(read_error=True), UiState())
    assert _alert_rows(unreadable)[0]["title"] == "无法读取数据库"
    privacy = build_model(snap(health={"code": "disk_full", "message": "x"}, disk_free_bytes=0), UiState(tab="privacy"))
    disk = row_by_title(privacy, "磁盘剩余")
    assert disk["value"]["text"] == "0 MB" and disk["value"]["tone"] == "red"


def test_hud_panel_alerts_no_fix_button_for_hud_hung_or_dead():
    from penpal.runtime.evaluate import Issue, RuntimeStatus, RuntimeState

    issue_hung = Issue("hud", "error", "HUD 无响应", "HUD hung", code="hud.hung")
    issue_dead = Issue("hud", "warning", "HUD 进程已退出", "HUD dead", code="hud.dead")
    issue_icon = Issue("hud", "error", "菜单栏图标被系统隐藏", "Icon hidden", code="hud.icon_hidden")

    # 1. hud.hung must not offer repair in HUD panel
    s_hung = snap(runtime_status=RuntimeStatus(RuntimeState.RUNNING, issues=(issue_hung,)))
    m_hung = build_model(s_hung, UiState())
    r_hung = _alert_rows(m_hung)[0]
    assert r_hung["value"] is None
    assert r_hung["action"] is None

    # 2. hud.dead must not offer repair in HUD panel
    s_dead = snap(runtime_status=RuntimeStatus(RuntimeState.RUNNING, issues=(issue_dead,)))
    m_dead = build_model(s_dead, UiState())
    r_dead = _alert_rows(m_dead)[0]
    assert r_dead["value"] is None
    assert r_dead["action"] is None

    # 3. hud.icon_hidden DOES offer repair
    s_icon = snap(runtime_status=RuntimeStatus(RuntimeState.RUNNING, issues=(issue_icon,)))
    m_icon = build_model(s_icon, UiState())
    r_icon = _alert_rows(m_icon)[0]
    assert r_icon["value"]["text"] == "修复"
    assert r_icon["action"] == "issue.repair"
    assert r_icon["args"] == {"issue_code": "hud.icon_hidden"}



def test_at09_one_source_unavailable_shows_reason_from_availability_codes():
    expected = {
        "file_missing": "找不到来源文件", "permission_denied": "完全磁盘访问", "db_locked": "占用",
        "unsupported_version": "格式不支持", "probe_error": "检测失败",
    }
    for code, text in expected.items():
        s = snap(sources=_sources(chrome_history={"unavailable_reason": code}))
        page = build_model(s, UiState(page="group:browser"))
        r = row_by_title(page, "Chrome 历史")
        assert r["dot"] == "amber" and text in r["sub"], code
        assert row_by_title(page, "Safari 历史")["dot"] == "ok"  # only that one source is affected
        has_fix = any(x["kind"] == "button" and x["args"] == {"pane": "full_disk"} for x in page["sections"])
        assert has_fix == (code == "permission_denied")


def test_source_never_seen_on_this_mac_is_not_an_alert():
    s = snap(sources=_sources(chrome_history={"unavailable_reason": "not_found"}))
    m = build_model(s, UiState())
    assert m["status"]["tone"] == "ok"
    assert row_by_title(build_model(s, UiState(page="group:browser")), "Chrome 历史")["sub"] == "没有检测到"


# ── second level and other tabs ─────────────────────────────────────────────────

def test_group_page_switches_and_back():
    m = build_model(snap(), UiState(page="group:live"))
    assert m["title"] == "实时采集" and m["back"] == {"label": "概览", "action": "nav.back"}
    kb = row_by_title(m, "键盘片段")
    assert kb["trail"] == {"type": "switch", "on": True}
    assert (kb["action"], kb["args"]) == ("source.toggle", {"name": "keyboard_chunk", "enable": False})
    ax = row_by_title(m, "当前看到的正文")
    assert ax["trail"]["on"] is False and ax["sub"] == "已关闭" and ax["dot"] == "grey"
    assert ax["args"] == {"name": "ax_text", "enable": True}


def test_unconfigured_capture_source_has_no_switch():
    s = snap(sources=_sources(ax_text={"configured": False}))
    r = row_by_title(build_model(s, UiState(page="group:live")), "当前看到的正文")
    assert r["trail"] is None and r["action"] is None and "配置文件" in r["sub"]


def test_vanished_page_falls_back_to_tab_home():
    for page in ("group:nope", "host:ghost", "perm:accessibility", "reveal", "bogus"):
        m = build_model(snap(), UiState(page=page))
        assert m["view"]["page"] == "" and m["back"] is None and m["title"] == "概览"


def test_access_tab_hosts_and_recent_queries():
    m = build_model(snap(), UiState(tab="access"))
    assert m["title"] == "AI 访问"
    hosts = m["sections"][0]
    assert hosts["header"] == "已连接的 AI"
    assert titles(hosts) == ["Claude Code", "Codex", "连接新的 AI"]
    assert hosts["rows"][0]["value"]["text"] == "今天 24 次" and hosts["rows"][0]["args"] == {"page": "host:claude"}
    recent = m["sections"][1]
    assert recent["header"] == "最近查询" and "不记录查到的内容" in recent["footer"]
    assert [r["value"]["text"] for r in recent["rows"]] == ["找到 5 条", "找到 1 条", "没有找到", "已拒绝：没有授权"]
    assert recent["rows"][3]["value"]["tone"] == "red"
    assert recent["rows"][0]["title"] == "Claude Code · 搜索" and recent["rows"][1]["title"] == "Claude Code · 打开出处"


def test_host_page_scope_usage_rotate_revoke():
    m = build_model(snap(), UiState(tab="access", page="host:claude"))
    assert m["title"] == "Claude Code" and m["back"]["label"] == "AI 访问"
    buckets = m["sections"][0]
    assert [(r["title"], r["trail"]["on"]) for r in buckets["rows"]] == [
        ("工作", True), ("个人", True), ("私密", False), ("财务", False)]
    assert "云端" in buckets["footer"]
    notice = T(
        "Work 包含 Claude Code、Codex 等 AI 工具里的全部对话；私人项目可用 `penpal rules add --scope project …` 归到 Personal。",
        "Work includes all conversations in Claude Code, Codex and other AI tools; move a private project to Personal with `penpal rules add --scope project …`.",
    )
    assert m["sections"][1]["header"] == "授权范围提示"
    assert notice in m["sections"][1]["footer"]
    usage = next(s for s in m["sections"] if s.get("header") == "使用情况")
    assert [r["value"]["text"] for r in usage["rows"]][1:] == ["132 次", "3 次"]
    buttons = [s for s in m["sections"] if s["kind"] == "button"]
    assert [(b["label"], b["tone"], b["action"], b["args"]) for b in buttons] == [
        ("轮换密钥", "primary", "grant.rotate", {"client_id": "claude"}),
        ("撤销 Claude Code 的授权", "danger", "confirm.ask", {"kind": "revoke", "client_id": "claude"}),
    ]
    codex = build_model(snap(), UiState(tab="access", page="host:codex"))
    assert codex["sections"][1]["header"] == "授权范围提示"
    assert codex["sections"][2]["header"] == "限定来源" and titles(codex["sections"][2]) == ["Claude Code"]

    disabled = build_model(
        snap(sources=_sources(claude_code={"enabled": False})),
        UiState(tab="access", page="host:codex"),
    )
    assert all(section.get("header") != "授权范围提示" for section in disabled["sections"])


def test_new_host_page_defaults_to_work_only():
    m = build_model(snap(hosts=()), UiState(tab="access", page="host:new"))
    assert m["title"] == "连接新的 AI"
    picks, buckets = m["sections"][0], m["sections"][1]
    assert titles(picks) == ["Claude Code", "Codex", "Cursor", "其他 MCP 客户端"]
    assert [r["trail"]["on"] for r in picks["rows"]] == [True, False, False, False]
    assert [(r["title"], r["trail"]["on"]) for r in buckets["rows"]] == [
        ("工作", True), ("个人", False), ("私密", False), ("财务", False)]
    assert m["sections"][2]["action"] == "grant.create" and m["sections"][2]["args"] == {"host": "claude"}
    taken = build_model(snap(), UiState(tab="access", page="host:new"))
    assert titles(taken["sections"][0]) == ["Cursor", "其他 MCP 客户端"]


def test_reveal_page_shows_secret_once_with_copy_and_done():
    ui = UiState(tab="access", page="reveal", reveal=Reveal("授权成功", "SECRET-CONFIG", "claude"))
    m = build_model(snap(), ui)
    assert m["title"] == "授权成功" and m["back"] == {"label": "完成", "action": "nav.back"}
    code = next(s for s in m["sections"] if s["kind"] == "code")
    assert code["text"] == "SECRET-CONFIG"
    assert next(s for s in m["sections"] if s["kind"] == "button")["action"] == "reveal.copy"
    gone = build_model(snap(), UiState(tab="access", page="reveal"))  # secret gone: no page, no text anywhere
    assert "SECRET-CONFIG" not in json.dumps(gone, ensure_ascii=False)


def test_privacy_tab_keyboard_note_chat_exclusions_storage_delete():
    m = build_model(snap(), UiState(tab="privacy"))
    assert m["title"] == "设置"
    scope = m["sections"][0]
    kb = scope["rows"][0]
    assert kb["title"] == "键盘片段"
    assert kb["sub"] == "过滤为尽力而为，可能记录到敏感内容，返回云端 AI 时可能出站"  # issue #29
    assert kb["trail"] == {"type": "switch", "on": True}
    assert (kb["action"], kb["args"]) == ("keyboard.toggle", {"enable": False})
    assert scope["rows"][1]["value"]["text"] == "1 个开启" and scope["rows"][1]["args"] == {"page": "chat"}
    assert scope["rows"][2]["value"]["text"] == "2 项" and scope["rows"][2]["args"] == {"page": "exclude"}
    storage = m["sections"][1]
    assert [(r["title"], r["value"]["text"]) for r in storage["rows"]] == [
        ("已存储", "38.2 万条 · 1.4 GB"), ("保留时间", "永久保留"), ("磁盘剩余", "86.0 GB")]
    delete = m["sections"][2]["rows"][0]
    assert delete["title"] == "删除数据" and delete["title_tone"] == "red" and delete["args"] == {"page": "delete"}
    quit_sec = m["sections"][3]
    assert titles(quit_sec) == ["退出 HUD"]
    assert quit_sec["rows"][0]["title_tone"] == "red"
    assert quit_sec["rows"][0]["action"] == "hud.quit"
    assert "退出后菜单栏图标消失" in quit_sec["footer"]


def test_keyboard_off_state_and_command_args():
    m = build_model(snap(keyboard_on=False), UiState(tab="privacy"))
    kb = m["sections"][0]["rows"][0]
    assert kb["trail"]["on"] is False and kb["args"] == {"enable": True}


def test_chat_app_page():
    m = build_model(snap(), UiState(tab="privacy", page="chat"))
    assert m["title"] == "聊天 App" and m["back"]["label"] == "设置"
    wechat, slack = m["sections"][0]["rows"]
    assert (wechat["title"], wechat["trail"]["on"], wechat["args"]) == ("微信", False, {"name": "WeChat", "allow": True})
    assert (slack["trail"]["on"], slack["args"]) == (True, {"name": "Slack", "allow": False})
    assert "私密" in m["sections"][0]["footer"]


def test_settings_tab_viewmodel_label_and_quit_hud_section():
    m = build_model(snap(), UiState(tab="privacy"))
    assert m["title"] == "设置"
    assert [t["label"] for t in m["tabs"]][2] == "设置"
    last_section = m["sections"][-1]
    assert titles(last_section) == ["退出 HUD"]
    row = last_section["rows"][0]
    assert row["title"] == "退出 HUD"
    assert row["title_tone"] == "red"
    assert row["action"] == "hud.quit"
    assert last_section["footer"] == "退出后菜单栏图标消失，后台记录继续。重新打开：打开 Penpal.app，或运行 penpal hud start。"


def test_exclusion_page_is_a_read_only_list():
    m = build_model(
        snap(exclusions=(("app", "1Password"), ("content", "password"), ("domain", "example.com"))),
        UiState(tab="privacy", page="exclude"),
    )
    rows = m["sections"][0]["rows"]
    assert [(r["title"], r["sub"]) for r in rows] == [
        ("1Password", "App"), ("password", "内容关键词"), ("example.com", "网站域名（含子域名）")]
    assert all(r["action"] is None for r in rows)
    assert "penpal privacy block-site" in m["sections"][0]["footer"]


def test_delete_page_with_dry_run_counts_and_danger_button():
    preview = {"raw_events": 128, "raw_events_fts": 128, "evidence": 40, "mcp_returned_results": 2, "mcp_feedback": 1, "sessions": 0, "search_docs": 0}
    m = build_model(snap(), UiState(tab="privacy", page="delete", delete_scope="source:safari_history"), preview)
    assert m["title"] == "删除数据"
    scope = m["sections"][0]
    assert titles(scope)[:2] == ["今天的全部记录", "最近 1 小时"]
    chosen = [r["title"] for r in scope["rows"] if r["trail"]["on"]]
    assert chosen == ["Safari 历史（按来源）"]
    counts = next(s for s in m["sections"] if s.get("header") == "将删除")
    assert [(r["title"], r["value"]["text"]) for r in counts["rows"]] == [
        ("记录原文", "128 条"), ("搜索索引", "128 条"), ("出处", "40 条"), ("相关 AI 查询记录", "3 条")]
    assert "原始文件不受影响" in counts["footer"]
    btn = m["sections"][-1]
    assert (btn["label"], btn["tone"], btn["action"], btn["args"]) == ("删除 128 条记录", "danger", "confirm.ask", {"kind": "purge"})


def test_delete_page_without_matches_has_no_delete_button():
    m = build_model(snap(), UiState(tab="privacy", page="delete"), {"raw_events": 0})
    assert not any(s["kind"] == "button" for s in m["sections"])
    assert build_model(snap(), UiState(tab="privacy", page="delete"), None)["sections"][-1]["rows"][0]["title"] == "统计失败"


def test_delete_options_map_to_purge_selectors():
    opts = {o.id: o for o in delete_options(snap())}
    assert opts["today"].kwargs == {"today": True} and opts["hour"].kwargs == {"last_hours": 1}
    assert opts["source:safari_history"].kwargs == {"source": "safari_history"}
    assert opts["app:Notes"].kwargs == {"app": "Notes"}


# ── confirm dialog and toast ────────────────────────────────────────────────────

def test_confirm_dialog_only_for_revoke_and_delete():
    revoke = build_model(snap(), UiState(tab="access", page="host:claude", confirm={"kind": "revoke", "client_id": "claude"}))
    assert revoke["confirm"] == {"title": "撤销授权？", "body": "Claude Code 的下一次查询会被拒绝。之后可以重新连接。", "ok": "撤销"}
    purge = build_model(snap(), UiState(tab="privacy", page="delete", confirm={"kind": "purge"}), {"raw_events": 42})
    assert purge["confirm"]["title"] == "删除「今天的全部记录」？" and "42 条" in purge["confirm"]["body"]
    assert purge["confirm"]["ok"] == "删除"
    assert build_model(snap(), UiState())["confirm"] is None
    assert build_model(snap(), UiState(confirm={"kind": "other"}))["confirm"] is None


def test_toast_carries_an_id():
    m = build_model(snap(), UiState(toast=(7, "已复制")))
    assert m["toast"] == {"id": 7, "text": "已复制"}
    assert build_model(snap(), UiState())["toast"] is None


# ── the v3 rules, checked over every page ──────────────────────────────────────

def _every_model():
    s = snap()
    bad = snap(health={"code": "daemon_not_running", "message": "x"}, paused=False)
    pages = [("overview", ""), ("overview", "group:live"), ("overview", "group:browser"), ("access", ""), ("access", "host:claude"),
             ("access", "host:new"), ("privacy", ""), ("privacy", "chat"), ("privacy", "exclude"), ("privacy", "delete")]
    for tab, page in pages:
        yield build_model(s, UiState(tab=tab, page=page), {"raw_events": 3})
    yield build_model(bad, UiState())
    yield build_model(snap(paused=True), UiState())


def test_all_pages_follow_the_v3_component_rules():
    for m in _every_model():
        json.dumps(m, ensure_ascii=False)  # plain data only
        assert len(m["tabs"]) == 3 and sum(t["current"] for t in m["tabs"]) == 1
        for sec in m["sections"]:
            assert sec["kind"] in {"list", "stats", "button", "code"}
            if sec["kind"] == "button":
                assert sec["tone"] in {"primary", "danger"}
            for r in sec.get("rows", []):
                assert r["trail"] is None or r["trail"]["type"] in {"switch", "check", "nav"}
                assert r["value"] is None or set(r["value"]) == {"text", "tone"}
                assert r["dot"] in {None, "ok", "amber", "red", "grey"}
                assert r["sub_tone"] in {None, "amber", "red"} and r["title_tone"] in {None, "red"}
        # at most two levels: a page is either a tab home or one step below it
        assert ":" not in m["view"]["page"].split(":", 1)[-1] or m["view"]["page"].startswith(("group:", "host:", "perm:"))
