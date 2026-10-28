from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from penpal.mcp.auth import create_grant
from penpal.store.db import close, init_db


ROOT = Path(__file__).resolve().parents[1]
SERVER_CODE = "from penpal.cli import main; main()"


def _package_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "installed" / "penpal"
    shutil.copytree(ROOT / "penpal", destination)
    return destination


def _environment(home: Path, package: Path, token: str = "kp_test-token") -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "PENPAL_ISOLATED": "1",
        "PENPAL_RUNTIME_DISABLE_LAUNCHD_PROBE": "1",
        "PENPAL_TEST_IDENTITY_INTERVAL_SEC": "0.2",
        "PENPAL_TEST_FINGERPRINT_ROOT": str(package),
        "PENPAL_TOKEN": token,
        "PENPAL_CLIENT": "r4-test-client",
        "PYTHONPATH": str(package.parent),
    }


def _grant(home: Path) -> str:
    init_db(home / ".penpal" / "penpal.db")
    token = create_grant("r4-test-client", buckets=["Work"])
    close()
    return token


class _JsonRpcClient:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.next_id = 1
        self.messages: list[dict] = []

    def send(self, method: str, params: dict | None = None, request_id: int | None = None) -> int:
        request_id = request_id or self.next_id
        self.next_id = max(self.next_id, request_id + 1)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        return request_id

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 8.0) -> tuple[dict, list[dict]]:
        request_id = self.send(method, params)
        seen: list[dict] = []
        deadline = time.monotonic() + timeout
        fd = self.process.stdout.fileno()
        while time.monotonic() < deadline:
            try:
                message = _read_json_line(fd, deadline - time.monotonic())
            except EOFError as exc:
                raise EOFError(f"MCP process exited rc={self.process.poll()}; stderr={_stderr_tail(self.process)}") from exc
            self.messages.append(message)
            seen.append(message)
            if message.get("id") == request_id:
                return message, seen
        raise TimeoutError(f"no JSON-RPC response for {method} id={request_id}; messages={seen!r}")


def _read_json_line(fd: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    line = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        byte = os.read(fd, 1)
        if not byte:
            raise EOFError("MCP stdio closed")
        if byte == b"\n":
            return json.loads(line.decode("utf-8"))
        line.extend(byte)
    raise TimeoutError("no JSON-RPC line before timeout")


def _read_stderr_until(process: subprocess.Popen[bytes], needle: bytes, timeout: float = 8.0) -> bytes:
    assert process.stderr is not None
    deadline = time.monotonic() + timeout
    captured = bytearray()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stderr.fileno()], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        byte = os.read(process.stderr.fileno(), 1)
        if not byte:
            break
        captured.extend(byte)
        if needle in captured:
            return bytes(captured)
    raise TimeoutError(f"stderr did not contain {needle!r}: {captured.decode(errors='replace')}")


def _stderr_tail(process: subprocess.Popen[bytes]) -> str:
    if process.stderr is None or process.poll() is None:
        return ""
    fd = process.stderr.fileno()
    captured = bytearray()
    while select.select([fd], [], [], 0)[0]:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        captured.extend(chunk)
    return captured.decode("utf-8", errors="replace")[-2000:]


