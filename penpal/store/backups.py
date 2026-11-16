"""Pre-migration database backups: creation, listing and pruning."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Matches the timestamp written by write_pre_migration_backup and its shorter historical forms.
_STAMP = r"\d{8}(?:T\d{6}(?:\d{6})?)?Z?"


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file)
    return None


def _backup_pattern(db_path: Path) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(db_path.name)}\.v\d+-(?P<stamp>{_STAMP})\.bak$")


def write_pre_migration_backup(conn: sqlite3.Connection, current_version: int) -> Path | None:
    """Copy an existing non-empty DB next to itself before schema changes (mode 0600)."""
    path = _db_file(conn)
    if path is None or not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.v{current_version}-{stamp}.bak")
    # Create the file owner-only first so the snapshot is never readable by others, even briefly.
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    dst = sqlite3.connect(str(target))
    try:
        # sqlite3 online backup API: consistent even with WAL contents pending.
        conn.backup(dst)
    finally:
        dst.close()
    LOGGER.info("db backup written before migration: %s", target.name)
    return target


def list_backups(db_path: Path) -> list[Path]:
    """Backups of ``db_path`` in the same directory, newest timestamp first."""
    path = Path(db_path)
    pattern = _backup_pattern(path)
    if not path.parent.is_dir():
        return []
    found = []
    for candidate in path.parent.iterdir():
        match = pattern.fullmatch(candidate.name)
        if candidate.is_file() and match is not None:
            found.append((match.group("stamp"), candidate.name, candidate))
    return [item[2] for item in sorted(found, key=lambda item: (item[0], item[1]), reverse=True)]


def prune_backups(db_path: Path, keep: int = 1) -> int:
    """Keep the newest ``keep`` backups and delete the rest; return the deletion count."""
    if keep < 0:
        raise ValueError("keep must be >= 0")
    doomed = list_backups(db_path)[keep:]
    for backup in doomed:
        backup.unlink()
    return len(doomed)
