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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from penpal.foundation.project import normalize_project
from penpal.utils.paths import get_codex_home
from penpal.sources.plugins.claude_code import _parse_iso8601, _parse_json_line
from penpal.sources.recipes.engine import zstd_available
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
        archived_sessions_root: Path | None = None,
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
        if archived_sessions_root is not None:
            self._archived_sessions_root = archived_sessions_root.expanduser()
        elif sessions_root is not None and (sessions_root.parent / "archived_sessions").is_dir():
            self._archived_sessions_root = (sessions_root.parent / "archived_sessions").expanduser()
        else:
            self._archived_sessions_root = (codex_home / "archived_sessions").expanduser()
        self._history_path = (history_path or (codex_home / "history.jsonl")).expanduser()

    # ── discovery ────────────────────────────────────────────────────────────

    def discover(self) -> list[DataSourceInstance]:
        if self._sessions_root.is_dir() or self._archived_sessions_root.is_dir():
            count = 0
            if self._sessions_root.is_dir():
                count += _count_rollout_files(self._sessions_root)
            if self._archived_sessions_root.is_dir():
                count += _count_rollout_files(self._archived_sessions_root)
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
            roots_to_scan: list[tuple[Path, str]] = []
            if self._sessions_root.is_dir():
                roots_to_scan.append((self._sessions_root.resolve(), ""))
            if self._archived_sessions_root.is_dir() and self._archived_sessions_root.resolve() != self._sessions_root.resolve():
                roots_to_scan.append((self._archived_sessions_root.resolve(), "archived_sessions/"))

            zstd_warned = False
            for scan_root, prefix in roots_to_scan:
                files = sorted([f for f in scan_root.rglob("*") if f.is_file() and _is_rollout_file(f)])
                LOGGER.info("codex_cli: scanning %d rollout files under %s (prefix=%s)", len(files), scan_root, prefix)
                for n, jsonl_file in enumerate(files, start=1):
                    rel = prefix + jsonl_file.relative_to(scan_root).as_posix()
                    try:
                        stat = jsonl_file.stat()
                    except OSError:
                        continue
                    is_zst = jsonl_file.name.endswith(".zst")
                    if is_zst and not zstd_available():
                        if not zstd_warned:
                            LOGGER.warning("codex_cli: zstandard is not installed; compressed sessions are skipped")
                            zstd_warned = True
                        continue
                    prior = files_state.get(rel) or {} if self._use_cursor else {}
                    state = dict(prior)
                    if is_zst:
                        if state.get("inode") != stat.st_ino or stat.st_size < int(state.get("size", 0)):
                            state = {}
                        if self._use_cursor and state.get("size") == stat.st_size and state.get("mtime") == stat.st_mtime:
                            continue
                    else:
                        if state.get("inode") != stat.st_ino or stat.st_size < int(state.get("offset", 0)):
                            state = {}  # new / rotated / truncated: start over (external_id dedups)
                        if self._use_cursor and state and stat.st_size == int(state.get("offset", 0)):
                            continue
                    emitted = 0
                    try:
                        scanner = (
                            self._scan_file_zst(root, jsonl_file, rel, state)
                            if is_zst
                            else self._scan_file(root, jsonl_file, rel, state)
                        )
                        for event in scanner:
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

    def _scan_file_zst(self, root: Path, zst_file: Path, rel: str, state: dict[str, Any]) -> Iterator[SemanticEvent]:
        import io
        import zstandard

        prior_rec_index = int(state.get("record_index", 0)) if self._use_cursor else 0
        state.update(
            sid=_session_id_from_name(zst_file),
            thread_id=_session_id_from_name(zst_file),
            conversation_id=None,
            meta_parsed=False,
            cwd=None,
            ts=None,
            echo=False,
        )
        line_no = 0
        names = resolve_server_names(self._penpal_server_names)
        name_bytes = tuple(n.encode() for n in names)

        dctx = zstandard.ZstdDecompressor()
        with zst_file.open("rb") as raw_handle:
            with dctx.stream_reader(raw_handle) as z_reader:
                buf = io.BufferedReader(z_reader)
                for raw in buf:
                    line_no += 1
                    rec_idx = line_no - 1
                    event = _process_rollout_line(
                        raw, line_no, state, root, zst_file, rel,
                        names, name_bytes, self.privacy_tier, self.name
                    )
                    state["line"] = line_no
                    if self._use_cursor and rec_idx < prior_rec_index:
                        continue
                    if event is not None:
                        state["record_index"] = rec_idx + 1
                        yield event
        state["record_index"] = max(prior_rec_index, line_no)

    def _scan_file(self, root: Path, jsonl_file: Path, rel: str, state: dict[str, Any]) -> Iterator[SemanticEvent]:
        """Stream one rollout file from ``state['offset']``; mutates ``state`` as lines are consumed."""
        offset = int(state.get("offset", 0))
        if offset == 0:
            state.update(
                sid=_session_id_from_name(jsonl_file),
                thread_id=_session_id_from_name(jsonl_file),
                conversation_id=None,
                meta_parsed=False,
                cwd=None,
                ts=None,
                echo=False,
            )
        line_no = int(state.get("line", 0))
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
                state["offset"] = offset
                state["line"] = line_no
                event = _process_rollout_line(
                    raw, line_no, state, root, jsonl_file, rel,
                    names, name_bytes, self.privacy_tier, self.name
                )
                if event is not None:
                    yield event

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


