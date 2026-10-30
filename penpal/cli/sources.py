from __future__ import annotations

from pathlib import Path

import click

from penpal.sources.recipes.catalog import load_combined_catalog
from penpal.sources.recipes.identity_gate import rollback_recipe, save_recipe
from penpal.utils.paths import get_data_dir

from . import main
from penpal.sources.discover import sources_group

main.add_command(sources_group)
main.add_command(sources_group, name="source")  # issue #28 spelling: `penpal source disable NAME`


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
