from __future__ import annotations
import json
import click
from rich.table import Table
from penpal.store.db import get_conn
from penpal.store.repository import get_all_policies, insert_policy
from penpal.store.models import Policy
from penpal.utils.paths import get_config_path
from penpal.i18n import T
from . import main
from ._shared import PLAIN_HELP, console, get_config, require_db


@main.group(help=T("管理隐私策略。", "Manage privacy policies."))
def rules():
    """Rules group."""
    pass

@main.group("privacy", help=T("管理隐私开关和网站排除名单。", "Manage privacy switches and blocked websites."))
def privacy_group():
    """Privacy group."""

def _set_url_deny_host(host: str, blocked: bool) -> None:
    from penpal.config import set_url_deny_host

    try:
        key, _hosts = set_url_deny_host(host, blocked)
    except ValueError as exc:
        raise click.ClickException(T(f"无法识别网站地址 {host!r}。", f"Could not parse website address {host!r}.")) from exc
    if blocked:
        message = T(
            f"已屏蔽网站 {key}（含其子域名）；新事件约 1 分钟内生效。",
            f"Blocked {key} and its subdomains; the change applies to new events within about a minute.",
        )
    else:
        message = T(
            f"已取消屏蔽网站 {key}（含其子域名）；新事件约 1 分钟内生效。",
            f"Unblocked {key} and its subdomains; the change applies to new events within about a minute.",
        )
    console.print(message, markup=False)

@privacy_group.command("block-site", help=T("屏蔽网站（同时覆盖它的子域名）。", "Block a website, including its subdomains."))
@click.argument("host")
def privacy_block_site(host):
    """Block a website host."""
    _set_url_deny_host(host, True)

@privacy_group.command("unblock-site", help=T("取消屏蔽网站及其子域名。", "Unblock a website and its subdomains."))
@click.argument("host")
def privacy_unblock_site(host):
    """Unblock a website host."""
    _set_url_deny_host(host, False)

@privacy_group.command("blocked-sites", help=T("列出当前屏蔽的网站。", "List currently blocked websites."))
def privacy_blocked_sites():
    """List blocked website hosts."""
    hosts = get_config().privacy.url_deny_hosts
    if not hosts:
        console.print(T("没有屏蔽的网站。", "No blocked websites."), markup=False)
        return
    console.print(T("已屏蔽的网站：", "Blocked websites:"), markup=False)
    for host in hosts:
        print(host)

@privacy_group.group("chat-apps", help=T("聊天 App 默认不采集；可逐个放行（放行后归入 Private 桶）。", "Chat apps are not captured by default; opt individual apps in (they land in the Private bucket)."))
def privacy_chat_apps():
    """Chat app opt-in."""

def _chat_app_key_or_error(name: str) -> str:
    from penpal.foundation.chat_apps import normalize_opt_ins

    try:
        return normalize_opt_ins([name])[0]
    except ValueError:
        from penpal.foundation.chat_apps import CHAT_APPS

        valid = ", ".join(app.key for app in CHAT_APPS)
        raise click.ClickException(f"Unknown chat app {name!r}. Valid names: {valid}")

def _set_chat_app_opt_in(name: str, allowed: bool) -> list[str]:
    from penpal.config import set_chat_app_opt_in

    _chat_app_key_or_error(name)
    key, current = set_chat_app_opt_in(name, allowed)
    verb = "allowed" if allowed else "denied"
    console.print(f"[green]{key} {verb}.[/green] Config: {get_config_path()}")
    if allowed:
        console.print("Note: an allowed chat app is captured into the Private bucket.")
    console.print(T("配置约 1 分钟内生效。", "The config change takes effect within about a minute."), markup=False)
    return current

