from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import datetime, time, timezone
from pathlib import Path

import click

from penpal.capture.capture_control import (
    CAPTURE_SOURCES,
    disabled_sources,
    set_source_enabled,
)
from penpal.capture.keyboard_notice import KEYBOARD_SOURCE, keyboard_capture_enabled, switch_hint
from penpal.config import Config
from penpal.foundation.approval_store import ApprovalRecord, ApprovalStore
from penpal.sources.discoverers import CandidateSource, discover_all_candidates
from penpal.sources.importer import DEFAULT_BATCH_SIZE
from penpal.sources.registry import discover_all, get_source, list_sources, read_all, source_availability
from penpal.sources.types import DataSourceInstance, SemanticEvent
from penpal.utils.paths import get_wechat_authorization_path
from penpal.sources.wechat_probe import (
    is_authorized,
    mark_authorized,
    probe,
    revoke_authorization,
)
from penpal.store.db import init_db
from penpal.utils.dates import local_timezone
from penpal.sources.coverage import coverage_command


@click.group(name="sources")
def sources_group() -> None:
    pass


sources_group.add_command(coverage_command)


@sources_group.command("list")
def list_command() -> None:
    cfg = Config.load()
    init_db(cfg.db_path_expanded)
    disabled = disabled_sources()
    for plugin, state in source_availability():
        desc = getattr(plugin, "description", "") or ""
        status = "[available]" if state.available else f"[unavailable: {state.reason}]"
        click.echo(
            f"{plugin.name}\t[{plugin.privacy_tier}/{plugin.liveness}]\t{_switch_label(plugin.name, disabled)}\t{status}\t{desc}"
        )
    for name in sorted(CAPTURE_SOURCES):
        if name == KEYBOARD_SOURCE:
            # Effective state (watcher config and switch) plus the command that changes it (issue #29).
            on = keyboard_capture_enabled(cfg.watchers.keyboard_chunk)
            label = "[on]" if on else "[off]"
            click.echo(f"{name}\t[capture watcher]\t{label}\t{switch_hint(on)}")
            continue
        click.echo(f"{name}\t[capture watcher]\t{_switch_label(name, disabled)}")


@sources_group.command("disable")
@click.argument("name")
def disable_command(name: str) -> None:
    """Stop writing new content from NAME (persists across restarts). keyboard_chunk is the keyboard off switch."""
    _set_switch(name, enabled=False)


@sources_group.command("enable")
@click.argument("name")
def enable_command(name: str) -> None:
    """Resume writing new content from NAME."""
    _set_switch(name, enabled=True)


def _set_switch(name: str, *, enabled: bool) -> None:
    init_db(Config.load().db_path_expanded)
    known = {plugin.name for plugin in list_sources()} | CAPTURE_SOURCES
    if name not in known:
        raise click.ClickException(f"unknown source: {name} (see `penpal sources list`)")
    set_source_enabled(name, enabled)
    click.echo(f"{name}\t{'on' if enabled else 'off'}")


def _switch_label(name: str, disabled: frozenset[str]) -> str:
    return "[off]" if name in disabled else "[on]"


@sources_group.command("discover")
def discover_command() -> None:
    discovered = discover_all()
    plugins = list_sources()
    recognized = [(plugin, discovered.get(plugin.name, [])) for plugin in plugins if discovered.get(plugin.name, [])]
    missing = [plugin.name for plugin in plugins if not discovered.get(plugin.name, [])]
    excluded = {instance.locator for _, instances in recognized for instance in instances}
    candidates = _flatten_candidates(discover_all_candidates(exclude_paths=excluded))
    store = ApprovalStore()

    click.echo("✅ 已识别（精读 plugins）：")
    if not recognized:
        click.echo("  (无)")
    for plugin, instances in recognized:
        click.echo(f"  {plugin.name}: {_recognized_summary(plugin.name, instances)}")

    click.echo("")
    click.echo("🟡 候选金矿（通用扫描，需用户确认）：")
    _print_candidates(candidates, store=store)

    click.echo("")
    click.echo("❌ 未发现：")
    if not missing:
        click.echo("  (无)")
    for name in missing:
        click.echo(f"  {name}")


@sources_group.command("candidates")
def candidates_command() -> None:
    candidates = _load_candidates()
    _print_candidates(candidates, store=ApprovalStore())


