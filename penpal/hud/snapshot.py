"""Read the core's current state into one plain snapshot for the HUD (no AppKit, no writes).

Everything here is read through the same functions the CLI uses (``penpal status`` / ``doctor`` /
``source list`` / ``mcp list`` / ``mcp audit``). Runtime state comes from ``penpal.runtime.evaluate``.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from penpal.runtime import RuntimeState, RuntimeStatus, evaluate, state_message
from penpal.utils.logging import get_logger

LOGGER = get_logger("hud.snapshot")

# Source -> overview group. Anything not listed lands in "other".
GROUPS: tuple[tuple[str, str], ...] = (
    ("live", "实时采集"),
    ("browser", "浏览器"),
    ("ai", "AI 对话"),
    ("dev", "开发与笔记"),
    ("other", "其他"),
)
SOURCE_GROUP: dict[str, str] = {
    "window": "live", "clipboard": "live", "keyboard_chunk": "live", "ax_text": "live",
    "idle": "live", "manual": "live",
    "browser": "browser", "browser_url": "browser", "browser_history": "browser",
    "chrome_history": "browser", "safari_history": "browser",
    "claude_code": "ai", "codex_cli": "ai",
    "zsh_history": "dev", "git_log": "dev", "markdown_vault": "dev",
}
SOURCE_LABELS: dict[str, str] = {
    "window": "前台窗口", "clipboard": "剪贴板", "keyboard_chunk": "键盘片段", "ax_text": "当前看到的正文",
    "idle": "空闲检测", "manual": "手动保存",
    "browser": "浏览器标签页", "browser_url": "当前网页地址", "browser_history": "浏览器历史",
    "chrome_history": "Chrome 历史", "safari_history": "Safari 历史",
    "claude_code": "Claude Code", "codex_cli": "Codex",
    "zsh_history": "终端命令", "git_log": "Git 提交", "markdown_vault": "笔记库",
    "wechat": "微信", "knowledgec": "系统使用记录", "spotlight": "Spotlight 文件",
    "approved_sqlite": "已批准的数据库", "leveldb_reader": "LevelDB 来源",
}
BUCKET_LABELS: dict[str, str] = {"Work": "工作", "Personal": "个人", "Private": "私密", "Financial": "财务"}
HOST_LABELS: dict[str, str] = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "generic": "其他 MCP 客户端"}


def source_label(name: str) -> str:
    return SOURCE_LABELS.get(name, name)


@dataclass(frozen=True)
class SourceInfo:
    name: str
    label: str
    group: str
    kind: str                          # "capture" | "plugin"
    enabled: bool                      # the on/off switch (not in disabled_sources)
    configured: bool = True            # capture watcher turned on in config.toml
    last_ts: Optional[str] = None
    today: int = 0
    unavailable_reason: Optional[str] = None   # sources.availability reason code
    missing_permissions: tuple[str, ...] = ()  # permission keys the daemon reports as denied


@dataclass(frozen=True)
class PermIssue:
    key: str
    label: str
    sources: tuple[str, ...]
    executables: tuple[str, ...]


@dataclass(frozen=True)
class HostInfo:
    client_id: str
    buckets: tuple[str, ...]
    sources: Optional[tuple[str, ...]]
    operations: tuple[str, ...]
    purpose: Optional[str] = None
    expires_at: Optional[str] = None
    today: int = 0
    week: int = 0
    denied: int = 0
    last_ts: Optional[str] = None


@dataclass(frozen=True)
class AuditRow:
    ts: str
    client: str
    tool: str
    allowed: bool
    reason_code: str
    result_count: Optional[int]


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    paused: bool = False
    health: Optional[dict] = None            # {"code", "message"} from capture_health, None = healthy
    runtime_status: Optional[RuntimeStatus] = None
    read_error: bool = False                 # the HUD could not read the database at all
    last_write_ts: Optional[str] = None
    today_records: int = 0
    sources: tuple[SourceInfo, ...] = ()
    perm_issues: tuple[PermIssue, ...] = ()
    hosts: tuple[HostInfo, ...] = ()
    audit: tuple[AuditRow, ...] = ()         # newest first
    ai_queries_today: int = 0
    keyboard_on: bool = True
    chat_apps: tuple[tuple[str, str, bool], ...] = ()   # (key, label, allowed)
    exclusions: tuple[tuple[str, str], ...] = ()        # (scope_type, scope_value)
    total_records: int = 0
    db_bytes: int = 0
    disk_free_bytes: Optional[int] = None
    delete_sources: tuple[tuple[str, int], ...] = ()    # (source name, rows)
    delete_apps: tuple[tuple[str, int], ...] = ()       # (app name, rows)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _safe(label: str, fn, default):
    try:
        return fn()
    except Exception as exc:  # one broken reader must not blank the whole HUD
        LOGGER.warning("hud snapshot: %s failed: %s", label, type(exc).__name__)
        return default


def _chat_label(app) -> str:
    return next((n for n in app.names if any("一" <= ch <= "鿿" for ch in n)), app.key)


def collect_snapshot(cfg, *, now: Optional[datetime] = None) -> Snapshot:
    """Read the whole HUD state. Never raises: an unreadable part falls back to an empty value, and an
    unreadable database sets ``read_error`` (shown as a storage error)."""
    from penpal.capture.capture_control import CAPTURE_SOURCES, disabled_sources
    from penpal.foundation.chat_apps import CHAT_APPS
    from penpal.capture.keyboard_notice import KEYBOARD_SOURCE, keyboard_capture_enabled
    from penpal.health import capture_health
    from penpal.mcp import audit, auth
    from penpal.services import activity
    from penpal.sources.registry import list_sources
    from penpal.store.repository import get_all_policies
    from penpal.utils.dates import local_day_bounds, resolve_local_date

    now = now or datetime.now(timezone.utc)
    read_error = False

    disabled = _safe("disabled", disabled_sources, frozenset())
    runtime_status = _safe("health", lambda: evaluate(now=now), None)
    paused = runtime_status is not None and runtime_status.state == RuntimeState.PAUSED
    health = None
    if runtime_status is not None and (runtime_status.state != RuntimeState.RUNNING or runtime_status.issues):
        health = {"code": runtime_status.code, "message": state_message(runtime_status, language="zh")}

    db_path = cfg.db_path_expanded

    day_start, _ = local_day_bounds(resolve_local_date(date="today"))
    try:
        by_source = activity.source_activity(day_start)
    except Exception as exc:
        LOGGER.warning("hud snapshot: activity failed: %s", type(exc).__name__)
        by_source, read_error = {}, True

    # macOS permissions: only the daemon's own report counts (it holds the TCC grants; the app has none).
    perm_findings = _safe("permissions", lambda: capture_health.permission_report(cfg, disabled=disabled), [])
    perm_issues = tuple(
        PermIssue(f.key, f.label, tuple(f.sources), tuple(f.executables))
        for f in perm_findings
        if f.checked_by == "daemon" and not f.granted and f.status == "missing"
    )
    perm_by_source: dict[str, list[str]] = {}
    for issue in perm_issues:
        for src in issue.sources:
            perm_by_source.setdefault(src, []).append(issue.key)

    sources: list[SourceInfo] = []
    plugin_names: set[str] = set()
    for plugin in _safe("plugins", list_sources, []):
        plugin_names.add(plugin.name)
        reason = None
        if plugin.cheap_probe:  # the other probes can shell out; `penpal source list` shows those
            state = _safe(plugin.name, plugin.availability, None)
            if state is not None and not state.available:
                reason = state.reason
        act = by_source.get(plugin.name, {})
        if reason == "file_missing" and not act.get("total"):
            reason = "not_found"  # never seen on this Mac (e.g. a browser that is not installed): not a fault
        sources.append(SourceInfo(
            name=plugin.name, label=source_label(plugin.name), group=SOURCE_GROUP.get(plugin.name, "other"),
            kind="plugin", enabled=plugin.name not in disabled, last_ts=act.get("last"),
            today=act.get("today", 0), unavailable_reason=reason,
        ))
    for name in sorted(CAPTURE_SOURCES - plugin_names):
        act = by_source.get(name, {})
        watcher_on = bool(getattr(cfg.watchers, name, True))
        if name == KEYBOARD_SOURCE:
            watcher_on = bool(cfg.watchers.keyboard_chunk)
        sources.append(SourceInfo(
            name=name, label=source_label(name), group=SOURCE_GROUP.get(name, "other"), kind="capture",
            enabled=name not in disabled, configured=watcher_on, last_ts=act.get("last"),
            today=act.get("today", 0), missing_permissions=tuple(perm_by_source.get(name, ())),
        ))

    # AI access
    grants = _safe("grants", auth.active_grants, [])
    rows = _safe("audit", lambda: audit.recent(1000), [])
    audit_rows = tuple(
        AuditRow(r["ts"], r["client_id"], r["tool"], r["decision"] == "allow", r["reason_code"], r["result_count"])
        for r in rows
    )
    week_start = now.timestamp() - 7 * 86400
    hosts: list[HostInfo] = []
    seen: set[str] = set()
    for g in grants:
        if g["client_id"] in seen:
            continue
        seen.add(g["client_id"])
        mine = [r for r in audit_rows if r.client == g["client_id"]]
        today = sum(1 for r in mine if r.allowed and r.ts >= day_start)
        week = sum(1 for r in mine if r.allowed and (_parse(r.ts) or now).timestamp() >= week_start)
        denied = sum(1 for r in mine if not r.allowed and (_parse(r.ts) or now).timestamp() >= week_start)
        hosts.append(HostInfo(
            client_id=g["client_id"], buckets=g["buckets"], sources=g["sources"], operations=g["operations"],
            purpose=g["purpose"], expires_at=g["expires_at"], today=today, week=week, denied=denied,
            last_ts=mine[0].ts if mine else None,
        ))

    # Privacy
    allowed_chat = set(cfg.privacy.chat_apps_opt_in)
    chat_apps = tuple((app.key, _chat_label(app), app.key in allowed_chat) for app in CHAT_APPS)
    exclusions = tuple(
        (p.get("scope_type") or "", p.get("scope_value") or "")
        for p in _safe("policies", get_all_policies, [])
        if p.get("mode") == "deny"
    ) + tuple(("domain", host) for host in cfg.privacy.url_deny_hosts)
    try:
        disk_free = shutil.disk_usage(db_path.parent if db_path.parent.exists() else os.path.expanduser("~")).free
    except OSError:
        disk_free = None
    db_bytes = db_path.stat().st_size if db_path.exists() else 0

    total = sum(a["total"] for a in by_source.values())
    last_write = max((a["last"] for a in by_source.values() if a.get("last")), default=None)
    ai_today = sum(1 for r in audit_rows if r.allowed and r.ts >= day_start)

    return Snapshot(
        now=now, paused=paused, health=health, runtime_status=runtime_status,
        read_error=read_error, last_write_ts=last_write,
        today_records=sum(a["today"] for a in by_source.values()),
        sources=tuple(sources), perm_issues=perm_issues, hosts=tuple(hosts), audit=audit_rows[:5],
        ai_queries_today=ai_today,
        keyboard_on=keyboard_capture_enabled(cfg.watchers.keyboard_chunk),
        chat_apps=chat_apps, exclusions=exclusions, total_records=total, db_bytes=db_bytes,
        disk_free_bytes=disk_free,
        delete_sources=tuple((n, a["total"]) for n, a in sorted(by_source.items()) if a["total"] > 0),
        delete_apps=tuple(_safe("apps", lambda: activity.top_apps(5), [])),
    )
