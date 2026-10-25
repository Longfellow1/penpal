"""Codex CLI history reader.

Primary input: rollout files under ``~/.codex/sessions/YYYY/MM/DD/*.jsonl`` (found
recursively). Fallback when no sessions tree exists: the legacy flat
``~/.codex/history.jsonl`` (user prompts only).

Rollout record shapes handled (everything else is tolerated and skipped):

* new  : ``{"timestamp", "ordinal", "type", "payload"}``
         - ``session_meta``  -> session id + cwd (project)
         - ``turn_context``  -> cwd update
         - ``response_item`` with ``payload.type == "message"`` -> user / assistant text
* old  : unwrapped ``{"type": "message", "role", "content"}`` records, first line a bare
         ``{"id", "timestamp", "instructions"}`` header (no ordinal -> line number is the id)

Skipped as noise: developer/system roles, reasoning, tool calls and outputs, ``event_msg``
(duplicates the response items), ``token_usage_record``, ``world_state``, ``compacted``
(summaries would duplicate content), injected context blocks in user turns
(``<environment_context>``, AGENTS.md instructions, ...).

Incremental mode (``use_cursor=True``) keeps a byte offset per rollout file in
``sync_cursors`` so a re-sync never re-parses already-ingested bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from penpal.foundation.project import normalize_project
from penpal.utils.paths import get_codex_home
from penpal.sources.plugins.claude_code import _parse_iso8601, _parse_json_line
from penpal.foundation.transcript_noise import clean_message_part
from penpal.sources.cleaning.noise import (
    advance_echo,
    is_penpal_call,
    resolve_server_names,
)
from penpal.sources.types import MAX_INTENT_CHARS, DataSource, DataSourceInstance, SemanticEvent
from penpal.utils.logging import get_logger

LOGGER = get_logger("sources.codex_cli")

# Cheap byte-level prefilter: only lines mentioning one of these can matter.
_MARKERS = (b'"message"', b'"session_meta"', b'"turn_context"')
# Tool-call records matter only for the via_penpal state, and only when they name a Penpal server.
_CALL_MARKERS = (b'"function_call"', b'"custom_tool_call"', b"mcp_tool_call")
_TEXT_PART_TYPES = {"input_text", "output_text", "text"}
_KEEP_ROLES = {"user", "assistant"}
_CALL_TYPES = {"function_call", "custom_tool_call", "mcp_tool_call", "mcp_tool_call_begin", "mcp_tool_call_end"}
_SESSION_ID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def default_codex_home() -> Path:
    """Codex's home directory (``~/.codex``). Shared with migration v31 so both read the same history file."""
    return get_codex_home()


