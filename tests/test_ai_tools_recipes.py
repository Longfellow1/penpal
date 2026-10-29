"""Comprehensive tests for AI tools recipe catalog, engine, plugin, and coverage."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import zstandard
from click.testing import CliRunner

from penpal.foundation.sqlite_rules import is_blocked_sqlite
from penpal.sources.coverage import compute_tool_coverage, coverage_command
from penpal.sources.plugins.ai_tools import AiToolsSource
from penpal.sources.recipes.catalog import (
    expand_vars_and_tilde,
    get_default_catalog_dir,
    load_catalog,
    load_recipe,
    load_recipe_from_string,
)
from penpal.sources.recipes.engine import RecipeEngine
from penpal.store.db import get_conn, init_db

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "ai_tools"
CANARY_SECRET = "VERY_CONFIDENTIAL_USER_PROMPT_CANARY_XYZ987654321"


def _compress_zstd(data: bytes) -> bytes:
    cctx = zstandard.ZstdCompressor()
    return cctx.compress(data)


# ── 1. Shipped Catalog Validation ────────────────────────────────────────────


def test_shipped_catalog_loads():
    recipes = load_catalog()
    assert len(recipes) >= 2
    by_id = {r.id: r for r in recipes}
    assert "claude_code" in by_id
    assert "codex_cli" in by_id

    claude = by_id["claude_code"]
    assert claude.readable is True
    assert claude.adapter == "claude_code"
    assert claude.mcp is not None
    assert claude.mcp.style == "json_mcpServers"

    codex = by_id["codex_cli"]
    assert codex.readable is True
    assert codex.adapter == "codex_cli"
    assert codex.mcp is not None
    assert codex.mcp.style == "toml_mcp_servers"


def test_catalog_validation_rejects_unknown_keys_and_invalid_queries():
    # Unknown top-level key
    with pytest.raises(ValueError, match="Unknown keys in .* top-level"):
        load_recipe_from_string('id = "test"\nname = "T"\nkind = "agent"\nbad_key = 1')

    # Unknown key in detect
    with pytest.raises(ValueError, match="Unknown keys in .* detect"):
        load_recipe_from_string('id = "test"\nname = "T"\nkind = "agent"\n[detect]\nfoo = []')

    # Unknown key in history
    with pytest.raises(ValueError, match="Unknown keys in .* history"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "jsonl"\nglob = "*.jsonl"\n'
            'fields = { role = "r", content = "c", created_at = "ts" }\nextra_key = 123'
        )

    # Unknown field key
    with pytest.raises(ValueError, match="history\\[0\\]\\.fields"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "jsonl"\nglob = "*.jsonl"\n'
            'fields = { role = "r", content = "c", created_at = "ts", bogus = "b" }'
        )

    # Missing required field
    with pytest.raises(ValueError, match="missing required keys"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "jsonl"\nglob = "*.jsonl"\n'
            'fields = { role = "r", content = "c" }'
        )

    # SQLite query not starting with SELECT
    with pytest.raises(ValueError, match="must start with SELECT"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "sqlite"\nglob = "*.db"\n'
            'query = "DELETE FROM sessions"\nfields = { role = "r", content = "c", created_at = "ts" }'
        )

    # SQLite query with multiple statements
    with pytest.raises(ValueError, match="must not contain multiple statements"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "sqlite"\nglob = "*.db"\n'
            'query = "SELECT 1; SELECT 2"\nfields = { role = "r", content = "c", created_at = "ts" }'
        )

    # Non-sqlite format with query
    with pytest.raises(ValueError, match="only allowed for format='sqlite'"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "jsonl"\nglob = "*.jsonl"\n'
            'query = "SELECT 1"\nfields = { role = "r", content = "c", created_at = "ts" }'
        )

    # Non-json format with records_path
    with pytest.raises(ValueError, match="only allowed for format='json'"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "jsonl"\nglob = "*.jsonl"\n'
            'records_path = "items"\nfields = { role = "r", content = "c", created_at = "ts" }'
        )

    # readable = false requires unreadable_reason
    with pytest.raises(ValueError, match="unreadable_reason"):
        load_recipe_from_string('id = "test"\nname = "T"\nkind = "agent"\nreadable = false')


# ── 2. Variable and Glob Expansion ───────────────────────────────────────────


def test_glob_and_variable_expansion(tmp_path: Path):
    env = {"CUSTOM_VAR": "/my/custom/path", "EMPTY_VAR": "", "HOME": str(tmp_path)}
    res1 = expand_vars_and_tilde("${CUSTOM_VAR}/sessions/*.jsonl", env)
    assert res1 == "/my/custom/path/sessions/*.jsonl"

    res2 = expand_vars_and_tilde("${UNSET_VAR:-/default/path}/data", env)
    assert res2 == "/default/path/data"

    res3 = expand_vars_and_tilde("~/sessions", env)
    assert res3 == f"{tmp_path}/sessions"


# ── 3. Engine Formats: JSONL, JSON, SQLite, plus zstd ────────────────────────


def test_engine_jsonl_and_zstd(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)

    # Plain JSONL
    f1 = sessions_dir / "session1.jsonl"
    lines1 = [
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": "Hello", "ts": "2026-05-01T10:00:00Z", "cwd": "/repo1"}),
        json.dumps({"msg_id": "m2", "conv_id": "c1", "role": "ai", "content": [{"type": "text", "text": "Hi there"}], "ts": "2026-05-01T10:00:05Z", "cwd": "/repo1"}),
    ]
    f1.write_text("\n".join(lines1) + "\n", encoding="utf-8")

    # Compressed JSONL.zst
    f2 = sessions_dir / "session2.jsonl.zst"
    lines2 = [
        json.dumps({"msg_id": "m3", "conv_id": "c2", "role": "human", "content": "How are you?", "ts": "2026-05-01T11:00:00Z", "cwd": "/repo2"}),
    ]
    f2.write_bytes(_compress_zstd(("\n".join(lines2) + "\n").encode("utf-8")))

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert err is None
    assert cursor is not None
    assert len(events) == 3

    e1 = next(e for e in events if e.external_id == "c1:m1")
    assert e1.actor == "user"
    assert e1.intent == "Hello"
    assert e1.metadata["workspace"] == "/repo1"
    assert e1.metadata["tool"] == "example_jsonl"
    assert e1.time == datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)

    e2 = next(e for e in events if e.external_id == "c1:m2")
    assert e2.actor == "assistant"
    assert e2.intent == "Hi there"

    e3 = next(e for e in events if e.external_id == "c2:m3")
    assert e3.actor == "user"
    assert e3.intent == "How are you?"


def test_engine_json_and_zstd(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_json" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "json_sessions"
    sessions_dir.mkdir(parents=True)

    # Plain JSON
    f1 = sessions_dir / "chat1.json"
    doc1 = {
        "messages": [
            {"id": "msg1", "session_id": "s1", "sender": "user", "body": "Ping", "time": 1777636800},
            {"id": "msg2", "session_id": "s1", "sender": "bot", "body": "Pong", "time": 1777636805},
        ]
    }
    f1.write_text(json.dumps(doc1), encoding="utf-8")

    # Compressed JSON (.zst)
    f2 = sessions_dir / "chat2.json.zst"
    doc2 = {
        "messages": [
            {"id": "msg3", "session_id": "s2", "sender": "user", "body": "Status?", "time": 1777636900},
        ]
    }
    f2.write_bytes(_compress_zstd(json.dumps(doc2).encode("utf-8")))

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert err is None
    assert cursor is not None
    assert len(events) == 3

    assert {e.external_id for e in events} == {"s1:msg1", "s1:msg2", "s2:msg3"}
    assert all(e.actor in {"user", "assistant"} for e in events)


def test_engine_sqlite_and_watermark(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_sqlite" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "sqlite_sessions"
    sessions_dir.mkdir(parents=True)
    db_file = sessions_dir / "history.db"

    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE turns (id INTEGER PRIMARY KEY, thread_id TEXT, role_type TEXT, text_content TEXT, created_time TEXT, folder TEXT)"
    )
    conn.execute("INSERT INTO turns VALUES (1, 't1', 'human', 'first', '2026-05-01T12:00:00Z', '/p1')")
    conn.execute("INSERT INTO turns VALUES (2, 't1', 'assistant', 'second', '2026-05-01T12:00:05Z', '/p1')")
    conn.commit()
    conn.close()

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events1, cursor1, err1 = engine.run_recipe(recipe, history, db_file, 1, {})
    assert err1 is None
    assert len(events1) == 2
    assert cursor1.get("watermark") == 2

    # Second run without new rows yields 0 events
    events2, cursor2, err2 = engine.run_recipe(recipe, history, db_file, 1, cursor1)
    assert err2 is None
    assert len(events2) == 0

    # Insert row 3
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO turns VALUES (3, 't1', 'human', 'third', '2026-05-01T12:01:00Z', '/p1')")
    conn.commit()
    conn.close()

    events3, cursor3, err3 = engine.run_recipe(recipe, history, db_file, 1, cursor2)
    assert err3 is None
    assert len(events3) == 1
    assert events3[0].intent == "third"
    assert cursor3.get("watermark") == 3


# ── 4. Stable IDs, Append, and Shrink/Rewrite ─────────────────────────────────


def test_stable_ids_across_two_runs(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "s.jsonl"
    f.write_text(
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": "Text", "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events1, _, _ = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    events2, _, _ = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert len(events1) == 1
    assert len(events2) == 1
    assert events1[0].external_id == events2[0].external_id
    assert events1[0].artifact == events2[0].artifact
    assert events1[0].raw_ref == events2[0].raw_ref


def test_append_and_shrink_or_rewrite(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "s.jsonl"
    f.write_text(
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": "Line 1", "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events1, cursor1, _ = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert len(events1) == 1

    # Append
    with f.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"msg_id": "m2", "conv_id": "c1", "role": "ai", "content": "Line 2", "ts": "2026-05-01T10:00:05Z"}) + "\n")

    events2, cursor2, _ = engine.run_recipe(recipe, history, sessions_dir, 1, cursor1)
    assert len(events2) == 1
    assert events2[0].intent == "Line 2"

    # Truncate / rewrite
    f.write_text(
        json.dumps({"msg_id": "m3", "conv_id": "c1", "role": "human", "content": "Replaced", "ts": "2026-05-01T10:00:10Z"}) + "\n",
        encoding="utf-8",
    )
    events3, cursor3, _ = engine.run_recipe(recipe, history, sessions_dir, 1, cursor2)
    assert len(events3) == 1
    assert events3[0].intent == "Replaced"


# ── 5. Format Drift Probe ────────────────────────────────────────────────────


def test_format_drift_fails_closed(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "drift.jsonl"

    # 25 total lines: 4 valid, 21 malformed (rate = 4/25 = 0.16 < 0.90)
    lines = []
    for i in range(4):
        lines.append(json.dumps({"msg_id": f"m{i}", "conv_id": "c1", "role": "human", "content": f"ok {i}", "ts": "2026-05-01T10:00:00Z"}))
    for i in range(21):
        lines.append(json.dumps({"msg_id": f"bad{i}", "conv_id": "c1", "role": "human", "content": "", "ts": "invalid_ts"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert events == []
    assert cursor is None
    assert err is not None
    assert "format_drift" in err


# ── 6. SQLite Locked Snapshot Fallback & Blocked Refusal ─────────────────────


def test_locked_sqlite_snapshot_fallback(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_sqlite" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "sqlite_sessions"
    sessions_dir.mkdir(parents=True)
    db_file = sessions_dir / "active.db"

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, thread_id TEXT, role_type TEXT, text_content TEXT, created_time TEXT, folder TEXT)")
    conn.execute("INSERT INTO turns VALUES (1, 't1', 'human', 'locked row', '2026-05-01T12:00:00Z', '/p1')")
    conn.commit()

    # Simulate OperationalError on direct read
    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    orig_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        uri_str = str(args[0]) if args else str(kwargs.get("database", ""))
        if "mode=ro&immutable=1" in uri_str:
            raise sqlite3.OperationalError("database is locked")
        return orig_connect(*args, **kwargs)

    with patch("sqlite3.connect", side_effect=fake_connect):
        events, cursor, err = engine.run_recipe(recipe, history, db_file, 1, {})

    assert err is None
    assert len(events) == 1
    assert events[0].intent == "locked row"
    conn.close()


def test_blocked_sqlite_refused(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_sqlite" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "sqlite_sessions"
    sessions_dir.mkdir(parents=True)
    blocked_file = sessions_dir / "Cookies.sqlite"
    blocked_file.touch()

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, blocked_file, 1, {})
    assert events == []
    assert err is not None
    assert "blocked_sqlite" in err


# ── 7. Truncation of Long Content ────────────────────────────────────────────


def test_content_truncation_over_20000(tmp_path: Path):
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "long.jsonl"
    long_text = "A" * 25000
    f.write_text(
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": long_text, "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, _, _ = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert len(events) == 1
    assert len(events[0].intent) == 20000
    assert events[0].metadata["truncated"] == 25000


# ── 8. Plugin Discovery and Ingestion ─────────────────────────────────────────


def test_ai_tools_plugin_discovery_and_read(tmp_path: Path):
    init_db(tmp_path / "penpal.db")
    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "test.jsonl"
    f.write_text(
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": CANARY_SECRET, "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    # Put a test recipe dir with our fixture recipe
    catalog_dir = tmp_path / "test_catalog"
    catalog_dir.mkdir(parents=True)
    recipe_content = (FIXTURES_DIR / "example_jsonl" / "recipe.toml").read_text(encoding="utf-8")
    (catalog_dir / "example_jsonl.toml").write_text(recipe_content, encoding="utf-8")

    source = AiToolsSource(
        catalog_dir=catalog_dir,
        use_cursor=True,
        env={"TEST_DIR": str(tmp_path)},
    )

    instances = source.discover()
    assert len(instances) == 1
    assert instances[0].metadata["tool_id"] == "example_jsonl"

    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)))
    assert len(events) == 1
    assert events[0].intent == CANARY_SECRET
    source.commit_cursors()

    # Second read yields 0 events
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)))
    assert len(events2) == 0


# ── 9. Coverage CLI and Privacy Guarantee ────────────────────────────────────


def test_coverage_output_contains_no_message_content(tmp_path: Path):
    init_db(tmp_path / "penpal.db")
    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "test.jsonl"
    f.write_text(
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": CANARY_SECRET, "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    catalog_dir = tmp_path / "test_catalog"
    catalog_dir.mkdir(parents=True)
    recipe_content = (FIXTURES_DIR / "example_jsonl" / "recipe.toml").read_text(encoding="utf-8")
    (catalog_dir / "example_jsonl.toml").write_text(recipe_content, encoding="utf-8")

    # Ingest the event so it exists in DB
    source = AiToolsSource(catalog_dir=catalog_dir, use_cursor=True, env={"TEST_DIR": str(tmp_path)})
    inst = source.discover()[0]
    for e in source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)):
        # Persist event
        from penpal.sources.sink import persist_semantic_event
        persist_semantic_event(e)
    source.commit_cursors()

    # Run coverage function
    rows, totals = compute_tool_coverage(catalog_dir=catalog_dir, env={"TEST_DIR": str(tmp_path)})
    assert len(rows) == 1
    assert rows[0]["id"] == "example_jsonl"
    assert rows[0]["detected"] is True
    assert rows[0]["sessions_on_disk"] == 1
    assert rows[0]["ingested_sessions"] == 1
    assert totals["percentage"] == 100.0

    # Run CLI command
    runner = CliRunner()
    with patch("penpal.sources.coverage.compute_tool_coverage", return_value=(rows, totals)):
        result = runner.invoke(coverage_command, [])
        assert result.exit_code == 0
        output = result.output

        # CLI output MUST NOT contain CANARY_SECRET!
        assert CANARY_SECRET not in output
        assert "example_jsonl" in output
        assert "100.0%" in output

        # Test --json flag
        json_result = runner.invoke(coverage_command, ["--json"])
        assert json_result.exit_code == 0
        json_output = json_result.output
        assert CANARY_SECRET not in json_output
        parsed = json.loads(json_output)
        assert parsed["totals"]["ingested_sessions"] == 1


# ── 10. Manager Review Defect Tests (4–14) ───────────────────────────────────


def test_drift_probe_ignores_normal_non_message_lines(tmp_path: Path):
    """Defect 4: 100 lines with 90 tool/system non-message lines and 10 valid messages must not drift."""
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "chat.jsonl"

    lines = []
    # 90 non-message lines (e.g. system, tool_use, tool_result)
    for i in range(90):
        lines.append(json.dumps({"msg_id": f"t{i}", "conv_id": "c1", "role": "tool_call", "content": "ls", "ts": "2026-05-01T10:00:00Z"}))
    # 10 valid user/assistant message lines
    for i in range(10):
        lines.append(json.dumps({"msg_id": f"m{i}", "conv_id": "c1", "role": "human" if i % 2 == 0 else "ai", "content": f"msg {i}", "ts": "2026-05-01T10:00:00Z"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert err is None
    assert cursor is not None
    assert len(events) == 10


def test_drift_probe_trips_on_renamed_role_field(tmp_path: Path):
    """Defect 4: Total records >= 50 and recognized == 0 (e.g. renamed role field) must trip format_drift."""
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "renamed.jsonl"

    lines = []
    for i in range(55):
        # 'sender' instead of 'role'
        lines.append(json.dumps({"msg_id": f"m{i}", "conv_id": "c1", "sender": "human", "content": f"msg {i}", "ts": "2026-05-01T10:00:00Z"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert events == []
    assert cursor is None
    assert err is not None
    assert "format_drift" in err


def test_drift_after_probe_window_keeps_streaming_but_drops_cursor(tmp_path: Path):
    """Defect 5: When probe passes at 50 records but cumulative rate drops below min_parse_rate,

    events continue streaming but cursor is not committed and drift error is recorded.
    """
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "streaming_drift.jsonl"

    lines = []
    # 50 valid messages (probe window passes)
    for i in range(50):
        lines.append(json.dumps({"msg_id": f"m{i}", "conv_id": "c1", "role": "human", "content": f"valid {i}", "ts": "2026-05-01T10:00:00Z"}))
    # 60 failed records (missing created_at)
    for i in range(60):
        lines.append(json.dumps({"msg_id": f"bad{i}", "conv_id": "c1", "role": "human", "content": f"failing {i}"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    stream = engine.stream_recipe(recipe, history, sessions_dir, 1, {})
    emitted = list(stream)

    # 50 valid records were streamed
    assert len(emitted) == 50
    # Final rate was 50/110 < 0.90, so cursor is NOT committed and drift error is recorded
    assert engine.last_cursor is None
    assert engine.last_error is not None
    assert "format_drift" in engine.last_error


def test_record_index_bookkeeping_with_trailing_blanks(tmp_path: Path):
    """Defect 6: rec_idx must count blank/invalid lines so cursor does not move backwards."""
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    # Using .zst to test record_index cursor
    f = sessions_dir / "session.jsonl.zst"

    lines = [
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": "A", "ts": "2026-05-01T10:00:00Z"}),
        json.dumps({"msg_id": "m2", "conv_id": "c1", "role": "ai", "content": "B", "ts": "2026-05-01T10:00:05Z"}),
        "",
        "   ",
        "",
    ]
    f.write_bytes(_compress_zstd(("\n".join(lines) + "\n").encode("utf-8")))

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events1, cursor1, err1 = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert err1 is None
    assert len(events1) == 2
    # Must have consumed all 5 lines, not just 2
    state = cursor1["files"]["session.jsonl.zst"]
    assert state["record_index"] >= 5


def test_sqlite_requires_message_id_in_catalog():
    """Defect 7: format='sqlite' must require message_id in catalog fields."""
    with pytest.raises(ValueError, match="must include 'message_id' for sqlite format"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "sqlite"\nglob = "*.db"\n'
            'query = "SELECT thread_id, role, content, ts FROM turns"\n'
            'fields = { conversation_id = "thread_id", role = "role", content = "content", created_at = "ts" }'
        )


def test_sqlite_live_wal_reads_uncheckpointed_rows(tmp_path: Path):
    """Defect 8: A live database with uncheckpointed WAL rows must be read via WAL snapshot."""
    recipe = load_recipe(FIXTURES_DIR / "example_sqlite" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "sqlite_sessions"
    sessions_dir.mkdir(parents=True)
    db_file = sessions_dir / "wal_test.db"

    # Open with WAL mode and insert rows without checkpointing
    writer_conn = sqlite3.connect(db_file)
    writer_conn.execute("PRAGMA journal_mode=WAL;")
    writer_conn.execute(
        "CREATE TABLE turns (id INTEGER PRIMARY KEY, thread_id TEXT, role_type TEXT, text_content TEXT, created_time TEXT, folder TEXT)"
    )
    writer_conn.execute("INSERT INTO turns VALUES (1, 't1', 'human', 'wal row 1', '2026-05-01T12:00:00Z', '/p1')")
    writer_conn.commit()

    wal_file = sessions_dir / "wal_test.db-wal"
    assert wal_file.is_file()
    assert wal_file.stat().st_size > 0

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, db_file, 1, {})
    assert err is None
    assert len(events) == 1
    assert events[0].intent == "wal row 1"
    writer_conn.close()


def test_catalog_rejects_sql_injection_watermark_column():
    """Defect 9: sqlite_watermark column identifier must be strictly validated."""
    with pytest.raises(ValueError, match="invalid sqlite watermark column identifier"):
        load_recipe_from_string(
            'id = "test"\nname = "T"\nkind = "agent"\n[[history]]\nformat = "sqlite"\nglob = "*.db"\n'
            'query = "SELECT id, role, content, ts FROM turns"\n'
            'cursor = "sqlite_watermark:id; DROP TABLE turns"\n'
            'fields = { message_id = "id", role = "role", content = "content", created_at = "ts" }'
        )


def test_two_tools_sharing_root_cursor_isolation(tmp_path: Path):
    """Defect 10: Two recipes sharing the same glob root must have isolated cursors."""
    init_db(tmp_path / "penpal.db")
    shared_root = tmp_path / "shared"
    shared_root.mkdir(parents=True)

    # Tool A writes to a.jsonl, Tool B writes to b.jsonl under shared_root
    (shared_root / "a.jsonl").write_text(
        json.dumps({"msg_id": "a1", "conv_id": "c1", "role": "user", "content": "from A", "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    (shared_root / "b.jsonl").write_text(
        json.dumps({"msg_id": "b1", "conv_id": "c2", "role": "user", "content": "from B", "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "tool_a.toml").write_text(
        f'id = "tool_a"\nname = "Tool A"\nkind = "agent"\n'
        f'[detect]\nany = [{{ dir = "{shared_root}" }}]\n'
        f'[[history]]\nformat = "jsonl"\nglob = "{shared_root}/*.jsonl"\n'
        f'fields = {{ message_id = "msg_id", conversation_id = "conv_id", role = "role", content = "content", created_at = "ts" }}\n',
        encoding="utf-8",
    )
    (catalog_dir / "tool_b.toml").write_text(
        f'id = "tool_b"\nname = "Tool B"\nkind = "agent"\n'
        f'[detect]\nany = [{{ dir = "{shared_root}" }}]\n'
        f'[[history]]\nformat = "jsonl"\nglob = "{shared_root}/*.jsonl"\n'
        f'fields = {{ message_id = "msg_id", conversation_id = "conv_id", role = "role", content = "content", created_at = "ts" }}\n',
        encoding="utf-8",
    )

    source = AiToolsSource(catalog_dir=catalog_dir, use_cursor=True)
    instances = source.discover()
    assert len(instances) == 2
    # Ensure locators are distinct
    assert instances[0].locator != instances[1].locator

    # Read both
    now = datetime.now(timezone.utc)
    events_a = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    events_b = list(source.read(instances[1], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    source.commit_cursors()

    assert len(events_a) >= 1
    assert len(events_b) >= 1

    # Verify sync_cursors has 2 distinct rows
    conn = get_conn()
    cursor_rows = conn.execute("SELECT count(*) FROM sync_cursors JOIN sources ON sources.id = sync_cursors.source_id WHERE sources.type = 'ai_tools'").fetchone()[0]
    assert cursor_rows == 2


def test_coverage_per_tool_last_error_isolation(tmp_path: Path):
    """Defect 11: Error on Tool A must not be shown on Tool B in coverage."""
    init_db(tmp_path / "penpal.db")
    from penpal.store.sync_repo import ensure_source, record_sync_error

    # Record error for tool_a only
    src_a = ensure_source("ai_tools", "tool_a#0@/fake/a")
    record_sync_error(src_a, "format_drift: 0.50")
    # Tool b has no error
    ensure_source("ai_tools", "tool_b#0@/fake/b")

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "tool_a.toml").write_text(
        'id = "tool_a"\nname = "Tool A"\nkind = "agent"\nreadable = true\n',
        encoding="utf-8",
    )
    (catalog_dir / "tool_b.toml").write_text(
        'id = "tool_b"\nname = "Tool B"\nkind = "agent"\nreadable = true\n',
        encoding="utf-8",
    )

    rows, _ = compute_tool_coverage(catalog_dir=catalog_dir)
    by_id = {r["id"]: r for r in rows}
    assert by_id["tool_a"]["last_error"] == "format_drift 0.50"
    assert by_id["tool_b"]["last_error"] is None


def test_coverage_hides_schema_mismatch_once_another_history_delivers(tmp_path: Path):
    """A tool with histories for two versions shows no error when only the uninstalled version mismatches."""
    init_db(tmp_path / "penpal.db")
    from penpal.store.sync_repo import ensure_source, record_sync_error

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "test.jsonl").write_text(
        json.dumps({"msg_id": "m1", "conv_id": "c1", "role": "human", "content": "hi", "ts": "2026-05-01T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    catalog_dir = tmp_path / "test_catalog"
    catalog_dir.mkdir(parents=True)
    recipe_content = (FIXTURES_DIR / "example_jsonl" / "recipe.toml").read_text(encoding="utf-8")
    (catalog_dir / "example_jsonl.toml").write_text(recipe_content, encoding="utf-8")
    env = {"TEST_DIR": str(tmp_path)}

    # The other version's history has no matching table on this machine.
    record_sync_error(ensure_source("ai_tools", "example_jsonl#1@/fake/other.db"), "schema_mismatch")
    rows, _ = compute_tool_coverage(catalog_dir=catalog_dir, env=env)
    assert rows[0]["last_error"] == "schema_mismatch"

    source = AiToolsSource(catalog_dir=catalog_dir, use_cursor=True, env=env)
    inst = source.discover()[0]
    from penpal.sources.sink import persist_semantic_event
    for e in source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)):
        persist_semantic_event(e)
    source.commit_cursors()

    rows, _ = compute_tool_coverage(catalog_dir=catalog_dir, env=env)
    assert rows[0]["ingested_sessions"] == 1
    assert rows[0]["last_error"] is None


def test_coverage_sanitizes_last_error_no_raw_paths():
    """Defect 12: Coverage last_error must never leak raw paths or exceptions."""
    from penpal.sources.coverage import _sanitize_last_error

    assert _sanitize_last_error("format_drift: 0.42") == "format_drift 0.42"
    assert _sanitize_last_error("blocked_sqlite: sensitive:/Users/foo/secret/Cookies") == "blocked_sqlite"
    assert _sanitize_last_error("probe_error") == "probe_error"
    for label in ("schema_mismatch", "sqlite_locked", "zstd_unavailable"):
        assert _sanitize_last_error(label) == label
    assert _sanitize_last_error("sqlite3.OperationalError: /Users/foo/sensitive.db is locked") == "read_error"
    assert _sanitize_last_error(None) is None


def test_engine_count_sqlite_conversations_public_helper(tmp_path: Path):
    """Defect 13: Public helper count_sqlite_conversations uses COUNT(DISTINCT)."""
    recipe = load_recipe(FIXTURES_DIR / "example_sqlite" / "recipe.toml")
    history = recipe.history[0]

    db_file = tmp_path / "counts.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, thread_id TEXT, role_type TEXT, text_content TEXT, created_time TEXT, folder TEXT)")
    conn.execute("INSERT INTO turns VALUES (1, 'conv_1', 'human', 'a', '2026-05-01T12:00:00Z', '/p')")
    conn.execute("INSERT INTO turns VALUES (2, 'conv_1', 'assistant', 'b', '2026-05-01T12:00:01Z', '/p')")
    conn.execute("INSERT INTO turns VALUES (3, 'conv_2', 'human', 'c', '2026-05-01T12:00:02Z', '/p')")
    conn.commit()
    conn.close()

    engine = RecipeEngine()
    count = engine.count_sqlite_conversations(db_file, history)
    assert count == 2


# ── 11. Manager Review Round 2 Defect Tests (Items 3–8) ──────────────────────


def test_drift_probe_missing_content_is_normal_skip(tmp_path: Path):
    """Item 3: Missing content field (e.g. tool call only steps) is a normal skip, not a failure."""
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "antigravity_style.jsonl"

    lines = []
    # 30 records without content key (e.g. tool call steps)
    for i in range(30):
        lines.append(json.dumps({"msg_id": f"t{i}", "conv_id": "c1", "role": "human" if i % 2 == 0 else "ai", "ts": "2026-05-01T10:00:00Z"}))
    # 30 records with content
    for i in range(30):
        lines.append(json.dumps({"msg_id": f"m{i}", "conv_id": "c1", "role": "human" if i % 2 == 0 else "ai", "content": f"msg {i}", "ts": "2026-05-01T10:00:00Z"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert err is None
    assert len(events) == 30


def test_drift_probe_recognized_without_content_trips_no_content(tmp_path: Path):
    """Item 3: Recognized >= 50 and none produced non-empty content trips format_drift: no_content."""
    recipe = load_recipe(FIXTURES_DIR / "example_jsonl" / "recipe.toml")
    history = recipe.history[0]

    sessions_dir = tmp_path / "jsonl_sessions"
    sessions_dir.mkdir(parents=True)
    f = sessions_dir / "no_content.jsonl"

    lines = []
    for i in range(55):
        lines.append(json.dumps({"msg_id": f"m{i}", "conv_id": "c1", "role": "human", "ts": "2026-05-01T10:00:00Z"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    engine = RecipeEngine(env={"TEST_DIR": str(tmp_path)})
    events, cursor, err = engine.run_recipe(recipe, history, sessions_dir, 1, {})
    assert err == "format_drift: no_content"
    assert events == []
    assert cursor is None


def test_catalog_file_mtime_timestamp_allows_omitting_created_at(tmp_path: Path):
    """Item 4: timestamp = 'file_mtime' allows fields.created_at to be omitted."""
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "cursor_style.toml"
    f.write_text(
        'id = "cursor_style"\nname = "Cursor"\nkind = "ide"\nreadable = true\n'
        '[detect]\nany = []\n'
        '[[history]]\nformat = "jsonl"\nglob = "/tmp/fake/*.jsonl"\n'
        'timestamp = "file_mtime"\n'
        'fields = { role = "role", content = "content" }\n',
        encoding="utf-8",
    )
    recipes = load_catalog(catalog_dir)
    assert len(recipes) == 1
    assert recipes[0].history[0].timestamp == "file_mtime"


def test_catalog_requires_created_at_when_not_file_mtime(tmp_path: Path):
    """Item 4: created_at is required unless timestamp is 'file_mtime'."""
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "bad.toml"
    f.write_text(
        'id = "bad"\nname = "Bad"\nkind = "ide"\nreadable = true\n'
        '[detect]\nany = []\n'
        '[[history]]\nformat = "jsonl"\nglob = "/tmp/fake/*.jsonl"\n'
        'timestamp = "auto"\n'
        'fields = { role = "role", content = "content" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required keys: .*created_at"):
        load_catalog(catalog_dir)


def test_engine_file_mtime_timestamp_uses_stat_mtime(tmp_path: Path):
    """Item 4: file_mtime uses file mtime as aware UTC event timestamp."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    jsonl_f = sessions_dir / "t.jsonl"
    jsonl_f.write_text(json.dumps({"role": "user", "content": "hello from mtime"}) + "\n", encoding="utf-8")
    expected_ts = datetime.fromtimestamp(jsonl_f.stat().st_mtime, tz=timezone.utc)

    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "tool.toml").write_text(
        'id = "tool"\nname = "Tool"\nkind = "agent"\nreadable = true\n'
        f'[detect]\nany = [{{ dir = "{sessions_dir}" }}]\n'
        '[[history]]\nformat = "jsonl"\n'
        f'glob = "{sessions_dir}/*.jsonl"\n'
        'timestamp = "file_mtime"\n'
        'fields = { role = "role", content = "content" }\n',
        encoding="utf-8",
    )
    recipe = load_catalog(catalog_dir)[0]
    engine = RecipeEngine()
    events, _, _ = engine.run_recipe(recipe, recipe.history[0], sessions_dir, 1, {})
    assert len(events) == 1
    assert events[0].time == expected_ts


