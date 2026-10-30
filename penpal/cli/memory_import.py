"""Import distilled memories from an explicitly selected Memmy database."""
from pathlib import Path
import sqlite3

import click

from penpal.cli import main
from penpal.cli._shared import get_config, require_db
from penpal.memory.memmy_import import import_memmy
from penpal.store.db import get_conn


@main.group(name="import")
def import_group():
    """Import memories from another application."""


@import_group.command(name="memmy")
@click.option("--db", type=click.Path(path_type=Path), default=lambda: Path.home() / ".memmy/memory-service/memory.sqlite")
@click.option("--dry-run", is_flag=True)
def memmy(db, dry_run):
    """Import activated Memmy memories without changing the source."""
    cfg = get_config()
    target = None
    try:
        if db.resolve() == cfg.db_path_expanded.resolve():
            raise ValueError("Memmy source and Penpal destination must be different databases")
        if dry_run:
            path = cfg.db_path_expanded
            if path.is_file():
                target = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        else:
            require_db(cfg)
            target = get_conn()
        counts = import_memmy(db, target, dry_run=dry_run)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        if dry_run and target is not None:
            target.close()
    for key, value in counts.items():
        click.echo(f"{key}: {value}")