@sources_group.command("approve")
@click.argument("candidate_id")
@click.option("--note", default="", help="Optional approval note")
def approve_command(candidate_id: str, note: str) -> None:
    store = ApprovalStore()
    candidates = _load_candidates()
    candidate = _find_candidate_by_id(candidate_id, candidates, store=store)
    if candidate is None:
        raise click.ClickException(f"candidate id not found: {candidate_id}")

    normalized_id = store.candidate_id(candidate)
    if store.status(normalized_id) == "rejected":
        click.echo(f"warning: candidate {normalized_id} was rejected, resetting to approved")

    record = store.approve(candidate, note=note)
    click.echo(
        f"[✅] id={record.candidate_id} [{candidate.discoverer}/{candidate.confidence}] "
        f"{_display_path(candidate.path)} ({candidate.app_hint or '?'})"
    )


@sources_group.command("reject")
@click.argument("candidate_id")
@click.option("--reason", default="user_choice", help="Reject reason")
def reject_command(candidate_id: str, reason: str) -> None:
    store = ApprovalStore()
    candidates = _load_candidates()
    candidate = _find_candidate_by_id(candidate_id, candidates, store=store)
    if candidate is None:
        raise click.ClickException(f"candidate id not found: {candidate_id}")

    normalized_id = store.candidate_id(candidate)
    if store.status(normalized_id) == "approved":
        click.echo(f"warning: candidate {normalized_id} was approved, resetting to rejected")

    record = store.reject(candidate, reason=reason)
    click.echo(
        f"[❌] id={record.candidate_id} [{candidate.discoverer}/{candidate.confidence}] "
        f"{_display_path(candidate.path)} ({candidate.app_hint or '?'}) reason={reason}"
    )


@sources_group.command("unset")
@click.argument("candidate_id")
def unset_command(candidate_id: str) -> None:
    store = ApprovalStore()
    normalized_id = candidate_id.strip().lower()
    store.unset(normalized_id)
    click.echo(f"[新] id={normalized_id} status cleared")


@sources_group.command("list-approved")
def list_approved_command() -> None:
    records = ApprovalStore().list_approved()
    _print_approval_records(records, status_symbol="✅", note_label="note")


@sources_group.command("list-rejected")
def list_rejected_command() -> None:
    records = ApprovalStore().list_rejected()
    _print_approval_records(records, status_symbol="❌", note_label="reason")


_WECHAT_AUTHORIZE_WARNING = """
⚠️  你即将写入本机的微信来源授权标记。

当前微信来源仅做本机探测，消息读取尚未实现：
  - 不会读取或导入微信消息
  - 不会生成 Daily 报告
  - 不会调用云端 LLM，也不会向云端发送微信内容

输入 "yes" 保存本机授权标记，其它取消：
""".strip("\n")


@sources_group.group("wechat")
def wechat_group() -> None:
    """WeChat red-zone probe controls."""


@wechat_group.command("status")
def wechat_status_command() -> None:
    result = probe()
    pid = _detect_wechat_pid() if result.wechat_running else None
    authorized_at = _read_wechat_authorized_at() if result.user_authorized else None
    missing: list[str] = []

    if result.chatlog_installed:
        version = _format_chatlog_version(result.chatlog_version)
        version_text = f" ({version})" if version else ""
        click.echo(f"chatlog 二进制：✅ {result.chatlog_path or 'unknown'}{version_text}")
    else:
        click.echo("chatlog 二进制：❌ 未找到（建议：brew install chatlog）")
        missing.append("[chatlog]")

    if result.wechat_running:
        if pid:
            click.echo(f"微信进程：     ✅ 运行中（PID {pid}）")
        else:
            click.echo("微信进程：     ✅ 运行中")
    else:
        click.echo("微信进程：     ❌ 未运行")
        missing.append("[微信]")

    if result.user_authorized:
        if authorized_at:
            click.echo(f"用户授权：     ✅ 已授权 {authorized_at}")
        else:
            click.echo("用户授权：     ✅ 已授权")
    else:
        click.echo("用户授权：     ❌ 未授权")
        missing.append("[授权]")

    if missing:
        click.echo(f"总体状态：     ⚠️  需补：{' '.join(missing)}")
        return
    click.echo("总体状态：     ✅ 就绪（前置条件全满足）")


@wechat_group.command("authorize")
def wechat_authorize_command() -> None:
    click.echo(_WECHAT_AUTHORIZE_WARNING)
    answer = click.prompt("", prompt_suffix="", default="", show_default=False)
    if answer.strip().lower() != "yes":
        click.echo("已取消，未写入授权标记。")
        return
    mark_authorized()
    timestamp = _read_wechat_authorized_at()
    if timestamp:
        click.echo(f"✅ 微信红区已授权：{timestamp}")
    else:
        click.echo("✅ 微信红区已授权。")


@wechat_group.command("revoke")
def wechat_revoke_command() -> None:
    existed = is_authorized()
    revoke_authorization()
    if existed:
        click.echo("✅ 已撤销微信红区授权。")
        return
    click.echo("ℹ️ 当前没有微信授权标记。")


