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

from penpal.i18n import T, current_lang
from penpal.runtime import RuntimeState, state_message, state_text
from penpal.hud.snapshot import (
    BUCKET_LABELS,
    GROUPS,
    HOST_LABELS,
    Snapshot,
    SourceInfo,
    source_label,
)

TABS = (
    ("overview", ("概览", "Overview")),
    ("access", ("AI 访问", "AI Access")),
    ("privacy", ("设置", "Settings")),
)
KEYBOARD_RISK_NOTE = ("过滤不保证完整，可能记下敏感内容，并随查询发往云端 AI", "Filtering isn't perfect: sensitive text may be kept and sent to cloud AI with results")
EGRESS_FOOTER = ("查到的内容会进入它的对话，可能发往云端。", "Results go into its chat and may be sent to the cloud.")

REASON_TEXT = {
    "file_missing": ("找不到来源文件", "Source file not found"),
    "permission_denied": ("读取被拒绝 · 可能需要「完全磁盘访问」", "Permission denied · Full Disk Access may be needed"),
    "db_locked": ("数据库被占用，稍后重试", "Database busy, retry later"),
    "unsupported_version": ("文件格式不支持", "Unsupported file format"),
    "not_found": ("未配置", "Not configured"),
    "probe_error": ("检测失败", "Probe failed"),
}
TOOL_TEXT = {
    "search_context": ("搜索", "Search"),
    "get_evidence": ("打开出处", "Evidence"),
    "get_timeline": ("时间线", "Timeline"),
    "rate_result": ("评价", "Feedback"),
}
DENY_TEXT = {
    "token_revoked": ("已拒绝：授权已撤销", "Denied: Token revoked"),
    "token_expired": ("已拒绝：授权已过期", "Denied: Token expired"),
    "token_unknown": ("已拒绝：没有授权", "Denied: Unauthorized"),
    "token_missing": ("已拒绝：没有授权", "Denied: Unauthorized"),
    "operation_denied": ("已拒绝：操作未授权", "Denied: operation not allowed"),
    "client_missing": ("已拒绝：客户端不匹配", "Denied: Client mismatch"),
    "client_invalid": ("已拒绝：客户端不匹配", "Denied: Client mismatch"),
    "client_mismatch": ("已拒绝：客户端不匹配", "Denied: Client mismatch"),
    "client_not_whitelisted": ("已拒绝：客户端不匹配", "Denied: Client mismatch"),
}
SCOPE_TYPE_TEXT = {
    "app": ("App", "App"),
    "window": ("窗口标题", "Window Title"),
    "source": ("来源", "Source"),
    "content": ("内容关键词", "Keyword"),
    "domain": ("网站域名（含子域名）", "Domain (inc. subdomains)"),
}
DELETE_COUNT_ROWS = (
    ("raw_events", ("记录原文", "Raw Events")),
    ("raw_events_fts", ("搜索索引", "Search Index")),
    ("evidence", ("出处", "Evidence")),
    ("sessions", ("会话", "Sessions")),
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
    opts = [
        DeleteOption("today", T("今天的全部记录", "All Today's Records"), {"today": True}),
        DeleteOption("hour", T("最近 1 小时", "Past 1 Hour"), {"last_hours": 1}),
    ]
    for name, rows in snap.delete_sources:
        label = source_label(name)
        opt_label = T("{label}（按来源）", "{label} (by source)").format(label=label)
        opts.append(DeleteOption(f"source:{name}", opt_label, {"source": name}, rows))
    for name, rows in snap.delete_apps:
        opt_label = T("App：{name}（精确匹配名称）", "App: {name} (exact match)").format(name=name)
        opts.append(DeleteOption(f"app:{name}", opt_label, {"app": name}, rows))
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
        return T("刚刚", "just now")
    if sec < 3600:
        return T("{n} 分钟前", "{n}m ago").format(n=sec // 60)
    if sec < 86400:
        return T("{n} 小时前", "{n}h ago").format(n=sec // 3600)
    if sec < 2 * 86400:
        return T("昨天", "yesterday")
    return T("{n} 天前", "{n}d ago").format(n=sec // 86400)


def clock(ts: str) -> str:
    then = _parse(ts)
    return then.astimezone().strftime("%H:%M") if then else ""


def fmt_count(n: int) -> str:
    return f"{n:,}"


def fmt_records(n: int) -> str:
    if current_lang() == "zh":
        return T("{n:.1f} 万条", "{n:.1f}0k records").format(n=n / 10000) if n >= 10000 else T("{n} 条", "{n} records").format(n=n)
    return f"{n:,} records"


def fmt_bytes(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    return f"{max(n, 0) / (1 << 20):.0f} MB"


def short_perm(label: str) -> str:
    open_paren = chr(0xFF08)
    close_paren = chr(0xFF09)
    if current_lang() == "zh":
        return label.split(open_paren)[0]
    if open_paren in label and close_paren in label:
        return label.split(open_paren)[1].split(close_paren)[0]
    if "(" in label and ")" in label:
        return label.split("(")[1].split(")")[0]
    return label


def host_name(client_id: str) -> str:
    pair = HOST_LABELS.get(client_id)
    if pair is None:
        return client_id
    return T(pair[0], pair[1])


def _plural_state(on: int, off: int) -> str:
    off_str = T(" · {off} 关", " · {off} off").format(off=off) if off else ""
    return T("{on} 开", "{on} on").format(on=on) + off_str


def reason_text(code: Optional[str]) -> str:
    pair = REASON_TEXT.get(code or "")
    if pair is None:
        return T("不可用", "Unavailable")
    return T(pair[0], pair[1])


def tool_text(tool: str) -> str:
    pair = TOOL_TEXT.get(tool)
    if pair is None:
        return tool
    return T(pair[0], pair[1])


def deny_text(reason: str) -> str:
    pair = DENY_TEXT.get(reason)
    if pair is None:
        return T("已拒绝", "Denied")
    return T(pair[0], pair[1])


def scope_type_text(kind: str) -> str:
    pair = SCOPE_TYPE_TEXT.get(kind)
    if pair is None:
        return kind
    return T(pair[0], pair[1])


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
        out.append(("red", row(T("无法读取数据库", "Cannot read database"), dot="red", sub=T("运行 penpal doctor 查看详情", "Run penpal doctor for details"), sub_tone="red")))
    if status is not None and status.state in {
        RuntimeState.NOT_INSTALLED, RuntimeState.NOT_RUNNING, RuntimeState.HUNG, RuntimeState.OUTDATED,
    }:
        code = f"daemon.{status.state.value}"
        fixable = is_fixable_in_hud(code)
        out.append(("red", row(
            state_text(status.state, language=current_lang()), dot="red", sub=state_message(status, language=current_lang()), sub_tone="red",
            value=T("修复", "Repair") if fixable else "", value_tone="blue" if fixable else None,
            action="daemon.restart" if fixable else None,
        )))
    elif status is not None and status.state == RuntimeState.STARTING:
        out.append(("amber", row(state_text(status.state, language=current_lang()), dot="amber", sub=state_message(status, language=current_lang()), sub_tone="amber")))
    if status is not None:
        for issue in status.issues:
            if issue.component == "daemon":
                continue
            tone = "red" if issue.severity == "error" else "amber"
            message = issue.message_zh if current_lang() == "zh" else (issue.message_en or issue.message_zh)
            label = (issue.label_zh if current_lang() == "zh" else (issue.label_en or issue.label_zh)) or issue.component
            fixable = is_fixable_in_hud(issue)
            code = getattr(issue, "code", "") or issue.component
            out.append((tone, row(
                label, dot=tone, sub=message, sub_tone=tone,
                value=T("修复", "Repair") if fixable else "", value_tone="blue" if fixable else None,
                action="issue.repair" if fixable else None,
                args={"issue_code": code} if fixable else {},
            )))
    for issue in snap.perm_issues:
        names = T("、", ", ").join(source_label(s) for s in issue.sources)
        perm = short_perm(issue.label)
        out.append(("amber", row(
            T("缺少「{perm}」权限", "Missing '{perm}' Permission").format(perm=perm), dot="amber",
            sub=T("影响：{names} · 点按处理", "Affects: {names} · Click to resolve").format(names=names),
            sub_tone="amber", nav=True, action="nav.page", args={"page": f"perm:{issue.key}"},
        )))
    for src in snap.sources:
        if has_issue(src) and src.unavailable_reason:
            out.append(("amber", row(
                T("{label} 不可用", "{label} Unavailable").format(label=src.label), dot="amber",
                sub=reason_text(src.unavailable_reason) + T(" · 点按处理", " · Click to resolve"),
                sub_tone="amber", nav=True, action="nav.page", args={"page": f"group:{src.group}"})))
    out.sort(key=lambda t: 0 if t[0] == "red" else 1)
    return out


def status_bar(snap: Snapshot, ui: Optional[UiState] = None) -> dict:
    status = snap.runtime_status
    if status is not None and status.state == RuntimeState.PAUSED:
        return {"code": status.code, "tone": "grey", "text": state_text(status.state, language=current_lang()), "sub": T("重启后仍保持暂停 · 不会补记", "Stays paused after restart · Nothing is back-filled")}
    found = alerts(snap)
    reds = [t for t in found if t[0] == "red"]
    last = rel_time(snap.now, snap.last_write_ts)
    label = state_text(status.state, language=current_lang()) if status is not None else T("未知", "Unknown")
    if ui and ui.rechecked_at:
        if found:
            sub = T("已重新检测 {time} · {n} 个问题", "Rechecked {time} · {n} issue(s)").format(time=ui.rechecked_at, n=len(found))
        else:
            sub = T("已重新检测 {time} · 正常", "Rechecked {time} · OK").format(time=ui.rechecked_at)
        return {"code": status.code if status else "unknown", "tone": "red" if reds else "amber" if found else "ok", "text": label, "sub": sub}
    if reds:
        tail = T(" · {last}最后写入", " · Last wrote {last}").format(last=last) if last else ""
        sub = T("{n} 个问题", "{n} issue(s)").format(n=len(found)) + tail
        return {"code": status.code if status else "unknown", "tone": "red", "text": label, "sub": sub}
    if found:
        return {"code": status.code if status else "unknown", "tone": "amber", "text": label,
                "sub": T("{n} 个问题需要处理", "{n} issue(s) need attention").format(n=len(found))}
    return {"code": status.code if status else "unknown", "tone": "ok", "text": label,
            "sub": T("一切正常 · {last}写入", "All normal · Wrote {last}").format(last=last) if last else T("一切正常 · 还没有记录", "All good · Nothing recorded yet")}


# ── pages ────────────────────────────────────────────────────────────────────────

def _source_row(src: SourceInfo, now: datetime) -> dict:
    issue = has_issue(src)
    if not src.configured:
        return row(src.label, dot="grey", sub=T("配置文件里已关闭", "Disabled in config"))
    if issue and src.unavailable_reason not in (None, "not_found"):
        sub, tone = reason_text(src.unavailable_reason), "amber"
    elif issue:
        sub, tone = T("缺少系统权限 · 在概览里处理", "Missing permission · Resolve in Overview"), "amber"
    elif src.enabled and src.unavailable_reason == "not_found":
        sub, tone = T("没有检测到", "Not detected"), None
    elif not src.enabled:
        sub, tone = T("已关闭", "Disabled"), None
    elif src.last_ts:
        sub, tone = T("{rel} · 今天 {today} 条", "{rel} · {today} today").format(rel=rel_time(now, src.last_ts), today=src.today), None
    else:
        sub, tone = T("还没有记录", "No records yet"), None
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
        sections.append(lst([r for _, r in found], T("需要处理", "Action Required")))
    sections.append({"kind": "stats", "header": T("今天", "Today"), "stats": [
        {"v": fmt_count(snap.today_records), "k": T("新增记录", "New Records")},
        {"v": fmt_count(snap.ai_queries_today), "k": T("AI 查询", "AI Queries")},
        {"v": fmt_count(len(snap.hosts)), "k": T("AI 已连接", "AI Connected")},
    ]})
    down = any(t == "red" for t, _ in found)
    rows = []
    for gid, pair in GROUPS:
        name = T(pair[0], pair[1])
        srcs = _group_sources(snap, gid)
        if not srcs:
            continue
        on, off, bad = _group_counts(srcs)
        if down:
            dot, value, tone = "red", T("已停止", "Stopped"), None
        elif bad:
            n = sum(1 for s in srcs if has_issue(s))
            dot, value, tone = "amber", T("{n} 个需处理", "{n} need attention").format(n=n), "amber"
        else:
            dot, value, tone = "ok", _plural_state(on, off), None
        rows.append(row(name, dot=dot, value=value, value_tone=tone, nav=True, action="nav.page", args={"page": f"group:{gid}"}))
    sections.append(lst(rows, T("数据来源", "Data Sources")))
    return sections


def page_group(snap: Snapshot, gid: str) -> Optional[tuple[str, list[dict]]]:
    pair = dict(GROUPS).get(gid)
    srcs = _group_sources(snap, gid)
    if pair is None or not srcs:
        return None
    name = T(pair[0], pair[1])
    footer = T("键盘片段只存过滤后的文字，不存逐个按键。", "Stores filtered text only, not keystrokes.") if gid == "live" else ""
    sections = [lst([_source_row(s, snap.now) for s in srcs], footer=footer)]
    if any(has_issue(s) and s.unavailable_reason == "permission_denied" for s in srcs):
        sections.append(button(T("打开系统设置 › 完全磁盘访问", "Open System Settings › Full Disk Access"), "settings.open", {"pane": "full_disk"}))
    for issue in snap.perm_issues:
        if any(s.name in issue.sources for s in srcs):
            btn_title = T("打开系统设置 › {perm}", "Open System Settings › {perm}").format(perm=short_perm(issue.label))
            sections.append(button(btn_title, "settings.open", {"pane": issue.key}))
    return name, sections


def page_perm(snap: Snapshot, key: str) -> Optional[tuple[str, list[dict]]]:
    issue = next((i for i in snap.perm_issues if i.key == key), None)
    if issue is None:
        return None
    label = short_perm(issue.label)
    exes = [row(e.rstrip("/").rsplit("/", 1)[-1] or e, sub=e) for e in issue.executables]
    sections = [
        lst([row(source_label(s), dot="amber", sub=T("需要这项权限，目前没有在记录", "Permission required; not recording")) for s in issue.sources], T("受影响的来源", "Affected Sources")),
    ]
    if exes:
        sections.append(lst(exes, T("需要授权的程序", "Apps Requiring Permission"), T("权限要授给上面的后台程序，不是这个窗口。", "Grant the permission to the background app above, not this window.")))
    sections.append(button(T("打开系统设置 › {label}", "Open System Settings › {label}").format(label=label), "settings.open", {"pane": issue.key}))
    return label, sections


def _result_text(r) -> tuple[str, Optional[str]]:
    if not r.allowed:
        return deny_text(r.reason_code), "red"
    if r.result_count is None:
        return T("完成", "Done"), None
    return (T("没有找到", "None found"), None) if r.result_count == 0 else (T("找到 {n} 条", "{n} results").format(n=r.result_count), None)


def page_access(snap: Snapshot) -> list[dict]:
    rows = [
        row(host_name(h.client_id), dot="ok",
            sub=(T("{rel}调用", "Called {rel}").format(rel=rel_time(snap.now, h.last_ts)) if h.last_ts else T("还没有调用", "Not called yet")),
            value=(T("今天 {n} 次", "{n} today").format(n=h.today) if h.today else ""),
            nav=True, action="nav.page", args={"page": f"host:{h.client_id}"})
        for h in snap.hosts
    ]
    rows.append(row(T("连接新的 AI", "Connect New AI"), nav=True, action="nav.page", args={"page": "host:new"}))
    sections = [lst(rows, T("已连接的 AI", "Connected AI"))]
    if snap.audit:
        recent = []
        for r in snap.audit:
            text, tone = _result_text(r)
            recent.append(row(f"{host_name(r.client)} · {tool_text(r.tool)}", sub=clock(r.ts), value=text, value_tone=tone))
        sections.append(lst(recent, T("最近查询", "Recent Queries"), T("只记谁、何时查过、查到几条，不记内容。", "Logs who searched, when, and how many results. Never the content.")))
    return sections


def page_host(snap: Snapshot, client_id: str) -> Optional[tuple[str, list[dict]]]:
    h = next((x for x in snap.hosts if x.client_id == client_id), None)
    if h is None:
        return None
    from penpal.mcp.grant_notice import work_project_notice

    buckets = [row(T(BUCKET_LABELS[b][0], BUCKET_LABELS[b][1]), check=b in h.buckets) for b in BUCKET_LABELS]
    scope = [lst(buckets, T("能看到的分类", "Visible Categories"), T(EGRESS_FOOTER[0], EGRESS_FOOTER[1]))]
    notice = work_project_notice(h.buckets, h.sources, [s.name for s in snap.sources if s.enabled])
    if notice:
        scope.append(lst([], T("授权范围提示", "Scope Notice"), notice))
    if h.sources is not None:
        scope.append(lst([row(source_label(s)) for s in h.sources], T("限定来源", "Restricted Sources")))
    usage = [
        row(T("最近一次", "Last Used"), value=rel_time(snap.now, h.last_ts) or "—"),
        row(T("本周查询", "Queries This Week"), value=T("{n} 次", "{n}").format(n=h.week)),
        row(T("本周被拒绝的调用", "Denied This Week"), value=T("{n} 次", "{n}").format(n=h.denied)),
    ]
    if h.expires_at:
        usage.append(row(T("授权到期", "Expires"), value=(_parse(h.expires_at) or snap.now).astimezone().strftime("%m-%d %H:%M")))
    h_name = host_name(h.client_id)
    return h_name, scope + [
        lst(usage, T("使用情况", "Usage")),
        button(T("轮换密钥", "Rotate Secret"), "grant.rotate", {"client_id": h.client_id}),
        button(T("撤销 {name} 的授权", "Revoke {name} Access").format(name=h_name), "confirm.ask", {"kind": "revoke", "client_id": h.client_id}, "danger"),
    ]


def page_new_host(snap: Snapshot, ui: UiState) -> tuple[str, list[dict]]:
    taken = {h.client_id for h in snap.hosts}
    choices = [c for c in NEW_HOST_CHOICES if c not in taken]
    host = ui.new_host if ui.new_host in choices else (choices[0] if choices else "")
    sections: list[dict] = []
    if not choices:
        sections.append(lst([row(T("四类 AI 都已连接", "All AI clients connected"))], footer=T("要换范围，请先撤销再重新连接。", "To change scope, revoke first and reconnect.")))
        return T("连接新的 AI", "Connect New AI"), sections
    sections.append(lst([row(T(HOST_LABELS[c][0], HOST_LABELS[c][1]), check=c == host, action="host.pick", args={"host": c}) for c in choices], T("选择 AI", "Select AI")))
    footer_text = T("默认只给「工作」。", "Default: Work only. ") + T(EGRESS_FOOTER[0], EGRESS_FOOTER[1]) + T("密钥只显示一次。", " Secret shown once.")
    sections.append(lst(
        [row(T(BUCKET_LABELS[b][0], BUCKET_LABELS[b][1]), switch=b in ui.new_buckets, action="host.bucket", args={"bucket": b}) for b in BUCKET_LABELS],
        T("能看到的分类", "Visible Categories"), footer_text))
    sections.append(button(T("授权并生成配置", "Authorize & Generate Config"), "grant.create", {"host": host}))
    return T("连接新的 AI", "Connect New AI"), sections


def page_reveal(reveal: Reveal) -> tuple[str, list[dict]]:
    return reveal.title, [
        lst([row(T("密钥只显示这一次", "Secret shown once only"), dot="amber", sub=T("关闭后无法再看；丢了就轮换一个新的。", "You can't view it again. If lost, rotate a new one."), sub_tone="amber")]),
        {"kind": "code", "header": T("连接配置", "Config"), "text": reveal.text},
        button(T("复制配置", "Copy Config"), "reveal.copy"),
    ]


def page_privacy(snap: Snapshot) -> list[dict]:
    current_lang_setting = getattr(snap, "language", "auto") or "auto"
    lang_section = lst([
        row(T("跟随系统", "Follow System"), check=current_lang_setting == "auto", action="language.set", args={"value": "auto"}),
        row(T("中文", "Chinese"), check=current_lang_setting == "zh", action="language.set", args={"value": "zh"}),
        row(T("English", "English"), check=current_lang_setting == "en", action="language.set", args={"value": "en"}),
    ], T("语言", "Language"))

    chat_on = sum(1 for _, _, allowed in snap.chat_apps if allowed)
    storage = snap.runtime_status.components.get("storage") if snap.runtime_status else None
    full = storage is not None and storage.state == "storage_error" and storage.detail == "disk_full"
    disk = fmt_bytes(snap.disk_free_bytes) if snap.disk_free_bytes is not None else "—"
    return [
        lang_section,
        lst([
            row(T("键盘片段", "Keyboard Snippets"), sub=T(KEYBOARD_RISK_NOTE[0], KEYBOARD_RISK_NOTE[1]), sub_tone="amber" if snap.keyboard_on else None, switch=snap.keyboard_on,
                action="keyboard.toggle", args={"enable": not snap.keyboard_on}),
            row(T("聊天 App", "Chat Apps"), value=T("{n} 个开启", "{n} enabled").format(n=chat_on), nav=True, action="nav.page", args={"page": "chat"}),
            row(T("不记录的 App 和网站", "Excluded Apps & Sites"), value=T("{n} 项", "{n} items").format(n=len(snap.exclusions)), nav=True, action="nav.page", args={"page": "exclude"}),
        ], T("采集范围", "Capture Scope"), T("只存过滤后的文字片段，不存按键；只有获准的 AI 能查到。", "Stores filtered text, not keystrokes. Only approved AI tools can search it.")),
        lst([
            row(T("已存储", "Stored"), value=f"{fmt_records(snap.total_records)} · {fmt_bytes(snap.db_bytes)}"),
            row(T("保留时间", "Retention"), value=T("永久保留", "Forever")),
            row(T("磁盘剩余", "Disk Free"), value=disk, value_tone="red" if full else None),
        ], T("存储", "Storage")),
        lst([row(T("删除数据", "Delete Data"), title_tone="red", nav=True, action="nav.page", args={"page": "delete"})]),
        lst(
            [row(T("退出 HUD", "Quit HUD"), title_tone="red", action="hud.quit")],
            footer=T("退出后图标消失，后台照常记录；打开 Penpal.app 即可恢复。", "The icon goes away; recording continues. Open Penpal.app to bring it back."),
        ),
    ]


def page_chat(snap: Snapshot) -> list[dict]:
    rows = [row(label, switch=allowed, action="chat.toggle", args={"name": key, "allow": not allowed})
            for key, label, allowed in snap.chat_apps]
    return [lst(rows, footer=T("默认不记录。打开后归入「私密」，只有获准查看「私密」的 AI 能查到；更改约 1 分钟内生效。",
                               "Excluded by default. Opted-in rows go to Private; takes effect in ~1 min."))]


def page_exclude(snap: Snapshot) -> list[dict]:
    rows = [row(value or T("（空）", "(empty)"), sub=scope_type_text(kind)) for kind, value in snap.exclusions]
    footer = T("这里的内容一律不记录。网站域名用 penpal privacy block-site / unblock-site 管理；新事件约 1 分钟内按新配置处理。其他策略用 penpal rules add / disable。",
               "Excluded from recording. Manage sites via penpal privacy block-site; takes effect in ~1 min. Rules via penpal rules add.")
    return [lst(rows, footer=footer)] if rows else [lst([row(T("还没有规则", "No rules yet"))], footer=footer)]


def page_delete(snap: Snapshot, ui: UiState, preview: Optional[dict[str, int]]) -> list[dict]:
    opts = delete_options(snap)
    chosen = next((o for o in opts if o.id == ui.delete_scope), opts[0])
    sections = [lst([row(o.label, check=o.id == chosen.id, action="delete.pick", args={"scope": o.id}) for o in opts], T("删除范围", "Delete Scope"))]
    if preview is None:
        sections.append(lst([row(T("统计失败", "Stats failed"), sub=T("运行 penpal purge --dry-run 查看", "Run penpal purge --dry-run to preview"), sub_tone="red")], T("将删除", "Will Delete")))
        return sections
    n = preview.get("raw_events", 0)
    shown = [row(T(pair[0], pair[1]), value=T("{n} 条", "{n}").format(n=preview.get(key, 0))) for key, pair in DELETE_COUNT_ROWS if preview.get(key, 0)]
    related = preview.get("mcp_returned_results", 0) + preview.get("mcp_feedback", 0)
    if related:
        shown.append(row(T("相关 AI 查询记录", "Related AI queries"), value=T("{n} 条", "{n}").format(n=related)))
    foot = T("只删 Penpal 里的副本，不动原文件；不想再导入，先在数据来源里关闭它。",
             "Deletes Penpal copies only, not original files. Disable source to stop re-importing.")
    if n == 0:
        sections.append(lst([row(T("没有可删除的内容", "Nothing to delete"))], T("将删除", "Will Delete"), foot))
    else:
        sections.append(lst(shown, T("将删除", "Will Delete"), foot))
        sections.append(button(T("删除 {n} 条记录", "Delete {n} records").format(n=n), "confirm.ask", {"kind": "purge"}, "danger"))
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
        name = host_name(h.client_id)
        return {
            "title": T("撤销授权？", "Revoke Authorization?"),
            "body": T("{name} 的下一次查询会被拒绝，之后可重新连接。",
                      "Next query from {name} will be denied; can reconnect later.").format(name=name),
            "ok": T("撤销", "Revoke"),
        }
    if c.get("kind") == "purge":
        chosen = next((o for o in delete_options(snap) if o.id == ui.delete_scope), None)
        if chosen is None or not preview:
            return None
        n = preview.get("raw_events", 0)
        return {
            "title": T("删除「{label}」？", "Delete '{label}'?").format(label=chosen.label),
            "body": T("共 {n} 条记录。删除后无法恢复，AI 也查不到。",
                      "{n} records. Cannot be recovered after deletion.").format(n=n),
            "ok": T("删除", "Delete"),
        }
    return None


# ── assembly ─────────────────────────────────────────────────────────────────────

def _back_label(page: str) -> str:
    if page.startswith(("group:", "perm:")):
        return T("概览", "Overview")
    if page.startswith("host:") or page == "reveal":
        return T("AI 访问", "AI Access")
    return T("设置", "Settings")


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
            found = (T("聊天 App", "Chat Apps"), page_chat(snap))
        elif page == "exclude":
            found = (T("不记录的 App 和网站", "Excluded Apps & Sites"), page_exclude(snap))
        elif page == "delete":
            found = (T("删除数据", "Delete Data"), page_delete(snap, ui, delete_preview))
        elif page == "reveal" and ui.reveal is not None:
            found = page_reveal(ui.reveal)
        if found is None:
            page = ""
        else:
            page_title, sections = found
    if not page:
        titles = {tid: T(p[0], p[1]) for tid, p in TABS}
        page_title = titles.get(ui.tab, T("概览", "Overview"))
        sections = {"overview": page_overview, "access": page_access, "privacy": page_privacy}.get(ui.tab, page_overview)(snap)

    found_alerts = [] if snap.paused else alerts(snap)
    badge = None
    if found_alerts:
        badge = "red" if any(t == "red" for t, _ in found_alerts) else "amber"
    bar = status_bar(snap, ui)
    return {
        "status": bar,
        "pause": {"label": T("继续记录", "Resume") if snap.paused else T("暂停", "Pause"), "action": "capture.resume" if snap.paused else "capture.pause"},
        "dim": snap.paused,
        "view": {"tab": ui.tab, "page": page},
        "title": page_title,
        "back": {"label": T("完成", "Done") if page == "reveal" else _back_label(page), "action": "nav.back"} if page else None,
        "sections": sections,
        "tabs": [{"id": tid, "label": T(p[0], p[1]), "current": tid == ui.tab, "badge": badge if tid == "overview" else None}
                 for tid, p in TABS],
        "confirm": build_confirm(ui, snap, delete_preview),
        "toast": {"id": ui.toast[0], "text": ui.toast[1]} if ui.toast else None,
    }