@privacy_chat_apps.command("list", help=T("列出所有已知聊天 App 及是否放行。", "List known chat apps and whether each is allowed."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def privacy_chat_apps_list(plain):
    """List chat apps."""
    from penpal.foundation.chat_apps import CHAT_APPS

    allowed = set(get_config().privacy.chat_apps_opt_in)
    if plain:
        for app in CHAT_APPS:
            print(f"{app.key}\t{'allowed' if app.key in allowed else 'denied'}")
        return
    table = Table(title="Chat apps", show_header=True, header_style="bold cyan")
    table.add_column("App")
    table.add_column("Status")
    for app in CHAT_APPS:
        table.add_row(app.key, "allowed (Private bucket)" if app.key in allowed else "denied (not captured)")
    console.print(table)

@privacy_chat_apps.command("allow", help=T("放行某个聊天 App（数据归入 Private 桶）。", "Allow a chat app (its data lands in the Private bucket)."))
@click.argument("name")
def privacy_chat_apps_allow(name):
    """Allow a chat app."""
    _set_chat_app_opt_in(name, True)

@privacy_chat_apps.command("deny", help=T("取消放行某个聊天 App（恢复默认不采集）。", "Deny a chat app again (back to the default: not captured)."))
@click.argument("name")
def privacy_chat_apps_deny(name):
    """Deny a chat app."""
    _set_chat_app_opt_in(name, False)

@rules.command("list", help=T("列出全部隐私策略。", "List all privacy policies."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def rules_list(plain):
    """List rules."""
    cfg = get_config()
    require_db(cfg)

    policies = get_all_policies()

    if plain:
        for p in policies:
            print(f"{p['id']}\t{p['scope_type']}\t{p['scope_value']}\t{p['mode']}\t{p['priority']}")
    else:
        if not policies:
            console.print("[yellow]No policies configured.[/yellow]")
            return

        table = Table(title="Privacy Policies", show_header=True, header_style="bold cyan")
        table.add_column("ID")
        table.add_column("Scope Type")
        table.add_column("Scope Value")
        table.add_column("Mode")
        table.add_column("Priority")

        for p in policies:
            table.add_row(
                str(p.get("id", "—")),
                p.get("scope_type", "—"),
                p.get("scope_value", "—"),
                p.get("mode", "—"),
                str(p.get("priority", "—")),
            )
        console.print(table)

@rules.command("add", help=T("新增隐私策略或分桶规则。", "Add a privacy policy or a bucket rule."))
@click.option("--scope-type", "--scope", required=True, help=T("范围类型（app、window、source、content；分桶规则支持 app / source / project）", "Scope type (app, window, source, content; bucket rules also support project)"))
@click.option("--scope-value", "--value", required=True, help=T("范围值（例如 'Safari'、'password'；project 使用项目目录名）", "Scope value (e.g., 'Safari', 'password'; project uses the project folder name)"))
@click.option("--mode", default=None, help=T("模式（allow、deny、metadata-only、redact、truncate）", "Mode (allow, deny, metadata-only, redact, truncate)"))
@click.option("--bucket", default=None, help=T("分桶规则：把该 App / 来源 / 项目的数据归入此桶（Work、Personal、Private、Financial）", "Bucket rule: file data from this app/source/project into a bucket (Work, Personal, Private, Financial)"))
@click.option("--priority", type=int, default=100, help=T("优先级（值越小优先级越高）", "Priority (lower = higher priority)"))
def rules_add(scope_type, scope_value, mode, bucket, priority):
    """Add rule."""
    cfg = get_config()
    require_db(cfg)

    config_json = None
    if bucket:
        from penpal.store.buckets import BUCKET_MODE, normalize_bucket

        if mode:
            raise click.UsageError("--bucket and --mode are mutually exclusive")
        if scope_type not in ("app", "source", "project"):
            raise click.UsageError("bucket rules support --scope-type app, source, or project")
        if scope_type == "project":
            from penpal.foundation.project import normalize_project

            scope_value = normalize_project(scope_value)
            if not scope_value:
                raise click.UsageError("project bucket rules require a project folder name")
        try:
            config_json = json.dumps({"bucket": normalize_bucket(bucket)})
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        mode = BUCKET_MODE
    elif not mode:
        raise click.UsageError("one of --mode or --bucket is required")
    elif scope_type == "project":
        raise click.UsageError("project scope is only supported for bucket rules")

    policy = Policy(
        scope_type=scope_type,
        scope_value=scope_value,
        mode=mode,
        priority=priority,
        config_json=config_json,
    )

    policy_id = insert_policy(policy)
    console.print(f"[green]Policy added (ID: {policy_id})[/green]")


@rules.command("projects", help=T("列出已导入的项目、来源、事件数和当前分桶；不显示内容。", "List imported projects, sources, event counts, and current buckets without showing content."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def rules_projects(plain):
    """List known project keys from event metadata, without reading event content."""
    from collections import Counter

    from penpal.foundation.project import fold_project_rows, project_from_metadata_json

    cfg = get_config()
    require_db(cfg)
    counts = Counter()
    for row in get_conn().execute(
        "SELECT source, bucket, metadata_json FROM raw_events WHERE metadata_json IS NOT NULL"
    ):
        project = project_from_metadata_json(row["metadata_json"])
        if project:
            counts[(row["source"], project, row["bucket"] or "—")] += 1

    rows = fold_project_rows(counts)
    if plain:
        for (source, project, bucket), count in rows:
            print(f"{source}\t{project}\t{count}\t{bucket}")
        return
    if not rows:
        console.print(T("还没有带项目标记的事件。", "No events with project metadata yet."), markup=False)
        return

    table = Table(title=T("已知项目", "Known projects"), show_header=True, header_style="bold cyan")
    table.add_column(T("来源", "Source"))
    table.add_column(T("项目目录", "Project folder"))
    table.add_column(T("事件数", "Events"), justify="right")
    table.add_column(T("当前桶", "Current bucket"))
    for (source, project, bucket), count in rows:
        table.add_row(source, project, str(count), bucket)
    console.print(table)

@rules.command("rebucket", help=T("按当前分桶规则重新计算已有数据的桶（规则变更不会自动回溯）。", "Re-evaluate buckets of stored events with the current rules (rule changes are not retroactive otherwise)."))
def rules_rebucket():
    """Reapply bucket rules to existing rows."""
    cfg = get_config()
    require_db(cfg)
    from penpal.store.buckets import reapply_buckets

    changed = reapply_buckets(get_conn())
    console.print(f"[green]Re-bucketed {changed} event(s).[/green]")

@rules.command("disable", help=T("禁用某条隐私策略。", "Disable a privacy policy."))
@click.argument("rule_id", type=int)
def rules_disable(rule_id):
    """Disable rule."""
    cfg = get_config()
    require_db(cfg)

    from penpal.store.db import get_conn
    conn = get_conn()
    conn.execute("UPDATE policies SET enabled=0 WHERE id=?", (rule_id,))
    conn.commit()

    console.print(f"[green]Policy {rule_id} disabled.[/green]")