@sources_group.command("read")
@click.option("--source", "source_name", default=None, help="Only read from one plugin")
@click.option("--since", default=None, help="ISO date/datetime, default: today 00:00 local")
@click.option("--until", default=None, help="ISO date/datetime, default: now")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output JSON Lines")
def read_command(source_name: str | None, since: str | None, until: str | None, as_json: bool) -> None:
    if source_name and get_source(source_name) is None:
        raise click.ClickException(f"unknown source: {source_name}")
    _require_history_gate_open()

    since_dt = _parse_bound(since, is_since=True)
    until_dt = _parse_bound(until, is_since=False)
    if until_dt < since_dt:
        raise click.ClickException("until must be >= since")

    init_db(Config.load().db_path_expanded)

    for event in read_all(since_dt, until_dt, source=source_name):
        if as_json:
            click.echo(json.dumps(_event_to_json_dict(event), ensure_ascii=False))
            continue
        click.echo(
            f"{event.time.isoformat()}\t{event.source}\t{event.actor}\t{event.intent}\t{event.artifact}"
        )


@sources_group.command("sync")
@click.option("--source", "source_name", default=None, help="Only sync one plugin")
@click.option("--lookback-hours", default=24, show_default=True, type=int,
              help="Window for plugins without a persistent cursor (cursor plugins resume where they left off)")
def sync_command(source_name: str | None, lookback_hours: int) -> None:
    """Pull new events from sources into the local DB (same path as the daemon scheduler)."""
    from datetime import timedelta

    if source_name and get_source(source_name) is None:
        raise click.ClickException(f"unknown source: {source_name}")
    init_db(Config.load().db_path_expanded)
    _require_history_gate_open()
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    for event in read_all(now - timedelta(hours=lookback_hours), now, source=source_name):
        counts[event.source] = counts.get(event.source, 0) + 1
    if not counts:
        click.echo("no new events")
    for name, count in sorted(counts.items()):
        click.echo(f"{name}\t{count}")


@sources_group.command("import")
@click.option("--source", "source_name", default=None, help="Only import one source")
@click.option("--days", default=None, type=int, help="Cap the window for sources without a cursor (default: all)")
@click.option("--batch-size", default=DEFAULT_BATCH_SIZE, show_default=True, type=int, help="Events stored per batch")
def import_command(source_name: str | None, days: int | None, batch_size: int) -> None:
    """Import enabled sources in batches. After an interruption, rerunning resumes from the saved cursor (#32)."""
    from penpal.capture.capture_control import source_writes_allowed
    from penpal.sources.cleaning.config import CleaningConfigError
    from penpal.sources import importer
    from penpal.sources.onboarding import EXCLUDED_FROM_CHOICE, first_import_since, format_import_result

    if source_name and get_source(source_name) is None:
        raise click.ClickException(f"unknown source: {source_name}")
    init_db(Config.load().db_path_expanded)
    _require_history_gate_open()
    names = [source_name] if source_name else [
        plugin.name for plugin in list_sources() if plugin.name not in EXCLUDED_FROM_CHOICE
    ]
    now = datetime.now(timezone.utc)
    for name in names:
        if not source_writes_allowed(name):
            click.echo(f"{name}\toff")
            continue
        try:
            result = importer.import_source(name, since=first_import_since(now, days), until=now, batch_size=batch_size)
        except CleaningConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{name}\t{format_import_result(result)}")


def _require_history_gate_open() -> None:
    """Reading historical sources waits for `penpal install onboard` on a fresh install (#32)."""
    from penpal.sources.history_gate import WAITING_TEXT, historical_imports_allowed

    if not historical_imports_allowed():
        raise click.ClickException(WAITING_TEXT)


def _parse_bound(raw: str | None, *, is_since: bool) -> datetime:
    local_tz = local_timezone()
    now_local = datetime.now(local_tz)

    if raw is None:
        if is_since:
            return datetime.combine(now_local.date(), time.min, tzinfo=local_tz).astimezone(timezone.utc)
        return now_local.astimezone(timezone.utc)

    if len(raw) == 10:
        day = datetime.fromisoformat(raw)
        if is_since:
            local_value = datetime.combine(day.date(), time.min, tzinfo=local_tz)
        else:
            local_value = datetime.combine(day.date(), time.max, tzinfo=local_tz)
        return local_value.astimezone(timezone.utc)

    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(timezone.utc)


def _event_to_json_dict(event: SemanticEvent) -> dict[str, object]:
    data = asdict(event)
    data["time"] = event.time.isoformat()
    return data


