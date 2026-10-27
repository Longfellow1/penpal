from __future__ import annotations
import os
import sys
import time
import signal
import subprocess
from pathlib import Path
import click
from rich.table import Table
from penpal.utils.paths import get_hud_lock_path, get_hud_pid_path
from penpal.utils.lock import SingleInstanceLock
from penpal.hud import run_hud
from penpal.i18n import T
from penpal.runtime import evaluate, find_legacy_hud_pids
from . import main
from ._shared import PLAIN_HELP, console, err_console, get_config, require_db
from .install import _penpal_executable_path


def _hud_lock() -> SingleInstanceLock:
    return SingleInstanceLock(get_hud_lock_path(), get_hud_pid_path())

def _find_legacy_hud_pid() -> int | None:
    pids = find_legacy_hud_pids()
    return pids[0] if pids else None

def _start_hud():
    cfg = get_config()
    lock = _hud_lock()
    if not lock.acquire():
        err_console.print(f"[yellow]HUD already running (PID {lock.get_pid()}).[/yellow]")
        sys.exit(1)
    legacy_pid = _find_legacy_hud_pid()
    if legacy_pid:
        lock.release()
        err_console.print(f"[yellow]HUD already running (PID {legacy_pid}).[/yellow]")
        sys.exit(1)

    require_db(cfg)
    try:
        run_hud(cfg)
    finally:
        lock.release()

def _stop_hud():
    lock = _hud_lock()
    pid = _find_legacy_hud_pid()

    if not lock.is_running() and not pid:
        console.print("[yellow]HUD is not running.[/yellow]")
        return
    if not pid:
        err_console.print("[yellow]HUD is running, but its process could not be identified safely.[/yellow]")
        raise SystemExit(1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        console.print("[yellow]HUD is not running.[/yellow]")
        return

    for _ in range(50):
        time.sleep(0.1)
        if not lock.is_running():
            console.print("[green]HUD stopped.[/green]")
            return

    console.print("[yellow]Stop signal sent (HUD may still be shutting down).[/yellow]")

def _show_hud_status(plain: bool = False):
    runtime = evaluate()
    hud = runtime.hud
    is_running = bool(hud and hud.alive)
    pid = hud.pid if hud else None
    issue = next((item for item in runtime.issues if item.component == "hud"), None)
    if not is_running:
        status = "not_running"
    elif issue is not None:
        status = "issue"
    elif hud.status_item_visible is None:
        status = "visibility_unknown"
    else:
        status = "running"

    if plain:
        print(f"running={is_running}")
        print(f"pid={pid or 'none'}")
        print(f"status={status}")
        if hud is not None:
            print(f"status_item_created={hud.status_item_created}")
            print(f"status_item_visible={'unknown' if hud.status_item_visible is None else hud.status_item_visible}")
            print(f"main_thread_tick_age_sec={hud.main_thread_tick_age_sec if hud.main_thread_tick_age_sec is not None else 'unknown'}")
        if issue:
            print(f"message={issue.message_zh}")
        return

    table = Table(show_header=False, box=None)
    status_text = {
        "running": "[green]运行中[/green]",
        "issue": "[red]存在问题[/red]",
        "visibility_unknown": "[yellow]可见性未知[/yellow]",
        "not_running": "[yellow]未运行[/yellow]",
    }[status]
    table.add_row("HUD 状态", status_text)
    table.add_row("PID", str(pid or "—"))
    if hud is not None:
        table.add_row("状态栏图标已创建", "是" if hud.status_item_created else "否")
        visible = "未知" if hud.status_item_visible is None else "可见" if hud.status_item_visible else "不可见"
        table.add_row("状态栏图标可见性", visible)
        if hud.main_thread_tick_age_sec is not None:
            table.add_row("主线程最近响应", f"{hud.main_thread_tick_age_sec:.0f} 秒前")
    if issue:
        table.add_row("详情", issue.message_zh)
    console.print(table)

@main.group(invoke_without_command=True, help=T("管理 macOS 菜单栏 HUD。", "Manage the macOS status bar HUD."))
@click.pass_context
def hud(ctx):
    """HUD."""
    if ctx.invoked_subcommand is None:
        _start_hud()

@hud.command("start", help=T("启动 HUD。", "Launch HUD."))
def hud_start():
    """Start HUD."""
    _start_hud()

@hud.command("stop", help=T("停止 HUD。", "Stop HUD."))
def hud_stop():
    """Stop HUD."""
    _stop_hud()

@hud.command("close", help=T("关闭 HUD。", "Close HUD."))
def hud_close():
    """Close HUD."""
    _stop_hud()

@hud.command("status", help=T("查看 HUD 进程状态。", "Show HUD process status."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def hud_status(plain):
    """HUD status."""
    _show_hud_status(plain=plain)

@main.group("app", help=T("可选桌面 App（菜单栏 HUD）的安装与卸载。", "Install / remove the optional desktop App (menu bar HUD)."))
def app_group():
    """Optional desktop App."""

def _app_dir_option(func):
    func = click.option("--agents-dir", "agents_dir", type=click.Path(path_type=Path), default=None, hidden=True,
                        help="Override the LaunchAgents directory (tests)")(func)
    return click.option("--dir", "apps_dir", type=click.Path(path_type=Path), default=None,
                        help=T("安装目录（默认 ~/Applications）", "Install directory (default ~/Applications)"))(func)

@app_group.command("install", help=T("安装 Penpal.app（原生启动已安装环境里的 HUD；自身不需要系统权限）。",
                                     "Install Penpal.app (a native launcher runs the HUD from the installed environment; it needs no system permission itself)."))
@click.option("--login", is_flag=True, default=False, help=T("登录时自动启动（注册 com.penpal.hud 启动项）", "Start at login (registers the com.penpal.hud launch agent)"))
@_app_dir_option
def app_install(login, apps_dir, agents_dir):
    """Install Penpal.app."""
    from penpal import __version__
    from penpal.app_bundle import AppBundleError, install_app

    try:
        result = install_app(_penpal_executable_path(), version=__version__, apps_dir=apps_dir,
                             agents_dir=agents_dir, login=login)
    except AppBundleError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in result.actions:
        console.print(f"[green]{line}[/green]")
    console.print(T("提示：App 只是 HUD 的入口，不需要任何系统权限；采集权限仍由后台 daemon 持有。",
                    "Note: the App is only a HUD launcher and needs no system permission; capture permissions stay with the daemon."))

@app_group.command("uninstall", help=T("卸载 Penpal.app 和登录启动项（不影响 daemon 与数据）。", "Remove Penpal.app and its login agent (the daemon and your data are untouched)."))
@_app_dir_option
def app_uninstall(apps_dir, agents_dir):
    """Uninstall Penpal.app."""
    from penpal.app_bundle import AppBundleError, uninstall_app

    try:
        result = uninstall_app(apps_dir=apps_dir, agents_dir=agents_dir)
    except AppBundleError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in result.actions or ["nothing to remove"]:
        console.print(f"[green]{line}[/green]")
