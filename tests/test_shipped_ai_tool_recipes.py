"""Regression tests for shipped AI tool recipes (Manager round 4)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from penpal.sources.coverage import compute_tool_coverage
from penpal.sources.plugins.ai_tools import AiToolsSource
from penpal.sources.recipes.catalog import load_catalog
from penpal.sources.types import SemanticEvent
from penpal.store.db import init_db


def _assert_metadata_hygiene(events: list[SemanticEvent]) -> None:
    """Regression check: no metadata value equals or contains stringified records from empty paths."""
    for e in events:
        for k, v in e.metadata.items():
            assert not isinstance(v, (dict, list)), f"metadata[{k!r}] must not be dict/list: {v!r}"
            assert "{" not in str(v), f"metadata[{k!r}] contains stringified dict: {v!r}"


# ── 1. Antigravity ────────────────────────────────────────────────────────────


def test_shipped_recipe_antigravity(tmp_path: Path):
    """Antigravity: brain/<uuid>/.system_generated/logs/transcript.jsonl."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    logs_dir = home / ".gemini" / "antigravity" / "brain" / "00000000-0000-0000-0000-000000000001" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript.jsonl"

    lines = [
        json.dumps({"type": "USER_INPUT", "content": "How do I build a rocket?", "created_at": "2026-10-01T10:00:00Z"}),
        # Noise: tool step without content key
        json.dumps({"type": "GENERIC", "created_at": "2026-10-01T10:00:01Z", "tool_calls": [{"name": "search", "args": {}}]}),
        # Noise: runtime note
        json.dumps({"type": "SYSTEM_NOTE", "content": "Model switched to pro", "created_at": "2026-10-01T10:00:02Z"}),
        json.dumps({"type": "PLANNER_RESPONSE", "content": "Start with fuel chemistry.", "created_at": "2026-10-01T10:00:05Z"}),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "antigravity"]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 2
    assert [e.actor for e in events] == ["user", "assistant"]

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 1
    assert "How do I build a rocket?" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


# ── 2. Cursor (history 1 & history 2) ─────────────────────────────────────────


def test_shipped_recipe_cursor_history_1(tmp_path: Path):
    """Cursor history 1: agent transcripts JSONL with timestamp = file_mtime."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    chat_dir = home / ".cursor" / "projects" / "my_project" / "agent-transcripts" / "chat_1"
    chat_dir.mkdir(parents=True)
    chat_file = chat_dir / "chat_1.jsonl"

    lines = [
        json.dumps({"role": "user", "message": {"content": [{"type": "text", "text": "Fix this bug in python"}]}}),
        json.dumps({"role": "assistant", "message": {"content": [{"type": "text", "text": "Here is the fixed code"}]}}),
    ]
    chat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mtime = 1770000000.0
    os.utime(chat_file, (mtime, mtime))
    expected_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "cursor" and i.metadata.get("history_idx") == 0]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 2
    assert [e.actor for e in events] == ["user", "assistant"]
    assert all(e.time == expected_ts for e in events)

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 1
    assert "Fix this bug in python" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


def test_shipped_recipe_cursor_history_2(tmp_path: Path):
    """Cursor history 2: state.vscdb cursorDiskKV table."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    storage_dir = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
    storage_dir.mkdir(parents=True)
    db_file = storage_dir / "state.vscdb"

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    conn.execute("INSERT INTO ItemTable VALUES ('some_key', 'some_val')")

    comp1 = "11111111-1111-1111-1111-111111111111"
    comp2 = "22222222-2222-2222-2222-222222222222"

    rows = [
        (f"bubbleId:{comp1}:b1", json.dumps({"type": 1, "text": "Cursor query prompt 1", "createdAt": "2026-10-01T10:00:00.000Z"})),
        (f"bubbleId:{comp1}:b_noise", json.dumps({"type": 2, "text": "", "createdAt": "2026-10-01T10:00:02.000Z"})),  # tool step (empty text)
        (f"bubbleId:{comp1}:b2", json.dumps({"type": 2, "text": "Cursor query answer 1", "createdAt": "2026-10-01T10:00:05.000Z"})),
        (f"bubbleId:{comp2}:b3", json.dumps({"type": 1, "text": "Cursor query prompt 2", "createdAt": "2026-10-01T10:01:00.000Z"})),
        (f"bubbleId:{comp2}:b4", json.dumps({"type": 2, "text": "Cursor query answer 2", "createdAt": "2026-10-01T10:01:05.000Z"})),
        (f"composerData:{comp1}", b"unrelated composer blob"),  # noise
    ]
    for k, v in rows:
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "cursor" and i.metadata.get("history_idx") == 1]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 4
    assert [e.actor for e in events] == ["user", "assistant", "user", "assistant"]

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 2
    assert comp1 in conv_ids and comp2 in conv_ids
    assert "Cursor query prompt 1" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