def _recognized_summary(plugin_name: str, instances: list[DataSourceInstance]) -> str:
    if plugin_name == "git_log":
        return f"{len(instances)} 个仓库"
    if plugin_name == "claude_code":
        root = Path(instances[0].locator).parent if instances else Path.home() / ".claude" / "projects"
        return f"{_display_path(str(root))} ({len(instances)} 个项目)"
    if plugin_name == "markdown_vault" and instances:
        label = instances[0].label
        return f"{_display_path(instances[0].locator)} ({label})"
    if len(instances) == 1:
        return _display_path(instances[0].locator)
    return f"{_display_path(instances[0].locator)} ... ({len(instances)} 个实例)"


def _flatten_candidates(candidates: dict[str, list[CandidateSource]]) -> list[CandidateSource]:
    flat: list[CandidateSource] = []
    for values in candidates.values():
        flat.extend(values)
    discoverer_order = {
        "leveldb": 0,
        "sqlite": 1,
        "markdown_vault": 2,
        "json_files": 3,
        "jsonl": 4,
        "plist": 5,
    }
    return sorted(flat, key=lambda candidate: (discoverer_order.get(candidate.discoverer, 99), candidate.path))


def _print_candidates(candidates: list[CandidateSource], *, store: ApprovalStore) -> None:
    if not candidates:
        click.echo("  (无)")
        click.echo("  共 0 个候选")
        return

    for candidate in candidates:
        candidate_id = store.candidate_id(candidate)
        status = _status_symbol(store.status(candidate_id))
        app_hint = candidate.app_hint or "?"
        click.echo(
            f"  [{status}] id={candidate_id} [{candidate.discoverer}/{candidate.confidence}] "
            f"{_display_path(candidate.path)} ({app_hint})"
        )
        if candidate.hint_tables:
            click.echo(f"    hint_tables: {', '.join(candidate.hint_tables)}")
        if candidate.hint_fields:
            click.echo(f"    hint_fields: {', '.join(candidate.hint_fields)}")
        if candidate.shape:
            click.echo(f"    shape: {candidate.shape}")
    click.echo(f"  共 {len(candidates)} 个候选")


def _load_candidates() -> list[CandidateSource]:
    discovered = discover_all()
    excluded = {
        instance.locator
        for instances in discovered.values()
        for instance in instances
    }
    return _flatten_candidates(discover_all_candidates(exclude_paths=excluded))


def _find_candidate_by_id(
    candidate_id: str,
    candidates: list[CandidateSource],
    *,
    store: ApprovalStore,
) -> CandidateSource | None:
    normalized_id = candidate_id.strip().lower()
    for candidate in candidates:
        if store.candidate_id(candidate) == normalized_id:
            return candidate
    return None


def _status_symbol(status: str) -> str:
    if status == "approved":
        return "✅"
    if status == "rejected":
        return "❌"
    return "新"


def _print_approval_records(records: list[ApprovalRecord], *, status_symbol: str, note_label: str) -> None:
    if not records:
        click.echo("  (无)")
        return
    for record in records:
        path = record.metadata.get("path", "")
        app_hint = record.metadata.get("app_hint") or "?"
        discoverer = record.metadata.get("discoverer", "?")
        ts = record.timestamp.isoformat() if record.timestamp else "-"
        line = (
            f"  [{status_symbol}] id={record.candidate_id} "
            f"[{discoverer}] {_display_path(path)} ({app_hint}) at={ts}"
        )
        if record.note:
            line += f" {note_label}={record.note}"
        click.echo(line)


def _display_path(path: str) -> str:
    try:
        expanded = Path(path).expanduser().resolve(strict=False)
        home = Path.home().resolve(strict=False)
    except Exception:
        return path

    try:
        rel = expanded.relative_to(home)
    except ValueError:
        return str(expanded)
    return "~/" + str(rel)


def _wechat_authorization_path() -> Path:
    return get_wechat_authorization_path()


def _read_wechat_authorized_at() -> str | None:
    auth_path = _wechat_authorization_path()
    try:
        content = auth_path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not content:
        return None
    if content.startswith("authorized_at="):
        value = content.split("=", 1)[1].strip()
        return value or None
    return content


def _detect_wechat_pid() -> str | None:
    for command in (["pgrep", "-i", "wechat"], ["pgrep", "-f", "WeChat"]):
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True)
        except Exception:
            continue
        if result.returncode != 0:
            continue
        first_line = ((result.stdout or "").strip().splitlines() or [""])[0].strip()
        if first_line:
            return first_line
    return None


def _format_chatlog_version(version: str | None) -> str | None:
    if not version:
        return None
    value = version.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered.startswith("chatlog "):
        value = value.split(" ", 1)[1].strip() or value
    return value
