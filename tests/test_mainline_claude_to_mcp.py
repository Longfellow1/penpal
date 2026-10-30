"""P0-02 tracer bullet: Claude Code history -> DB v20 -> FTS -> MCP search_context (token)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from penpal.config import Config
from penpal.mcp import auth
from penpal.mcp.cli import mcp_group
from mcp_helpers import server_for
from penpal.sources import registry
from penpal.sources.cleaning.config import CleaningConfig
from penpal.sources.discover import sources_group
from penpal.sources.plugins.claude_code import ClaudeCodeSource
from penpal.sources.scheduler import SourcesScheduler
from penpal.sources.sink import flush_named_entity_backfill
from penpal.store.db import close, get_conn, init_db

FAKE_KEY = "sk-test-FAKE0000000000000000000000"
PROJECT = "-tmp-demo-project"


def _line(kind: str, uuid: str, ts: str, text: str, session: str = "sess-1") -> str:
    return json.dumps(
        {
            "type": kind,
            "uuid": uuid,
            "sessionId": session,
            "timestamp": ts,
            "message": {"role": kind, "content": [{"type": "text", "text": text}]},
        }
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    project_dir = tmp_path / "claude" / "projects" / PROJECT
    project_dir.mkdir(parents=True)
    session = project_dir / "sess-1.jsonl"
    session.write_text(
        "\n".join(
            [
                _line("user", "u-1", "2026-04-28T01:00:00Z", "please investigate the zebrafish pipeline slowdown"),
                _line("assistant", "a-1", "2026-04-28T01:05:00Z", "the zebrafish pipeline stalls on the retry queue"),
                json.dumps({"type": "summary", "summary": "ignored row"}),
                _line("user", "u-2", "2026-04-28T01:10:00Z", f"my deploy credential is api_key={FAKE_KEY} do not leak it"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def make_source() -> ClaudeCodeSource:
        return ClaudeCodeSource(
            projects_root=tmp_path / "claude" / "projects", self_exclude_dirs=set(), use_cursor=True
        )

    monkeypatch.setattr(registry, "_PLUGINS", {"claude_code": make_source()})
    monkeypatch.setattr(registry, "load_cleaning_config", lambda: CleaningConfig())
    db_path = tmp_path / "penpal.db"
    init_db(db_path)
    yield type("Env", (), {"tmp": tmp_path, "session": session, "db": db_path, "make_source": staticmethod(make_source)})
    close()


def _sync() -> int:
    now = datetime.now(timezone.utc)
    n = sum(1 for _ in registry.read_all(datetime(2000, 1, 1, tzinfo=timezone.utc), now, source="claude_code"))
    flush_named_entity_backfill()
    return n


def _count_events() -> int:
    return get_conn().execute("SELECT count(*) FROM raw_events").fetchone()[0]


def _call(token, **args):
    """Call search_context through the MCP server layer (SDK in-memory client/session)."""
    server = server_for(token)

    async def run():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await client.call_tool("search_context", args)

    return asyncio.run(run())


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@pytest.fixture
def token(env) -> str:
    return auth.create_grant("claude")


# ── AT01 / AT02 ───────────────────────────────────────────────────────────────

def test_at01_session_found_by_exact_word_with_provenance(env, token) -> None:
    assert _sync() == 3  # 2 messages + 1 redacted-credential message; summary row skipped

    result = _call(token, keywords=["zebrafish"])
    assert result.isError is False
    data = _payload(result)
    assert result.structuredContent == data
    assert data["status"] == "ok"
    assert len(data["results"]) == 2
    by_time = sorted(data["results"], key=lambda r: r["occurred_at"])
    first = by_time[0]
    assert first["occurred_at"] == "2026-04-28T01:00:00+00:00"  # real message time, not ingest time
    assert first["source"] == "claude_code"
    assert first["bucket"] == "Work"
    assert "zebrafish" in first["snippet"]
    assert first["match"] == "strong" and first["match_reason"] == "keyword match: 1 of 1 keywords"
    assert first["id"].startswith("evt_") and first["evidence_id"].startswith("evd_")
    assert data["coverage"]["partial"] is False

    ev = get_conn().execute(
        "SELECT * FROM evidence WHERE id = ?", (int(first["evidence_id"].removeprefix("evd_")),)
    ).fetchone()
    assert ev["locator"] == str(env.session.resolve())
    assert ev["offset_or_line"] == 1
    assert ev["available"] == 1


def test_at02_unknown_word_is_no_results_and_fabricates_nothing(env, token) -> None:
    _sync()
    data = _payload(_call(token, keywords=["quetzalcoatlus"]))
    assert data["status"] == "no_results"
    assert data["results"] == []
    assert "snippet" not in json.dumps(data["results"])


def test_time_filter_uses_occurred_at_not_ingest_time(env, token) -> None:
    _sync()  # ingested "now" (2026+), events occurred 2026-04-28
    inside = _payload(_call(token, keywords=["zebrafish"], time_range={"start": "2026-04-28", "end": "2026-04-28"}))
    assert inside["status"] == "ok" and len(inside["results"]) == 2
    before = _payload(_call(token, keywords=["zebrafish"], time_range={"end": "2026-04-27"}))
    assert before["status"] == "no_results"
    narrow = _payload(
        _call(token, keywords=["zebrafish"], time_range={"start": "2026-04-28T01:03:00Z", "end": "2026-04-28T01:30:00Z"})
    )
    assert [r["occurred_at"] for r in narrow["results"]] == ["2026-04-28T01:05:00+00:00"]
    bad = _payload(_call(token, keywords=["zebrafish"], time_range={"start": "not-a-date"}))
    assert bad["status"] == "invalid_argument"


def test_mcp_echo_parameter_rename_accepts_deprecated_alias(env, token, monkeypatch) -> None:
    from penpal.mcp import tools as mcp_tools

    received: list[bool] = []

    def search_context(_token, _keywords, _time_range=None, _sources=None, _limit=10,
                       include_penpal_echoes=False, *, client=None):
        received.append(include_penpal_echoes)
        return {"status": "ok", "results": []}

    def get_timeline(_token, _start, _end, _sources=None, _limit=50, _cursor=None,
                     include_penpal_echoes=False, *, client=None):
        received.append(include_penpal_echoes)
        return {"status": "ok", "results": []}

    monkeypatch.setattr(mcp_tools, "search_context", search_context)
    monkeypatch.setattr(mcp_tools, "get_timeline", get_timeline)
    server = server_for(token)

    async def run():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            registered = {tool.name: tool for tool in (await client.list_tools()).tools}
            search = registered["search_context"].inputSchema["properties"]
            timeline = registered["get_timeline"].inputSchema["properties"]
            search_new = await client.call_tool(
                "search_context", {"keywords": ["zebrafish"], "include_penpal_echoes": True}
            )
            search_old = await client.call_tool(
                "search_context", {"keywords": ["zebrafish"], "include_keypulse_echoes": True}
            )
            timeline_new = await client.call_tool(
                "get_timeline", {"start": "2026-04-28", "end": "2026-04-28", "include_penpal_echoes": True}
            )
            timeline_old = await client.call_tool(
                "get_timeline", {"start": "2026-04-28", "end": "2026-04-28", "include_keypulse_echoes": True}
            )
            return search, timeline, search_new, search_old, timeline_new, timeline_old

    search_schema, timeline_schema, *results = asyncio.run(run())
    for schema in (search_schema, timeline_schema):
        assert schema["include_penpal_echoes"]["type"] == "boolean"
        assert schema["include_keypulse_echoes"]["deprecated"] is True
        assert "include_penpal_echoes" in schema["include_keypulse_echoes"]["description"]
    assert received == [True, True, True, True]
    assert all(json.loads(result.content[0].text) == {"status": "ok", "results": []} for result in results)


def test_mcp_deprecated_echo_alias_keeps_boolean_validation(env, token) -> None:
    data = _payload(_call(token, keywords=["zebrafish"], include_keypulse_echoes="yes"))
    assert data == {"status": "invalid_argument", "message": "include_penpal_echoes must be a boolean"}


def test_source_filter_and_short_cjk_terms(env, token) -> None:
    with env.session.open("a", encoding="utf-8") as fh:
        fh.write(_line("user", "u-3", "2026-04-28T02:00:00Z", "排查失败恢复流程的原因") + "\n")
    _sync()
    assert _payload(_call(token, keywords=["失败恢复"]))["status"] == "ok"
    assert _payload(_call(token, keywords=["失败"]))["status"] == "ok"  # 2 chars: LIKE fallback
    assert _payload(_call(token, keywords=["zebrafish"], sources=["git_log"]))["status"] == "no_results"
    assert _payload(_call(token, keywords=["zebrafish"], sources=["claude_code"]))["status"] == "ok"


# ── resume / no duplicates ────────────────────────────────────────────────────

def test_restart_resumes_from_cursor_without_duplicates(env) -> None:
    assert _sync() == 3
    assert _count_events() == 3

    # "restart": drop the DB connection and plugin state, reopen
    close()
    init_db(env.db)
    registry._PLUGINS["claude_code"] = env.make_source()
    assert _sync() == 0
    assert _count_events() == 3

    cur = get_conn().execute("SELECT cursor FROM sync_cursors").fetchone()[0]
    assert json.loads(cur)["files"]["sess-1.jsonl"]["line"] == 4

    with env.session.open("a", encoding="utf-8") as fh:
        fh.write(_line("assistant", "a-2", "2026-04-28T03:00:00Z", "follow-up about the zebrafish fix") + "\n")
    assert _sync() == 1
    assert _count_events() == 4


def test_lost_cursor_reimport_is_deduplicated_by_external_id(env) -> None:
    _sync()
    get_conn().execute("DELETE FROM sync_cursors")
    get_conn().commit()
    registry._PLUGINS["claude_code"] = env.make_source()
    _sync()
    assert _count_events() == 3
    assert get_conn().execute("SELECT count(*) FROM evidence").fetchone()[0] == 3


def test_partial_trailing_line_is_not_consumed_until_complete(env) -> None:
    env.session.write_text(env.session.read_text() + '{"type": "user", "uuid": "u-9", "timest', encoding="utf-8")
    assert _sync() == 3
    with env.session.open("a", encoding="utf-8") as fh:
        fh.write('amp": "2026-04-28T04:00:00Z", "sessionId": "sess-1", "message": {"content": "late completed line"}}\n')
    assert _sync() == 1
    assert _count_events() == 4


def test_cursor_not_advanced_when_persist_fails(env, monkeypatch) -> None:
    def boom(_event):
        raise RuntimeError("disk full")

    monkeypatch.setattr(registry, "persist_semantic_event", boom)
    _sync()
    assert _count_events() == 0
    assert get_conn().execute("SELECT count(*) FROM sync_cursors WHERE cursor IS NOT NULL").fetchone()[0] == 0
    monkeypatch.undo()
    monkeypatch.setattr(registry, "load_cleaning_config", lambda: CleaningConfig())
    registry._PLUGINS["claude_code"] = env.make_source()
    assert _sync() == 3
    assert _count_events() == 3


# ── redaction ─────────────────────────────────────────────────────────────────

def test_fake_secret_redacted_in_db_and_fts(env, token) -> None:
    _sync()
    conn = get_conn()
    texts = [r[0] for r in conn.execute("SELECT content_text FROM raw_events")]
    assert not any("FAKE0000" in t for t in texts)
    assert any("[REDACTED]" in t and "do not leak it" in t for t in texts)
    for blob in (
        "SELECT count(*) FROM raw_events_fts WHERE raw_events_fts MATCH '\"FAKE0000\"'",
        "SELECT count(*) FROM raw_events WHERE metadata_json LIKE '%FAKE0000%'",
    ):
        assert conn.execute(blob).fetchone()[0] == 0
    assert _payload(_call(token, keywords=["FAKE0000"]))["status"] == "no_results"
    assert _payload(_call(token, keywords=["leak"]))["status"] == "ok"


# ── auth ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", "kp_wrong-token"])
def test_missing_or_wrong_token_is_denied_without_data(env, token, bad) -> None:
    _sync()
    result = _call(bad, keywords=["zebrafish"])
    assert result.isError is True
    data = _payload(result)
    assert data == {
        "status": "permission_denied",
        "reason_code": "TOKEN_INVALID",
        "message": "Penpal access was not granted for this client.",
        "next_action": "review_access_in_penpal",
    }
    assert "zebrafish" not in json.dumps(result.model_dump())


def test_revoked_token_denied_immediately_and_other_client_unaffected(env) -> None:
    _sync()
    t_a, t_b = auth.create_grant("claude"), auth.create_grant("codex")
    assert _payload(_call(t_a, keywords=["zebrafish"]))["status"] == "ok"
    assert auth.revoke_grants("claude") == 1
    assert _payload(_call(t_a, keywords=["zebrafish"]))["status"] == "permission_denied"
    assert _payload(_call(t_b, keywords=["zebrafish"]))["status"] == "ok"


def test_token_plaintext_never_stored(env, token) -> None:
    _sync()
    _call(token, keywords=["zebrafish"])
    row = get_conn().execute("SELECT * FROM access_grants").fetchone()
    assert token not in tuple(row)
    assert row["token_hash"] == auth.hash_token(token) and len(row["token_hash"]) == 64
    get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for f in env.tmp.glob("penpal.db*"):
        assert token.encode() not in f.read_bytes()


# ── wiring: scheduler + CLI ───────────────────────────────────────────────────

def test_daemon_scheduler_scan_populates_events_via_same_path(env) -> None:
    from penpal.sources.history_gate import mark_onboarded

    mark_onboarded("test")  # historical imports wait for onboarding on a fresh install (#32)
    scheduler = SourcesScheduler(enabled=True, poll_interval_sec=600)
    scheduler._scan_plugin("claude_code", "test")
    flush_named_entity_backfill()
    assert _count_events() == 3
    scheduler._scan_plugin("claude_code", "test")
    assert _count_events() == 3
    row = get_conn().execute("SELECT source_id, external_id, kind FROM raw_events WHERE external_id = 'sess-1:u-1'").fetchone()
    assert row["source_id"] is not None and row["kind"] == "message"


@pytest.fixture
def cli_config(env, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(env.db)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    return cfg


def test_cli_sources_sync_populates_db(env, cli_config) -> None:
    from penpal.sources.history_gate import mark_onboarded

    mark_onboarded("test")  # historical imports wait for onboarding on a fresh install (#32)
    out = CliRunner().invoke(sources_group, ["sync", "--source", "claude_code"])
    assert out.exit_code == 0, out.output
    assert "claude_code\t3" in out.output
    again = CliRunner().invoke(sources_group, ["sync", "--source", "claude_code"])
    assert "no new events" in again.output


def test_cli_grant_prints_token_once_and_stores_hash(env, cli_config) -> None:
    out = CliRunner().invoke(mcp_group, ["grant", "claude"])
    assert out.exit_code == 0, out.output
    token = next(w for w in out.output.split() if w.startswith("kp_"))
    assert f"claude mcp add penpal -e PENPAL_TOKEN={token} -e PENPAL_CLIENT=claude -- " in out.output
    assert '"mcpServers"' in out.output and "mcp" in out.output and "serve" in out.output
    assert auth.authenticate(token).client_id == "claude"
    assert token not in tuple(get_conn().execute("SELECT * FROM access_grants").fetchone())
    listing = CliRunner().invoke(mcp_group, ["list"])
    assert token not in listing.output and "claude" in listing.output
    assert CliRunner().invoke(mcp_group, ["revoke", "claude"]).exit_code == 0
    assert auth.authenticate(token) is None


# ── real stdio transport ─────────────────────────────────────────────────────

def test_stdio_subprocess_end_to_end(env, token) -> None:
    _sync()
    child_env = {**os.environ, "PENPAL_TOKEN": token, "PENPAL_CLIENT": "claude", "KP_TEST_DB": str(env.db)}
    code = (
        "import os; from pathlib import Path; from penpal.store.db import init_db; "
        "from penpal.mcp.server import serve_stdio; init_db(Path(os.environ['KP_TEST_DB'])); serve_stdio()"
    )
    async def run(extra_env):
        p = StdioServerParameters(command=sys.executable, args=["-c", code], env={**child_env, **extra_env})
        async with stdio_client(p) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                res = await s.call_tool("search_context", {"keywords": ["zebrafish"]})
                return [t.name for t in tools.tools], json.loads(res.content[0].text)

    names, ok = asyncio.run(run({}))
    assert names == ["search_context", "get_evidence", "get_timeline", "rate_result", "commit_memory", "update_memory", "propose_source_recipe"] and ok["status"] == "ok"
    _, denied = asyncio.run(run({"PENPAL_TOKEN": "kp_nope"}))
    assert denied["status"] == "permission_denied" and "results" not in denied


# ── lead review fixes ─────────────────────────────────────────────────────────

def test_dedup_hit_on_legacy_row_backfills_provenance(env) -> None:
    from penpal.sources.sink import semantic_event_to_raw_event
    from penpal.store.repository import insert_raw_event

    legacy = ClaudeCodeSource(projects_root=env.tmp / "claude" / "projects", self_exclude_dirs=set())
    inst = legacy.discover()[0]
    since, until = datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2100, 1, 1, tzinfo=timezone.utc)
    for ev in legacy.read(inst, since, until):
        ev.metadata.setdefault("dedup_count", 1)  # what the legacy registry path added
        raw = semantic_event_to_raw_event(ev, include_named_entities=False)
        assert raw.source_id is None and raw.external_id is None
        raw.bucket = raw.kind = None  # legacy-style: no provenance at all
        insert_raw_event(raw)
    assert _count_events() == 3
    assert get_conn().execute("SELECT count(*) FROM raw_events WHERE source_id IS NOT NULL").fetchone()[0] == 0

    _sync()

    conn = get_conn()
    assert _count_events() == 3  # still one row per message
    rows = conn.execute("SELECT source_id, external_id, bucket, kind FROM raw_events").fetchall()
    assert all(r["source_id"] is not None and r["external_id"] and r["bucket"] == "Work" and r["kind"] == "message" for r in rows)
    assert conn.execute("SELECT count(*) FROM evidence").fetchone()[0] == 3


def test_dedup_backfill_never_overwrites_existing_values(env) -> None:
    _sync()
    conn = get_conn()
    conn.execute("UPDATE raw_events SET bucket = 'Personal'")
    conn.commit()
    conn.execute("DELETE FROM sync_cursors")
    conn.commit()
    registry._PLUGINS["claude_code"] = env.make_source()
    _sync()
    assert {r[0] for r in conn.execute("SELECT bucket FROM raw_events")} == {"Personal"}
    assert _count_events() == 3


def test_long_message_tail_is_searchable_but_snippet_stays_short(env, token) -> None:
    long_text = ("filler words about nothing " * 190) + " ending with narwhalquux tail"
    assert 4800 < len(long_text) < 6000
    with env.session.open("a", encoding="utf-8") as fh:
        fh.write(_line("user", "u-long", "2026-04-28T05:00:00Z", long_text) + "\n")
    _sync()
    stored = get_conn().execute("SELECT content_text FROM raw_events WHERE external_id = 'sess-1:u-long'").fetchone()[0]
    assert stored.endswith("narwhalquux tail")
    data = _payload(_call(token, keywords=["narwhalquux"]))
    assert data["status"] == "ok" and len(data["results"]) == 1
    snippet = data["results"][0]["snippet"]
    assert "narwhalquux" in snippet and len(snippet) < 300


def test_message_over_cap_is_truncated_to_32k(env) -> None:
    from penpal.sources.types import MAX_INTENT_CHARS

    with env.session.open("a", encoding="utf-8") as fh:
        fh.write(_line("user", "u-huge", "2026-04-28T06:00:00Z", "z" * (MAX_INTENT_CHARS + 5000)) + "\n")
    _sync()
    n = get_conn().execute("SELECT length(content_text) FROM raw_events WHERE external_id = 'sess-1:u-huge'").fetchone()[0]
    assert n == MAX_INTENT_CHARS == 32_000
