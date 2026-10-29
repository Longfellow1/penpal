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
from penpal.runtime import active_hud_pid, evaluate, find_legacy_hud_pids
from penpal.runtime.control import pid_is_running, relaunch_hud, wait_for_hud_ready
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

def _running_hud_pids() -> list[int]:
    """The current HUD (its runtime record, start time checked) and any HUD that predates the record.

    The current HUD runs from Penpal.app's launcher, whose command line is not "penpal hud", so only its
    record identifies it."""
    current = active_hud_pid()
    return ([current] if current else []) + [pid for pid in find_legacy_hud_pids() if pid != current]


def _stop_hud():
    from penpal.foundation.hud_record import remove_hud_record

    lock = _hud_lock()
    # The record is what identifies the app-launched HUD, so find it before the record goes.
    pids = _running_hud_pids()
    # With no record the supervisor treats the stop as deliberate and does not relaunch the HUD.
    remove_hud_record()

    if not lock.is_running() and not pids:
        console.print("[yellow]HUD is not running.[/yellow]")
        return
    if not pids:
        err_console.print("[yellow]HUD is running, but its process could not be identified safely.[/yellow]")
        raise SystemExit(1)

    signalled_pids: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            signalled_pids.append(pid)
        except ProcessLookupError:
            continue
    if not signalled_pids:
        console.print("[yellow]HUD is not running.[/yellow]")
        return

    for _ in range(50):
        time.sleep(0.1)
        if not lock.is_running() and not any(pid_is_running(pid) for pid in signalled_pids):
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
            if hud.supervisor_summary_zh:
                print(f"supervisor={hud.supervisor_summary_zh}")
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
        if hud.supervisor_summary_zh:
            table.add_row("自愈记录", hud.supervisor_summary_zh)
    if issue:
        table.add_row("详情", issue.message_zh)
    console.print(table)

@main.group(invoke_without_command=True, help=T("管理 macOS 菜单栏 HUD。", "Manage the macOS status bar HUD."))
@click.pass_context
def hud(ctx):
    """HUD."""
    if ctx.invoked_subcommand is None:
        # Bare 'hud' is the bundle's internal entry and developer foreground mode.
        _start_hud()

@hud.command("start", help=T("打开 Penpal.app 启动 HUD。", "Open Penpal.app to launch HUD."))
def hud_start():
    """Start HUD by opening Penpal.app."""
    running_pid = active_hud_pid() or _find_legacy_hud_pid()
    if running_pid is not None:
        err_console.print(f"[yellow]HUD already running (PID {running_pid}).[/yellow]")
        sys.exit(1)

    result = relaunch_hud()
    if not result.ok:
        err_console.print(T(result.error_zh, result.error_en))
        sys.exit(1)

    if not wait_for_hud_ready():
        err_console.print(T(
            "已请求打开 Penpal.app，但 10 秒内 HUD 未就绪；运行 penpal doctor 查看。",
            "Asked Penpal.app to open, but the HUD was not ready within 10 seconds; run penpal doctor.",
        ))
        sys.exit(1)

    pid = active_hud_pid()
    console.print(T(f"HUD 已启动（PID {pid}）。", f"HUD started (PID {pid})."))

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

@app_group.command("install", help=T("安装 Penpal.app（原生启动器，HUD 和后台 daemon 都从它运行）。",
                                     "Install Penpal.app (the native launcher that runs the HUD and the background daemon)."))
@click.option("--login", is_flag=True, default=False, help=T("登录时自动启动（注册 com.penpal.hud 启动项）", "Start at login (registers the com.penpal.hud launch agent)"))
@_app_dir_option
def app_install(login, apps_dir, agents_dir):
    """Install Penpal.app."""
    from penpal.app_bundle import AppBundleError, install_app

    try:
        result = install_app(_penpal_executable_path(), apps_dir=apps_dir,
                             agents_dir=agents_dir, login=login)
    except AppBundleError as exc:
        raise click.ClickException(str(exc)) from exc
    for line in result.actions:
        console.print(f"[green]{line}[/green]")
    console.print(T("提示：后台 daemon 也从这个 App 运行，系统设置里的采集权限请授予 Penpal。",
                    "Note: the background daemon also runs from this App; grant capture permissions to Penpal in System Settings."))

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
