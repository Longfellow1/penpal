"""Migration v31: evidence rows for old codex_cli history rows (Codex's flat ``history.jsonl``).

Rows ingested from ``~/.codex/history.jsonl`` before the locator fix have raw_ref ``codex:history:<N>``
(N = 1-based line in that file) and no sources locator. Migration v28 needs an absolute sources
locator for them, so it left them without evidence.

This pass uses the history file path shared by CodexCliSource and ``penpal.utils.paths``. New history
ingest writes the same locator string and line.

  * A row gets evidence only when it has no evidence yet, no absolute sources locator, a well-formed
    N >= 1, and the history file exists now. Otherwise nothing is written for it.
  * The locator is path.resolve() of the history file, the same string new ingest writes.
  * The `codex:sessions:` rows are never touched (different raw_ref prefix).
  * Idempotent: only rows without any evidence are considered; run_migrations wraps this in one
    transaction together with the version stamp.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone

from penpal.utils.paths import get_codex_home

LOGGER = logging.getLogger(__name__)
_BATCH = 2000
_SOURCE = "codex_cli"
_PREFIX = "codex:history:"


def _line_number(raw_ref: str) -> int | None:
    """N from ``codex:history:<N>``; None when N is not a plain positive integer."""
    tail = raw_ref[len(_PREFIX):]
    if tail.isascii() and tail.isdigit() and 0 < len(tail) < 19 and int(tail) >= 1:
        return int(tail)
    return None


def migrate_v31(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    history = (get_codex_home() / "history.jsonl").expanduser()
    if not history.is_file():
        LOGGER.info("migration v31 codex history evidence backfill: %s not found, nothing written", history)
        return {}
    locator = str(history.resolve())
    now = datetime.now(timezone.utc).isoformat()
    created = skipped = 0
    last = 0
    while True:
        rows = conn.execute(
            "SELECT e.id, e.content_text, json_extract(e.metadata_json, '$.raw_ref') FROM raw_events e "
            "LEFT JOIN sources s ON s.id = e.source_id "
            "WHERE e.event_type = 'semantic_event' AND e.source = ? AND e.id > ? "
            "AND CASE WHEN json_valid(e.metadata_json) THEN json_extract(e.metadata_json, '$.raw_ref') END LIKE ? "
            "AND COALESCE(s.locator, '') NOT LIKE '/%' "
            "AND NOT EXISTS (SELECT 1 FROM evidence ev WHERE ev.event_id = e.id) "
            "ORDER BY e.id LIMIT ?",
            (_SOURCE, last, _PREFIX + "%", _BATCH),
        ).fetchall()
        if not rows:
            break
        inserts = []
        for rid, text, raw_ref in rows:
            line = _line_number(raw_ref or "")
            if line is None:
                skipped += 1
                continue
            digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
            inserts.append((rid, locator, line, digest, 1, now))
            created += 1
        if inserts:
            conn.executemany(
                "INSERT INTO evidence(event_id, locator, offset_or_line, content_hash, available, created_at) "
                "VALUES (?,?,?,?,?,?)",
                inserts,
            )
        last = rows[-1][0]
    LOGGER.info("migration v31 codex history evidence backfill: %d created, %d left without evidence",
                created, skipped)
    return {_SOURCE: (created, skipped)} if created or skipped else {}
