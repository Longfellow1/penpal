"""CLI commands for Penpal connect and disconnect (issue #173)."""
from __future__ import annotations

import click
from rich.table import Table

from penpal.cli import main
from penpal.cli._shared import console
from penpal.mcp.connect import auto_connect, disconnect_all, disconnect_tool, list_tools


@main.command("connect", help="自动接入已安装的 AI 工具或查看接入状态。")
@click.option("--auto", is_flag=True, default=False, help="自动检测已安装的 AI 工具并写入 MCP 配置及技能文件")
@click.option("--list", "list_mode", is_flag=True, default=False, help="列出各工具接入状态")
@click.option("--dry-run", is_flag=True, default=False, help="演练模式，不写任何文件")
def connect_cmd(auto: bool, list_mode: bool, dry_run: bool) -> None:
    """Connect AI tools to Penpal MCP or list statuses."""
    if not auto and not list_mode:
        console.print("请指定 --auto 自动接入，或 --list 查看接入状态。")
        console.print("用法: penpal connect --auto [--dry-run] | penpal connect --list")
        return

    if list_mode:
        statuses = list_tools()
        table = Table(title="Penpal AI 工具 MCP 接入状态", show_header=True, header_style="bold cyan")
        table.add_column("工具 ID", style="bold")
        table.add_column("名称")
        table.add_column("状态")
        table.add_column("说明")

        status_colors = {
            "已接入": "green",
            "未安装": "dim",
            "不支持 MCP": "yellow",
            "解析失败": "red",
            "未接入": "cyan",
        }

        for s in statuses:
            color = status_colors.get(s.state, "white")
            table.add_row(s.tool_id, s.tool_name, f"[{color}]{s.state}[/{color}]", s.detail)

        console.print(table)
        return

    if auto:
        results = auto_connect(dry_run=dry_run)
        if not results:
            console.print("未检测到任何已安装的 AI 工具。")
            return

        for r in results:
            if r.status == "connected":
                console.print(f"[green]✓[/green] 已接入 {r.tool_name} ({r.tool_id}) -> {r.config_path}")
            elif r.status == "already_connected":
                console.print(f"[green]✓[/green] {r.tool_name} ({r.tool_id}): 已接入（配置未变）")
            elif r.status == "dry_run":
                console.print(f"[cyan][演练][/cyan] {r.tool_name} ({r.tool_id}) -> {r.config_path}")
            elif r.status == "manual":
                console.print(f"[yellow]·[/yellow] {r.tool_name} ({r.tool_id}): {r.message}")
            elif r.status == "parse_error":
                console.print(f"[red]✗[/red] {r.tool_name} ({r.tool_id}): {r.message}")
            elif r.status == "unsupported":
                console.print(f"[dim]· {r.tool_name} ({r.tool_id}): {r.message}[/dim]")


@main.command("disconnect", help="撤销 AI 工具的 Penpal MCP 接入并恢复原配置。")
@click.argument("tool", required=False, default=None)
@click.option("--all", "all_tools", is_flag=True, default=False, help="断开所有已接入的工具")
def disconnect_cmd(tool: str | None, all_tools: bool) -> None:
    """Disconnect AI tools from Penpal MCP."""
    if not tool and not all_tools:
        raise click.ClickException("请指定要断开的工具 ID（例如: penpal disconnect claude_code），或使用 --all 断开全部。")

    if all_tools:
        results = disconnect_all()
        if not results:
            console.print("当前无任何已接入的工具。")
            return
        for r in results:
            console.print(f"[green]✓[/green] {r.message}")
        return

    if tool:
        result = disconnect_tool(tool)
        if result.status == "not_connected":
            console.print(f"[yellow]·[/yellow] {result.message}")
        else:
            console.print(f"[green]✓[/green] {result.message}")
