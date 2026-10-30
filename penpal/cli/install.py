from __future__ import annotations
import plistlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
import os
from typing import Any

import click
from rich.table import Table
from penpal.config import Config, write_default_config
from penpal.store.db import init_db
from penpal.utils.paths import get_data_dir, get_pid_path, get_hud_pid_path, get_log_path, get_config_path, get_launchd_plist_path, get_legacy_venv_dir
from penpal.runtime import ICON_OCCLUDED_HINT_ZH, evaluate, launchd_label_loaded
from penpal.runtime.control import (
    LEGACY_LAUNCHD_LABELS,
    load_job,
    restart as control_restart,
    stop_legacy_huds,
    unload_job,
    unload_legacy_jobs,
)
from penpal.app_bundle import AppBundleError, ensure_app_bundle, installed_app, open_app, remove_launcher_stamp
from penpal.foundation.runtime_plist import launchd_jobs, resolve_penpal_executable
from penpal.capture.keyboard_notice import mark_notice_shown, notice_pending, notice_text
from penpal.i18n import T
from . import main
from ._shared import _runtime_fix_command, _runtime_status_line, console


def _install_logs_dir() -> Path:
    return get_data_dir() / "logs"

def _penpal_executable_path() -> Path:
    try:
        return resolve_penpal_executable()
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


def _launchd_jobs(executable: Path) -> dict[str, dict[str, Any]]:
    return launchd_jobs(executable)

def _launchd_label_loaded(label: str) -> bool:
    return launchd_label_loaded(label)

def _launchd_bootout(plist_path: Path) -> bool:
    return unload_job(plist_path.stem, plist_path)

_LAUNCHD_LABELS = (
    "com.penpal.daemon",
    "com.penpal.healthcheck",
)


def _remove_legacy_launchd_jobs() -> list[str]:
    removed: list[str] = []
    failures = unload_legacy_jobs()
    if failures:
        details = "; ".join(failures)
        raise click.ClickException(T(
            f"无法卸载旧版 launchd 服务：{details}",
            f"Could not unload legacy launchd jobs: {details}",
        ))
    for label in LEGACY_LAUNCHD_LABELS:
        path = _launchd_plist_path(label)
        if _unlink_if_exists(path):
            removed.append(str(path))
    return removed

def _launchd_plist_path(label: str) -> Path:
    return get_launchd_plist_path(label)

def _is_own_plist(existing: Any, label: str) -> bool:
    """A plist carrying our Label was written by an earlier Penpal install, so it follows the current template
    on every install (an upgrade that changes the template, a pinned Cellar path, a binary that is gone).
    Only a file that is not a plist or names another job counts as a user's own and needs --force."""
    return isinstance(existing, dict) and existing.get("Label") == label

def _write_launchd_plist(label: str, payload: dict[str, Any], *, force: bool = False) -> str:
    path = _launchd_plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = plistlib.loads(path.read_bytes())
        except Exception:
            existing = None
        if existing == payload:
            return "unchanged"
        if not force and not _is_own_plist(existing, label):
            raise click.ClickException(f"{path} already exists and differs. Re-run with --force to replace it.")
        status = "updated"
    else:
        status = "created"

    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return status

def _launchd_load_plist(path: Path, *, replace: bool = False) -> bool:
    return load_job(path.stem, path, replace=replace)

def _install_launchd_jobs(
    *,
    load: bool,
    force: bool,
    launcher_path: Path | None = None,
) -> list[tuple[str, Path, str, bool]]:
    executable = _penpal_executable_path()
    _install_logs_dir().mkdir(parents=True, exist_ok=True)
    _remove_legacy_launchd_jobs()
    results: list[tuple[str, Path, str, bool]] = []
    pending: list[tuple[str, Path, str]] = []
    for label, payload in launchd_jobs(executable, launcher_path=launcher_path).items():
        status = _write_launchd_plist(label, payload, force=force)
        path = _launchd_plist_path(label)
        pending.append((label, path, status))
    if load:
        daemon_status = next(status for label, _path, status in pending if label == "com.penpal.daemon")
        result = control_restart("install", force_reload=daemon_status != "unchanged")
        if not result.ok:
            raise click.ClickException(T(result.error_zh, result.error_en))
    for label, path, status in pending:
        if load and label == "com.penpal.healthcheck" and not _launchd_load_plist(path, replace=True):
            raise click.ClickException(T(f"无法加载健康检查任务：{path}", f"Could not load healthcheck job: {path}"))
        results.append((label, path, status, launchd_label_loaded(label) if load else False))
    return results

