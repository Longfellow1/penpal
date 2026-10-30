"""AI tools source coverage reporting CLI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import click

from penpal.config import Config
from penpal.sources.plugins.claude_code import ClaudeCodeSource
from penpal.sources.plugins.codex_cli import CodexCliSource
from penpal.sources.recipes.catalog import (
    is_tool_detected,
    load_combined_catalog,
)
from penpal.sources.recipes.engine import RecipeEngine, resolve_history_files
from penpal.store.db import get_conn, init_db
from penpal.utils.logging import get_logger

LOGGER = get_logger("sources.coverage")


# Fixed labels the engine writes; they carry no path or content, so they are shown as is.
_ENGINE_ERROR_LABELS = frozenset({"probe_error", "schema_mismatch", "sqlite_locked", "zstd_unavailable"})


def _sanitize_last_error(raw_err: str | None) -> str | None:
    """Sanitize error string so raw paths and stacktraces are never printed."""
    if not raw_err:
        return None
    raw = str(raw_err).strip()
    if raw.startswith("format_drift:"):
        rate = raw.split(":", 1)[1].strip()
        return f"format_drift {rate}"
    if raw.startswith("blocked_sqlite"):
        return "blocked_sqlite"
    if raw in _ENGINE_ERROR_LABELS:
        return raw
    return "read_error"


def compute_tool_coverage(
    catalog_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tools = load_combined_catalog(catalog_dir)
    conn = get_conn()
    engine = RecipeEngine(env=env)

    rows: list[dict[str, Any]] = []
    total_on_disk = 0
    total_ingested = 0

    for tool in tools:
        detected = is_tool_detected(tool, env=env)
        readable = tool.readable
        sessions_on_disk = 0
        ingested_sessions = 0
        last_error: str | None = None

        if tool.user_recipe:
            from penpal.sources.recipes.identity_gate import validate_binding

            try:
                validate_binding(tool, env)
                detected = True
            except (ValueError, OSError) as exc:
                detected = False
                last_error = str(exc)

        if readable and detected:
            try:
                if tool.adapter == "claude_code":
                    source = ClaudeCodeSource()
                    insts = source.discover()
                    sessions_on_disk = sum(int(i.metadata.get("session_count", 0)) for i in insts)
                elif tool.adapter == "codex_cli":
                    source = CodexCliSource()
                    insts = source.discover()
                    sessions_on_disk = sum(int(i.metadata.get("session_count", 0)) for i in insts)
                else:
                    for history in tool.history:
                        files = resolve_history_files(history, env=env)
                        if history.format == "sqlite":
                            for db_f in files:
                                sessions_on_disk += engine.count_sqlite_conversations(db_f, history)
                        else:
                            sessions_on_disk += len(files)
            except Exception as exc:
                LOGGER.warning("coverage probe failed for %s: %s", tool.id, type(exc).__name__)
                last_error = "probe_error"

        # Ingested sessions & last error from sync_cursors
        try:
            if tool.adapter == "claude_code":
                res = conn.execute(
                    "SELECT count(DISTINCT json_extract(metadata_json, '$.session_id')) FROM raw_events WHERE source = 'claude_code'"
                ).fetchone()
                ingested_sessions = int(res[0]) if res and res[0] else 0
                if last_error is None:
                    err_row = conn.execute(
                        """SELECT sync_cursors.last_error FROM sync_cursors
                           JOIN sources ON sources.id = sync_cursors.source_id
                           WHERE sources.type = 'claude_code' AND sync_cursors.last_error IS NOT NULL
                           ORDER BY sync_cursors.rowid DESC LIMIT 1"""
                    ).fetchone()
                    if err_row and err_row[0]:
                        last_error = _sanitize_last_error(str(err_row[0]))
            elif tool.adapter == "codex_cli":
                res = conn.execute(
                    "SELECT count(DISTINCT COALESCE(json_extract(metadata_json, '$.thread_id'), json_extract(metadata_json, '$.session_id'))) FROM raw_events WHERE source = 'codex_cli'"
                ).fetchone()
                ingested_sessions = int(res[0]) if res and res[0] else 0
                if last_error is None:
                    err_row = conn.execute(
                        """SELECT sync_cursors.last_error FROM sync_cursors
                           JOIN sources ON sources.id = sync_cursors.source_id
                           WHERE sources.type = 'codex_cli' AND sync_cursors.last_error IS NOT NULL
                           ORDER BY sync_cursors.rowid DESC LIMIT 1"""
                    ).fetchone()
                    if err_row and err_row[0]:
                        last_error = _sanitize_last_error(str(err_row[0]))
            else:
                res = conn.execute(
                    """SELECT count(DISTINCT json_extract(metadata_json, '$.conversation_id'))
                       FROM raw_events
                       WHERE source = 'ai_tools' AND json_extract(metadata_json, '$.tool') = ?""",
                    (tool.id,),
                ).fetchone()
                ingested_sessions = int(res[0]) if res and res[0] else 0
                if last_error is None:
                    err_row = conn.execute(
                        """SELECT sync_cursors.last_error FROM sync_cursors
                           JOIN sources ON sources.id = sync_cursors.source_id
                           WHERE sources.type = 'ai_tools' AND sources.locator LIKE ? AND sync_cursors.last_error IS NOT NULL
                           ORDER BY sync_cursors.rowid DESC LIMIT 1""",
                        (f"{tool.id}#%",),
                    ).fetchone()
                    if err_row and err_row[0]:
                        last_error = _sanitize_last_error(str(err_row[0]))
                # A recipe can carry histories for several versions of a tool (opencode 1.x and 2.x). Once
                # one of them delivers, a schema mismatch only means another version is not installed.
                if last_error == "schema_mismatch" and ingested_sessions > 0:
                    last_error = None
        except Exception as exc:
            LOGGER.warning("coverage query failed for %s: %s", tool.id, type(exc).__name__)
            if last_error is None:
                last_error = "read_error"

        total_on_disk += sessions_on_disk
        total_ingested += ingested_sessions

        rows.append(
            {
                "id": tool.id,
                "user_recipe": tool.user_recipe,
                "enabled": tool.enabled,
                "detected": detected,
                "readable": readable,
                "sessions_on_disk": sessions_on_disk,
                "ingested_sessions": ingested_sessions,
                "last_error": last_error,
            }
        )

    percentage = (total_ingested / total_on_disk * 100.0) if total_on_disk > 0 else 0.0
    totals = {
        "sessions_on_disk": total_on_disk,
        "ingested_sessions": total_ingested,
        "percentage": round(percentage, 1),
    }
    return rows, totals


@click.command("coverage")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output")
def coverage_command(as_json: bool) -> None:
    """Show session coverage across AI tools."""
    cfg = Config.load()
    init_db(cfg.db_path_expanded)

    rows, totals = compute_tool_coverage()

    if as_json:
        click.echo(json.dumps({"tools": rows, "totals": totals}, indent=2, ensure_ascii=False))
        return

    # Table output
    header = f"{'TOOL':<16} {'DETECTED':<10} {'READABLE':<10} {'ON DISK':<10} {'INGESTED':<10} {'LAST ERROR'}"
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        det_str = "yes" if r["detected"] else "no"
        read_str = "yes" if r["readable"] else "no"
        err_str = r["last_error"] or "-"
        label = r["id"] + (" (AI 生成" + (", 已停用" if not r.get("enabled", True) else "") + ")" if r.get("user_recipe", False) else "")
        click.echo(
            f"{label:<16} {det_str:<10} {read_str:<10} {r['sessions_on_disk']:<10} {r['ingested_sessions']:<10} {err_str}"
        )
    click.echo("-" * len(header))
    click.echo(
        f"Total: {totals['ingested_sessions']}/{totals['sessions_on_disk']} sessions ({totals['percentage']}%)"
    )
    click.echo("Tools marked not readable are excluded from percentage calculation.")
