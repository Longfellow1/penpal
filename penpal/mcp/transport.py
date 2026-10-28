"""MCP stdio transport with request-boundary reloads and session handoff."""
from __future__ import annotations

import json
import os
import secrets
import sys
from io import TextIOWrapper
from pathlib import Path
from typing import Any

import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError, JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from penpal.config import Config
from penpal.foundation.runtime_identity import IdentityWatcher, RuntimeIdentity
from penpal.runtime.evaluate import current_identity, loaded_identity
from penpal.runtime.process import current_exec_command, exec_self

_HANDOFF_ENV = "PENPAL_MCP_SESSION_HANDOFF"


class _RequestTracker:
    def __init__(self) -> None:
        self.in_flight: set[str | int] = set()
        self.initialize_params: dict[str, Any] | None = None
        self.synthetic_initialize_id: str | None = None


class _IdentityAwareStdin:
    """Own stdin buffering so buffered host messages can be delivered before an exec."""

    def __init__(self, watcher: IdentityWatcher, tracker: _RequestTracker, handoff: dict[str, Any]):
        self.fd = 0
        self.watcher = watcher
        self.tracker = tracker
        self.handoff = handoff
        self.reload_requested = False
        self.buffer = bytearray()
        self.eof = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                return line.decode("utf-8", errors="replace")
            if self.eof:
                if self.buffer:
                    line = bytes(self.buffer)
                    self.buffer.clear()
                    return line.decode("utf-8", errors="replace")
                raise StopAsyncIteration

            if not self.reload_requested:
                try:
                    self.reload_requested = self.watcher.check()
                except (OSError, RuntimeError, ValueError):
                    # Identity reads are retried on the next cadence; a transient config read must
                    # not make a connected host lose its MCP process.
                    self.reload_requested = False
            if self.reload_requested and not self.tracker.in_flight and not self.buffer:
                self._exec_with_handoff()

            timeout = min(1.0, max(0.05, self.watcher.seconds_until_check()))
            with anyio.move_on_after(timeout) as scope:
                await anyio.wait_readable(self.fd)
            if scope.cancel_called:
                continue

            chunk = os.read(self.fd, 8192)
            if not chunk:
                self.eof = True
            else:
                self.buffer.extend(chunk)

    def _exec_with_handoff(self) -> None:
        target, argv = current_exec_command()
        state = {
            "initialize_params": self.tracker.initialize_params,
            "tool_signature": self.handoff.get("tool_signature"),
            "generation": int(self.handoff.get("generation", 0)) + 1,
        }
        os.environ[_HANDOFF_ENV] = json.dumps(state, separators=(",", ":"))
        print(
            f"penpal mcp serve reload generation={state['generation']} pid={os.getpid()}",
            file=sys.stderr,
            flush=True,
        )
        exec_self(executable=target, argv=argv)


class _ReadStream:
    def __init__(self, upstream, tracker: _RequestTracker, writer, init_params: dict[str, Any] | None,
                 synthetic_id: str | None, notify_tools_changed: bool):
        self.upstream = upstream
        self.tracker = tracker
        self.writer = writer
        self.synthetic_id = synthetic_id
        self.notify_tools_changed = notify_tools_changed
        self.synthetic: list[SessionMessage] = []
        if init_params is not None and synthetic_id is not None:
            self.synthetic = [
                SessionMessage(JSONRPCMessage.model_validate({
                    "jsonrpc": "2.0", "id": synthetic_id, "method": "initialize", "params": init_params,
                })),
                SessionMessage(JSONRPCMessage.model_validate({
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })),
            ]

    async def __aenter__(self):
        await self.upstream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self.upstream.__aexit__(*exc)

    async def aclose(self):
        await self.upstream.aclose()

    async def __aiter__(self):
        for index, message in enumerate(self.synthetic):
            yield message
            if index == 1 and self.notify_tools_changed:
                await self.writer.send(SessionMessage(JSONRPCMessage.model_validate({
                    "jsonrpc": "2.0", "method": "notifications/tools/list_changed",
                })))

        async for message in self.upstream:
            if not isinstance(message, SessionMessage):
                yield message
                continue
            root = message.message.root
            if isinstance(root, JSONRPCRequest):
                if root.method == "initialize" and isinstance(root.params, dict):
                    self.tracker.initialize_params = root.params
                self.tracker.in_flight.add(root.id)
            yield message


