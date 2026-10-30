from __future__ import annotations
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any
import click
from rich.table import Table
from penpal.config import read_toml
from penpal.utils.paths import get_apps_dir, get_config_path, get_data_dir, get_launchd_agents_dir
from penpal.runtime import ICON_OCCLUDED_HINT_ZH, evaluate
from penpal.capture.capture_control import disabled_sources
from penpal.capture.keyboard_notice import DISABLE_COMMAND, ENABLE_COMMAND, KEYBOARD_SOURCE, keyboard_capture_enabled, switch_hint
from penpal.i18n import T
from . import main
from .legacy import find_legacy_artifacts
from ._shared import PLAIN_HELP, _runtime_fix_command, _runtime_status_line, console, get_config, require_db
from .install import _stale_moved_dev_venv_script


def _is_legacy_mcp_command(command: Any) -> bool:
    if isinstance(command, list):
        command = command[0] if command and isinstance(command[0], str) else ""
    return isinstance(command, str) and Path(command.strip()).name == "keypulse"

def _legacy_mcp_server_details(payload: Any, source: str) -> list[str]:
    details: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"mcpServers", "mcp_servers"} and isinstance(value, dict):
                for server_name, server_config in value.items():
                    if not isinstance(server_config, dict):
                        continue
                    reasons = []
                    if str(server_name).casefold() == "keypulse":
                        reasons.append("server=keypulse")
                    if _is_legacy_mcp_command(server_config.get("command")):
                        reasons.append("command=keypulse")
                    if reasons:
                        details.append(f"{source}: {', '.join(reasons)}")
            details.extend(_legacy_mcp_server_details(value, source))
    elif isinstance(payload, list):
        for value in payload:
            details.extend(_legacy_mcp_server_details(value, source))
    return details

