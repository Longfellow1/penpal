"""Inspect and delete locally stored memories."""
import re

import click

from penpal.cli import main
from penpal.cli._shared import get_config, require_db
from penpal.i18n import T
from penpal.mcp import audit
from penpal.store import memories


@main.group(name="memory", help=T("查看和删除记忆。", "List and delete memories."))
def memory_group():
    pass


@memory_group.command(name="list", help=T("查看记忆。", "List memories."))
@click.option("--source", type=click.Choice(["host", "memmy"]), help=T("记忆来源", "Memory source"))
@click.option("--tool", help=T("来源工具", "Source tool"))
@click.option("--status", type=click.Choice(["active", "all"]), default="active", show_default=True,
              help=T("记忆状态", "Memory status"))
@click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True,
              help=T("最多显示条数", "Maximum rows to show"))
def list_command(source, tool, status, limit):
    require_db(get_config())
    rows, total = memories.list_memories(source=source, tool=tool, status=status, limit=limit)
    for row in rows:
        scope = f"project:{row['project']}" if row["scope"] == "project" else row["scope"]
        content = " ".join(row["content"].split())[:80]
        click.echo(f"mem_{row['id']} | {row['status']} | {row['type']} | {scope} | "
                   f"{row['source_tool']} | {row['created_at'][:10]} | {content}")
    click.echo(f"{T('总数', 'Total')}: {total}")


@memory_group.command(name="delete", help=T("删除指定记忆及其旧版本。", "Delete memories and their old versions."))
@click.argument("memory_ids", nargs=-1)
@click.option("--source", type=click.Choice(["host", "memmy"]), help=T("整批删除此来源", "Delete this source"))
@click.option("--dry-run", is_flag=True, help=T("只统计，不删除", "Count without deleting"))
@click.option("--yes", is_flag=True, help=T("跳过确认", "Skip confirmation"))
def delete_command(memory_ids, source, dry_run, yes):
    if bool(memory_ids) == bool(source):
        raise click.UsageError(T("请指定记忆 ID 或 --source，二选一。", "Specify memory IDs or --source, not both."))
    if any(not re.fullmatch(r"mem_[0-9]{1,18}", value) for value in memory_ids):
        raise click.UsageError(T("记忆 ID 格式应为 mem_<数字>。", "Memory IDs must have the form mem_<number>."))
    require_db(get_config())
    ids = [int(value[4:]) for value in memory_ids]
    count = memories.delete_memories(ids=ids, source=source, dry_run=True)
    if dry_run:
        click.echo(f"{T('将删除', 'Would delete')}: {count}")
        return
    if not yes and not click.confirm(T(f"删除 {count} 条记忆？", f"Delete {count} memories?")):
        return
    count = memories.delete_memories(ids=ids, source=source)
    audit.record(client="cli", tool="delete_memory", allowed=True, reason_code="ok", result_count=count)
    click.echo(f"{T('已删除', 'Deleted')}: {count}")
