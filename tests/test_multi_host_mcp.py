"""#25 multi-host: Claude-, Codex- and Cursor-style clients on one stdio MCP server (AT04 shape, AT12).

Each client is launched from the config that `penpal mcp grant --host <h>` prints (parsed back here),
so the test checks the printed config as well as the server. Children run the real `penpal` console
script with `mcp serve`; HOME is a temp dir so they never touch a real ~/.penpal.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from click.testing import CliRunner
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from penpal.config import Config
from penpal.mcp import auth
from penpal.mcp.cli import mcp_group
from penpal.sources import registry
from penpal.sources.cleaning.config import CleaningConfig
from penpal.sources.plugins.claude_code import ClaudeCodeSource
from penpal.sources.plugins.codex_cli import CodexCliSource
from penpal.store import buckets
from penpal.store.db import close, init_db
from test_mainline_codex_git import _read_all, _rollout_lines, _write_rollout

KEYWORD = "narwhalquux"


@pytest.fixture
def hosts(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # Config.load() and the child processes both resolve here
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PENPAL_TOKEN", raising=False)
    monkeypatch.delenv("PENPAL_CLIENT", raising=False)
    buckets.reset_cache()
    init_db(Config.load().db_path_expanded)

    codex_root = tmp_path / "codex" / "sessions"
    _write_rollout(codex_root, _rollout_lines())
    proj = tmp_path / "claude" / "projects" / "-work-demo-app"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(
        json.dumps({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "2026-05-01T07:00:00Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": f"why is the {KEYWORD} cache slow?"}]}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_PLUGINS", {
        "claude_code": ClaudeCodeSource(projects_root=tmp_path / "claude" / "projects", self_exclude_dirs=set(), use_cursor=True),
        "codex_cli": CodexCliSource(sessions_root=codex_root, use_cursor=True),
    })
    monkeypatch.setattr(registry, "load_cleaning_config", lambda: CleaningConfig())
    _read_all()
    yield home
    close()


def _grant(client: str, host: str) -> tuple[dict, str]:
    """Run the real `mcp grant` and return (the server entry it printed, the token)."""
    out = CliRunner().invoke(mcp_group, ["grant", client, "--host", host, "--buckets", "Work"])
    assert out.exit_code == 0, out.output
    token = next(w for w in out.output.split() if w.startswith("kp_"))
    if host == "codex":
        start = out.output.index("[mcp_servers.penpal]")
        block = "\n".join(out.output[start:].splitlines()[:5])
        return tomllib.loads(block)["mcp_servers"]["penpal"], token
    start, end = out.output.index("{"), out.output.rindex("}") + 1
    return json.loads(out.output[start:end])["mcpServers"]["penpal"], token


def _params(server: dict, home: Path, token: str | None = None, client: str | None = None) -> StdioServerParameters:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), **server["env"]}
    if token is not None:
        env["PENPAL_TOKEN"] = token
    if client is not None:
        env["PENPAL_CLIENT"] = client
    return StdioServerParameters(command=server["command"], args=server["args"], env=env)


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


async def _call(session: ClientSession, name: str, args: dict) -> dict:
    return _payload(await session.call_tool(name, args))


async def _open(stack: AsyncExitStack, params: StdioServerParameters) -> ClientSession:
    r, w = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(r, w))
    await session.initialize()
    return session


def _run(coro):
    return asyncio.run(coro)


# ── printed config is launchable and serves search + evidence (AT04 shape) ────────────────────

def test_claude_codex_cursor_configs_each_search_and_get_evidence(hosts) -> None:
    async def one(host: str, client: str) -> None:
        server, token = _grant(client, host)
        assert os.path.dirname(server["command"]) == os.path.dirname(sys.executable)  # venv's penpal, not a system one
        async with AsyncExitStack() as stack:
            session = await _open(stack, _params(server, hosts))
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"search_context", "get_evidence"} <= names

            found = await _call(session, "search_context", {"keywords": [KEYWORD]})
            assert found["status"] == "ok", (host, found)
            sources = {r["source"] for r in found["results"]}
            assert {"claude_code", "codex_cli"} <= sources
            hit = next(r for r in found["results"] if r["source"] == "codex_cli")

            ev = await _call(session, "get_evidence", {"evidence_id": hit["evidence_id"]})
            assert ev["status"] == "ok" and ev["evidence"]["available"] is True
            assert KEYWORD in ev["evidence"]["excerpt"]

    async def run_all():
        for host, client in (("claude", "claude"), ("codex", "codex"), ("cursor", "cursor")):
            await one(host, client)

    _run(run_all())


def test_a_token_presented_under_another_hosts_name_is_denied(hosts) -> None:
    cursor_server, _ = _grant("cursor", "cursor")

    async def run():
        async with AsyncExitStack() as stack:
            session = await _open(stack, _params(cursor_server, hosts, client="claude"))
            return await _call(session, "search_context", {"keywords": [KEYWORD]})

    denied = _run(run())
    assert denied["status"] == "permission_denied" and "results" not in denied


# ── AT12: same grants -> same facts; revoke / rotate are per host and immediate ───────────────

def test_two_hosts_with_same_buckets_get_the_same_facts(hosts) -> None:
    claude_server, _ = _grant("claude", "claude")
    codex_server, _ = _grant("codex", "codex")

    async def query(server):
        async with AsyncExitStack() as stack:
            session = await _open(stack, _params(server, hosts))
            return await _call(session, "search_context", {"keywords": [KEYWORD]})

    async def run():
        return await query(claude_server), await query(codex_server)

    a, b = _run(run())
    assert a["status"] == b["status"] == "ok"
    assert a["results"] == b["results"]  # same ids, snippets, times, sources, evidence ids
    assert {r["bucket"] for r in a["results"]} == {"Work"}


def test_revoke_one_host_fails_its_next_call_and_the_other_keeps_working(hosts) -> None:
    claude_server, _ = _grant("claude", "claude")
    cursor_server, _ = _grant("cursor", "cursor")

    async def run():
        async with AsyncExitStack() as stack:
            claude = await _open(stack, _params(claude_server, hosts))
            cursor = await _open(stack, _params(cursor_server, hosts))
            before = await _call(claude, "search_context", {"keywords": [KEYWORD]})
            assert before["status"] == "ok"

            out = CliRunner().invoke(mcp_group, ["revoke", "claude"])
            assert out.exit_code == 0 and "Revoked 1" in out.output

            claude_after = await _call(claude, "search_context", {"keywords": [KEYWORD]})
            cursor_after = await _call(cursor, "search_context", {"keywords": [KEYWORD]})
            return claude_after, cursor_after

    claude_after, cursor_after = _run(run())
    assert claude_after["status"] == "permission_denied" and "results" not in claude_after
    assert cursor_after["status"] == "ok" and cursor_after["results"]


def test_rotate_one_host_leaves_the_other_untouched(hosts) -> None:
    codex_server, codex_old = _grant("codex", "codex")
    cursor_server, _ = _grant("cursor", "cursor")

    async def run():
        async with AsyncExitStack() as stack:
            codex = await _open(stack, _params(codex_server, hosts))
            cursor = await _open(stack, _params(cursor_server, hosts))
            out = CliRunner().invoke(mcp_group, ["rotate", "codex"])
            assert out.exit_code == 0, out.output
            codex_new = next(w for w in out.output.split() if w.startswith("kp_"))
            assert codex_new != codex_old

            old_call = await _call(codex, "search_context", {"keywords": [KEYWORD]})
            cursor_call = await _call(cursor, "search_context", {"keywords": [KEYWORD]})
        async with AsyncExitStack() as stack:
            new_session = await _open(stack, _params(codex_server, hosts, token=codex_new))
            new_call = await _call(new_session, "search_context", {"keywords": [KEYWORD]})
        return old_call, cursor_call, new_call

    old_call, cursor_call, new_call = _run(run())
    assert old_call["status"] == "permission_denied"
    assert cursor_call["status"] == "ok" and cursor_call["results"]
    assert new_call["status"] == "ok" and new_call["results"]
    assert auth.authenticate(codex_old) is None
