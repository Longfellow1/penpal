"""Declarative recipe execution engine for AI tool history sync."""
from __future__ import annotations

import glob
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


from penpal.foundation.project import normalize_project
from penpal.foundation.sqlite_rules import is_blocked_sqlite
from penpal.sources.recipes.catalog import HistoryRecipe, ToolRecipe, expand_vars_and_tilde
from penpal.sources.types import MAX_INTENT_CHARS, SemanticEvent
from penpal.utils.logging import get_logger

LOGGER = get_logger("sources.recipes.engine")


def get_dot_path(obj: Any, dot_path: str) -> Any:
    """Traverse dot path in dictionary/list structure."""
    if not dot_path:
        return None
    current = obj
    for part in dot_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.isdigit():
            idx = int(part)
            if 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        else:
            return None
        if current is None:
            return None
    return current


_TEXT_PART_TYPES = {"text", "input_text", "output_text"}


def extract_content(raw: Any) -> str:
    """Extract text content from string or list of text parts."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type in _TEXT_PART_TYPES and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item_type is None and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "\n".join(parts)
    if raw is not None:
        return str(raw)
    return ""


def parse_timestamp(val: Any, mode: str = "auto") -> datetime | None:
    """Parse timestamp into timezone-aware UTC datetime."""
    if val is None:
        return None
    if mode == "unix_s" or (mode == "auto" and isinstance(val, (int, float)) and val < 1e11):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if mode == "unix_ms" or (mode == "auto" and isinstance(val, (int, float)) and val >= 1e11):
        try:
            return datetime.fromtimestamp(float(val) / 1000.0, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        if mode == "unix_s":
            try:
                return datetime.fromtimestamp(float(val), tz=timezone.utc)
            except (ValueError, OSError):
                return None
        if mode == "unix_ms":
            try:
                return datetime.fromtimestamp(float(val) / 1000.0, tz=timezone.utc)
            except (ValueError, OSError):
                return None
        # Try ISO
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
        if mode == "auto":
            try:
                num = float(val)
                if num >= 1e11:
                    return datetime.fromtimestamp(num / 1000.0, tz=timezone.utc)
                return datetime.fromtimestamp(num, tz=timezone.utc)
            except (ValueError, OSError):
                pass
    return None


class ProbeTracker:
    """Consolidated drift probe and streaming tracker shared by files and SQLite."""

    def __init__(self, min_parse_rate: float) -> None:
        self.min_parse_rate = min_parse_rate
        self.total_records = 0
        self.recognized_records = 0
        self.failed_records = 0
        self.non_empty_content_count = 0
        self.probe_decided = False
        self.probe_buffer: list[SemanticEvent] = []
        self.drift_error: str | None = None

    def record_raw(self) -> None:
        self.total_records += 1

    def observe(
        self,
        event: SemanticEvent | None,
        is_recognized: bool,
        is_failed: bool,
        has_content: bool,
    ) -> Iterator[SemanticEvent]:
        if is_recognized:
            self.recognized_records += 1
        if is_failed:
            self.failed_records += 1
        if has_content:
            self.non_empty_content_count += 1

        if not self.probe_decided:
            if self.recognized_records >= 20:
                valid_rate = (self.recognized_records - self.failed_records) / self.recognized_records
                if valid_rate < self.min_parse_rate:
                    self.drift_error = f"format_drift: {valid_rate:.2f}"
                    self.probe_buffer.clear()
                    return

            if self.recognized_records >= 50:
                if self.non_empty_content_count == 0:
                    self.drift_error = "format_drift: no_content"
                    self.probe_buffer.clear()
                    return
                self.probe_decided = True
                yield from self.probe_buffer
                self.probe_buffer.clear()

        if event is not None:
            if self.probe_decided:
                yield event
            elif self.drift_error is None:
                self.probe_buffer.append(event)

    def finish(self) -> Iterator[SemanticEvent]:
        if self.drift_error is not None:
            return

        if not self.probe_decided:
            if self.total_records >= 50 and self.recognized_records == 0:
                self.drift_error = "format_drift: 0.00"
                self.probe_buffer.clear()
                return

            if self.recognized_records >= 50 and self.non_empty_content_count == 0:
                self.drift_error = "format_drift: no_content"
                self.probe_buffer.clear()
                return

            if self.recognized_records >= 20:
                valid_rate = (self.recognized_records - self.failed_records) / self.recognized_records
                if valid_rate < self.min_parse_rate:
                    self.drift_error = f"format_drift: {valid_rate:.2f}"
                    self.probe_buffer.clear()
                    return

            self.probe_decided = True
            yield from self.probe_buffer
            self.probe_buffer.clear()

        if self.recognized_records >= 20:
            final_rate = (self.recognized_records - self.failed_records) / self.recognized_records
            if final_rate < self.min_parse_rate:
                self.drift_error = f"format_drift: {final_rate:.2f}"
                return


def get_base_dir_from_glob(pattern: str, env: Mapping[str, str] | None = None) -> Path:
    """Extract static directory root before first wildcard character."""
    expanded = expand_vars_and_tilde(pattern, env)
    wildcards = {"*", "?", "["}
    parts = Path(expanded).parts
    static_parts: list[str] = []
    for part in parts:
        if any(w in part for w in wildcards):
            break
        static_parts.append(part)
    if not static_parts:
        return Path(".")
    return Path(*static_parts)


def resolve_history_files(
    recipe: HistoryRecipe,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Find files matching recipe.glob (and .zst variants when compression=auto)."""
    expanded = expand_vars_and_tilde(recipe.glob, env)
    patterns = [expanded]
    if recipe.compression == "auto" and not expanded.endswith(".zst"):
        patterns.append(expanded + ".zst")

    matched_paths: set[Path] = set()
    for pat in patterns:
        for p in glob.glob(pat, recursive=True):
            file_path = Path(p)
            if file_path.is_file():
                matched_paths.add(file_path.resolve())

    return sorted(matched_paths)


