from __future__ import annotations

import json
import os
import shlex
import shutil
import sys

import click

from penpal.config import Config
from penpal.foundation.runtime_identity import RELOAD_EXIT_CODE
from penpal.mcp.auth import CLIENT_ENV, TOKEN_ENV
from penpal.store.db import DatabaseSchemaTooNewError, init_db

HOSTS = ("claude", "codex", "cursor", "generic")
EGRESS_NOTICE = "提示：Penpal 返回给该 AI 的内容会进入它的对话，并可能发送到该 AI 的云端模型服务。"


@click.group(name="mcp", help="Penpal MCP server and client access.")
def mcp_group() -> None:
    pass


def _penpal_executable() -> str:
    """Absolute path of the running penpal, so the written config works outside the shell PATH."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and os.path.basename(argv0) == "penpal" and os.path.isfile(argv0):
        return os.path.abspath(argv0)
    for name in ("penpal", "keypulse"):
        beside_python = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.isfile(beside_python):
            return beside_python
    found = shutil.which("penpal") or shutil.which("keypulse")
    return os.path.abspath(found) if found else "penpal"


def _host_config_lines(host: str, exe: str, token: str, client_id: str) -> list[str]:
    from penpal.mcp.auth import CLIENT_ENV, TOKEN_ENV

    server = {"command": exe, "args": ["mcp", "serve"], "env": {TOKEN_ENV: token, CLIENT_ENV: client_id}}
    json_block = json.dumps({"mcpServers": {"penpal": server}}, indent=2)
    if host == "claude":
        return [
            "Claude Code (CLI):\n",
            f"  claude mcp add penpal -e {TOKEN_ENV}={token} -e {CLIENT_ENV}={client_id} -- {shlex.quote(exe)} mcp serve\n",
            ".mcp.json / claude_desktop_config.json snippet:\n",
            json_block,
        ]
    if host == "codex":
        # json.dumps yields TOML-compatible basic strings for these plain values.
        return [
            "Codex CLI: add this block to ~/.codex/config.toml:\n",
            "[mcp_servers.penpal]",
            f"command = {json.dumps(exe)}",
            'args = ["mcp", "serve"]',
            f"env = {{ {TOKEN_ENV} = {json.dumps(token)}, {CLIENT_ENV} = {json.dumps(client_id)} }}\n",
            "Or run this command (注意：它会把 token 记入 shell 历史；若 shell 设置了 HIST_IGNORE_SPACE，可在命令前加一个空格):\n",
            f"  codex mcp add penpal --env {TOKEN_ENV}={token} --env {CLIENT_ENV}={client_id} -- {shlex.quote(exe)} mcp serve\n",
        ]
    if host == "cursor":
        # Cursor's docs list "type": "stdio" for stdio servers (the example omits it); explicit is harmless.
        cursor_server = {"type": "stdio", **server}
        cursor_block = json.dumps({"mcpServers": {"penpal": cursor_server}}, indent=2)
        return [
            "Cursor: write this JSON to ~/.cursor/mcp.json (all projects) or .cursor/mcp.json (one project):\n",
            cursor_block,
        ]
    return ["Generic MCP client (JSON snippet):\n", json_block]


def grant_output_lines(
    client_id: str,
    scope: str,
    token: str,
    host: str,
    exe: str,
    *,
    buckets: tuple[str, ...] | list[str] = (),
    sources: tuple[str, ...] | list[str] | None = None,
    enabled_sources: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """What `penpal mcp grant` prints after creating a grant (shared with the HUD, which shows it once)."""
    from penpal.mcp.grant_notice import enabled_coding_agent_sources, work_project_notice

    lines = [f"Granted client '{client_id}' ({scope}). Token (shown once, only its hash is stored):\n", f"  {token}\n"]
    lines.extend(_host_config_lines(host, exe, token, client_id))
    lines.append("\nBoth values are required; a call without the matching client id is denied.")
    notice = work_project_notice(
        buckets, sources,
        enabled_coding_agent_sources() if enabled_sources is None else enabled_sources,
    )
    if notice:
        lines.append(notice)
    lines.append(EGRESS_NOTICE)
    lines.append("Keep the config file readable by your user only. Revoke with: penpal mcp revoke " + client_id)
    return lines


def rotate_output_lines(
    client_id: str,
    token: str,
    exe: str,
    *,
    buckets: tuple[str, ...] | list[str] = (),
    sources: tuple[str, ...] | list[str] | None = None,
    enabled_sources: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """What `penpal mcp rotate` prints (shared with the HUD, which shows it once)."""
    from penpal.mcp.grant_notice import enabled_coding_agent_sources, work_project_notice

    server = {"command": exe, "args": ["mcp", "serve"], "env": {TOKEN_ENV: token, CLIENT_ENV: client_id}}
    lines = [
        f"Rotated token for '{client_id}'. The previous token is now invalid. New token (shown once):\n",
        f"  {token}\n",
        "Claude Code (CLI):\n",
        f"  claude mcp add penpal -e {TOKEN_ENV}={token} -e {CLIENT_ENV}={client_id} -- {shlex.quote(exe)} mcp serve\n",
        ".mcp.json / claude_desktop_config.json snippet:\n",
        json.dumps({"mcpServers": {"penpal": server}}, indent=2),
    ]
    notice = work_project_notice(
        buckets, sources,
        enabled_coding_agent_sources() if enabled_sources is None else enabled_sources,
    )
    if notice:
        lines.append(notice)
    return lines


@mcp_group.command("serve", help="Run the MCP server over stdio (env PENPAL_TOKEN + PENPAL_CLIENT).")
def serve_command() -> None:
    parent_pid = os.getppid()
    from penpal.mcp import tools
    from penpal.mcp.server import serve_stdio

    cfg = Config.load()
    tools.configure(mask_emails=cfg.privacy.redact_emails)
    try:
        init_db(cfg.db_path_expanded)
    except DatabaseSchemaTooNewError as exc:
        click.echo(
            f"Database schema {exc.recorded} is newer than this version supports ({exc.supported}); refusing writes.",
            err=True,
        )
        raise click.exceptions.Exit(RELOAD_EXIT_CODE) from exc
    serve_stdio(parent_pid=parent_pid)


@mcp_group.command("grant", help="Authorize an AI client: creates a token (shown once) and prints connection config.")
@click.argument("client_id")
@click.option("--buckets", default="Work", show_default=True,
              help="Comma-separated buckets the client may read: Work,Personal,Private,Financial.")
@click.option("--sources", default=None, help="Comma-separated source names (default: every source inside the buckets).")
@click.option("--operations", default="read", show_default=True, help="Comma-separated: read[,write].")
@click.option("--purpose", default=None, help="Why this client needs access (shown in 'mcp list').")
@click.option("--expires-in-days", type=click.IntRange(min=1), default=None, help="Grant expires after N days.")
@click.option("--host", type=click.Choice(HOSTS), default="claude", show_default=True,
              help="Which AI client's connection config to print.")
def grant_command(client_id: str, buckets: str, sources: str | None, operations: str,
                  purpose: str | None, expires_in_days: int | None, host: str) -> None:
    from datetime import datetime, timedelta, timezone

    from penpal.mcp.auth import create_grant

    init_db(Config.load().db_path_expanded)
    expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days) if expires_in_days else None
    try:
        token = create_grant(
            client_id,
            buckets=buckets.split(","),
            sources=sources.split(",") if sources is not None else None,
            operations=operations.split(","),
            purpose=purpose,
            expires_at=expires_at,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    scope = f"buckets={buckets}, sources={sources or 'all in buckets'}, operations={operations}"
    for line in grant_output_lines(
        client_id, scope, token, host, _penpal_executable(),
        buckets=buckets.split(","), sources=sources.split(",") if sources is not None else None,
    ):
        click.echo(line)


@mcp_group.command("revoke", help="Revoke all active tokens of a client.")
@click.argument("client_id")
def revoke_command(client_id: str) -> None:
    from penpal.mcp.auth import revoke_grants

    init_db(Config.load().db_path_expanded)
    count = revoke_grants(client_id)
    click.echo(f"Revoked {count} grant(s) for '{client_id}'.")


@mcp_group.command("list", help="List client grants (never shows tokens).")
def list_command() -> None:
    from penpal.mcp.auth import list_grants
    from penpal.mcp.grant_notice import enabled_coding_agent_sources, work_project_notice

    init_db(Config.load().db_path_expanded)
    enabled_sources = enabled_coding_agent_sources()
    for g in list_grants():
        state = f"revoked {g['revoked_at']}" if g["revoked_at"] else "active"
        scope = f"buckets={g['buckets']} sources={g['sources'] or 'all'} ops={g['operations']}"
        expiry = f" expires {g['expires_at']}" if g["expires_at"] else ""
        click.echo(f"{g['id']}\t{g['client_id']}\t{g['created_at']}\t{state}\t{scope}{expiry}")
        if not g["revoked_at"]:
            try:
                grant_buckets = json.loads(g["buckets"] or "[]")
                grant_sources = json.loads(g["sources"]) if g["sources"] else None
            except (TypeError, ValueError):
                continue
            notice = work_project_notice(grant_buckets, grant_sources, enabled_sources)
            if notice:
                click.echo(f"  {notice}")


@mcp_group.command("rotate", help="Issue a new token for a client's grant (same scope). The old token stops working at once.")
@click.argument("client_id")
def rotate_command(client_id: str) -> None:
    from penpal.mcp.auth import active_grants, rotate_grant
    from penpal.mcp.grant_notice import enabled_coding_agent_sources

    init_db(Config.load().db_path_expanded)
    try:
        token = rotate_grant(client_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    grant = next((g for g in active_grants() if g["client_id"] == client_id), None)
    for line in rotate_output_lines(
        client_id, token, shutil.which("penpal") or "penpal",
        buckets=grant["buckets"] if grant else (), sources=grant["sources"] if grant else None,
        enabled_sources=enabled_coding_agent_sources(),
    ):
        click.echo(line)


@mcp_group.command("audit", help="Show recent MCP calls (allowed and denied). Never contains queries or tokens.")
@click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True)
def audit_command(limit: int) -> None:
    from penpal.mcp.audit import recent

    init_db(Config.load().db_path_expanded)
    for r in recent(limit):
        count = "" if r["result_count"] is None else f" results={r['result_count']}"
        click.echo(
            f"{r['ts']}\t{r['client_id']}\t{r['tool']}\t{r['decision']}\t{r['reason_code']}\t"
            f"scope={r['scope'] or '-'}{count}"
        )
