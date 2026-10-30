"""HUD view-model: core snapshot + UI state -> one page as plain data (no AppKit, no I/O, no core calls).

The renderer (``render.py``) draws exactly this dict following the v3 interaction rules:
- fixed status bar and tab bar, one scroll area, at most two levels (tab page -> second-level page);
- one grouped list-row component: status dot + title + sub, trailing one of value / switch / check / chevron;
- one button style (primary, danger), one confirm dialog (delete and revoke only), a toast for feedback;
- colour only means status: tones are "ok" / "amber" / "red" / "grey".

A row has an optional value and an optional trailing control (switch / check / chevron).
Row / section actions are ``{"action": "<name>", "args": {...}}`` and are routed by ``controller.HudController``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from penpal.runtime import ICON_OCCLUDED_HINT_ZH, RuntimeState, state_message, state_text
from penpal.hud.snapshot import (
    BUCKET_LABELS,
    GROUPS,
    HOST_LABELS,
    Snapshot,
    SourceInfo,
    source_label,
)

TABS = (("overview", "概览"), ("access", "AI 访问"), ("privacy", "设置"))
KEYBOARD_RISK_NOTE = "过滤为尽力而为，可能记录到敏感内容，返回云端 AI 时可能出站"
EGRESS_FOOTER = "查到的内容会进入它的对话，可能发送到它的云端服务。"

REASON_TEXT = {
    "file_missing": "找不到来源文件",
    "permission_denied": "读取被拒绝 · 可能需要「完全磁盘访问」",
    "db_locked": "数据库被占用，稍后重试",
    "unsupported_version": "文件格式不支持",
    "not_found": "未配置",
    "probe_error": "检测失败",
}
TOOL_TEXT = {"search_context": "搜索", "get_evidence": "打开出处", "get_timeline": "时间线", "rate_result": "评价"}
DENY_TEXT = {
    "token_revoked": "已拒绝：授权已撤销",
    "token_expired": "已拒绝：授权已过期",
    "token_unknown": "已拒绝：没有授权",
    "token_missing": "已拒绝：没有授权",
    "operation_denied": "已拒绝：操作未授权",
    "client_missing": "已拒绝：客户端不匹配",
    "client_invalid": "已拒绝：客户端不匹配",
    "client_mismatch": "已拒绝：客户端不匹配",
    "client_not_whitelisted": "已拒绝：客户端不匹配",
}
SCOPE_TYPE_TEXT = {"app": "App", "window": "窗口标题", "source": "来源", "content": "内容关键词", "domain": "网站域名（含子域名）"}
DELETE_COUNT_ROWS = (
    ("raw_events", "记录原文"),
    ("raw_events_fts", "搜索索引"),
    ("evidence", "出处"),
    ("sessions", "会话"),
)
NEW_HOST_CHOICES = ("claude", "codex", "cursor", "generic")


# ── UI state (owned by the controller, passed in so the builder stays pure) ─────

@dataclass(frozen=True)
class Reveal:
    """A just-created secret, shown once. Lives only in controller memory; never persisted or logged."""
    title: str
    text: str
    client_id: str = ""


@dataclass(frozen=True)
class UiState:
    tab: str = "overview"
    page: str = ""                                # "" = the tab's home
    delete_scope: str = "today"
    confirm: Optional[dict] = None                # {"kind": "revoke", "client_id"} | {"kind": "purge"}
    new_host: str = "claude"
    new_buckets: tuple[str, ...] = ("Work",)
    toast: Optional[tuple[int, str]] = None       # (id, text)
    reveal: Optional[Reveal] = None
    rechecked_at: Optional[str] = None


@dataclass(frozen=True)
class DeleteOption:
    id: str
    label: str
    kwargs: dict = field(default_factory=dict)    # purge_selector() keyword arguments
    rows: int = 0


def delete_options(snap: Snapshot) -> list[DeleteOption]:
    opts = [DeleteOption("today", "今天的全部记录", {"today": True}), DeleteOption("hour", "最近 1 小时", {"last_hours": 1})]
    for name, rows in snap.delete_sources:
        opts.append(DeleteOption(f"source:{name}", f"{source_label(name)}（按来源）", {"source": name}, rows))
    for name, rows in snap.delete_apps:
        opts.append(DeleteOption(f"app:{name}", f"App：{name}（精确匹配名称）", {"app": name}, rows))
    return opts


# ── small helpers ────────────────────────────────────────────────────────────────

def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def rel_time(now: datetime, ts: Optional[str]) -> str:
    then = _parse(ts)
    if then is None:
        return ""
    sec = max(0, int((now - then).total_seconds()))
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    if sec < 2 * 86400:
        return "昨天"
    return f"{sec // 86400} 天前"


def clock(ts: str) -> str:
    then = _parse(ts)
    return then.astimezone().strftime("%H:%M") if then else ""


def fmt_count(n: int) -> str:
    return f"{n:,}"


def fmt_records(n: int) -> str:
    return f"{n / 10000:.1f} 万条" if n >= 10000 else f"{n} 条"


def fmt_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    return f"{max(n, 0) / (1 << 20):.0f} MB"


def short_perm(label: str) -> str:
    return label.split("（")[0]


def host_name(client_id: str) -> str:
    return HOST_LABELS.get(client_id, client_id)


def _plural_state(on: int, off: int) -> str:
    return f"{on} 开" + (f" · {off} 关" if off else "")


# ── row / section constructors (the single row component) ───────────────────────

def row(title: str, *, dot: Optional[str] = None, sub: str = "", sub_tone: Optional[str] = None,
        title_tone: Optional[str] = None, value: str = "", value_tone: Optional[str] = None,
        switch: Optional[bool] = None, check: Optional[bool] = None, nav: bool = False,
        action: Optional[str] = None, args: Optional[dict] = None) -> dict:
    trail: Optional[dict] = None
    if switch is not None:
        trail = {"type": "switch", "on": switch}
    elif check is not None:
        trail = {"type": "check", "on": check}
    elif nav:
        trail = {"type": "nav"}
    return {"dot": dot, "title": title, "title_tone": title_tone, "sub": sub, "sub_tone": sub_tone,
            "value": {"text": value, "tone": value_tone} if value else None,
            "trail": trail, "action": action, "args": args or {}}


def lst(rows: list[dict], header: str = "", footer: str = "") -> dict:
    return {"kind": "list", "header": header, "footer": footer, "rows": rows}


def button(label: str, action: str, args: Optional[dict] = None, tone: str = "primary") -> dict:
    return {"kind": "button", "label": label, "tone": tone, "action": action, "args": args or {}}


# ── status ───────────────────────────────────────────────────────────────────────

def has_issue(src: SourceInfo) -> bool:
    """Something to fix: an enabled source whose original cannot be read, or whose permission was lost.
    ``not_found`` (never set up on this Mac) is a state, not a fault."""
    if not (src.enabled and src.configured):
        return False
    return bool(src.missing_permissions) or src.unavailable_reason not in (None, "not_found")


def alerts(snap: Snapshot) -> list[tuple[str, dict]]:
    """(tone, row) for runtime and source issues, red first."""
    from penpal.services.repair import is_fixable_in_hud

    out: list[tuple[str, dict]] = []
    status = snap.runtime_status
    if snap.read_error:
        out.append(("red", row("无法读取数据库", dot="red", sub="运行 penpal doctor 查看详情", sub_tone="red")))
    if status is not None and status.state in {
        RuntimeState.NOT_INSTALLED, RuntimeState.NOT_RUNNING, RuntimeState.HUNG, RuntimeState.OUTDATED,
    }:
        code = f"daemon.{status.state.value}"
        fixable = is_fixable_in_hud(code)
        out.append(("red", row(
            state_text(status.state, language="zh"), dot="red", sub=state_message(status, language="zh"), sub_tone="red",
            value="修复" if fixable else "", value_tone="blue" if fixable else None,
            action="daemon.restart" if fixable else None,
        )))
    elif status is not None and status.state == RuntimeState.STARTING:
        out.append(("amber", row(state_text(status.state, language="zh"), dot="amber", sub=state_message(status, language="zh"), sub_tone="amber")))
    if status is not None:
        for issue in status.issues:
            if issue.component == "daemon":
                continue
            tone = "red" if issue.severity == "error" else "amber"
            message = issue.message_zh
            label = issue.label_zh or issue.component
            fixable = is_fixable_in_hud(issue)
            code = getattr(issue, "code", "") or issue.component
            out.append((tone, row(
                label, dot=tone, sub=message, sub_tone=tone,
                value="修复" if fixable else "", value_tone="blue" if fixable else None,
                action="issue.repair" if fixable else None,
                args={"issue_code": code} if fixable else {},
            )))
    for issue in snap.perm_issues:
        names = "、".join(source_label(s) for s in issue.sources)
        out.append(("amber", row(
            f"缺少「{short_perm(issue.label)}」权限", dot="amber", sub=f"影响：{names} · 点按处理",
            sub_tone="amber", nav=True, action="nav.page", args={"page": f"perm:{issue.key}"},
        )))
    for src in snap.sources:
        if has_issue(src) and src.unavailable_reason:
            out.append(("amber", row(f"{src.label} 不可用", dot="amber", sub=REASON_TEXT.get(src.unavailable_reason, "不可用") + " · 点按处理",
                                     sub_tone="amber", nav=True, action="nav.page", args={"page": f"group:{src.group}"})))
    out.sort(key=lambda t: 0 if t[0] == "red" else 1)
    return out


def status_bar(snap: Snapshot, ui: Optional[UiState] = None) -> dict:
    status = snap.runtime_status
    if status is not None and status.state == RuntimeState.PAUSED:
        return {"code": status.code, "tone": "grey", "text": state_text(status.state, language="zh"), "sub": "重启后仍保持暂停 · 不会补记"}
    found = alerts(snap)
    reds = [t for t in found if t[0] == "red"]
    last = rel_time(snap.now, snap.last_write_ts)
    label = state_text(status.state, language="zh") if status is not None else "未知"
    if ui and ui.rechecked_at:
        if found:
            sub = f"已重新检测 {ui.rechecked_at} · {len(found)} 个问题"
        else:
            sub = f"已重新检测 {ui.rechecked_at} · 正常"
        return {"code": status.code if status else "unknown", "tone": "red" if reds else "amber" if found else "ok", "text": label, "sub": sub}
    if reds:
        sub = f"{len(found)} 个问题" + (f" · {last}最后写入" if last else "")
        return {"code": status.code if status else "unknown", "tone": "red", "text": label, "sub": sub}
    if found:
        return {"code": status.code if status else "unknown", "tone": "amber", "text": label,
                "sub": f"{len(found)} 个问题需要处理"}
    return {"code": status.code if status else "unknown", "tone": "ok", "text": label,
            "sub": f"一切正常 · {last}写入" if last else "一切正常 · 还没有记录"}


# ── pages ────────────────────────────────────────────────────────────────────────

def _source_row(src: SourceInfo, now: datetime) -> dict:
    issue = has_issue(src)
    if not src.configured:
        return row(src.label, dot="grey", sub="配置文件里已关闭")
    if issue and src.unavailable_reason not in (None, "not_found"):
        sub, tone = REASON_TEXT.get(src.unavailable_reason, "不可用"), "amber"
    elif issue:
        sub, tone = "缺少系统权限 · 在概览里处理", "amber"
    elif src.enabled and src.unavailable_reason == "not_found":
        sub, tone = "没有检测到", None
    elif not src.enabled:
        sub, tone = "已关闭", None
    elif src.last_ts:
        sub, tone = f"{rel_time(now, src.last_ts)} · 今天 {src.today} 条", None
    else:
        sub, tone = "还没有记录", None
    dot = "amber" if issue else ("ok" if src.enabled else "grey")
    return row(src.label, dot=dot, sub=sub, sub_tone=tone, switch=src.enabled, action="source.toggle",
               args={"name": src.name, "enable": not src.enabled})


def _group_sources(snap: Snapshot, gid: str) -> list[SourceInfo]:
    return [s for s in snap.sources if s.group == gid]


def _group_counts(srcs: list[SourceInfo]) -> tuple[int, int, bool]:
    live = [s for s in srcs if s.configured]
    on = sum(1 for s in live if s.enabled)
    bad = any(has_issue(s) for s in live)
    return on, len(live) - on, bad


def page_overview(snap: Snapshot) -> list[dict]:
    sections: list[dict] = []
    found = [] if snap.paused else alerts(snap)
    if found:
        sections.append(lst([r for _, r in found], "需要处理"))
    sections.append({"kind": "stats", "header": "今天", "stats": [
        {"v": fmt_count(snap.today_records), "k": "新增记录"},
        {"v": fmt_count(snap.ai_queries_today), "k": "AI 查询"},
        {"v": fmt_count(len(snap.hosts)), "k": "AI 已连接"},
    ]})
    down = any(t == "red" for t, _ in found)
    rows = []
    for gid, name in GROUPS:
        srcs = _group_sources(snap, gid)
        if not srcs:
            continue
        on, off, bad = _group_counts(srcs)
        if down:
            dot, value, tone = "red", "已停止", None
        elif bad:
            n = sum(1 for s in srcs if has_issue(s))
            dot, value, tone = "amber", f"{n} 个需处理", "amber"
        else:
            dot, value, tone = "ok", _plural_state(on, off), None
        rows.append(row(name, dot=dot, value=value, value_tone=tone, nav=True, action="nav.page", args={"page": f"group:{gid}"}))
    sections.append(lst(rows, "数据来源"))
    return sections


def page_group(snap: Snapshot, gid: str) -> Optional[tuple[str, list[dict]]]:
    name = dict(GROUPS).get(gid)
    srcs = _group_sources(snap, gid)
    if name is None or not srcs:
        return None
    footer = "键盘片段只存过滤后的文字，不存逐个按键。" if gid == "live" else ""
    sections = [lst([_source_row(s, snap.now) for s in srcs], footer=footer)]
    if any(has_issue(s) and s.unavailable_reason == "permission_denied" for s in srcs):
        sections.append(button("打开系统设置 › 完全磁盘访问", "settings.open", {"pane": "full_disk"}))
    for issue in snap.perm_issues:
        if any(s.name in issue.sources for s in srcs):
            sections.append(button(f"打开系统设置 › {short_perm(issue.label)}", "settings.open", {"pane": issue.key}))
    return name, sections


def page_perm(snap: Snapshot, key: str) -> Optional[tuple[str, list[dict]]]:
    issue = next((i for i in snap.perm_issues if i.key == key), None)
    if issue is None:
        return None
    label = short_perm(issue.label)
    exes = [row(e.rstrip("/").rsplit("/", 1)[-1] or e, sub=e) for e in issue.executables]
    sections = [
        lst([row(source_label(s), dot="amber", sub="需要这项权限，目前没有在记录") for s in issue.sources], "受影响的来源"),
    ]
    if exes:
        sections.append(lst(exes, "需要授权的程序", "权限属于后台记录进程，不属于这个窗口；请授权给上面的程序。"))
    sections.append(button(f"打开系统设置 › {label}", "settings.open", {"pane": issue.key}))
    return label, sections


def _result_text(r) -> tuple[str, Optional[str]]:
    if not r.allowed:
        return DENY_TEXT.get(r.reason_code, "已拒绝"), "red"
    if r.result_count is None:
        return "完成", None
    return ("没有找到", None) if r.result_count == 0 else (f"找到 {r.result_count} 条", None)


def page_access(snap: Snapshot) -> list[dict]:
    rows = [
        row(host_name(h.client_id), dot="ok", sub=(f"{rel_time(snap.now, h.last_ts)}调用" if h.last_ts else "还没有调用"),
            value=(f"今天 {h.today} 次" if h.today else ""), nav=True, action="nav.page", args={"page": f"host:{h.client_id}"})
        for h in snap.hosts
    ]
    rows.append(row("连接新的 AI", nav=True, action="nav.page", args={"page": "host:new"}))
    sections = [lst(rows, "已连接的 AI")]
    if snap.audit:
        recent = []
        for r in snap.audit:
            text, tone = _result_text(r)
            recent.append(row(f"{host_name(r.client)} · {TOOL_TEXT.get(r.tool, r.tool)}", sub=clock(r.ts), value=text, value_tone=tone))
        sections.append(lst(recent, "最近查询", "只记录谁在什么时候查了、结果有几条，不记录查到的内容。"))
    return sections


def page_host(snap: Snapshot, client_id: str) -> Optional[tuple[str, list[dict]]]:
    h = next((x for x in snap.hosts if x.client_id == client_id), None)
    if h is None:
        return None
    from penpal.mcp.grant_notice import work_project_notice

    buckets = [row(BUCKET_LABELS[b], check=b in h.buckets) for b in BUCKET_LABELS]
    scope = [lst(buckets, "能看到的分类", EGRESS_FOOTER)]
    notice = work_project_notice(h.buckets, h.sources, [s.name for s in snap.sources if s.enabled])
    if notice:
        scope.append(lst([], "授权范围提示", notice))
    if h.sources is not None:
        scope.append(lst([row(source_label(s)) for s in h.sources], "限定来源"))
    usage = [
        row("最近一次", value=rel_time(snap.now, h.last_ts) or "—"),
        row("本周查询", value=f"{h.week} 次"),
        row("本周被拒绝的调用", value=f"{h.denied} 次"),
    ]
    if h.expires_at:
        usage.append(row("授权到期", value=(_parse(h.expires_at) or snap.now).astimezone().strftime("%m-%d %H:%M")))
    return host_name(h.client_id), scope + [
        lst(usage, "使用情况"),
        button("轮换密钥", "grant.rotate", {"client_id": h.client_id}),
        button(f"撤销 {host_name(h.client_id)} 的授权", "confirm.ask", {"kind": "revoke", "client_id": h.client_id}, "danger"),
    ]


def page_new_host(snap: Snapshot, ui: UiState) -> tuple[str, list[dict]]:
    taken = {h.client_id for h in snap.hosts}
    choices = [c for c in NEW_HOST_CHOICES if c not in taken]
    host = ui.new_host if ui.new_host in choices else (choices[0] if choices else "")
    sections: list[dict] = []
    if not choices:
        sections.append(lst([row("四类 AI 都已连接")], footer="要换范围，请先撤销再重新连接。"))
        return "连接新的 AI", sections
    sections.append(lst([row(HOST_LABELS[c], check=c == host, action="host.pick", args={"host": c}) for c in choices], "选择 AI"))
    sections.append(lst(
        [row(BUCKET_LABELS[b], switch=b in ui.new_buckets, action="host.bucket", args={"bucket": b}) for b in BUCKET_LABELS],
        "能看到的分类", "默认只给「工作」。" + EGRESS_FOOTER + "密钥只会显示一次，只保存它的哈希。"))
    sections.append(button("授权并生成配置", "grant.create", {"host": host}))
    return "连接新的 AI", sections


def page_reveal(reveal: Reveal) -> tuple[str, list[dict]]:
    return reveal.title, [
        lst([row("密钥只显示这一次", dot="amber", sub="关闭后无法再查看；只保存它的哈希。丢了就轮换一个新的。", sub_tone="amber")]),
        {"kind": "code", "header": "连接配置", "text": reveal.text},
        button("复制配置", "reveal.copy"),
    ]


def page_privacy(snap: Snapshot) -> list[dict]:
    chat_on = sum(1 for _, _, allowed in snap.chat_apps if allowed)
    storage = snap.runtime_status.components.get("storage") if snap.runtime_status else None
    full = storage is not None and storage.state == "storage_error" and storage.detail == "disk_full"
    disk = fmt_bytes(snap.disk_free_bytes) if snap.disk_free_bytes is not None else "—"
    return [
        lst([
            row("键盘片段", sub=KEYBOARD_RISK_NOTE, sub_tone="amber" if snap.keyboard_on else None, switch=snap.keyboard_on,
                action="keyboard.toggle", args={"enable": not snap.keyboard_on}),
            row("聊天 App", value=f"{chat_on} 个开启", nav=True, action="nav.page", args={"page": "chat"}),
            row("不记录的 App 和网站", value=f"{len(snap.exclusions)} 项", nav=True, action="nav.page", args={"page": "exclude"}),
        ], "采集范围", "只存过滤后的文字片段，不存逐个按键；和其他来源一样，只有获得对应分类授权的 AI 能查到。"),
        lst([
            row("已存储", value=f"{fmt_records(snap.total_records)} · {fmt_bytes(snap.db_bytes)}"),
            row("保留时间", value="永久保留"),
            row("磁盘剩余", value=disk, value_tone="red" if full else None),
        ], "存储"),
        lst([row("删除数据", title_tone="red", nav=True, action="nav.page", args={"page": "delete"})]),
        lst(
            [row("退出 HUD", title_tone="red", action="hud.quit")],
            footer=f"退出后菜单栏图标消失，后台记录继续。重新打开：打开 Penpal.app，或运行 penpal hud start。{ICON_OCCLUDED_HINT_ZH}。",
        ),
    ]


def page_chat(snap: Snapshot) -> list[dict]:
    rows = [row(label, switch=allowed, action="chat.toggle", args={"name": key, "allow": not allowed})
            for key, label, allowed in snap.chat_apps]
    return [lst(rows, footer="默认都不记录。打开后，这个 App 的内容进入「私密」分类，只有授权了「私密」的 AI 能查到。"
                             "更改约 1 分钟内生效。")]


def page_exclude(snap: Snapshot) -> list[dict]:
    rows = [row(value or "（空）", sub=SCOPE_TYPE_TEXT.get(kind, kind)) for kind, value in snap.exclusions]
    footer = "这里的内容一律不记录。网站域名用 penpal privacy block-site / unblock-site 管理；新事件约 1 分钟内按新配置处理。其他策略用 penpal rules add / disable。"
    return [lst(rows, footer=footer)] if rows else [lst([row("还没有规则")], footer=footer)]


def page_delete(snap: Snapshot, ui: UiState, preview: Optional[dict[str, int]]) -> list[dict]:
    opts = delete_options(snap)
    chosen = next((o for o in opts if o.id == ui.delete_scope), opts[0])
    sections = [lst([row(o.label, check=o.id == chosen.id, action="delete.pick", args={"scope": o.id}) for o in opts], "删除范围")]
    if preview is None:
        sections.append(lst([row("统计失败", sub="运行 penpal purge --dry-run 查看", sub_tone="red")], "将删除"))
        return sections
    n = preview.get("raw_events", 0)
    shown = [row(label, value=f"{preview.get(key, 0)} 条") for key, label in DELETE_COUNT_ROWS if preview.get(key, 0)]
    related = preview.get("mcp_returned_results", 0) + preview.get("mcp_feedback", 0)
    if related:
        shown.append(row("相关 AI 查询记录", value=f"{related} 条"))
    foot = "只删 Penpal 里的副本，原始文件不受影响；若不想再导入，请先在数据来源里关闭它。"
    if n == 0:
        sections.append(lst([row("没有可删除的内容")], "将删除", foot))
    else:
        sections.append(lst(shown, "将删除", foot))
        sections.append(button(f"删除 {n} 条记录", "confirm.ask", {"kind": "purge"}, "danger"))
    return sections


# ── confirm dialog ───────────────────────────────────────────────────────────────

def build_confirm(ui: UiState, snap: Snapshot, preview: Optional[dict[str, int]]) -> Optional[dict]:
    c = ui.confirm
    if not c:
        return None
    if c.get("kind") == "revoke":
        h = next((x for x in snap.hosts if x.client_id == c.get("client_id")), None)
        if h is None:
            return None
        return {"title": "撤销授权？", "body": f"{host_name(h.client_id)} 的下一次查询会被拒绝。之后可以重新连接。", "ok": "撤销"}
    if c.get("kind") == "purge":
        chosen = next((o for o in delete_options(snap) if o.id == ui.delete_scope), None)
        if chosen is None or not preview:
            return None
        n = preview.get("raw_events", 0)
        return {"title": f"删除「{chosen.label}」？",
                "body": f"共 {n} 条记录及其索引和出处。删除后不能恢复，AI 也再查不到。", "ok": "删除"}
    return None


# ── assembly ─────────────────────────────────────────────────────────────────────

def _back_label(page: str) -> str:
    if page.startswith(("group:", "perm:")):
        return "概览"
    if page.startswith("host:") or page == "reveal":
        return "AI 访问"
    return "设置"


def build_model(snap: Snapshot, ui: UiState, delete_preview: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """The full render state for the current tab / page. Unknown or vanished pages fall back to the tab home."""
    page_title, sections, page = "", [], ui.page
    if page:
        found: Optional[tuple[str, list[dict]]] = None
        kind, _, arg = page.partition(":")
        if kind == "group":
            found = page_group(snap, arg)
        elif kind == "perm":
            found = page_perm(snap, arg)
        elif kind == "host" and arg == "new":
            found = page_new_host(snap, ui)
        elif kind == "host":
            found = page_host(snap, arg)
        elif page == "chat":
            found = ("聊天 App", page_chat(snap))
        elif page == "exclude":
            found = ("不记录的 App 和网站", page_exclude(snap))
        elif page == "delete":
            found = ("删除数据", page_delete(snap, ui, delete_preview))
        elif page == "reveal" and ui.reveal is not None:
            found = page_reveal(ui.reveal)
        if found is None:
            page = ""
        else:
            page_title, sections = found
    if not page:
        titles = dict(TABS)
        page_title = titles.get(ui.tab, "概览")
        sections = {"overview": page_overview, "access": page_access, "privacy": page_privacy}.get(ui.tab, page_overview)(snap)

    found_alerts = [] if snap.paused else alerts(snap)
    badge = None
    if found_alerts:
        badge = "red" if any(t == "red" for t, _ in found_alerts) else "amber"
    bar = status_bar(snap, ui)
    return {
        "status": bar,
        "pause": {"label": "继续记录" if snap.paused else "暂停", "action": "capture.resume" if snap.paused else "capture.pause"},
        "dim": snap.paused,
        "view": {"tab": ui.tab, "page": page},
        "title": page_title,
        "back": {"label": "完成" if page == "reveal" else _back_label(page), "action": "nav.back"} if page else None,
        "sections": sections,
        "tabs": [{"id": tid, "label": label, "current": tid == ui.tab, "badge": badge if tid == "overview" else None}
                 for tid, label in TABS],
        "confirm": build_confirm(ui, snap, delete_preview),
        "toast": {"id": ui.toast[0], "text": ui.toast[1]} if ui.toast else None,
    }