def _write_default_user_config(*, force: bool = False) -> str:
    return write_default_config(get_config_path(), force=force)

@main.group("install", help="Install, initialize, launchd, and uninstall management.", invoke_without_command=True)
@click.option("--no-connect", is_flag=True, default=False, help="Skip auto-connecting AI tools")
@click.pass_context
def install_group(ctx, no_connect: bool = False):
    """Install lifecycle group."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(install_init, no_connect=no_connect)
        return
    if ctx.invoked_subcommand == "init":
        failures = unload_legacy_jobs()
        if failures:
            raise click.ClickException(T(
                f"无法卸载旧版 launchd 服务：{'; '.join(failures)}",
                f"Could not unload legacy launchd jobs: {'; '.join(failures)}",
            ))
    try:
        get_data_dir()
    except Exception as exc:
        from penpal.utils.paths import DataDirMigrationError

        if isinstance(exc, DataDirMigrationError):
            raise click.ClickException(str(exc)) from exc
        raise

def _echo_launchd_results(results: list[tuple[str, Path, str, bool]], *, plain: bool = False) -> None:
    for label, path, status_text, loaded in results:
        if plain:
            print(f"{label}={status_text},path={path},loaded={loaded}")
        else:
            load_text = "loaded" if loaded else "not loaded"
            console.print(f"[green]{label}[/green] {status_text} ({load_text}) -> {path}")

def _format_import_progress(record: dict) -> str:
    """One line for `penpal status` from the last import record (issue #32)."""
    state = record.get("state")
    batches = int(record.get("batches") or 0)
    events = int(record.get("events") or 0)
    resumed = "；上次中断后从游标续传" if record.get("resumed") else ""
    if state == "done":
        return f"已完成：{batches} 批，{events} 条{resumed}"
    if state == "interrupted":
        return f"已中断：已入库 {batches} 批（本次 {events} 条）；重新运行将从游标续传{resumed}"
    if state == "failed":
        return f"写入失败：已入库 {batches} 批；重新运行将从游标续传{resumed}"
    if state == "running":
        return f"进行中：已入库 {batches} 批（若长时间不更新，说明已中断，重新运行即可续传）{resumed}"
    return str(state or "未知")

def _show_keyboard_notice_once() -> None:
    """Print the keyboard capture risk notice the first time install/onboarding runs (issue #29)."""
    if not notice_pending():
        return
    click.echo(notice_text())
    click.echo("")
    mark_notice_shown()

def _stale_moved_dev_venv_script() -> tuple[Path, str] | None:
    bin_dir = get_data_dir() / "venv" / "bin"
    old_venv = get_legacy_venv_dir()
    for name in ("penpal", "keypulse"):
        script = bin_dir / name
        try:
            with script.open(encoding="utf-8") as handle:
                first_line = handle.readline().strip()
        except OSError:
            continue
        if first_line.startswith("#!") and str(old_venv) in first_line:
            return script, first_line[2:].strip()
    return None

def _isolated() -> bool:
    return os.environ.get("PENPAL_ISOLATED") == "1"


def _ensure_daemon_launcher(executable: Path) -> tuple[Path | None, str]:
    """Build or keep Penpal.app. The daemon runs from its launcher so macOS lists its privacy grants under Penpal;
    without it the daemon keeps the stable entry and the grants show as Python."""
    if _isolated():
        return None, "skipped"  # tests never compile or sign
    try:
        launcher, state, _result = ensure_app_bundle(executable)
    except (AppBundleError, OSError) as exc:
        console.print(T(
            f"警告：无法构建 Penpal.app（{exc}），权限列表将显示为 Python。",
            f"Warning: could not build Penpal.app ({exc}); the permission list will show Python.",
        ))
        return None, "fallback"
    if state == "fallback":
        console.print(T(
            "未能构建原生启动器，权限列表将显示为 Python。",
            "Native launcher unavailable; the permission list will show Python.",
        ))
    return launcher, state


def _cleanup_legacy_huds() -> list[int]:
    """Close a HUD that predates the HUD runtime record: it cannot self-reload and `hud status` cannot see it."""
    try:
        stopped = stop_legacy_huds()
    except OSError as exc:
        console.print(T(f"警告：无法停止旧版 HUD：{exc}", f"Warning: could not stop legacy HUD: {exc}"))
        return []

    if not stopped:
        return []

    pids_str = ", ".join(str(p) for p in stopped)
    console.print(T(f"已停止旧版菜单栏 HUD（PID {pids_str}）。", f"Stopped legacy menu bar HUD (PID {pids_str})."))

    bundle = installed_app()
    if bundle is not None:
        try:
            open_app(bundle)
        except OSError as exc:
            console.print(T(f"警告：无法重新打开 Penpal.app：{exc}", f"Warning: could not reopen Penpal.app: {exc}"))
    else:
        console.print(T(
            "旧版菜单栏 HUD 已关闭，运行 penpal app install 可安装新版应用。",
            "Old menu bar HUD was closed; run penpal app install to install the new one.",
        ))

    return stopped


@install_group.command("launchd", help="Write and load Penpal launchd jobs.")
@click.option("--no-load", is_flag=True, default=False, help="Write plists without loading launchd")
@click.option("--force", is_flag=True, default=False, help="Also replace a file at the plist path that Penpal did not write")
@click.option("--plain", is_flag=True, default=False, help="Plain text output")
def install_launchd(no_load, force, plain):
    """Install launchd jobs."""
    if sys.platform != "darwin":
        raise click.ClickException("launchd installation is only supported on macOS.")
    results = _install_launchd_jobs(load=not no_load, force=force)
    _echo_launchd_results(results, plain=plain)

@install_group.command("init", help="Initialize the Penpal runtime after Homebrew install.")
@click.option("--no-launchd", is_flag=True, default=False, help="Skip launchd job installation")
@click.option("--no-load", is_flag=True, default=False, help="Write launchd plists without loading them")
@click.option("--force-config", is_flag=True, default=False, help="Overwrite existing config.toml")
@click.option("--force-launchd", is_flag=True, default=False, help="Also replace a file at a plist path that Penpal did not write")
@click.option("--no-connect", is_flag=True, default=False, help="Skip auto-connecting AI tools")
@click.option("--plain", is_flag=True, default=False, help="Plain text output")
def install_init(no_launchd, no_load, force_config, force_launchd, no_connect, plain):
    """Initialize runtime."""
    if sys.platform != "darwin":
        raise click.ClickException("Penpal install init is currently macOS-only.")

    data_dir = get_data_dir()
    logs_dir = _install_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    config_status = _write_default_user_config(force=force_config)
    cfg = Config.load()
    init_db(cfg.db_path_expanded)

    bundle_launcher, launcher_state = _ensure_daemon_launcher(_penpal_executable_path())

    launchd_results: list[tuple[str, Path, str, bool]] = []
    if not no_launchd:
        launchd_results = _install_launchd_jobs(
            load=not no_load,
            force=force_launchd,
            launcher_path=bundle_launcher,
        )

    if not no_launchd and not no_load:
        _cleanup_legacy_huds()
        if launcher_state in {"built", "rebuilt"}:
            console.print(T(
                "请在「系统设置 → 隐私与安全性」中为 Penpal 授予「输入监控」、「辅助功能」和「完全磁盘访问权限」；旧的 python3.13 项目可移除。",
                "Grant Input Monitoring, Accessibility and Full Disk Access to \"Penpal\" in System Settings → Privacy & Security; the old python3.13 entry can be removed.",
            ))

    _show_keyboard_notice_once()

    if not no_connect:
        try:
            from penpal.mcp.connect import auto_connect

            connect_results = auto_connect()
            connected_names = [r.tool_name for r in connect_results if r.status in ("connected", "already_connected")]
            failed_msgs = [f"{r.tool_name}: {r.error}" for r in connect_results if r.status == "parse_error"]
            if plain:
                if connected_names:
                    print(f"connect=connected:{','.join(connected_names)}")
                if failed_msgs:
                    print(f"connect_failed={';'.join(failed_msgs)}")
            else:
                if connected_names:
                    console.print(f"[green]已自动接入 AI 工具：{', '.join(connected_names)}[/green]")
                if failed_msgs:
                    console.print(f"[yellow]部分 AI 工具接入失败：{'; '.join(failed_msgs)}[/yellow]")
        except Exception as exc:
            if plain:
                print(f"connect_error={exc}")
            else:
                console.print(f"[yellow]警告：自动接入 AI 工具失败（{exc}），跳过。后续可运行 `penpal connect --auto`。[/yellow]")

    from penpal.sources.history_gate import historical_imports_allowed

    history_waiting = not historical_imports_allowed()
    if plain:
        print(f"data_dir={data_dir}")
        print(f"logs_dir={logs_dir}")
        print(f"config={config_status}:{get_config_path()}")
        print(f"db_path={cfg.db_path_expanded}")
        for label, path, status_text, loaded in launchd_results:
            print(f"launchd.{label}={status_text},path={path},loaded={loaded}")
        if history_waiting:
            print("next=penpal install onboard")
        return

    console.print("[green]Penpal runtime initialized.[/green]")
    console.print(f"Data: {data_dir}")
    console.print(f"Logs: {logs_dir}")
    console.print(f"Config: {get_config_path()} ({config_status})")
    console.print(f"Database: {cfg.db_path_expanded}")
    if launchd_results:
        _echo_launchd_results(launchd_results)
    console.print("")
    console.print("Next steps:")
    if history_waiting:
        console.print("  penpal install onboard   # 选择要导入的历史来源（之前不读取历史）")
    console.print("  penpal doctor")

@install_group.command("doctor", help="Check install-level status.")
@click.option("--plain", is_flag=True, default=False, help="Plain text output")
def install_doctor(plain):
    """Install diagnostics."""
    checks: dict[str, tuple[bool, str]] = {}
    runtime_status = evaluate()
    runtime_fix = _runtime_fix_command(runtime_status)
    runtime_detail = f"state={_runtime_status_line(runtime_status)}"
    if runtime_fix:
        runtime_detail += f"; fix=`{runtime_fix}`"
    checks["runtime"] = (not runtime_status.failed, runtime_detail)
    hud_issue = next((issue for issue in runtime_status.issues if issue.component == "hud"), None)
    if runtime_status.hud is not None and runtime_status.hud.alive:
        visible = runtime_status.hud.status_item_visible
        # As in `penpal doctor`, only the evaluator decides pass/fail; visibility is informational.
        checks["hud"] = (
            hud_issue is None,
            hud_issue.message_zh if hud_issue else
            "主线程有响应；" + ("状态栏图标可见" if visible is True else "状态栏图标可见性未知") + f"。{ICON_OCCLUDED_HINT_ZH}",
        )
    try:
        executable = _penpal_executable_path()
        checks["executable"] = (executable.exists(), str(executable))
    except click.ClickException as exc:
        checks["executable"] = (False, str(exc))

    config_path = get_config_path()
    checks["config"] = (config_path.exists(), str(config_path))
    cfg = Config.load()
    checks["database"] = (cfg.db_path_expanded.exists(), str(cfg.db_path_expanded))

    for label in _LAUNCHD_LABELS:
        path = _launchd_plist_path(label)
        loaded = runtime_status.managed if label == "com.penpal.daemon" else _launchd_label_loaded(label)
        checks[f"launchd.{label}"] = (path.exists() and loaded, f"path={path},loaded={loaded}")
    for label in LEGACY_LAUNCHD_LABELS:
        path = _launchd_plist_path(label)
        loaded = _launchd_label_loaded(label)
        checks[f"legacy_launchd.{label}"] = (not path.exists() and not loaded, f"path={path},loaded={loaded}")
    stale_venv = _stale_moved_dev_venv_script()
    if stale_venv:
        checks["dev_venv"] = (False, f"{stale_venv[0]} points to {stale_venv[1]}; rerun install.sh --dev")

    if plain:
        for name, (ok, detail) in checks.items():
            print(f"{name}={'OK' if ok else 'FAIL'} {detail}")
    else:
        table = Table(title="Penpal install doctor", show_header=True, header_style="bold cyan")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        for name, (ok, detail) in checks.items():
            table.add_row(name, "[green]OK[/green]" if ok else "[red]FAIL[/red]", detail)
        console.print(table)
    if not all(ok for ok, _detail in checks.values()):
        raise SystemExit(1)

def _unlink_if_exists(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True

def _remove_legacy_path(path: Path) -> bool:
    data_dir = get_data_dir().resolve()
    target = path.expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError:
        resolved = target
    if data_dir != resolved and data_dir not in resolved.parents:
        raise click.ClickException(f"Refusing to remove path outside ~/.penpal: {target}")
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return _unlink_if_exists(target)

@install_group.command(
    "onboard",
    help=T(
        "首次使用引导：数据边界、逐个确认来源、来源所需权限、首次导入，再引导授予 macOS 权限（辅助功能 / 输入监控）。",
        "First-use guide: data boundary, one-by-one source choice, source permissions, first import, then macOS permission grants.",
    ),
)
@click.option("--non-interactive", is_flag=True, default=False, help="Skip interactive prompts (for CI / automation)")
@click.option(
    "--sources",
    "sources_opt",
    default=None,
    help="Non-interactive choice: comma-separated source names (e.g. claude_code), or 'all' for every source found",
)
@click.option("--yes", "yes_all", is_flag=True, default=False, help="Enable every source found, no prompts (implies --non-interactive)")
def install_onboard(non_interactive: bool, sources_opt: str | None, yes_all: bool):
    """First-use guide: data boundary, source choice, source permissions, first import, then TCC grants."""
    if sys.platform != "darwin":
        raise click.ClickException("install onboard is macOS-only.")
    if yes_all:
        non_interactive = True

    from penpal.permissions import (
        PERMISSION_SPECS,
        PermissionStatus,
        open_settings_pane,
    )

    console.print("[bold]Penpal 首次使用引导[/bold]\n")
    _show_keyboard_notice_once()
    from penpal.sources import onboarding

    for line in onboarding.BOUNDARY_LINES:
        console.print(f"· {line}", markup=False)
    console.print("")

    chosen = _onboard_choose_sources(
        non_interactive=non_interactive, sources_opt=sources_opt, yes_all=yes_all
    )
    chosen_names = sorted(chosen or [])
    _onboard_source_permissions(chosen_names, non_interactive=non_interactive)

    console.print("[bold]Penpal 权限引导[/bold]\n")
    console.print(
        "重新打包后 macOS 会撤销以下权限，需要重新授权。"
        "我会按顺序检查每一项，缺失时打开系统设置对应面板。\n"
    )

    for spec in PERMISSION_SPECS:
        status = spec.check()
        if status == PermissionStatus.GRANTED:
            console.print(f"[green]✓[/green] {spec.label}: 已授权")
            continue
        if status == PermissionStatus.UNAVAILABLE:
            console.print(f"[yellow]?[/yellow] {spec.label}: API 不可用（跳过）")
            continue

        console.print(f"[red]✗[/red] {spec.label}: 未授权")

        # Trigger native prompt (will only show if state is undetermined).
        prompt_result = spec.request()
        if prompt_result == PermissionStatus.GRANTED:
            console.print("  [green]→ 已通过原生授权框完成[/green]")
            continue

        # State is denied → native prompt won't show, must open settings.
        console.print(f"  打开系统设置面板：{spec.settings_pane}")
        open_settings_pane(spec)
        if non_interactive:
            console.print("  [yellow]non-interactive 模式：跳过等待，请稍后重跑此命令验证[/yellow]")
            continue
        console.print("  请在系统设置里勾选 Penpal 对此项的授权。")
        click.confirm("  完成后回车继续 →", default=True, show_default=False)

        # Re-check
        recheck = spec.check()
        if recheck == PermissionStatus.GRANTED:
            console.print("  [green]✓ 已确认授权[/green]")
        else:
            console.print(
                "  [yellow]仍未授权——如系统设置里没有 Penpal 条目，"
                "点击「➕」选择 /Applications/Penpal.app[/yellow]"
            )

    if chosen_names:
        _onboard_first_import(chosen_names)
    else:
        console.print("来源：未启用任何来源，之后可用 `penpal source enable NAME` 打开。\n")

    console.print("\n[bold]权限引导完成，正在重启 daemon 让 TCC 缓存刷新...[/bold]")
    restart_result = control_restart("onboarding")
    if restart_result.ok:
        console.print(T("daemon 已重启。运行 `penpal healthcheck` 验证状态。", "Daemon restarted. Run `penpal healthcheck` to verify status."))
    else:
        console.print(T(restart_result.error_zh, restart_result.error_en))

def _onboard_choose_sources(*, non_interactive: bool, sources_opt: str | None, yes_all: bool) -> list[str] | None:
    """Ask about each source found, one by one (or take the scripted choice). Returns the chosen names, or None
    when a non-interactive run gave no choice, in which case the switches are left as they are."""
    from penpal.sources import onboarding
    from penpal.store.db import init_db

    init_db(Config.load().db_path_expanded)
    if sources_opt is None and not yes_all and non_interactive:
        console.print("来源：未指定（非交互模式请加 --sources claude_code，或 --sources all）。开关保持不变。\n")
        return None

    offers = onboarding.offered_sources()
    offered_names = [offer.name for offer in offers]
    if sources_opt is not None and not yes_all:
        requested = [part.strip() for part in sources_opt.split(",") if part.strip()]
        if requested == ["all"]:
            chosen = list(offered_names)
        else:
            unknown = sorted(set(requested) - onboarding.choosable_names())
            if unknown:
                raise click.ClickException(f"unknown source: {', '.join(unknown)} (see `penpal source list`)")
            chosen = requested
    elif yes_all:
        chosen = list(offered_names)
    else:
        if offers:
            console.print("来源：逐个确认。默认不启用，只有你回答 y 的来源才会被读取。\n")
        chosen = []
        for offer in offers:
            label = f"  启用 {offer.name}（{offer.description}；{onboarding.describe_instances(offer)}）？"
            if click.confirm(label, default=False):
                chosen.append(offer.name)
        console.print("")

    applied = onboarding.apply_choice(chosen)
    on = [name for name, enabled in applied.items() if enabled]
    off = [name for name, enabled in applied.items() if not enabled]
    console.print(f"来源已确认：启用 {', '.join(on) or '无'}；关闭 {', '.join(off) or '无'}。\n", markup=False)
    return on

def _onboard_source_permissions(chosen: list[str], *, non_interactive: bool) -> None:
    """Show the macOS permission each chosen source needs, wait for the user to return, then re-check the sources
    with the same availability probe `penpal doctor` uses."""
    from penpal.sources import onboarding

    pairs = onboarding.permissions_for(chosen)
    if not pairs:
        return
    console.print("[bold]已启用来源需要的系统权限[/bold]", markup=True)
    shown: set[str] = set()
    for name, key in pairs:
        console.print(f"  {name} 需要：{onboarding.PERMISSION_LABELS[key]}", markup=False)
        if key not in shown:
            shown.add(key)
            console.print(f"  打开设置：open \"{onboarding.PERMISSION_PANES[key]}\"", markup=False)
    if not non_interactive:
        click.confirm("  在系统设置里完成授权后回车继续 →", default=True, show_default=False)
    _echo_source_recheck(chosen)
    console.print("")

def _echo_source_recheck(chosen: list[str]) -> None:
    from penpal.sources import onboarding
    from penpal.sources.registry import list_sources

    by_name = {plugin.name: plugin for plugin in list_sources()}
    for name in chosen:
        state = onboarding.check_source(name, by_name)
        if state.available:
            console.print(f"  [green]✓[/green] {name}: 可读取", markup=True)
        else:
            console.print(f"  [red]✗[/red] {name}: {state.reason}", markup=False)
            console.print(f"    修复：{onboarding.fix_hint(name, state.reason or '')}", markup=False)

def _onboard_first_import(chosen: list[str]) -> None:
    """First import of the chosen sources, in batches; a failure leaves the saved cursor for the next run."""

    from penpal.sources import importer, onboarding

    now = datetime.now(timezone.utc)
    since = onboarding.first_import_since(now)
    console.print("[bold]首次导入[/bold]")
    for name in chosen:
        console.print(f"  {name} …", markup=False)

        def _batch_done(result, _name=name):
            console.print(f"    第 {result.batches} 批已入库（本次 {result.events} 条）", markup=False)

        try:
            result = importer.import_source(name, since=since, until=now, on_batch=_batch_done)
        except Exception as exc:
            console.print(f"    [yellow]导入中断：{exc}。重新运行即可从已保存的游标续传。[/yellow]", markup=False)
            continue
        console.print(f"    {onboarding.format_import_result(result)}", markup=False)
    console.print("")

@install_group.command("uninstall", help="Uninstall launchd jobs and optionally remove runtime/data.")
@click.option("--remove-runtime", is_flag=True, default=False, help="Remove logs, pid files, health state, and other runtime files")
@click.option("--remove-data", is_flag=True, default=False, help="Remove config, database, and state files (requires --force)")
@click.option("--force", is_flag=True, default=False, help="Confirm destructive removal")
@click.option("--plain", is_flag=True, default=False, help="Plain text output")
def install_uninstall(remove_runtime, remove_data, force, plain):
    """Uninstall runtime wiring."""
    if remove_data and not force:
        raise click.ClickException("--remove-data requires --force.")

    removed: list[str] = []
    for label in (*_LAUNCHD_LABELS, *LEGACY_LAUNCHD_LABELS):
        path = _launchd_plist_path(label)
        if path.exists():
            _launchd_bootout(path)
        else:
            unload_job(label)
        if _unlink_if_exists(path):
            removed.append(str(path))

    if remove_runtime:
        remove_launcher_stamp()
        for path in (
            get_pid_path(),
            get_hud_pid_path(),
            get_log_path(),
            get_data_dir() / "health.json",
            _install_logs_dir(),
        ):
            if _remove_legacy_path(path):
                removed.append(str(path))

    # Full uninstall always removes the app bundle and the stamp
    bundle = installed_app()
    if bundle is not None and bundle.exists():
        shutil.rmtree(bundle)
        remove_launcher_stamp()
        removed.append(str(bundle))

    if remove_data:
        cfg = Config.load()
        for path in (
            cfg.db_path_expanded,
            get_config_path(),
        ):
            if _remove_legacy_path(path):
                removed.append(str(path))

    if plain:
        for item in removed:
            print(f"removed={item}")
        print("data_preserved=" + str(not remove_data))
        return

    console.print("[green]Penpal launchd jobs removed.[/green]")
    if removed:
        for item in removed:
            console.print(f"removed: {item}")
    if not remove_data:
        console.print(f"User data preserved under {get_data_dir()}")
