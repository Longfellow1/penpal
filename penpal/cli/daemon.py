from __future__ import annotations
import click
from rich.table import Table
from penpal.config import Config, read_config
from penpal.store.repository import get_state
from penpal.runtime import RuntimeState, evaluate, state_message
from penpal.app import run
from penpal.capture.capture_control import disabled_sources, set_capture_paused
from penpal.i18n import T
from . import main
from ._shared import PLAIN_HELP, _runtime_fix_command, _runtime_status_line, console, err_console, get_config, require_db
from .install import _format_import_progress
from penpal.runtime import restart as control_restart, start as control_start, stop as control_stop

def _control_command(result, success_zh: str, success_en: str) -> None:
    if not result.ok:
        err_console.print(T(result.error_zh, result.error_en))
        raise click.exceptions.Exit(1)
    suffix = f" generation={result.generation}" if result.generation else ""
    console.print(T(f"{success_zh}（{result.action}{suffix}）", f"{success_en} ({result.action}{suffix})"))


@main.command(help=T("通过 launchd 启动 Penpal daemon。", "Start the Penpal daemon through launchd."))
def start():
    """Start the managed daemon."""
    _control_command(control_start(), "后台服务已启动", "Daemon started")

@main.command(help=T("在前台运行 Penpal（常用于 launchd/supervisor）。", "Run Penpal in the foreground (for launchd/supervisor)."))
@click.option("--config", "config_path", default=None, help=T("配置文件路径（config.toml）", "Path to config.toml"))
def serve(config_path):
    """Run foreground service."""
    cfg = Config.load() if not config_path else _load_config_from(config_path)
    run(cfg, config_path=config_path)

def _load_config_from(path: str) -> Config:
    """Load config from explicit path."""
    return read_config(path)

@main.command(help=T("停止 Penpal daemon。", "Stop the Penpal daemon."))
def stop():
    """Stop the launchd-managed daemon."""
    _control_command(control_stop(), "后台服务已停止", "Daemon stopped")


@main.command(help=T("通过 launchd 安全重启 Penpal daemon。", "Restart the Penpal daemon through launchd."))
def restart():
    """Restart the launchd-managed daemon."""
    _control_command(control_restart("cli"), "后台服务已重启", "Daemon restarted")

@main.command(help=T("暂停活动采集。", "Pause activity monitoring."))
def pause():
    """Pause monitoring."""
    cfg = get_config()
    require_db(cfg)
    set_capture_paused(True)
    console.print("[yellow]Monitoring paused[/yellow]")

@main.command(help=T("恢复活动采集。", "Resume activity monitoring."))
def resume():
    """Resume monitoring."""
    cfg = get_config()
    require_db(cfg)
    set_capture_paused(False)
    console.print("[green]Monitoring resumed[/green]")