class CodexCliSource(DataSource):
    name = "codex_cli"
    privacy_tier = "green"
    liveness = "always"
    fast_poll = True
    description = "Codex CLI session rollout reader"

    def __init__(
        self,
        *,
        sessions_root: Path | None = None,
        history_path: Path | None = None,
        use_cursor: bool = False,
        penpal_server_names: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        # MCP server name(s) Penpal is registered under; None = cleaning.toml / "penpal".
        self._penpal_server_names = penpal_server_names
        self._use_cursor = use_cursor
        self._pending_cursors: dict[int, dict[str, Any]] = {}
        codex_home = default_codex_home()
        self._sessions_root = (sessions_root or (codex_home / "sessions")).expanduser()
        self._history_path = (history_path or (codex_home / "history.jsonl")).expanduser()

    # ── discovery ────────────────────────────────────────────────────────────

    def discover(self) -> list[DataSourceInstance]:
        if self._sessions_root.is_dir():
            count = sum(1 for _ in self._sessions_root.rglob("*.jsonl"))
            return [
                DataSourceInstance(
                    plugin=self.name,
                    locator=str(self._sessions_root.resolve()),
                    label="codex sessions",
                    metadata={"session_count": count, "layout": "sessions"},
                )
            ]
        if self._history_path.is_file():
            return [
                DataSourceInstance(
                    plugin=self.name,
                    locator=str(self._history_path.resolve()),
                    label="codex history",
                    metadata={"layout": "history"},
                )
            ]
        return []

    def read(self, instance: DataSourceInstance, since: datetime, until: datetime) -> Iterator[SemanticEvent]:
        path = Path(instance.locator).expanduser()
        if path.is_dir():
            return self._read_sessions(path, since, until)
        if path.is_file():
            return self._read_history(path, since, until)
        return iter(())

    # ── rollout sessions ─────────────────────────────────────────────────────

    def _read_sessions(self, root: Path, since: datetime, until: datetime) -> Iterator[SemanticEvent]:
        root = root.resolve()
        files_state: dict[str, dict[str, Any]] = {}
        source_id = None
        if self._use_cursor:
            from penpal.store.sync_repo import ensure_source, get_cursor

            source_id = ensure_source(self.name, str(root))
            files_state = dict(get_cursor(source_id).get("files") or {})

        def _iter() -> Iterator[SemanticEvent]:
            files = sorted(root.rglob("*.jsonl"))
            LOGGER.info("codex_cli: scanning %d rollout files under %s", len(files), root)
            for n, jsonl_file in enumerate(files, start=1):
                rel = jsonl_file.relative_to(root).as_posix()
                try:
                    stat = jsonl_file.stat()
                except OSError:
                    continue
                prior = files_state.get(rel) or {} if self._use_cursor else {}
                state = dict(prior)
                if state.get("inode") != stat.st_ino or stat.st_size < int(state.get("offset", 0)):
                    state = {}  # new / rotated / truncated: start over (external_id dedups)
                if self._use_cursor and state and stat.st_size == int(state.get("offset", 0)):
                    continue
                emitted = 0
                try:
                    for event in self._scan_file(root, jsonl_file, rel, state):
                        if not self._use_cursor and (event.time < since or event.time > until):
                            continue
                        emitted += 1
                        yield event
                except Exception as exc:  # one bad file must not stop the others
                    LOGGER.warning("codex_cli: skipping %s: %s", rel, exc)
                    continue
                state.update(size=stat.st_size, mtime=stat.st_mtime, inode=stat.st_ino)
                files_state[rel] = state
                if n % 25 == 0:
                    LOGGER.info("codex_cli: %d/%d files scanned", n, len(files))
            if source_id is not None:
                self._pending_cursors[source_id] = {"files": files_state}

        return _iter()

    def _scan_file(self, root: Path, jsonl_file: Path, rel: str, state: dict[str, Any]) -> Iterator[SemanticEvent]:
        """Stream one rollout file from ``state['offset']``; mutates ``state`` as lines are consumed."""
        offset = int(state.get("offset", 0))
        line_no = int(state.get("line", 0))
        sid = state.get("sid") or _session_id_from_name(jsonl_file)
        sid_from_meta = bool(state.get("sid_meta"))
        cwd = state.get("cwd")
        start_ts = state.get("ts")
        echo = bool(state.get("echo"))
        names = resolve_server_names(self._penpal_server_names)
        name_bytes = tuple(n.encode() for n in names)
        with jsonl_file.open("rb") as handle:
            handle.seek(offset)
            for raw in handle:
                complete = raw.endswith(b"\n")
                if not complete:
                    if _parse_json_line(raw.decode("utf-8", errors="replace")) is None:
                        break  # partial trailing write: re-read next sync
                offset += len(raw)
                line_no += 1
                is_call = any(m in raw for m in _CALL_MARKERS) and any(n in raw.lower() for n in name_bytes)
                if line_no != 1 and not is_call and not any(m in raw for m in _MARKERS):
                    continue
                row = _parse_json_line(raw.decode("utf-8", errors="replace"))
                if row is None:
                    continue
                payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
                rtype = row.get("type")
                if is_call:
                    call = payload if payload is not None else row
                    if call.get("type") in _CALL_TYPES:
                        if is_penpal_call(call, names):
                            echo = True
                            state["echo"] = True
                        continue

                if rtype == "session_meta" or (rtype is None and line_no == 1 and "id" in row):
                    meta = payload if rtype == "session_meta" and payload is not None else row
                    if not sid_from_meta:  # the file's own session is the first meta; later ones are forks/parents
                        found = meta.get("session_id") or meta.get("id")
                        if isinstance(found, str) and found:
                            sid, sid_from_meta = found, True
                    if not cwd and isinstance(meta.get("cwd"), str):
                        cwd = meta["cwd"]
                    if not start_ts and isinstance(meta.get("timestamp"), str):
                        start_ts = meta["timestamp"]
                    continue
                if rtype == "turn_context":
                    if payload and isinstance(payload.get("cwd"), str) and payload["cwd"]:
                        cwd = payload["cwd"]
                    continue
                if rtype == "response_item" and payload is not None:
                    message = payload
                elif rtype == "message" and payload is None:
                    message = row  # old unwrapped shape
                else:
                    continue
                if message.get("type") not in (None, "message") or message.get("role") not in _KEEP_ROLES:
                    continue
                role = message["role"]
                text = _message_text(message.get("content"), role)
                if not text:
                    continue
                marked, echo = advance_echo(echo, role, True, False)
                when = _parse_iso8601(row.get("timestamp")) or _parse_iso8601(message.get("timestamp")) or _parse_iso8601(start_ts)
                if when is None:
                    when = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
                ordinal = row.get("ordinal")
                external_id = f"{sid}:{ordinal}" if isinstance(ordinal, int) else f"{sid}:L{line_no}"
                metadata: dict[str, Any] = {"session_id": sid}
                if marked:
                    metadata["via_penpal"] = True
                if cwd:
                    metadata["cwd"] = cwd
                    metadata["project_dir"] = cwd
                    metadata["project"] = normalize_project(cwd)
                state.update(offset=offset, line=line_no, sid=sid, sid_meta=sid_from_meta, cwd=cwd, ts=start_ts, echo=echo)
                yield SemanticEvent(
                    time=when,
                    source=self.name,
                    actor=role,
                    intent=text[:MAX_INTENT_CHARS],
                    artifact=f"codex:session:{sid}:msg:{ordinal if isinstance(ordinal, int) else 'L' + str(line_no)}",
                    raw_ref=f"codex:sessions:{rel}:{line_no}",
                    privacy_tier=self.privacy_tier,
                    metadata=metadata,
                    external_id=external_id,
                    source_locator=str(root),
                    evidence_locator=str(jsonl_file.resolve()),
                    evidence_line=line_no,
                    kind="message",
                )
        state.update(offset=offset, line=line_no, sid=sid, sid_meta=sid_from_meta, cwd=cwd, ts=start_ts, echo=echo)

    def commit_cursors(self, *, failed: bool = False) -> None:
        """Persist resume positions once the registry has stored the events."""
        from penpal.store.sync_repo import record_sync_error, save_cursor

        pending, self._pending_cursors = self._pending_cursors, {}
        for source_id, cursor in pending.items():
            if failed:
                record_sync_error(source_id, "persist failed; cursor not advanced")
            else:
                save_cursor(source_id, cursor)

    # ── legacy flat history ──────────────────────────────────────────────────

    def _read_history(self, history_path: Path, since: datetime, until: datetime) -> Iterator[SemanticEvent]:
        def _iter_events() -> Iterator[SemanticEvent]:
            try:
                handle = history_path.open("r", encoding="utf-8", errors="replace")
            except Exception:
                return
            with handle:
                for line_idx, raw_line in enumerate(handle, start=1):
                    row = _parse_json_line(raw_line)
                    if row is None:
                        continue
                    session_id = row.get("session_id")
                    ts = row.get("ts")
                    text = row.get("text")
                    if not isinstance(session_id, str) or not session_id:
                        continue
                    if not isinstance(ts, int) or not isinstance(text, str):
                        continue
                    event_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                    if event_time < since or event_time > until:
                        continue
                    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
                    yield SemanticEvent(
                        time=event_time,
                        source=self.name,
                        actor="user",
                        intent=text[:MAX_INTENT_CHARS],
                        artifact=f"codex:session:{session_id}",
                        raw_ref=f"codex:history:{line_idx}",
                        privacy_tier=self.privacy_tier,
                        metadata={"session_id": session_id},
                        external_id=f"{session_id}:{ts}:{digest}",
                        source_locator=str(history_path.resolve()),
                        evidence_locator=str(history_path.resolve()),
                        evidence_line=line_idx,
                        kind="message",
                    )

        return _iter_events()


def _session_id_from_name(path: Path) -> str:
    match = _SESSION_ID_RE.search(path.stem)
    return match.group(1) if match else path.stem


def _message_text(content: object, role: str) -> str:
    if isinstance(content, str):
        parts: list[object] = [{"type": "input_text", "text": content}]
    elif isinstance(content, list):
        parts = content
    else:
        return ""
    out: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") not in _TEXT_PART_TYPES:
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        # Automation blocks (e.g. <heartbeat>) also appear as assistant output_text, so filter both roles.
        cleaned = clean_message_part(text)
        if cleaned:
            out.append(cleaned)
    return "\n".join(out)
