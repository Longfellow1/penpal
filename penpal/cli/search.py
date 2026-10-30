from __future__ import annotations
import re
import sqlite3
import sys
from datetime import datetime, timedelta
import click
from rich.table import Table
from rich.panel import Panel
from penpal.store.repository import get_sessions, get_session_by_id
from penpal.store.models import SearchDoc
from penpal.services.timeline import get_timeline_rows
from penpal.services.stats import get_stats
from penpal.search.engine import recent_clipboard, recent_manual, recent_sessions_docs
from penpal.capture.normalizer import normalize_manual_event
from penpal.utils.dates import resolve_local_date, local_timezone
from penpal.i18n import T
from penpal.search.backends import resolve_search_backend
from . import main
from ._shared import DATE_HELP_TEXT, PLAIN_HELP, console, err_console, get_config, require_db

import logging
logger = logging.getLogger(__name__)


def _parse_date(arg: str) -> str:
    raw = (arg or "").strip()
    if not raw:
        raise click.UsageError("`--date` 不能为空")

    normalized = raw.lower()
    if normalized in {"today", "yesterday"}:
        return resolve_local_date(normalized, yesterday=False)

    relative_match = re.fullmatch(r"-(\d+)", normalized)
    if relative_match:
        days_ago = int(relative_match.group(1))
        today_local = datetime.now(local_timezone()).date()
        return (today_local - timedelta(days=days_ago)).isoformat()

    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise click.UsageError(f"`--date` 无效：{raw}。支持 today / yesterday / -N / YYYY-MM-DD。") from exc

@main.command(help=T("保存一条手动笔记。", "Save a manual note."))
@click.option("--text", default=None, help=T("要保存的文本", "Text to save"))
@click.option("--tag", default=None, help=T("笔记标签", "Tag for the note"))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def save(text, tag, plain):
    """Save note."""
    # Read from stdin if no --text provided
    if text is None:
        if not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            err_console.print("[red]No text provided. Use --text or pipe via stdin.[/red]")
            sys.exit(1)

    if not text or not text.strip():
        err_console.print("[red]Cannot save empty text.[/red]")
        sys.exit(1)

    cfg = get_config()
    require_db(cfg)

    # Normalize and insert
    from penpal.store.repository import insert_raw_event

    event = normalize_manual_event(text.strip(), tags=tag)
    event_id = insert_raw_event(event)

    # Also insert search doc
    doc = SearchDoc(
        ref_type="manual",
        ref_id=str(event_id),
        title=text[:100] if text else "Note",
        body=text,
        tags=tag,
        app_name=None,
    )
    from penpal.store.repository import insert_search_doc
    insert_search_doc(doc)

    if not plain:
        console.print("[green]Saved.[/green]")
    else:
        print("saved")

@main.command(help=T("查看某天活动时间线。", "Show activity timeline for a day."))
@click.option("--date", default=None, help=DATE_HELP_TEXT)
@click.option("--today", is_flag=True, default=False, help=T("显示今天的时间线", "Show today's timeline"))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def timeline(date, today, plain):
    """Timeline."""
    cfg = get_config()
    require_db(cfg)

    # Determine which date to use
    if date is None and today:
        date_str = None  # Will default to today
    elif date:
        date_str = _parse_date(date)
    else:
        date_str = None  # Default to today

    rows = get_timeline_rows(date_str)

    if plain:
        for row in rows:
            print(f"{row['start']}\t{row['end']}\t{row['app']}\t{row['title']}\t{row['duration']}")
    else:
        if not rows:
            console.print("[yellow]No activities found.[/yellow]")
            return

        table = Table(title="Activity Timeline", show_header=True, header_style="bold cyan")
        table.add_column("Start")
        table.add_column("End")
        table.add_column("App")
        table.add_column("Title")
        table.add_column("Duration")

        for row in rows:
            table.add_row(
                row["start"],
                row["end"],
                row["app"],
                row["title"],
                row["duration"],
            )
        console.print(table)

@main.command(help=T("查看最近活动条目。", "Show recent activity items."))
@click.option("--type", "item_type", default=None, type=click.Choice(["clipboard", "manual", "session"]),
              help=T("按类型过滤", "Filter by type"))