def _spawn_server(home: Path, package: Path, token: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", SERVER_CODE, "mcp", "serve"],
        cwd=home.parent,
        env=_environment(home, package, token),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()


def test_mcp_exec_handoff_keeps_session_and_notifies_tool_list_change(tmp_path):
    home = tmp_path / "home"
    package = _package_copy(tmp_path)
    token = _grant(home)
    process = _spawn_server(home, package, token)
    client = _JsonRpcClient(process)
    original_pid = process.pid
    try:
        _read_stderr_until(process, b"generation=0")
        initialize, _ = client.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "r4-test", "version": "1"},
        })
        assert initialize["result"]["capabilities"]["tools"]["listChanged"] is True
        client.notify("notifications/initialized")
        tools_before, _ = client.request("tools/list")
        before_names = {item["name"] for item in tools_before["result"]["tools"]}
        assert "search_context" in before_names
        first_call, _ = client.request("tools/call", {
            "name": "search_context", "arguments": {"keywords": ["r4-empty-before"]},
        })
        assert first_call["result"]["isError"] is False

        server_path = package / "mcp" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        assert "    def search_context(\n" in source
        server_path.write_text(source.replace("    def search_context(\n", "    def search_context_after_reload(\n", 1), encoding="utf-8")

        reload_log = _read_stderr_until(process, b"reload generation=1", timeout=10)
        assert f"pid={original_pid}".encode() in reload_log
        tools_after, messages = client.request("tools/list", timeout=8)
        after_names = {item["name"] for item in tools_after["result"]["tools"]}
        assert "search_context_after_reload" in after_names
        assert "search_context" not in after_names
        assert any(message.get("method") == "notifications/tools/list_changed" for message in messages)
        handoff_responses = [
            message for message in client.messages
            if message.get("id") == "penpal-handoff" or str(message.get("id", "")).startswith("penpal-handoff-")
        ]
        assert handoff_responses == []
        initialize_responses = [message for message in client.messages if message.get("id") == 1]
        assert len(initialize_responses) == 1

        next_call, _ = client.request("tools/call", {
            "name": "search_context_after_reload", "arguments": {"keywords": ["r4-empty-after"]},
        })
        assert next_call["result"]["isError"] is False

        # A second reload must hand the session over again (it once handed over None and every later call failed).
        tools_path = package / "mcp" / "tools.py"
        tools_path.write_text(tools_path.read_text(encoding="utf-8") + "\n# second reload\n", encoding="utf-8")
        _read_stderr_until(process, b"reload generation=2", timeout=10)
        third_call, _ = client.request("tools/call", {
            "name": "search_context_after_reload", "arguments": {"keywords": ["r4-empty-second"]},
        }, timeout=8)
        assert "error" not in third_call
        assert third_call["result"]["isError"] is False
        assert process.pid == original_pid
    finally:
        _stop_process(process)


def test_mcp_exits_when_stdin_reaches_eof(tmp_path):
    home = tmp_path / "home"
    package = _package_copy(tmp_path)
    token = _grant(home)
    process = _spawn_server(home, package, token)
    try:
        _read_stderr_until(process, b"generation=0")
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=8) == 0
    finally:
        _stop_process(process)


def test_mcp_exits_when_its_parent_process_dies(tmp_path):
    home = tmp_path / "home"
    package = _package_copy(tmp_path)
    token = _grant(home)
    env = _environment(home, package, token)
    pid_path = tmp_path / "child.pid"
    exit_marker = tmp_path / "child-exited"
    env["PENPAL_TEST_MCP_EXIT_MARKER"] = str(exit_marker)
    child_command = [sys.executable, "-c", SERVER_CODE, "mcp", "serve"]
    parent_code = (
        "import os, subprocess, sys, time\n"
        f"child = subprocess.Popen({child_command!r}, cwd={str(home.parent)!r}, env=os.environ.copy())\n"
        f"open({str(pid_path)!r}, 'w').write(str(child.pid))\n"
        "while True: time.sleep(1)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        cwd=home.parent,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    child_pid = None
    stdin_guard = os.dup(parent.stdin.fileno()) if parent.stdin is not None else None
    try:
        assert _wait_for(lambda: pid_path.exists(), timeout=8), "intermediate process did not start MCP server"
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        assert child_pid > 0
        startup_log = _read_stderr_until(parent, f"parent_pid={parent.pid}".encode())
        assert b"generation=0" in startup_log
        assert _pid_exists(child_pid), "MCP process exited before its parent was stopped"
        parent.kill()
        parent.wait(timeout=5)
        assert _wait_for(
            lambda: exit_marker.exists(), timeout=5,
        ), "MCP child outlived its parent"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if parent.stdin:
            parent.stdin.close()
        if stdin_guard is not None:
            os.close(stdin_guard)
        if parent.stderr and not parent.stderr.closed:
            parent.stderr.close()
        if child_pid and not exit_marker.exists() and _pid_exists(child_pid):
            os.kill(child_pid, signal.SIGTERM)
            _wait_for(lambda: not _pid_exists(child_pid), timeout=5)


def _wait_for(predicate, *, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