def _legacy_mcp_config_findings(home: Path | None = None) -> list[str]:
    home = home or Path.home()
    findings: list[str] = []
    claude_config = home / ".claude.json"
    codex_config = home / ".codex" / "config.toml"

    if claude_config.is_file():
        try:
            payload = json.loads(claude_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = None
        findings.extend(_legacy_mcp_server_details(payload, "~/.claude.json"))

    if codex_config.is_file():
        try:
            payload = read_toml(codex_config)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            payload = None
        findings.extend(_legacy_mcp_server_details(payload, "~/.codex/config.toml"))

    return findings


def _doctor_apps_dirs(home: Path) -> tuple[Path, ...]:
    # Test/probe isolation must never enumerate the host's /Applications.
    if os.environ.get("PENPAL_ISOLATED") == "1":
        return (home / "Applications",)
    return (Path("/Applications"), get_apps_dir())

@main.command(help=T("检查系统配置、权限与依赖。", "Check system configuration, permissions, and dependencies."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
@click.option("--json", "json_output", is_flag=True, default=False, help=T("输出 JSON", "Emit JSON"))
@click.option("--fix", is_flag=True, default=False, help=T("尝试自动修复检测到的问题", "Attempt to automatically fix detected issues"))
def doctor(plain, json_output, fix):
    """Run diagnostics."""
    if fix:
        from penpal.services.repair import is_fixable_issue, repair

        before_status = evaluate()
        repair_results = []
        # A pause is the user's own choice (severity info); --fix repairs faults and leaves it alone.
        fixable = [issue for issue in before_status.issues if is_fixable_issue(issue) and issue.severity != "info"]
        for issue in fixable:
            repair_results.append(repair(issue))
        after_status = evaluate()

        if json_output:
            payload = {
                "before": {"state": before_status.code, "issues": [i.message_zh for i in before_status.issues]},
                "repairs": [
                    {"code": r.issue_code, "attempted": r.attempted, "fixed": r.fixed, "message": r.message_zh}
                    for r in repair_results
                ],
                "after": {"state": after_status.code, "issues": [i.message_zh for i in after_status.issues]},
            }
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        if plain:
            print(f"before: state={before_status.code} issues={len(before_status.issues)}")
            for r in repair_results:
                status_str = "FIXED" if r.fixed else "FAILED" if r.attempted else "SKIPPED"
                print(f"repair {r.issue_code}: {status_str} {r.message_zh}")
            print(f"after: state={after_status.code} issues={len(after_status.issues)}")
            return

        console.print(T("[bold cyan]── 自动修复前 ──[/bold cyan]", "[bold cyan]── Before Auto-Repair ──[/bold cyan]"))
        console.print(T(f"后台状态：{before_status.code} · 问题数：{len(before_status.issues)}", f"Background state: {before_status.code} · issues: {len(before_status.issues)}"))
        for issue in before_status.issues:
            console.print(f"  • {issue.message_zh}")
        console.print(T("\n[bold cyan]── 执行修复 ──[/bold cyan]", "\n[bold cyan]── Executing Repairs ──[/bold cyan]"))
        if not repair_results:
            console.print(T("没有可自动修复的问题。", "No fixable issues found."))
        else:
            for r in repair_results:
                tag = "[green]已修复[/green]" if r.fixed else "[red]失败[/red]" if r.attempted else "[yellow]跳过[/yellow]"
                console.print(f"  {tag} {r.message_zh}")
        console.print(T("\n[bold cyan]── 自动修复后 ──[/bold cyan]", "\n[bold cyan]── After Auto-Repair ──[/bold cyan]"))
        console.print(T(f"后台状态：{after_status.code} · 问题数：{len(after_status.issues)}", f"Background state: {after_status.code} · issues: {len(after_status.issues)}"))
        for issue in after_status.issues:
            console.print(f"  • {issue.message_zh}")
        return

    runtime_status = evaluate()
    runtime_fix = _runtime_fix_command(runtime_status)
    runtime_detail = f"state={_runtime_status_line(runtime_status)}"
    if runtime_status.issues:
        from penpal.runtime import state_message

        runtime_detail += f"; detail={state_message(runtime_status)}"
    if runtime_fix:
        runtime_detail += f"; fix=`{runtime_fix}`"
    checks = {"后台服务": not runtime_status.failed}
    check_details = {"后台服务": runtime_detail}
    hud_issues = [issue for issue in runtime_status.issues if issue.component == "hud"]
    if runtime_status.hud is not None and runtime_status.hud.alive:
        visible = runtime_status.hud.status_item_visible
        # The evaluator alone decides pass/fail; unknown visibility is informational (macOS hides the toggle).
        checks["菜单栏 HUD"] = not hud_issues
        hud_detail = (
            hud_issues[0].message_zh if hud_issues else
            f"主线程有响应；状态栏图标可见。{ICON_OCCLUDED_HINT_ZH}" if visible is True else
            f"主线程有响应；图标可见性未知，请在系统设置 → 菜单栏确认 Penpal 已打开。{ICON_OCCLUDED_HINT_ZH}"
        )
        if runtime_status.hud.supervisor_summary_zh:
            hud_detail += f"；{runtime_status.hud.supervisor_summary_zh}"
        check_details["菜单栏 HUD"] = hud_detail
    cfg = get_config()
    require_db(cfg)

    stale_venv = _stale_moved_dev_venv_script()
    if stale_venv:
        checks["开发 venv 入口仍指向旧目录"] = False

    home = Path.home()
    legacy_artifacts = find_legacy_artifacts(
        home=home,
        apps_dirs=_doctor_apps_dirs(home),
        agents_dir=get_launchd_agents_dir(),
        data_dir=get_data_dir(),
    )

    legacy_mcp_configs = _legacy_mcp_config_findings()
    checks["MCP host 配置已迁移为 penpal"] = not legacy_mcp_configs

    # Python version
    import sys as sys_module
    checks["Python >= 3.11"] = sys_module.version_info >= (3, 11)

    # Cocoa / AppKit
    try:
        import AppKit  # noqa: F401
        checks["macOS 原生界面能力"] = True
    except ImportError:
        checks["macOS 原生界面能力"] = False

    # Quartz
    try:
        import Quartz
        checks["图像与事件框架"] = True
    except ImportError:
        checks["图像与事件框架"] = False

    # macOS permissions needed by the sources that are switched on (read-only; never prompts).
    # Prefers the daemon's own report; falls back to this terminal process when there is none.
    from penpal.health.capture_health import permission_report

    perm_findings = permission_report(cfg)
    for finding in perm_findings:
        if finding.checked_by == "daemon":
            who = f"后台服务，{finding.age_sec} 秒前检查"
        else:
            who = "当前终端进程，非后台服务"
        checks[f"{finding.label} 权限（{', '.join(finding.sources)}；{who}）"] = finding.granted

    # DB path writable
    db_path = cfg.db_path_expanded
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        test_file = db_path.parent / ".write_test"
        test_file.write_text("test", encoding="utf-8")
        test_file.unlink()
        checks["数据库目录可写"] = True
    except Exception:
        checks["数据库目录可写"] = False

    # Config path exists
    config_path = get_config_path()
    checks["配置文件存在"] = config_path.exists()
    components = runtime_status.components
    if "ax_text" in components:
        checks["后台正文采集线程"] = components["ax_text"].state in {"running", "idle"}
    if "keyboard_chunk" in components:
        checks["后台键盘分块线程"] = components["keyboard_chunk"].state in {"running", "idle"}
    # OCR is switched off (watcher commented out in capture/manager.py). Shown for information only, never a failure.
    # Information only, never a failure: (table note, plain-output text). Keyboard capture is a user choice.
    keyboard_on = keyboard_capture_enabled(cfg.watchers.keyboard_chunk)
    from penpal.sources.history_gate import WAITING_TEXT, historical_imports_allowed

    info_rows = {
        "屏幕识别（OCR）": ("已停用（OCR 已关闭）", "DISABLED (OCR off)"),
        f"键盘文本片段（{KEYBOARD_SOURCE}）": (
            f"{'开启' if keyboard_on else '关闭'}；{switch_hint(keyboard_on)}",
            (f"ON; to turn off: {DISABLE_COMMAND}" if keyboard_on
             else f"OFF; to turn on again: {ENABLE_COMMAND}"),
        ),
    }
    if not historical_imports_allowed():
        info_rows["历史导入"] = (WAITING_TEXT, "WAITING: run penpal install onboard")
    # Sources that cannot be read right now are a status, not a failure (issue #22). Checked for every source
    # that is switched on, with the fix path (issue #32); a source that was never found is not a fault.
    from penpal.sources.onboarding import source_problems
    from penpal.sources.registry import list_sources

    enabled_now = [plugin.name for plugin in list_sources() if plugin.name not in disabled_sources()]
    unavailable_rows = source_problems(enabled_now)

    if json_output:
        payload = {
            "checks": {
                name: {
                    "status": "ok" if passed else "fail",
                    "detail": check_details.get(name, ""),
                }
                for name, passed in checks.items()
            },
            "info": {
                name: {"text": note, "plain": plain_text}
                for name, (note, plain_text) in info_rows.items()
            },
            "unavailable_sources": unavailable_rows,
            "legacy_artifacts": legacy_artifacts,
            "missing_permissions": [
                {
                    "label": finding.label,
                    "sources": list(finding.sources),
                    "executables": list(finding.executables),
                    "settings_pane": finding.settings_pane,
                }
                for finding in perm_findings
                if not finding.granted
            ],
        }
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    elif plain:
        for check_name, passed in checks.items():
            status = "OK" if passed else "FAIL"
            detail = f" {check_details[check_name]}" if check_name in check_details else ""
            print(f"{check_name}: {status}{detail}")
        for check_name, (_note, plain_text) in info_rows.items():
            print(f"{check_name}: {plain_text}")
        for row in unavailable_rows:
            print(f"source {row['source']}: UNAVAILABLE reason={row['reason']}; fix: {row['fix']}")
            if row["pane"]:
                print(f"source {row['source']}: open \"{row['pane']}\"")
        if stale_venv:
            print(
                f"venv_entry={stale_venv[0]} points to {stale_venv[1]}; "
                "run `bash install.sh --dev` to rebuild the developer venv"
            )
        for finding in legacy_mcp_configs:
            print(f"legacy_mcp_config={finding}; change the MCP server name and command to `penpal`")
        if legacy_artifacts:
            print(f"{T('遗留文件', 'Legacy files')}: WARN")
            for item in legacy_artifacts:
                print(f"  {item['kind']}: {item['path']}")
                print(f"    {T('原因', 'Why')}: {item['why']}")
                print(f"    {T('建议', 'Suggested fix')}: {item['fix_command']}")
    else:
        table = Table(title="系统健康检查", show_header=True, header_style="bold cyan")
        table.add_column("检查项")
        table.add_column("结果")
        table.add_column("详情")
        for check_name, passed in checks.items():
            status = "[green]正常[/green]" if passed else "[red]未通过[/red]"
            table.add_row(check_name, status, check_details.get(check_name, ""))
        for check_name, (note, _plain_text) in info_rows.items():
            table.add_row(check_name, note)
        for row in unavailable_rows:
            table.add_row(f"数据源 {row['source']}", f"不可用（{row['reason']}）；{row['fix']}")
        console.print(table)
        if stale_venv:
            console.print(
                f"venv entry {stale_venv[0]} points to {stale_venv[1]}; "
                "run `bash install.sh --dev` to rebuild the developer venv",
                markup=False,
            )
        for finding in legacy_mcp_configs:
            console.print(
                f"Legacy MCP config remains: {finding}; change the MCP server name and command to `penpal`",
                markup=False,
            )
        if legacy_artifacts:
            table = Table(title=T("遗留文件", "Legacy files"), show_header=True, header_style="bold yellow")
            table.add_column(T("级别", "Level"))
            table.add_column(T("类型", "Kind"))
            table.add_column(T("路径", "Path"))
            table.add_column(T("原因 / 建议", "Why / Suggested fix"))
            for item in legacy_artifacts:
                table.add_row(
                    "WARN",
                    item["kind"],
                    item["path"],
                    f"{item['why']}\n{item['fix_command']}",
                )
            console.print(table)
    missing = [f for f in perm_findings if not f.granted]
    for finding in (() if json_output else missing):
        if finding.checked_by == "daemon":
            header = f"后台服务缺少权限：{finding.label}（{', '.join(finding.sources)} 需要）。请在系统设置里授予下面的程序："
        else:
            header = (
                f"后台服务状态不可用（未运行或无近期记录），以下只是当前终端进程的检查结果：{finding.label} 未授予"
                f"（{', '.join(finding.sources)} 需要）。采集正常可忽略；异常时请在系统设置里授予下面的程序："
            )
        lines = [
            header,
            *[f"  - {exe}" for exe in finding.executables],
            f"  打开设置面板：open \"{finding.settings_pane}\"",
        ]
        if plain:
            for line in lines:
                print(line)
        else:
            console.print("\n".join(lines), markup=False)
    # Exit with error if any check failed
    if not all(checks.values()):
        sys.exit(1)

@main.command(help=T("运行健康检查，写入 ~/.penpal/health.json。", "Run healthcheck and write ~/.penpal/health.json."))
@click.option("--config", "config_path", default=None)
def healthcheck(config_path):
    """Run healthcheck."""
    from penpal.health import run_healthcheck

    result = run_healthcheck(config_path=config_path)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result["overall"] == "alert" and any(alert["severity"] == "error" for alert in result["alerts"]):
        raise SystemExit(1)

main.add_command(healthcheck, name="health")  # D33: the short, discoverable spelling; `healthcheck` stays (launchd uses it)