@click.option("--limit", default=20, help=T("显示条目数", "Number of items to show"))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def recent(item_type, limit, plain):
    """Recent items."""
    cfg = get_config()
    require_db(cfg)

    items = []

    if item_type is None or item_type == "clipboard":
        items.extend([(item, "clipboard") for item in recent_clipboard(limit)])
    if item_type is None or item_type == "manual":
        items.extend([(item, "manual") for item in recent_manual(limit)])
    if item_type is None or item_type == "session":
        items.extend([(item, "session") for item in recent_sessions_docs(limit)])

    # Sort by created_at descending
    items.sort(key=lambda x: x[0].get("created_at") or x[0].get("started_at", ""), reverse=True)
    items = items[:limit]

    if plain:
        for item, item_type_val in items:
            title = item.get("title") or item.get("app_name") or "—"
            body = item.get("body", "")[:50] if item.get("body") else ""
            ts = item.get("created_at") or item.get("started_at", "—")
            print(f"{ts}\t{item_type_val}\t{title}\t{body}")
    else:
        if not items:
            console.print("[yellow]No recent items found.[/yellow]")
            return

        table = Table(title="Recent Items", show_header=True, header_style="bold cyan")
        table.add_column("Time")
        table.add_column("Type")
        table.add_column("Title/App")
        table.add_column("Body")

        for item, item_type_val in items:
            title = item.get("title") or item.get("app_name") or "—"
            body = item.get("body", "")[:80] if item.get("body") else ""
            ts = item.get("created_at") or item.get("started_at", "—")

            # Format timestamp nicely
            try:
                dt = datetime.fromisoformat(ts)
                ts_fmt = dt.astimezone(local_timezone()).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_fmt = ts

            table.add_row(ts_fmt, item_type_val, title, body)

        console.print(table)

@main.command(help=T("查看活动统计。", "Show activity statistics."))
@click.option("--days", default=7, help=T("分析天数", "Number of days to analyze"))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def stats(days, plain):
    """Stats."""
    cfg = get_config()
    require_db(cfg)

    stats_data = get_stats(days)

    if plain:
        print(f"days={stats_data['days']}")
        print(f"total_sessions={stats_data['total_sessions']}")
        print(f"total_active_secs={stats_data['total_active_secs']}")
        print(f"total_active_human={stats_data['total_active_human']}")
        print(f"active_days={stats_data['active_days']}")
        print(f"clipboard_count={stats_data['clipboard_count']}")
        print(f"manual_count={stats_data['manual_count']}")
        for app in stats_data['app_distribution']:
            h = app['duration_sec'] // 3600
            m = (app['duration_sec'] % 3600) // 60
            print(f"app={app['app']},duration={h}h{m}m")
    else:
        # Summary panel
        summary_text = f"""
Total Sessions: {stats_data['total_sessions']}
Active Time: {stats_data['total_active_human']}
Active Days: {stats_data['active_days']}
Clipboard Events: {stats_data['clipboard_count']}
Manual Saves: {stats_data['manual_count']}
        """.strip()
        console.print(Panel(summary_text, title=f"Activity Stats — Last {days} days", expand=False))

        # App distribution
        if stats_data['app_distribution']:
            table = Table(title="Top Apps by Duration", show_header=True, header_style="bold cyan")
            table.add_column("App")
            table.add_column("Duration")
            for app in stats_data['app_distribution']:
                h = app['duration_sec'] // 3600
                m = (app['duration_sec'] % 3600) // 60
                table.add_row(app['app'], f"{h}h {m}m")
            console.print(table)

