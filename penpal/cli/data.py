from __future__ import annotations
import sys
from pathlib import Path
import click
from penpal.services.export import export_json, export_csv, export_markdown
from penpal.i18n import T
from . import main
from ._shared import DATE_HELP_TEXT, console, err_console, get_config, require_db
from .search import _parse_date


@main.command(help=T("导出活动数据。", "Export activity data."))
@click.option("--format", type=click.Choice(["json", "csv", "md"]), default="json",
              help=T("导出格式", "Export format"))
@click.option("--days", default=None, type=int, help=T("导出天数", "Number of days to export"))
@click.option("--date", default=None, help=DATE_HELP_TEXT)
@click.option("--output", default=None, help=T("输出文件路径", "Output file path"))
def export(format, days, date, output):
    """Export data."""
    cfg = get_config()
    require_db(cfg)
    parsed_date = _parse_date(date) if date else None

    if format == "json":
        data = export_json(days=days, date_str=parsed_date)
    elif format == "csv":
        data = export_csv(days=days, date_str=parsed_date)
    elif format == "md":
        data = export_markdown(days=days, date_str=parsed_date)
    else:
        err_console.print(f"[red]Unknown format: {format}[/red]")
        sys.exit(1)

    if output:
        Path(output).write_text(data, encoding="utf-8")
        console.print(f"[green]Exported to {output}[/green]")
    else:
        print(data)

@main.command(help=T("删除活动数据。", "Delete activity data."))
@click.option("--today", is_flag=True, default=False, help=T("清理今天的数据", "Purge today's data"))
@click.option("--last-hours", type=int, default=None, help=T("清理最近 N 小时数据", "Purge last N hours"))
@click.option("--app", default=None, help=T("清理指定应用的数据（精确匹配，不区分大小写）", "Purge data for one app (exact name, case-insensitive)"))
@click.option("--source", default=None, help=T("清理指定来源的数据（精确匹配）", "Purge data from one source (exact match)"))
@click.option("--dry-run", is_flag=True, default=False, help=T("只统计将删除的数量，不删除", "Only count what would be deleted; delete nothing"))
@click.option("--confirm", is_flag=True, default=False, help=T("无需确认提示直接删除", "Confirm deletion without prompt"))
def purge(today, last_hours, app, source, dry_run, confirm):
    """Purge data (raw events plus their derived rows: FTS, evidence, feedback, search docs, sessions)."""
    cfg = get_config()
    require_db(cfg)

    from penpal.services.purge import purge_selector, run_purge

    selector = purge_selector(today=today, last_hours=last_hours, app=app, source=source)
    if selector is None:
        err_console.print("[red]Specify --today, --last-hours, --app or --source[/red]")
        sys.exit(1)

    counts = run_purge(selector, dry_run=True)
    console.print("Would delete:" if dry_run else "Will delete:")
    for name, n in counts.items():
        console.print(f"  {name}: {n}")
    if dry_run:
        console.print("[yellow]Dry run: nothing deleted.[/yellow]")
        return
    if counts["raw_events"] == 0 and counts["memories"] == 0:
        console.print("[yellow]No data found to delete.[/yellow]")
        return

    if not confirm:
        msg = f"Delete {counts['raw_events']} events and {counts['memories']} memories"
        if app:
            msg += f" from app {app}"
        if source:
            msg += f" from source {source}"
        msg += " and their derived data?"
        if not click.confirm(msg):
            console.print("[yellow]Cancelled.[/yellow]")
            return

    counts = run_purge(selector)
    console.print("[green]Deleted:[/green]")
    for name, n in counts.items():
        console.print(f"  {name}: {n}")
    if source:
        console.print(T("提示：这只删除 Penpal 里的副本，原始文件不受影响；若不想再导入，请用 `penpal source disable` 停用该来源。",
                        "Note: only Penpal's copy is deleted, the original file is untouched; to stop re-importing it, disable the source."))