class _WriteStream:
    def __init__(self, upstream, tracker: _RequestTracker):
        self.upstream = upstream
        self.tracker = tracker

    async def __aenter__(self):
        await self.upstream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self.upstream.__aexit__(*exc)

    async def aclose(self):
        await self.upstream.aclose()

    async def send(self, message: SessionMessage) -> None:
        root = message.message.root
        request_id = root.id if isinstance(root, (JSONRPCResponse, JSONRPCError)) else None
        if request_id is not None and request_id == self.tracker.synthetic_initialize_id:
            return
        await self.upstream.send(message)


class _FlushAwareStdout:
    """Release request tracking only after the response has been flushed to the host pipe."""

    def __init__(self, tracker: _RequestTracker):
        self.tracker = tracker
        self.stream = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
        self._last_write = ""

    async def write(self, value: str) -> int:
        self._last_write = value
        return await self.stream.write(value)

    async def flush(self) -> None:
        await self.stream.flush()
        await anyio.lowlevel.checkpoint()
        try:
            payload = json.loads(self._last_write)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("id"), (str, int)):
            self.tracker.in_flight.discard(payload["id"])


async def _serve_async(
    server: Any,
    config: Config,
    start_identity: RuntimeIdentity,
    handoff: dict[str, Any],
    parent_pid: int,
) -> None:
    tracker = _RequestTracker()
    synthetic_id = None
    init_params = handoff.get("initialize_params")
    if isinstance(init_params, dict):
        synthetic_id = f"penpal-handoff-{secrets.token_hex(8)}"
        tracker.synthetic_initialize_id = synthetic_id
        # Carry the host's initialize forward: the next exec must hand it over again.
        tracker.initialize_params = init_params

    current_tools = await server.list_tools()
    current_signature_payload = [
        {"name": item.name, "inputSchema": item.inputSchema}
        for item in current_tools
    ]
    import hashlib

    current_signature = hashlib.sha256(
        json.dumps(current_signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    notify_tools_changed = bool(
        synthetic_id
        and handoff.get("tool_signature")
        and handoff["tool_signature"] != current_signature
    )
    handoff["tool_signature"] = current_signature
    stdin = _IdentityAwareStdin(
        IdentityWatcher(start_identity, lambda: current_identity(config)), tracker, handoff
    )
    stdout = _FlushAwareStdout(tracker)
    async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
        wrapped_read = _ReadStream(read_stream, tracker, write_stream, init_params, synthetic_id, notify_tools_changed)
        wrapped_write = _WriteStream(write_stream, tracker)
        initialization_options = server._mcp_server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True)
        )

        async with anyio.create_task_group() as task_group:
            async def run_server() -> None:
                try:
                    await server._mcp_server.run(wrapped_read, wrapped_write, initialization_options)
                finally:
                    task_group.cancel_scope.cancel()

            async def watch_parent() -> None:
                while True:
                    if os.getppid() != parent_pid:
                        break
                    await anyio.sleep(0.5)
                print("penpal mcp serve parent exited; stopping", file=sys.stderr, flush=True)
                if os.environ.get("PENPAL_ISOLATED") == "1":
                    marker_path = os.environ.get("PENPAL_TEST_MCP_EXIT_MARKER")
                    if marker_path:
                        Path(marker_path).write_text("exited\n", encoding="ascii")
                os._exit(0)

            task_group.start_soon(run_server)
            task_group.start_soon(watch_parent)


def serve_stdio(server: Any, config: Config | None = None, *, parent_pid: int | None = None) -> None:
    """Run FastMCP on SDK stdio streams and resume the negotiated session after exec."""
    parent_pid = os.getppid() if parent_pid is None else parent_pid
    cfg = config or Config.load()
    start_identity = loaded_identity(cfg)
    raw_handoff = os.environ.pop(_HANDOFF_ENV, "")
    handoff: dict[str, Any] = {}
    if raw_handoff:
        try:
            parsed = json.loads(raw_handoff)
            if isinstance(parsed, dict):
                handoff = parsed
        except json.JSONDecodeError:
            handoff = {}
    generation = int(handoff.get("generation", 0))
    print(
        f"penpal mcp serve generation={generation} pid={os.getpid()} parent_pid={parent_pid}",
        file=sys.stderr,
        flush=True,
    )
    anyio.run(_serve_async, server, cfg, start_identity, handoff, parent_pid)