# ── 3. WorkBuddy ──────────────────────────────────────────────────────────────


def test_shipped_recipe_workbuddy(tmp_path: Path):
    """WorkBuddy: projects/*/*.jsonl and subagents/*/*.jsonl."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    proj_dir = home / ".workbuddy" / "projects" / "my_project"
    sub_dir = proj_dir / "sess1" / "subagents"
    sub_dir.mkdir(parents=True)

    main_lines = [
        json.dumps({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Workbuddy user prompt"}],
            "sessionId": "wb-sess-1",
            "id": "m1",
            "timestamp": 1770000000000,
            "cwd": "/workspace/workbuddy",
        }),
        json.dumps({"type": "tool_call", "id": "t1", "timestamp": 1770000002000}),  # noise (no role)
        json.dumps({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Workbuddy asst reply"}],
            "sessionId": "wb-sess-1",
            "id": "m2",
            "timestamp": 1770000005000,
            "cwd": "/workspace/workbuddy",
        }),
    ]
    (proj_dir / "sess1.jsonl").write_text("\n".join(main_lines) + "\n", encoding="utf-8")

    sub_lines = [
        json.dumps({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Subagent user query"}],
            "sessionId": "wb-sub-1",
            "id": "sm1",
            "timestamp": 1770000010000,
            "cwd": "/workspace/workbuddy",
        }),
        json.dumps({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Subagent asst answer"}],
            "sessionId": "wb-sub-1",
            "id": "sm2",
            "timestamp": 1770000015000,
            "cwd": "/workspace/workbuddy",
        }),
    ]
    (sub_dir / "agent1.jsonl").write_text("\n".join(sub_lines) + "\n", encoding="utf-8")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "workbuddy"]
    assert len(instances) == 2

    now = datetime.now(timezone.utc)
    events: list[SemanticEvent] = []
    for inst in instances:
        events.extend(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), now))

    assert len(events) == 4
    assert {e.actor for e in events} == {"user", "assistant"}

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 2
    assert "wb-sess-1" in conv_ids and "wb-sub-1" in conv_ids
    assert "Workbuddy user prompt" in [e.intent for e in events]
    assert all(e.metadata.get("workspace") == "/workspace/workbuddy" for e in events)
    # Same project keys as Claude Code / Codex, so project bucket rules apply to AI tool events.
    assert all(e.metadata.get("cwd") == "/workspace/workbuddy" and e.metadata.get("project") == "workbuddy" for e in events)
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2: list[SemanticEvent] = []
    for inst in instances:
        events2.extend(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


# ── 4. Kiro ───────────────────────────────────────────────────────────────────


def test_shipped_recipe_kiro(tmp_path: Path):
    """Kiro: workspace-sessions/*/*.json with sessions.json noise list."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    ws_dir = home / "Library" / "Application Support" / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent" / "workspace-sessions" / "b64dir"
    ws_dir.mkdir(parents=True)

    session_doc = {
        "sessionId": "kiro-s1",
        "title": "Kiro Session",
        "workspaceDirectory": "/workspace/kiro",
        "history": [
            {
                "message": {
                    "id": "k1",
                    "role": "user",
                    "content": "Kiro user question",
                }
            },
            {
                "message": {
                    "id": "k2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Kiro assistant answer"}],
                }
            },
        ],
    }
    (ws_dir / "session1.json").write_text(json.dumps(session_doc), encoding="utf-8")

    # Noise: sessions.json whose top-level is a list (must yield nothing and not raise)
    sessions_list = [
        {"sessionId": "s1", "title": "Index title", "dateCreated": "2026-10-01", "workspaceDirectory": "/workspace/kiro"}
    ]
    (ws_dir / "sessions.json").write_text(json.dumps(sessions_list), encoding="utf-8")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "kiro"]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 2
    assert [e.actor for e in events] == ["user", "assistant"]

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 1
    assert "Kiro user question" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


# ── 5. OpenCode (history 1 & 2.x) ─────────────────────────────────────────────


def test_shipped_recipe_opencode_history_1(tmp_path: Path):
    """OpenCode history 1: 1.x tables (part, message, session) with 2.x missing as schema_mismatch."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_file = db_dir / "opencode.db"

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE session (id TEXT, directory TEXT)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, data TEXT)")

    conn.execute("INSERT INTO session VALUES ('s1', '/workspace/opencode')")
    conn.execute(
        "INSERT INTO message VALUES ('m1', 's1', 1770000000000, ?)",
        (json.dumps({"role": "user", "time": {"created": 1770000000000}}),),
    )
    conn.execute(
        "INSERT INTO message VALUES ('m2', 's1', 1770000005000, ?)",
        (json.dumps({"role": "assistant", "time": {"created": 1770000005000}}),),
    )

    conn.execute("INSERT INTO part VALUES ('p1', 'm1', ?)", (json.dumps({"type": "text", "text": "Opencode 1.x user"}),))
    conn.execute("INSERT INTO part VALUES ('p2', 'm2', ?)", (json.dumps({"type": "text", "text": "Opencode 1.x asst"}),))
    # Noise parts
    conn.execute("INSERT INTO part VALUES ('p3', 'm2', ?)", (json.dumps({"type": "tool", "call": "sh"}),))
    conn.execute("INSERT INTO part VALUES ('p4', 'm2', ?)", (json.dumps({"type": "text", "text": "synth", "synthetic": True}),))
    conn.commit()
    conn.close()

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "opencode"]
    assert len(instances) == 2

    inst_h1 = next(i for i in instances if i.metadata.get("history_idx") == 0)
    inst_h2 = next(i for i in instances if i.metadata.get("history_idx") == 1)

    now = datetime.now(timezone.utc)
    events_h1 = list(source.read(inst_h1, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events_h1) == 2
    assert [e.actor for e in events_h1] == ["user", "assistant"]
    assert "Opencode 1.x user" in [e.intent for e in events_h1]
    _assert_metadata_hygiene(events_h1)

    # History 2 lacks 2.x tables: must not raise and must record schema_mismatch
    events_h2 = list(source.read(inst_h2, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events_h2) == 0
    assert source._engine.last_error == "schema_mismatch"

    source.commit_cursors()
    events_h1_again = list(source.read(inst_h1, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events_h1_again) == 0


def test_shipped_recipe_opencode_2x(tmp_path: Path):
    """OpenCode 2.x: only session_v2 and session_message present."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_file = db_dir / "opencode.db"

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT)")
    conn.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")

    conn.execute("INSERT INTO session_v2 VALUES ('s2', '/workspace/opencode2')")
    conn.execute(
        "INSERT INTO session_message VALUES ('sm1', 's2', 'user', 1770000000000, ?)",
        (json.dumps({"text": "Opencode 2.x user text"}),),
    )
    conn.execute(
        "INSERT INTO session_message VALUES ('sm2', 's2', 'assistant', 1770000005000, ?)",
        (json.dumps({"content": [{"type": "text", "text": "Opencode 2.x asst text"}, {"type": "tool"}]}),),
    )
    conn.commit()
    conn.close()

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "opencode"]
    assert len(instances) == 2

    inst_h1 = next(i for i in instances if i.metadata.get("history_idx") == 0)
    inst_h2 = next(i for i in instances if i.metadata.get("history_idx") == 1)

    now = datetime.now(timezone.utc)
    # History 1 lacks 1.x tables: must not raise and records schema_mismatch
    events_h1 = list(source.read(inst_h1, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events_h1) == 0
    assert source._engine.last_error == "schema_mismatch"

    # History 2 yields the user and assistant turns
    events_h2 = list(source.read(inst_h2, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events_h2) == 2
    assert [e.actor for e in events_h2] == ["assistant", "user"] or set([e.actor for e in events_h2]) == {"user", "assistant"}
    intents = [e.intent for e in events_h2]
    assert "Opencode 2.x user text" in intents
    assert "Opencode 2.x asst text" in intents
    _assert_metadata_hygiene(events_h2)

    source.commit_cursors()
    events_h2_again = list(source.read(inst_h2, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events_h2_again) == 0


# ── 6. Hermes Agent ───────────────────────────────────────────────────────────


def test_shipped_recipe_hermes(tmp_path: Path):
    """Hermes Agent: state.db and profile state.db with watermark advancement."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    hermes_dir = home / ".hermes"
    profile_dir = hermes_dir / "profiles" / "work"
    profile_dir.mkdir(parents=True)

    def _setup_db(path: Path, sess_id: str, cwd: str, pfx: str):
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER, compacted INTEGER)"
        )
        conn.execute("CREATE TABLE sessions (id TEXT, cwd TEXT)")
        conn.execute("INSERT INTO sessions VALUES (?, ?)", (sess_id, cwd))
        rows = [
            (1, sess_id, "user", f"{pfx} user prompt", 1770000000.0, 1, 0),
            (2, sess_id, "assistant", f"{pfx} asst response", 1770000005.0, 1, 0),
            (3, sess_id, "tool", "tool result", 1770000002.0, 1, 0),  # noise
            (4, sess_id, "user", "inactive msg", 1770000003.0, 0, 0),  # noise (active=0, compacted=0)
            (5, sess_id, "assistant", "", 1770000004.0, 1, 0),  # noise (empty content)
        ]
        for r in rows:
            conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", r)
        conn.commit()
        conn.close()

    main_db = hermes_dir / "state.db"
    profile_db = profile_dir / "state.db"
    _setup_db(main_db, "sess_main", "/workspace/hermes", "Main")
    _setup_db(profile_db, "sess_work", "/workspace/hermes/work", "Work")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "hermes"]
    assert len(instances) == 2

    now = datetime.now(timezone.utc)
    events: list[SemanticEvent] = []
    for inst in instances:
        events.extend(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), now))

    assert len(events) == 4
    assert {e.actor for e in events} == {"user", "assistant"}

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 2
    assert "sess_main" in conv_ids and "sess_work" in conv_ids
    assert "Main user prompt" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()

    # Insert one new message into main_db
    conn = sqlite3.connect(main_db)
    conn.execute(
        "INSERT INTO messages VALUES (6, 'sess_main', 'user', 'Main incremental message', 1770000020.0, 1, 0)"
    )
    conn.commit()
    conn.close()

    inst_main = next(i for i in instances if "profiles" not in i.locator)
    new_events = list(source.read(inst_main, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(new_events) == 1
    assert new_events[0].intent == "Main incremental message"


# ── 7. Pi ─────────────────────────────────────────────────────────────────────


def test_shipped_recipe_pi(tmp_path: Path):
    """Pi agent: sessions/<slug>/<file>.jsonl with 2 files -> 2 conversations."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    sessions_dir = home / ".pi" / "agent" / "sessions" / "cwd-slug"
    sessions_dir.mkdir(parents=True)

    file_1 = sessions_dir / "20261001_s1.jsonl"
    lines_1 = [
        json.dumps({"type": "session", "id": "s1", "timestamp": "2026-10-01T10:00:00Z", "cwd": "/workspace/pi"}),
        json.dumps({"type": "message", "timestamp": "2026-10-01T10:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "Pi prompt 1"}]}}),
        json.dumps({"type": "message", "timestamp": "2026-10-01T10:00:02Z", "message": {"role": "toolResult", "content": [{"type": "text", "text": "tool output"}]}}),  # noise
        json.dumps({"type": "message", "timestamp": "2026-10-01T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Pi answer 1"}, {"type": "toolCall", "tool": "sh"}]}}),
    ]
    file_1.write_text("\n".join(lines_1) + "\n", encoding="utf-8")

    file_2 = sessions_dir / "20261001_s2.jsonl"
    lines_2 = [
        json.dumps({"type": "session", "id": "s2", "timestamp": "2026-10-01T10:01:00Z", "cwd": "/workspace/pi"}),
        json.dumps({"type": "message", "timestamp": "2026-10-01T10:01:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "Pi prompt 2"}]}}),
        json.dumps({"type": "message", "timestamp": "2026-10-01T10:01:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Pi answer 2"}]}}),
    ]
    file_2.write_text("\n".join(lines_2) + "\n", encoding="utf-8")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "pi"]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 4
    assert [e.actor for e in events] == ["user", "assistant", "user", "assistant"]

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 2
    assert "Pi prompt 1" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


# ── 8. Cline (history 1 & history 2) ──────────────────────────────────────────


def test_shipped_recipe_cline_history_1(tmp_path: Path):
    """Cline history 1: ~/.cline/data/sessions/<id>/<id>.messages.json."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    sess_dir = home / ".cline" / "data" / "sessions" / "sess1"
    sess_dir.mkdir(parents=True)
    msg_file = sess_dir / "sess1.messages.json"

    data = {
        "messages": [
            {"id": "c1", "role": "user", "content": [{"type": "text", "text": "Cline user text"}], "ts": 1770000000000},
            {"id": "c2", "role": "assistant", "content": [{"type": "text", "text": "Cline asst text"}], "ts": 1770000005000},
        ]
    }
    msg_file.write_text(json.dumps(data), encoding="utf-8")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "cline" and i.metadata.get("history_idx") == 0]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 2
    assert [e.actor for e in events] == ["user", "assistant"]

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 1
    assert "Cline user text" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


def test_shipped_recipe_cline_history_2(tmp_path: Path):
    """Cline history 2: VS Code extension tasks/*/api_conversation_history.json."""
    init_db(tmp_path / "penpal.db")
    home = tmp_path / "home"
    task_dir = home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" / "task1"
    task_dir.mkdir(parents=True)
    hist_file = task_dir / "api_conversation_history.json"

    data = [
        {"role": "user", "content": "VSCode cline user text", "ts": 1770000010000},
        {"role": "assistant", "content": [{"type": "text", "text": "VSCode cline asst text"}], "ts": 1770000015000},
    ]
    hist_file.write_text(json.dumps(data), encoding="utf-8")

    source = AiToolsSource(use_cursor=True, env={"HOME": str(home)})
    instances = [i for i in source.discover() if i.metadata.get("tool_id") == "cline" and i.metadata.get("history_idx") == 1]
    assert len(instances) == 1

    now = datetime.now(timezone.utc)
    events = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events) == 2
    assert [e.actor for e in events] == ["user", "assistant"]

    conv_ids = {e.metadata.get("conversation_id") for e in events}
    assert len(conv_ids) == 1
    assert "VSCode cline user text" in [e.intent for e in events]
    _assert_metadata_hygiene(events)

    source.commit_cursors()
    events2 = list(source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(events2) == 0


# ── 9. Catalog & Coverage Invariants ──────────────────────────────────────────


def test_shipped_catalog_loads_exactly_15_ids():
    """Shipped catalog loads exactly 15 ids (8 readable + 5 unreadable + 2 adapters)."""
    recipes = load_catalog()
    ids = {r.id for r in recipes}
    expected = {
        "antigravity",
        "cursor",
        "workbuddy",
        "kiro",
        "opencode",
        "hermes",
        "pi",
        "cline",
        "claude_desktop",
        "chatgpt",
        "doubao",
        "kimi",
        "deepseek",
        "claude_code",
        "codex_cli",
    }
    assert ids == expected
    assert len(recipes) == 15


def test_shipped_catalog_unreadable_entries():
    """Every readable = false entry has non-empty unreadable_reason and no history."""
    recipes = load_catalog()
    unreadable = [r for r in recipes if not r.readable]
    assert len(unreadable) == 5
    for r in unreadable:
        assert isinstance(r.unreadable_reason, str) and r.unreadable_reason.strip()
        assert len(r.history) == 0


def test_shipped_coverage_unreadable_doubao_excluded_from_totals(tmp_path: Path):
    """compute_tool_coverage on tmp HOME with fake Doubao dir lists doubao as unreadable and leaves it out of totals."""
    init_db(tmp_path / "penpal.db")
    doubao_dir = tmp_path / "Library" / "Application Support" / "Doubao"
    doubao_dir.mkdir(parents=True)

    rows, totals = compute_tool_coverage(env={"HOME": str(tmp_path), "PATH": ""})
    by_id = {r["id"]: r for r in rows}
    assert "doubao" in by_id
    doubao = by_id["doubao"]
    assert doubao["detected"] is True
    assert doubao["readable"] is False
    assert doubao["sessions_on_disk"] == 0
    assert doubao["ingested_sessions"] == 0
    assert totals["sessions_on_disk"] == 0
    assert totals["ingested_sessions"] == 0
    assert totals["percentage"] == 0.0


def test_ai_tools_events_default_to_the_work_bucket():
    """Captured AI tool chats must land in a shareable bucket, or no granted host can read them."""
    from penpal.store.buckets import default_bucket_for_source

    assert default_bucket_for_source("ai_tools", overrides={}) == default_bucket_for_source("codex_cli", overrides={}) == "Work"
