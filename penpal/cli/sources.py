from __future__ import annotations

import re
from pathlib import Path

import click

from penpal.i18n import T
from penpal.sources.recipes.catalog import load_combined_catalog, load_recipe, load_recipe_from_string
from penpal.sources.recipes.identity_gate import rollback_recipe, save_recipe
from penpal.utils.paths import get_data_dir

from . import main
from penpal.sources.discover import sources_group

main.add_command(sources_group)
main.add_command(sources_group, name="source")  # issue #28 spelling: `penpal source disable NAME`

_RECIPE_ID_RE = re.compile(r"[a-z0-9_-]+")


def _proposal_path(recipe_id: str) -> Path | None:
    """The pending proposal for a recipe id, or None (candidate ids of `sources discover` use other shapes)."""
    if not _RECIPE_ID_RE.fullmatch(recipe_id):
        return None
    path = get_data_dir() / "catalog" / f"{recipe_id}.proposed"
    return path if path.is_file() else None


@sources_group.command("propose")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def propose_command(file: Path) -> None:
    """Validate and enable a recipe from FILE."""
    try:
        summary: dict = {}
        save_recipe(file.read_text(encoding="utf-8"), summary=summary)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{summary['id']}\t{summary['name']}\t{summary['matched_files']} files\t{summary['parsed_turns']} turns")


@sources_group.command("recipes")
def recipes_command() -> None:
    """List user recipes and their state."""
    for tool in load_combined_catalog():
        if tool.user_recipe:
            click.echo(f"{tool.id}\t{tool.name}\t{'enabled' if tool.enabled else 'disabled'}")
    directory = get_data_dir() / "catalog"
    if directory.is_dir():
        for path in sorted(directory.glob("*.proposed")):
            try:
                recipe = load_recipe(path)
            except (OSError, ValueError):
                continue
            if path.stem != recipe.id:
                continue
            click.echo(f"{recipe.id}\t{recipe.name}\t{T('待批准', 'pending')}")


_previous_approve = sources_group.commands["approve"]
_previous_reject = sources_group.commands["reject"]


@sources_group.command("approve", help=T("批准并启用待审核的配方。", "Approve and enable a pending recipe proposal."))
@click.argument("id")
@click.option("--yes", is_flag=True, default=False, help=T("跳过确认直接启用", "Enable without confirmation prompt"))
@click.option("--note", default="", help="Optional approval note")
@click.pass_context
def approve_command(ctx: click.Context, id: str, yes: bool, note: str) -> None:
    """Approve and enable a pending recipe proposal or discovered source."""
    proposed = _proposal_path(id)
    if proposed is None:
        # Not a pending recipe: fall back to approving a discovered source candidate.
        ctx.invoke(_previous_approve, candidate_id=id, note=note)
        return
    try:
        content = proposed.read_text(encoding="utf-8")
        recipe = load_recipe_from_string(content, origin=str(proposed))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if recipe.id != id:
        raise click.ClickException(T("配方 ID 不匹配", "Proposal id mismatch"))
    click.echo(T(f"配方 ID：{recipe.id}", f"Recipe ID: {recipe.id}"))
    click.echo(T(f"名称：{recipe.name}", f"Name: {recipe.name}"))
    click.echo(T(f"提案文件：{proposed}", f"Proposal file: {proposed}"))
    click.echo(T("将读取的路径：", "Paths/globs to read:"))
    for history in recipe.history:
        click.echo(f"  - {history.glob}")
    if not yes and not click.confirm(T("确认批准并启用该配方？", "Approve and enable this recipe?"), default=False):
        return
    try:
        save_recipe(content)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    proposed.unlink(missing_ok=True)
    click.echo(f"{recipe.id}\tenabled")


@sources_group.command("reject", help=T("拒绝并删除待审核的配方。", "Reject and delete a pending recipe proposal."))
@click.argument("id")
@click.option("--reason", default="user_choice", help="Reject reason")
@click.pass_context
def reject_command(ctx: click.Context, id: str, reason: str) -> None:
    """Reject and delete a pending recipe proposal or discovered source."""
    proposed = _proposal_path(id)
    if proposed is None:
        ctx.invoke(_previous_reject, candidate_id=id, reason=reason)
        return
    try:
        proposed.unlink()
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{id}\trejected")


@sources_group.command("rollback")
@click.argument("id")
def rollback_command(id: str) -> None:
    """Restore and enable the previous validated recipe."""
    try:
        tool = rollback_recipe(id)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{tool.id}\tenabled")


_previous_disable = sources_group.commands["disable"]


@sources_group.command("disable")
@click.argument("name")
@click.pass_context
def disable_command(ctx: click.Context, name: str) -> None:
    """Disable a user recipe or an existing source."""
    tool = next((t for t in load_combined_catalog() if t.user_recipe and t.id == name), None)
    if tool is None:
        ctx.invoke(_previous_disable, name=name)
        return
    directory = get_data_dir() / "catalog"
    (directory / (tool.id + ".disabled")).touch(mode=0o600)
    click.echo(f"{tool.id}\tdisabled")
