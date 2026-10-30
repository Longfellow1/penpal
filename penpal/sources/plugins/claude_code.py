from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from penpal.foundation.project import normalize_project
from penpal.foundation.transcript_noise import clean_message_part
from penpal.sources.cleaning.noise import (
    advance_echo,
    is_penpal_call,
    resolve_server_names,
)
from penpal.sources.types import MAX_INTENT_CHARS, DataSource, DataSourceInstance, SemanticEvent

_SKIP_CONTENT_TYPES = {"tool_use", "tool_result", "thinking", "image"}


class ClaudeCodeSource(DataSource):
    name = "claude_code"
    privacy_tier = "green"
    liveness = "always"
    fast_poll = True
    description = "Claude Code session JSONL reader"

    def __init__(
        self,
        *,
        projects_root: Path | None = None,
        self_exclude_dirs: set[str] | frozenset[str] | None = None,
        use_cursor: bool = False,
        penpal_server_names: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        # MCP server name(s) Penpal is registered under; None = cleaning.toml / "penpal".
        self._penpal_server_names = penpal_server_names
        # use_cursor=True: incremental sync driven by a persistent per-file
        # cursor in `sync_cursors` (needs an initialised DB). The since/until
        # window is ignored in that mode; resume position replaces it.
        self._use_cursor = use_cursor
        self._pending_cursors: dict[int, dict[str, Any]] = {}
        self._projects_root = (projects_root or (Path.home() / ".claude" / "projects")).expanduser()
        # No self-exclusion by default: Penpal's own dev sessions are tagged via `via_penpal` (D27).
        self._self_exclude_dirs = frozenset(self_exclude_dirs or ())

    def discover(self) -> list[DataSourceInstance]:
        if not self._projects_root.exists() or not self._projects_root.is_dir():
            return []

        instances: list[DataSourceInstance] = []
        for project_dir in sorted(self._projects_root.iterdir()):
            if not project_dir.is_dir():
                continue
            if project_dir.name in self._self_exclude_dirs:
                continue
            session_files = list(project_dir.glob("*.jsonl"))
            metadata = {
                "project_path": _decode_project_path(project_dir.name),
                "project": normalize_project(project_dir),
                "session_count": len(session_files),
            }
            instances.append(
                DataSourceInstance(
                    plugin=self.name,
                    locator=str(project_dir.resolve()),
                    label=_decode_project_label(project_dir.name),
                    metadata=metadata,
                )
            )
        return instances

    def read(
        self,
        instance: DataSourceInstance,
        since: datetime,
        until: datetime,
    ) -> Iterator[SemanticEvent]:
        project_dir = Path(instance.locator).expanduser()
        if not project_dir.exists() or not project_dir.is_dir():
            return iter(())
        if project_dir.name in self._self_exclude_dirs:
            return iter(())
        if self._use_cursor:
            return self._read_incremental(project_dir)

        def _iter_events() -> Iterator[SemanticEvent]:
            names = resolve_server_names(self._penpal_server_names)
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                session_id_default = jsonl_file.stem
                echo = False
                try:
                    handle = jsonl_file.open("r", encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with handle:
                    for line_idx, raw_line in enumerate(handle, start=1):
                        row = _parse_json_line(raw_line)
                        if row is None:
                            continue
                        row_type = row.get("type")
                        if row_type not in {"user", "assistant"}:
                            continue
                        event_time = _parse_iso8601(row.get("timestamp"))
                        if event_time is None:
                            continue
                        intent_text = _extract_message_text(row).strip()
                        # State advances for every row, also those outside the window.
                        marked, echo = _echo_state(row, str(row_type), bool(intent_text), echo, names)
                        if event_time < since or event_time > until:
                            continue
                        if not intent_text:
                            continue

                        session_id = str(row.get("sessionId") or session_id_default)
                        message_id = str(row.get("uuid") or row.get("messageId") or line_idx)
                        yield SemanticEvent(
                            time=event_time,
                            source=self.name,
                            actor=str(row_type),
                            intent=intent_text[:MAX_INTENT_CHARS],
                            artifact=f"claude:session:{session_id}:msg:{message_id}",
                            raw_ref=(
                                f"claude:projects:{project_dir.name}:{jsonl_file.name}:{line_idx}"
                            ),
                            privacy_tier=self.privacy_tier,
                            metadata=_row_metadata(session_id, row, project_dir, marked),
                        )

        return _iter_events()

    # ── incremental (cursor) mode ────────────────────────────────────────────

    def _read_incremental(self, project_dir: Path) -> Iterator[SemanticEvent]:
        from penpal.store.sync_repo import ensure_source, get_cursor

        locator = str(project_dir.resolve())
        source_id = ensure_source(self.name, locator)
        files_state: dict[str, dict[str, Any]] = dict(get_cursor(source_id).get("files") or {})
        # Live view: the importer commits this between batches, so the saved position is the last event it
        # stored (issue #32). Updated before every yield below, never ahead of the stream.
        self._pending_cursors[source_id] = {"files": files_state}

        def _iter() -> Iterator[SemanticEvent]:
            names = resolve_server_names(self._penpal_server_names)
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                try:
                    stat = jsonl_file.stat()
                    handle = jsonl_file.open("rb")
                except OSError:
                    continue
                prior = files_state.get(jsonl_file.name) or {}
                offset = int(prior.get("offset", 0))
                line_no = int(prior.get("line", 0))
                echo = bool(prior.get("echo"))
                if stat.st_size < offset:  # truncated / rewritten: start over
                    offset, line_no, echo = 0, 0, False
                if stat.st_size == offset:
                    handle.close()
                    continue
                with handle:
                    handle.seek(offset)
                    for raw in handle:
                        complete = raw.endswith(b"\n")
                        text = raw.decode("utf-8", errors="replace")
                        row = _parse_json_line(text)
                        if not complete and row is None:
                            break  # partial trailing write: re-read next sync
                        offset += len(raw)
                        line_no += 1
                        if row is None:
                            continue
                        event, echo = self._row_to_event(row, project_dir, jsonl_file, line_no, echo, names)
                        if event is not None:
                            files_state[jsonl_file.name] = {
                                "offset": offset,
                                "line": line_no,
                                "echo": echo,
                                "mtime": stat.st_mtime,
                                "size": stat.st_size,
                            }
                            yield event
                files_state[jsonl_file.name] = {
                    "offset": offset,
                    "line": line_no,
                    "echo": echo,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }

        return _iter()

    def commit_cursors(self, *, failed: bool = False) -> None:
        """Persist resume positions once the registry or importer has stored the events.

        Entries stay pending: the importer commits after every batch, and each batch needs the live view.
        """
        from penpal.store.sync_repo import record_sync_error, save_cursor

        for source_id, cursor in list(self._pending_cursors.items()):
            if failed:
                record_sync_error(source_id, "persist failed; cursor not advanced")
            else:
                save_cursor(source_id, cursor)

    def _row_to_event(
        self,
        row: dict[str, Any],
        project_dir: Path,
        jsonl_file: Path,
        line_idx: int,
        echo: bool = False,
        names: tuple[str, ...] = ("penpal",),
    ) -> tuple[SemanticEvent | None, bool]:
        """Returns ``(event_or_None, new_echo_state)``; the state advances on every user/assistant row."""
        row_type = row.get("type")
        if row_type not in {"user", "assistant"}:
            return None, echo
        event_time = _parse_iso8601(row.get("timestamp"))
        if event_time is None:
            return None, echo
        intent_text = _extract_message_text(row).strip()
        marked, echo = _echo_state(row, str(row_type), bool(intent_text), echo, names)
        if not intent_text:
            return None, echo
        session_id = str(row.get("sessionId") or jsonl_file.stem)
        uuid = row.get("uuid") or row.get("messageId")
        message_id = str(uuid or line_idx)
        return SemanticEvent(
            time=event_time,
            source=self.name,
            actor=str(row_type),
            intent=intent_text[:MAX_INTENT_CHARS],
            artifact=f"claude:session:{session_id}:msg:{message_id}",
            raw_ref=f"claude:projects:{project_dir.name}:{jsonl_file.name}:{line_idx}",
            privacy_tier=self.privacy_tier,
            metadata=_row_metadata(session_id, row, project_dir, marked),
            external_id=f"{session_id}:{uuid}" if uuid else f"{jsonl_file.name}:L{line_idx}",
            source_locator=str(project_dir.resolve()),
            evidence_locator=str(jsonl_file.resolve()),
            evidence_line=line_idx,
            kind="message",
        ), echo


def _row_metadata(session_id: str, row: dict[str, Any], project_dir: Path, via_penpal: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "parent_uuid": row.get("parentUuid"),
        "project_dir": str(project_dir.resolve()),
        # The real folder name from the row's cwd; the encoded projects dir name is lossy ("life-admin" -> "life", "admin").
        "project": normalize_project(row.get("cwd")) or normalize_project(project_dir),
    }
    if via_penpal:
        metadata["via_penpal"] = True
    return metadata


def _echo_state(row: dict[str, Any], role: str, has_text: bool, echo: bool, names: tuple[str, ...]) -> tuple[bool, bool]:
    calls = False
    if role == "assistant":
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            calls = any(
                isinstance(p, dict) and p.get("type") == "tool_use" and is_penpal_call(p, names) for p in content
            )
    return advance_echo(echo, role, has_text, calls)


def _parse_json_line(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_iso8601(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    value = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_message_text(row: dict[str, Any]) -> str:
    message = row.get("message")
    text = _coerce_text(message)
    if text:
        return text
    return _coerce_text(row.get("content"))


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        parsed = _try_literal_eval(stripped)
        if parsed is not None:
            nested = _coerce_text(parsed)
            if nested:
                return nested
        return clean_message_part(stripped)
    if isinstance(value, dict):
        if "content" in value:
            nested = _coerce_text(value.get("content"))
            if nested:
                return nested
        text_value = value.get("text")
        if isinstance(text_value, str):
            return clean_message_part(text_value)
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    cleaned = clean_message_part(item["text"])
                    if cleaned:
                        parts.append(cleaned)
                    continue
                if item_type in _SKIP_CONTENT_TYPES:
                    continue
            nested = _coerce_text(item)
            if nested:
                parts.append(nested)
        return "\n".join(part for part in parts if part).strip()
    return str(value).strip()


def _try_literal_eval(value: str) -> object | None:
    if not value or value[0] not in "[{(":
        return None
    try:
        parsed = ast.literal_eval(value)
    except Exception:
        return None
    if isinstance(parsed, (dict, list, str)):
        return parsed
    return None


def _decode_project_path(encoded: str) -> str:
    if not encoded:
        return ""
    tokens = [token for token in encoded.split("-") if token]
    if not tokens:
        return ""
    return "/" + "/".join(tokens)


def _decode_project_label(encoded: str) -> str:
    path = _decode_project_path(encoded)
    if not path:
        return encoded or "unknown"
    parts = [part for part in path.split("/") if part]
    return parts[-1] if parts else "unknown"