def test_get_dot_path_empty_returns_none():
    """Item 5: get_dot_path(record, '') returns None, never whole record."""
    from penpal.sources.recipes.engine import get_dot_path

    assert get_dot_path({"workspace": "/tmp"}, "") is None
    assert get_dot_path({"workspace": "/tmp"}, "workspace") == "/tmp"


def test_catalog_drops_empty_optional_fields_at_load_time(tmp_path: Path):
    """Item 5: Empty string optional fields are dropped at load time."""
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "empty_opt.toml"
    f.write_text(
        'id = "opt"\nname = "Opt"\nkind = "agent"\nreadable = true\n'
        '[detect]\nany = []\n'
        '[[history]]\nformat = "jsonl"\nglob = "/tmp/fake/*.jsonl"\n'
        'fields = { role = "role", content = "content", created_at = "ts", workspace = "", message_id = "" }\n',
        encoding="utf-8",
    )
    recipes = load_catalog(catalog_dir)
    assert len(recipes) == 1
    fields = recipes[0].history[0].fields
    assert "workspace" not in fields
    assert "message_id" not in fields


def test_catalog_rejects_empty_required_fields(tmp_path: Path):
    """Item 5: Required fields must be non-empty strings."""
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "bad_req.toml"
    f.write_text(
        'id = "bad"\nname = "Bad"\nkind = "agent"\nreadable = true\n'
        '[detect]\nany = []\n'
        '[[history]]\nformat = "jsonl"\nglob = "/tmp/fake/*.jsonl"\n'
        'fields = { role = "", content = "content", created_at = "ts" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a non-empty string"):
        load_catalog(catalog_dir)


def test_extract_content_accepts_input_text_and_output_text():
    """Item 6: extract_content accepts input_text and output_text."""
    from penpal.sources.recipes.engine import extract_content

    parts = [
        {"type": "input_text", "text": "Prompt line"},
        {"type": "output_text", "text": "Response line"},
        {"text": "Untyped text"},
        {"type": "tool_call", "text": "ignored"},
    ]
    extracted = extract_content(parts)
    assert extracted == "Prompt line\nResponse line\nUntyped text"


def test_catalog_query_allows_semicolons_in_string_literals(tmp_path: Path):
    """Item 7: Semicolons inside string literals do not trip the multi-statement check."""
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "cursor.toml"
    f.write_text(
        'id = "cursor"\nname = "Cursor"\nkind = "ide"\nreadable = true\n'
        '[detect]\nany = []\n'
        '[[history]]\nformat = "sqlite"\nglob = "/tmp/fake.db"\n'
        'query = "SELECT id, role, content, ts FROM turns WHERE key < \'bubbleId;\' ORDER BY id ASC"\n'
        'fields = { message_id = "id", conversation_id = "id", role = "role", content = "content", created_at = "ts" }\n',
        encoding="utf-8",
    )
    recipes = load_catalog(catalog_dir)
    assert len(recipes) == 1
    assert "bubbleId;" in recipes[0].history[0].query


def test_engine_sqlite_schema_mismatch_yields_nothing_and_records_error(tmp_path: Path):
    """Item 7: SQLite OperationalError for no such table records schema_mismatch, yields nothing."""
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE existing (id INTEGER)")
    conn.commit()
    conn.close()

    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "mismatch.toml"
    f.write_text(
        'id = "mismatch"\nname = "Mismatch"\nkind = "agent"\nreadable = true\n'
        f'[detect]\nany = [{{ dir = "{tmp_path}" }}]\n'
        '[[history]]\nformat = "sqlite"\n'
        f'glob = "{db_file}"\n'
        'query = "SELECT id, role, content, ts FROM missing_table"\n'
        'fields = { message_id = "id", conversation_id = "id", role = "role", content = "content", created_at = "ts" }\n',
        encoding="utf-8",
    )
    recipe = load_catalog(catalog_dir)[0]
    engine = RecipeEngine()
    events, cursor, err = engine.run_recipe(recipe, recipe.history[0], db_file, 1, {})
    assert events == []
    assert err == "schema_mismatch"


def test_engine_sqlite_locked_over_512mb_does_not_snapshot(tmp_path: Path):
    """Item 7: SQLite database locked over 512MB records sqlite_locked and does not snapshot."""
    db_file = tmp_path / "huge.db"
    db_file.write_bytes(b"dummy")

    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir(parents=True)
    f = catalog_dir / "huge.toml"
    f.write_text(
        'id = "huge"\nname = "Huge"\nkind = "agent"\nreadable = true\n'
        f'[detect]\nany = [{{ dir = "{tmp_path}" }}]\n'
        '[[history]]\nformat = "sqlite"\n'
        f'glob = "{db_file}"\n'
        'query = "SELECT id, role, content, ts FROM t"\n'
        'fields = { message_id = "id", conversation_id = "id", role = "role", content = "content", created_at = "ts" }\n',
        encoding="utf-8",
    )
    recipe = load_catalog(catalog_dir)[0]
    engine = RecipeEngine()

    from unittest.mock import patch
    with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")), \
         patch("pathlib.Path.stat") as mock_stat, \
         patch("shutil.copy2") as mock_copy:
        import os
        mock_stat.return_value = os.stat_result((0, 0, 0, 0, 0, 0, 600 * 1024 * 1024, 0, 0, 0))
        events, cursor, err = engine.run_recipe(recipe, recipe.history[0], db_file, 1, {})
        assert events == []
        assert err == "sqlite_locked"
        mock_copy.assert_not_called()


def test_coverage_plain_output_contains_unreadable_note():
    """Item 8: penpal sources coverage plain output contains note about unreadable tools."""
    from click.testing import CliRunner
    from unittest.mock import patch
    from penpal.sources.coverage import coverage_command

    rows = [{"id": "foo", "detected": True, "readable": False, "sessions_on_disk": 0, "ingested_sessions": 0, "last_error": None}]
    totals = {"sessions_on_disk": 0, "ingested_sessions": 0, "percentage": 0.0}

    runner = CliRunner()
    with patch("penpal.sources.coverage.compute_tool_coverage", return_value=(rows, totals)):
        result = runner.invoke(coverage_command, [])
        assert result.exit_code == 0
        assert "tools marked not readable are excluded from percentage calculation" in result.output.lower()