def _process_rollout_line(
    raw: bytes,
    line_no: int,
    state: dict[str, Any],
    root: Path,
    jsonl_file: Path,
    rel: str,
    names: tuple[str, ...],
    name_bytes: tuple[bytes, ...],
    privacy_tier: str,
    source_name: str,
) -> SemanticEvent | None:
    is_call = any(m in raw for m in _CALL_MARKERS) and any(n in raw.lower() for n in name_bytes)
    if line_no != 1 and not is_call and not any(m in raw for m in _MARKERS):
        return None
    row = _parse_json_line(raw.decode("utf-8", errors="replace"))
    if row is None:
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
    rtype = row.get("type")
    echo = bool(state.get("echo"))
    if is_call:
        call = payload if payload is not None else row
        if call.get("type") in _CALL_TYPES:
            if is_penpal_call(call, names):
                echo = True
                state["echo"] = True
            return None

    thread_id = state.get("thread_id") or _session_id_from_name(jsonl_file)
    conversation_id = state.get("conversation_id")
    meta_parsed = bool(state.get("meta_parsed"))
    cwd = state.get("cwd")
    start_ts = state.get("ts")

    if rtype == "session_meta" or (rtype is None and line_no == 1 and "id" in row):
        meta = payload if rtype == "session_meta" and payload is not None else row
        if not meta_parsed:
            meta_id = meta.get("id") if isinstance(meta.get("id"), str) and meta.get("id") else None
            meta_sid = meta.get("session_id") if isinstance(meta.get("session_id"), str) and meta.get("session_id") else None
            if not thread_id and meta_id:
                thread_id = meta_id
            conversation_id = meta_sid or meta_id or thread_id
            meta_parsed = True
        if not cwd and isinstance(meta.get("cwd"), str):
            cwd = meta["cwd"]
        if not start_ts and isinstance(meta.get("timestamp"), str):
            start_ts = meta["timestamp"]
        state.update(
            sid=conversation_id,
            thread_id=thread_id,
            conversation_id=conversation_id,
            meta_parsed=meta_parsed,
            cwd=cwd,
            ts=start_ts,
        )
        return None

    if rtype == "turn_context":
        if payload and isinstance(payload.get("cwd"), str) and payload["cwd"]:
            cwd = payload["cwd"]
            state["cwd"] = cwd
        return None

    if rtype == "response_item" and payload is not None:
        message = payload
    elif rtype == "message" and payload is None:
        message = row  # old unwrapped shape
    else:
        return None

    if message.get("type") not in (None, "message") or message.get("role") not in _KEEP_ROLES:
        return None

    role = message["role"]
    text = _message_text(message.get("content"), role)
    if not text:
        return None

    if not conversation_id:
        conversation_id = thread_id

    marked, echo = advance_echo(echo, role, True, False)
    when = _parse_iso8601(row.get("timestamp")) or _parse_iso8601(message.get("timestamp")) or _parse_iso8601(start_ts)
    if when is None:
        when = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
    ordinal = row.get("ordinal")
    msg_tag = str(ordinal) if isinstance(ordinal, int) else f"L{line_no}"
    external_id = f"{thread_id}:{ordinal}" if isinstance(ordinal, int) else f"{thread_id}:L{line_no}"
    metadata: dict[str, Any] = {"session_id": conversation_id, "thread_id": thread_id}
    if marked:
        metadata["via_penpal"] = True
    if cwd:
        metadata["cwd"] = cwd
        metadata["project_dir"] = cwd
        metadata["project"] = normalize_project(cwd)

    state.update(
        sid=conversation_id,
        thread_id=thread_id,
        conversation_id=conversation_id,
        meta_parsed=meta_parsed,
        cwd=cwd,
        ts=start_ts,
        echo=echo,
    )

    return SemanticEvent(
        time=when,
        source=source_name,
        actor=role,
        intent=text[:MAX_INTENT_CHARS],
        artifact=f"codex:session:{thread_id}:msg:{msg_tag}",
        raw_ref=f"codex:sessions:{rel}:{line_no}",
        privacy_tier=privacy_tier,
        metadata=metadata,
        external_id=external_id,
        source_locator=str(root),
        evidence_locator=str(jsonl_file.resolve()),
        evidence_line=line_no,
        kind="message",
    )


def _is_rollout_file(path: Path) -> bool:
    name = path.name
    return name.endswith(".jsonl") or name.endswith(".jsonl.zst")


def _count_rollout_files(root: Path) -> int:
    return sum(1 for f in root.rglob("*") if f.is_file() and _is_rollout_file(f))


def _session_id_from_name(path: Path) -> str:
    name = path.name
    if name.endswith(".zst"):
        name = name[:-4]
    stem = Path(name).stem
    match = _SESSION_ID_RE.search(stem)
    return match.group(1) if match else stem


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