@main.command(help=T("查看 daemon 状态与采集运行信息。", "Show daemon status and capture runtime details."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def status(plain):
    """Show status."""
    cfg = get_config()
    require_db(cfg)

    runtime_status = evaluate()
    pid = runtime_status.pid
    is_running = runtime_status.state.value not in {"not_installed", "not_running"}
    supervised = runtime_status.managed
    status_val = (
        "paused" if runtime_status.state == RuntimeState.PAUSED
        else "stopped" if runtime_status.state in {RuntimeState.NOT_INSTALLED, RuntimeState.NOT_RUNNING}
        else "running"
    )
    started_at = get_state("started_at") or "—"
    last_flush = get_state("last_flush") or "—"
    components = runtime_status.components
    ax_running = components.get("ax_text") is not None and components["ax_text"].state == "running"

    # DB size
    db_path = cfg.db_path_expanded
    db_size = db_path.stat().st_size if db_path.exists() else 0
    db_size_mb = db_size / (1024 * 1024)

    # Enabled watchers
    enabled = []
    if cfg.watchers.window:
        enabled.append("窗口活动")
    if cfg.watchers.idle:
        enabled.append("空闲检测")
    if cfg.watchers.clipboard:
        enabled.append("剪贴板")
    if cfg.watchers.manual:
        enabled.append("手动保存")
    if getattr(cfg.watchers, "keyboard_chunk", False):
        enabled.append("键盘分块")
    if cfg.watchers.browser:
        enabled.append("浏览器")
    if getattr(cfg.watchers, "ax_text", False):
        enabled.append("当前看到的正文")
    # OCR ("屏幕识别补充") is switched off in capture/manager.py, so it is never listed as enabled.

    paused_now = runtime_status.state.value == "paused"
    disabled_now = sorted(disabled_sources())
    from penpal.sources import importer

    import_rows = importer.progress_rows()
    from penpal.sources.history_gate import WAITING_TEXT, historical_imports_allowed

    history_open = historical_imports_allowed()

    if plain:
        print(f"runtime_state={runtime_status.code}")
        if runtime_status.issues:
            print(f"runtime_message={state_message(runtime_status)}")
        if runtime_status.hud is not None:
            hud = runtime_status.hud
            hud_issue = next((issue for issue in runtime_status.issues if issue.component == "hud"), None)
            print(f"hud_running={hud.alive}")
            print(f"hud_status_item_created={hud.status_item_created}")
            print(f"hud_status_item_visible={'unknown' if hud.status_item_visible is None else hud.status_item_visible}")
            print(f"hud_main_thread_tick_age_sec={hud.main_thread_tick_age_sec if hud.main_thread_tick_age_sec is not None else 'unknown'}")
            if hud_issue:
                print(f"hud_message={hud_issue.message_zh}")
        print(f"running={is_running}")
        print(f"pid={pid or 'none'}")
        print(f"supervised_by_launchd={supervised}")
        print(f"status={status_val}")
        print(f"paused={paused_now}")
        print(f"disabled_sources={','.join(disabled_now)}")
        for source_name, record in import_rows:
            print(
                f"import.{source_name}={record.get('state')},batches={record.get('batches', 0)},"
                f"events={record.get('events', 0)},resumed={bool(record.get('resumed'))},"
                f"updated_at={record.get('updated_at', '')}"
            )
        print(f"history_imports={'enabled' if history_open else 'waiting_onboarding'}")
        print(f"started_at={started_at}")
        print(f"db_path={db_path}")
        print(f"db_size_mb={db_size_mb:.2f}")
        print(f"last_flush={last_flush}")
        print(f"enabled_watchers={','.join(enabled)}")
        print(f"runtime_ax_running={ax_running}")
        runtime_fix = _runtime_fix_command(runtime_status)
        if runtime_fix:
            print(f"runtime_fix={runtime_fix}")
    else:
        status_label = {
            "running": "运行中",
            "paused": "已暂停",
            "stopped": "已停止",
            "unknown": "未知",
        }.get(status_val, status_val)
        table = Table(title="采集状态", show_header=False, box=None)
        table.add_row("服务状态", _runtime_status_line(runtime_status))
        if runtime_status.issues:
            table.add_row("运行详情", state_message(runtime_status))
        if runtime_status.hud is not None:
            hud = runtime_status.hud
            hud_issue = next((issue for issue in runtime_status.issues if issue.component == "hud"), None)
            table.add_row("菜单栏 HUD", "运行中" if hud.alive and not hud_issue else "存在问题")
            table.add_row(
                "HUD 图标可见性",
                "未知" if hud.status_item_visible is None else "可见" if hud.status_item_visible else "不可见",
            )
            if hud_issue:
                table.add_row("HUD 详情", hud_issue.message_zh)
        if pid is not None:
            table.add_row("进程 PID", str(pid))
        table.add_row("运行方式", "launchd 托管" if supervised else "手动启动")
        table.add_row("采集阶段", status_label)
        table.add_row("全局暂停", "[yellow]是[/yellow]" if paused_now else "否")
        table.add_row("已关闭的数据源", "、".join(disabled_now) if disabled_now else "无")
        if not history_open:
            table.add_row("历史导入", WAITING_TEXT)
        for source_name, record in import_rows:
            table.add_row(f"数据导入（{source_name}）", _format_import_progress(record))
        table.add_row("启动时间", started_at)
        table.add_row("数据库路径", str(db_path))
        table.add_row("数据库大小", f"{db_size_mb:.2f} MB")
        table.add_row("最近一次写入", last_flush)
        table.add_row("已启用采集源", "、".join(enabled) if enabled else "无")
        table.add_row("键盘分块状态", "已启用" if getattr(cfg.watchers, "keyboard_chunk", False) else "未启用")
        table.add_row("正文采集状态", "已运行" if ax_running else "未见运行")
        table.add_row("屏幕识别状态", "已停用（OCR 已关闭）")
        runtime_fix = _runtime_fix_command(runtime_status)
        if runtime_fix:
            table.add_row("修复命令", runtime_fix)
        console.print(table)