@main.command(
    name="search",
    help=T(
        "搜索历史活动。QUERY 按空格拆成关键词，不做语义理解：请直接写关键词，不要写整句。"
        "例如 penpal search \"签证 visa 材料\"。AI 客户端（MCP）则由客户端自己提取关键词，见 search_context 工具说明。",
        "Search historical activity. QUERY is split on whitespace into keywords; there is no language "
        "understanding, so give keywords, not a sentence. Example: penpal search \"visa checklist\". "
        "AI hosts (MCP) extract the keywords themselves, see the search_context tool description.",
    ),
)
@click.argument("query")
@click.option("--app", default=None, help=T("按应用名过滤", "Filter by app name"))
@click.option("--since", default=None, help=T("时间过滤（7d、24h 或 YYYY-MM-DD）", "Time filter (7d, 24h, or YYYY-MM-DD)"))
@click.option("--source", default=None, help=T("按来源过滤（clipboard、manual、session）", "Filter by source (clipboard, manual, session)"))
@click.option("--limit", default=50, help=T("结果数量", "Number of results"))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def search_cmd(query, app, since, source, limit, plain):
    """Search."""
    cfg = get_config()
    require_db(cfg)

    backend = resolve_search_backend()
    try:
        results = backend.search(query, app_name=app, since=since, source=source, limit=limit)
    except sqlite3.Error as exc:
        raise click.ClickException(T("搜索失败：数据库查询出错。", "Search failed: database query error.")) from exc

    if plain:
        for result in results:
            body = result.get("body", "")[:80] if result.get("body") else ""
            title = result.get("title", "")[:80] if result.get("title") else ""
            ts = result.get("created_at", "—")
            ref_type = result.get("ref_type", "—")
            app_name = result.get("app_name", "—")
            print(f"{ts}\t{ref_type}\t{app_name}\t{title}\t{body}")
    else:
        if not results:
            console.print("[yellow]No results found.[/yellow]")
            return

        table = Table(title=f"Search Results for '{query}'", show_header=True, header_style="bold cyan")
        table.add_column("Time")
        table.add_column("Type")
        table.add_column("App")
        table.add_column("Title/Body")

        for result in results:
            body = result.get("body", "")[:80] if result.get("body") else ""
            title = result.get("title", "")[:80] if result.get("title") else ""
            content = body or title or "—"
            ts = result.get("created_at", "—")

            # Format timestamp
            try:
                dt = datetime.fromisoformat(ts)
                ts_fmt = dt.astimezone(local_timezone()).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_fmt = ts

            ref_type = result.get("ref_type", "—")
            app_name = result.get("app_name", "—")

            table.add_row(ts_fmt, ref_type, app_name, content)

        console.print(table)

@main.group(help=T("管理会话。", "Manage sessions."))
def session():
    """Session group."""
    pass

@session.command("list", help=T("列出会话。", "List sessions."))
@click.option("--date", default=None, help=DATE_HELP_TEXT)
@click.option("--limit", default=100, help=T("显示会话数", "Number of sessions to show"))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def session_list(date, limit, plain):
    """List sessions."""
    cfg = get_config()
    require_db(cfg)

    parsed_date = _parse_date(date) if date else None
    sessions = get_sessions(date_str=parsed_date, limit=limit)

    if plain:
        for s in sessions:
            session_id = s["id"][:8] if s["id"] else "—"
            start = s.get("started_at", "—")
            end = s.get("ended_at", "—")
            app = s.get("app_name", "—")
            title = (s.get("primary_window_title") or "")[:60]
            duration = s.get("duration_sec", 0)
            print(f"{session_id}\t{start}\t{end}\t{app}\t{title}\t{duration}")
    else:
        if not sessions:
            console.print("[yellow]No sessions found.[/yellow]")
            return

        table = Table(title="Sessions", show_header=True, header_style="bold cyan")
        table.add_column("ID")
        table.add_column("Start")
        table.add_column("End")
        table.add_column("App")
        table.add_column("Title")
        table.add_column("Duration")

        for s in sessions:
            session_id = s["id"][:8] if s["id"] else "—"
            start = s.get("started_at", "—")
            end = s.get("ended_at", "—")
            app = s.get("app_name", "—")
            title = (s.get("primary_window_title") or "")[:60]
            duration = f"{s.get('duration_sec', 0)}s"

            # Format timestamps
            try:
                start_dt = datetime.fromisoformat(start)
                start = start_dt.astimezone(local_timezone()).strftime("%H:%M:%S")
            except Exception as exc:
                logger.debug("session list failed: %s", type(exc).__name__)
                pass

            try:
                end_dt = datetime.fromisoformat(end)
                end = end_dt.astimezone(local_timezone()).strftime("%H:%M:%S")
            except Exception as exc:
                logger.debug("session list failed: %s", type(exc).__name__)
                pass

            table.add_row(session_id, start, end, app, title, duration)

        console.print(table)

@session.command("show", help=T("查看单个会话详情。", "Show details for one session."))
@click.argument("session_id")
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def session_show(session_id, plain):
    """Show session."""
    cfg = get_config()
    require_db(cfg)

    session_data = get_session_by_id(session_id)
    if not session_data:
        err_console.print(f"[red]Session not found: {session_id}[/red]")
        sys.exit(1)

    if plain:
        for key, value in session_data.items():
            print(f"{key}={value}")
    else:
        table = Table(show_header=False, box=None)
        for key, value in session_data.items():
            table.add_row(str(key), str(value))
        console.print(table)