@lru_cache(maxsize=1)
def zstd_available() -> bool:
    """zstandard is imported on first use: without the wheel only compressed files are skipped."""
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


class RecipeEngine:
    """Executes history recipes with cursor tracking, drift probes, and streaming output."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env
        self.last_cursor: dict[str, Any] | None = None
        self.last_error: str | None = None

    def run_recipe(
        self,
        tool: ToolRecipe,
        history: HistoryRecipe,
        root_path: Path,
        source_id: int,
        initial_cursor: dict[str, Any],
    ) -> tuple[list[SemanticEvent], dict[str, Any] | None, str | None]:
        """Convenience wrapper that streams and collects events into a list."""
        events: list[SemanticEvent] = []
        for event in self.stream_recipe(tool, history, root_path, source_id, initial_cursor):
            events.append(event)
        return events, self.last_cursor, self.last_error

    def stream_recipe(
        self,
        tool: ToolRecipe,
        history: HistoryRecipe,
        root_path: Path,
        source_id: int,
        initial_cursor: dict[str, Any],
    ) -> Iterator[SemanticEvent]:
        """Stream events with bounded buffering during format drift probe window."""
        self.last_cursor = None
        self.last_error = None

        if history.format == "sqlite":
            yield from self._stream_sqlite(tool, history, root_path, source_id, initial_cursor)
        else:
            yield from self._stream_files(tool, history, root_path, source_id, initial_cursor)

    # ── File-based formats (jsonl, json) ──────────────────────────────────────

    def _stream_files(
        self,
        tool: ToolRecipe,
        history: HistoryRecipe,
        root_path: Path,
        source_id: int,
        initial_cursor: dict[str, Any],
    ) -> Iterator[SemanticEvent]:
        files_state: dict[str, dict[str, Any]] = dict(initial_cursor.get("files") or {})
        matched_files = resolve_history_files(history, self._env)
        root_resolved = root_path.resolve()
        files = [f for f in matched_files if f == root_resolved or f.is_relative_to(root_resolved)]

        tracker = ProbeTracker(history.min_parse_rate)

        for file_path in files:
            rel = (
                file_path.relative_to(root_resolved).as_posix()
                if file_path != root_resolved
                else file_path.name
            )
            try:
                stat = file_path.stat()
            except OSError:
                continue

            prior = dict(files_state.get(rel) or {})
            is_zst = file_path.name.endswith(".zst")
            if is_zst and not zstd_available():
                # Cursor left as is, so the file is read once the dependency is installed.
                self.last_error = "zstd_unavailable"
                continue
            use_record_index = history.cursor == "record_index" or is_zst

            if use_record_index:
                prior_size = prior.get("size")
                prior_mtime = prior.get("mtime")
                prior_inode = prior.get("inode")
                rec_idx = int(prior.get("record_index", 0))

                if stat.st_ino != prior_inode or stat.st_size < int(prior_size or 0):
                    rec_idx = 0
                elif stat.st_size == prior_size and stat.st_mtime == prior_mtime:
                    continue  # unchanged

                state = {
                    "record_index": rec_idx,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "inode": stat.st_ino,
                }
            else:
                prior_inode = prior.get("inode")
                prior_offset = int(prior.get("offset", 0))
                line_no = int(prior.get("line", 0))

                if stat.st_ino != prior_inode or stat.st_size < prior_offset:
                    prior_offset, line_no = 0, 0
                elif stat.st_size == prior_offset:
                    continue  # unchanged

                state = {
                    "offset": prior_offset,
                    "line": line_no,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "inode": stat.st_ino,
                }

            # Scan items in this file
            file_items = self._read_file_items(history, file_path, state, use_record_index)
            prior_rec_idx = int(state.get("record_index", 0)) if use_record_index else 0
            max_consumed_line = int(state.get("line", 0))

            for rec_idx, line_idx, record in file_items:
                max_consumed_line = max(max_consumed_line, line_idx or (rec_idx + 1))
                if use_record_index and rec_idx < prior_rec_idx:
                    continue

                tracker.record_raw()
                event, is_recognized, is_failed, has_content = self._evaluate_record(
                    tool, history, record, rel, root_resolved, file_path, line_idx or (rec_idx + 1)
                )

                if event is not None and use_record_index:
                    state["record_index"] = rec_idx + 1

                yield from tracker.observe(event, is_recognized, is_failed, has_content)
                if tracker.drift_error is not None:
                    self.last_error = tracker.drift_error
                    return

            if use_record_index:
                consumed = int(state.get("line", 0)) or max_consumed_line
                state["record_index"] = max(prior_rec_idx, consumed)
            files_state[rel] = state

        yield from tracker.finish()
        if tracker.drift_error is not None:
            self.last_error = tracker.drift_error
            self.last_cursor = None
            return

        self.last_cursor = {"files": files_state}

    def _read_file_items(
        self,
        history: HistoryRecipe,
        file_path: Path,
        state: dict[str, Any],
        use_record_index: bool,
    ) -> Iterator[tuple[int, int, dict[str, Any]]]:
        if history.format == "jsonl":
            yield from self._stream_jsonl(file_path, state, use_record_index)
        elif history.format == "json":
            yield from self._load_json(file_path, history.records_path, state)

    def _stream_jsonl(
        self,
        file_path: Path,
        state: dict[str, Any],
        use_record_index: bool,
    ) -> Iterator[tuple[int, int, dict[str, Any]]]:
        is_zst = file_path.name.endswith(".zst")
        offset = int(state.get("offset", 0))
        line_no = int(state.get("line", 0))
        rec_idx = 0

        if is_zst:
            import zstandard

            dctx = zstandard.ZstdDecompressor()
            with file_path.open("rb") as raw_f:
                with dctx.stream_reader(raw_f) as z_reader:
                    buf = io.BufferedReader(z_reader)
                    for raw_line in buf:
                        line_no += 1
                        current_rec = rec_idx
                        rec_idx += 1
                        state["line"] = line_no
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        try:
                            item = json.loads(stripped.decode("utf-8", errors="replace"))
                        except Exception:
                            item = None
                        if isinstance(item, dict):
                            yield (current_rec, line_no, item)
        else:
            with file_path.open("rb") as handle:
                handle.seek(offset)
                for raw_line in handle:
                    complete = raw_line.endswith(b"\n")
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    item = None
                    if text:
                        try:
                            item = json.loads(text)
                        except Exception:
                            item = None
                    if not complete and item is None:
                        break
                    offset += len(raw_line)
                    line_no += 1
                    current_rec = rec_idx
                    rec_idx += 1
                    state["offset"] = offset
                    state["line"] = line_no
                    if isinstance(item, dict):
                        yield (current_rec, line_no, item)

    def _load_json(
        self,
        file_path: Path,
        records_path: str,
        state: dict[str, Any],
    ) -> Iterator[tuple[int, int, dict[str, Any]]]:
        is_zst = file_path.name.endswith(".zst")
        if is_zst:
            import zstandard

            dctx = zstandard.ZstdDecompressor()
            with file_path.open("rb") as raw_f:
                with dctx.stream_reader(raw_f) as z_reader:
                    text = z_reader.read().decode("utf-8", errors="replace")
        else:
            text = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            data = json.loads(text)
        except Exception:
            return

        array_obj = get_dot_path(data, records_path) if records_path else data
        if not isinstance(array_obj, list):
            return

        state["line"] = len(array_obj)
        for idx, item in enumerate(array_obj):
            if isinstance(item, dict):
                yield (idx, 0, item)

    # ── SQLite format ────────────────────────────────────────────────────────

    def _stream_sqlite(
        self,
        tool: ToolRecipe,
        history: HistoryRecipe,
        root_path: Path,
        source_id: int,
        initial_cursor: dict[str, Any],
    ) -> Iterator[SemanticEvent]:
        blocked, reason = is_blocked_sqlite(root_path)
        if blocked:
            LOGGER.warning("Refusing blocked SQLite database %s: %s", root_path, reason)
            self.last_error = f"blocked_sqlite: {reason}"
            return

        watermark_col = ""
        if history.cursor.startswith("sqlite_watermark:"):
            watermark_col = history.cursor.split(":", 1)[1].strip()

        # Defect 8: check if DB and WAL are unchanged since cursor to avoid unnecessary copies
        wal_file = root_path.with_name(root_path.name + "-wal")
        try:
            db_stat = root_path.stat()
            wal_size = wal_file.stat().st_size if wal_file.is_file() else 0
            wal_mtime = wal_file.stat().st_mtime if wal_file.is_file() else 0
        except OSError:
            return

        prior_db_size = initial_cursor.get("db_size")
        prior_db_mtime = initial_cursor.get("db_mtime")
        prior_wal_size = initial_cursor.get("wal_size")
        prior_wal_mtime = initial_cursor.get("wal_mtime")

        if (
            prior_db_size == db_stat.st_size
            and prior_db_mtime == db_stat.st_mtime
            and prior_wal_size == wal_size
            and prior_wal_mtime == wal_mtime
        ):
            # Unchanged
            self.last_cursor = initial_cursor
            return

        prior_watermark = initial_cursor.get("watermark")
        rows, cols = self._query_sqlite_auto(root_path, history.query, watermark_col, prior_watermark)
        if self.last_error in {"schema_mismatch", "sqlite_locked", "read_error"}:
            return

        rel = root_path.name
        last_watermark = prior_watermark
        tracker = ProbeTracker(history.min_parse_rate)

        for idx, row_dict in enumerate(rows):
            tracker.record_raw()
            if watermark_col and watermark_col in row_dict:
                last_watermark = row_dict[watermark_col]

            event, is_recognized, is_failed, has_content = self._evaluate_record(
                tool, history, row_dict, rel, root_path.parent, root_path, idx + 1
            )
            yield from tracker.observe(event, is_recognized, is_failed, has_content)
            if tracker.drift_error is not None:
                self.last_error = tracker.drift_error
                return

        yield from tracker.finish()
        if tracker.drift_error is not None:
            self.last_error = tracker.drift_error
            self.last_cursor = None
            return

        new_cursor = dict(initial_cursor)
        new_cursor.update(
            db_size=db_stat.st_size,
            db_mtime=db_stat.st_mtime,
            wal_size=wal_size,
            wal_mtime=wal_mtime,
        )
        if watermark_col and last_watermark is not None:
            new_cursor["watermark"] = last_watermark

        self.last_cursor = new_cursor

    def count_sqlite_conversations(
        self,
        db_path: Path,
        history: HistoryRecipe,
    ) -> int:
        """Public helper to count distinct conversations without loading full content rows."""
        clean_q = history.query.strip().rstrip(";")
        conv_col = history.fields.get("conversation_id")
        if conv_col:
            count_q = f'SELECT COUNT(DISTINCT _q."{conv_col}") FROM ({clean_q}) AS _q'
        else:
            count_q = f'SELECT COUNT(*) FROM ({clean_q}) AS _q'

        rows, _ = self._query_sqlite_auto(db_path, count_q, "", None)
        if rows:
            val = list(rows[0].values())[0]
            return int(val or 0)
        return 0

    def _query_sqlite_auto(
        self,
        db_path: Path,
        query: str,
        watermark_col: str,
        prior_watermark: Any,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        clean_q = query.strip().rstrip(";")
        params: list[Any] = []
        if watermark_col:
            if prior_watermark is not None:
                wrapped_q = f'SELECT * FROM ({clean_q}) AS _q WHERE _q."{watermark_col}" > ? ORDER BY _q."{watermark_col}" ASC'
                params.append(prior_watermark)
            else:
                wrapped_q = f'SELECT * FROM ({clean_q}) AS _q ORDER BY _q."{watermark_col}" ASC'
        else:
            wrapped_q = clean_q

        wal_file = db_path.with_name(db_path.name + "-wal")
        shm_file = db_path.with_name(db_path.name + "-shm")
        is_wal = (wal_file.is_file() and wal_file.stat().st_size > 0) or shm_file.is_file()

        uri = f"file:{db_path.resolve()}?mode=ro" if is_wal else f"file:{db_path.resolve()}?mode=ro&immutable=1"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            if is_wal:
                conn.execute("PRAGMA query_only=1;")
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(wrapped_q, params)
            rows = [dict(r) for r in cursor.fetchall()]
            cols = [col[0] for col in cursor.description] if cursor.description else []
            conn.close()
            return rows, cols
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "no such column" in msg:
                LOGGER.info("Schema mismatch on %s: %s", db_path, exc)
                self.last_error = "schema_mismatch"
                return [], []
            if "database is locked" in msg or "unable to open" in msg or "locked" in msg:
                try:
                    db_size = db_path.stat().st_size
                except OSError:
                    db_size = 0
                if db_size > 512 * 1024 * 1024:
                    LOGGER.warning("SQLite db %s locked and size (%d bytes) > 512MB, skipping snapshot", db_path, db_size)
                    self.last_error = "sqlite_locked"
                    return [], []
                LOGGER.info("Read-only open locked on %s (%s); copying to temp snapshot", db_path, exc)
                try:
                    return self._query_sqlite_snapshot(db_path, wrapped_q, params)
                except sqlite3.OperationalError as snap_exc:
                    snap_msg = str(snap_exc).lower()
                    if "no such table" in snap_msg or "no such column" in snap_msg:
                        self.last_error = "schema_mismatch"
                        return [], []
                    self.last_error = "sqlite_locked"
                    return [], []
            LOGGER.warning("SQLite OperationalError on %s: %s", db_path, exc)
            self.last_error = "read_error"
            return [], []

    def _query_sqlite_snapshot(
        self,
        db_path: Path,
        wrapped_q: str,
        params: list[Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_db = Path(tmp_dir) / db_path.name
            shutil.copy2(db_path, temp_db)
            wal = db_path.with_name(db_path.name + "-wal")
            if wal.exists():
                shutil.copy2(wal, Path(tmp_dir) / wal.name)
            shm = db_path.with_name(db_path.name + "-shm")
            if shm.exists():
                shutil.copy2(shm, Path(tmp_dir) / shm.name)

            conn = sqlite3.connect(f"file:{temp_db.resolve()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(wrapped_q, params)
            rows = [dict(r) for r in cursor.fetchall()]
            cols = [col[0] for col in cursor.description] if cursor.description else []
            conn.close()
            return rows, cols

    # ── Record evaluation & mapping ──────────────────────────────────────────

    def _evaluate_record(
        self,
        tool: ToolRecipe,
        history: HistoryRecipe,
        record: dict[str, Any],
        rel_path: str,
        root: Path,
        file_path: Path,
        record_idx_or_line: int,
    ) -> tuple[SemanticEvent | None, bool, bool, bool]:
        """Evaluate record for drift statistics and return (event_or_None, is_recognized, is_failed, has_content)."""
        fields = history.fields

        # Role
        role_raw = get_dot_path(record, fields.get("role", ""))
        if role_raw is None:
            return None, False, False, False

        role_str = str(role_raw)
        role = history.role_map.get(role_str, role_str)
        if role not in history.keep_roles:
            return None, False, False, False

        # Recognized record!
        is_recognized = True

        # Timestamp
        if history.timestamp == "file_mtime":
            try:
                event_time = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                event_time = datetime.now(timezone.utc)
        else:
            created_path = fields.get("created_at")
            created_raw = get_dot_path(record, created_path) if created_path else None
            event_time = parse_timestamp(created_raw, history.timestamp)
            if event_time is None:
                # Failed: created_at missing or unparseable
                return None, is_recognized, True, False

        # Content: missing or empty is a normal skip, not a failure
        content_path = fields.get("content")
        content_val = get_dot_path(record, content_path) if content_path else None
        if content_val is None:
            return None, is_recognized, False, False

        content = extract_content(content_val).strip()
        if not content:
            return None, is_recognized, False, False

        metadata: dict[str, Any] = {
            "tool": tool.id,
            "role": role,
        }

        original_length = len(content)
        if original_length > 20000:
            content = content[:20000]
            metadata["truncated"] = original_length

        if "workspace" in fields and fields["workspace"]:
            ws_val = get_dot_path(record, fields["workspace"])
            if ws_val is not None and not isinstance(ws_val, (dict, list)):
                metadata["workspace"] = str(ws_val)
                # Same keys as the Claude Code / Codex plugins, so project rules and labels apply.
                metadata["cwd"] = str(ws_val)
                project = normalize_project(str(ws_val))
                if project:
                    metadata["project"] = project

        conv_path = fields.get("conversation_id")
        conv_raw = get_dot_path(record, conv_path) if conv_path else None
        if conv_raw is not None and str(conv_raw).strip() and not isinstance(conv_raw, (dict, list)):
            conversation_id = str(conv_raw).strip()
        else:
            conversation_id = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()
        metadata["conversation_id"] = conversation_id

        # Message ID & external ID
        if history.format == "sqlite":
            msg_raw = record.get(fields["message_id"])
            if msg_raw is None or not str(msg_raw).strip():
                return None, is_recognized, True, False
            message_id = str(msg_raw).strip()
            external_id = f"{conversation_id}:{message_id}"
        else:
            msg_path = fields.get("message_id")
            msg_raw = get_dot_path(record, msg_path) if msg_path else None
            if msg_raw is not None and str(msg_raw).strip() and not isinstance(msg_raw, (dict, list)):
                message_id = str(msg_raw).strip()
                external_id = f"{conversation_id}:{message_id}"
            else:
                message_id = str(record_idx_or_line)
                external_id = hashlib.sha1(f"{rel_path}:{record_idx_or_line}".encode("utf-8")).hexdigest()

        event = SemanticEvent(
            time=event_time,
            source="ai_tools",
            actor=role,
            intent=content[:MAX_INTENT_CHARS],
            artifact=f"{tool.id}:session:{conversation_id}:msg:{message_id}",
            raw_ref=f"{tool.id}:{rel_path}:{record_idx_or_line}",
            privacy_tier="green",
            metadata=metadata,
            external_id=external_id,
            source_locator=str(root.resolve()),
            evidence_locator=str(file_path.resolve()),
            evidence_line=record_idx_or_line,
            kind="message",
        )
        return event, is_recognized, False, True
