import json
import sqlite3

import pytest
from click.testing import CliRunner

from penpal.cli import main
from penpal.mcp import auth, memory_tools, tools
from penpal.memory.memmy_import import import_memmy
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v36 import migrate_v36
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event


@pytest.fixture
def databases(tmp_path):
    close()
    init_db(tmp_path / "penpal.sqlite")
    source = tmp_path / "source.sqlite"
    conn = sqlite3.connect(source)
    conn.execute("""CREATE TABLE memories (id TEXT, timeline TEXT, user_id TEXT, conversation_id TEXT,
        session_id TEXT, agent_id TEXT, app_id TEXT, memory_type TEXT, status TEXT, visibility TEXT,
        memory_key TEXT, memory_value TEXT, tags_json TEXT, info_json TEXT, properties_json TEXT,
        memory_layer TEXT, content_hash TEXT, version INTEGER, created_at TEXT, updated_at TEXT, deleted_at TEXT)""")
    conn.executemany("INSERT INTO memories(id,agent_id,memory_value,info_json,memory_layer,status,created_at) VALUES (?,?,?,?,?,?,?)", [
        ("1", "codex", "detail apple\n" + "kp_" + "x" * 40, json.dumps({"summary": "apple\nsummary"}), "L1", "activated", "2026-01-01"),
        ("2", "memmy-agent", "banana detail", json.dumps({"title": "banana title"}), "Skill", "activated", "2026-01-02"),
        ("3", "cursor", "inactive", "{}", "L1", "deleted", "2026-01-01"),
        ("4", "cursor", "other", "{}", "L2", "activated", "2026-01-01"),
        ("5", "cursor", "", "{}", "L1", "activated", "2026-01-01"),
    ])
    conn.commit()
    conn.close()
    yield source
    close()


def test_import_repeat_dry_run_and_index(databases):
    conn = get_conn()
    before = import_memmy(databases, conn, dry_run=True)
    assert (before["eligible"], before["imported"], before["invalid"], before["redacted"]) == (3, 2, 1, 1)
    assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    assert import_memmy(databases, conn) == before
    rows = conn.execute("SELECT * FROM memories ORDER BY external_id").fetchall()
    assert (rows[0]["type"], rows[0]["content"], rows[0]["source_tool"], rows[0]["scope"]) == ("fact", "apple summary", "codex_cli", "global")
    assert "kp_" not in rows[0]["detail"] and "[REDACTED]" in rows[0]["detail"]
    assert (rows[1]["type"], rows[1]["content"], rows[1]["source_tool"]) == ("procedure", "banana title", "memmy")
    assert rows[0]["created_at"] == rows[0]["valid_from"] == "2026-01-01"
    assert conn.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'detail'").fetchone()[0] == 2
    migrate_v36(conn)
    assert conn.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'detail'").fetchone()[0] == 2
    again = import_memmy(databases, conn)
    assert (again["imported"], again["existing"], again["invalid"]) == (0, 2, 1)
    assert sum(again[key] for key in ("imported", "existing", "invalid")) == again["eligible"]


def test_import_errors(databases, tmp_path):
    missing = tmp_path / "empty.sqlite"
    sqlite3.connect(missing).close()
    with pytest.raises(ValueError, match="memories table"):
        import_memmy(missing, get_conn())
    lock = sqlite3.connect(databases)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(ValueError, match="locked"):
            import_memmy(databases, get_conn())
    finally:
        lock.rollback()
        lock.close()


def test_search_visibility_and_audit(databases):
    import_memmy(databases, get_conn())
    insert_raw_event(RawEvent(source="claude_code", event_type="semantic_event", ts_start="2026-01-01", content_text="apple event", content_hash="apple-event", bucket="Work"))
    token = auth.create_grant("reader", buckets=("Work",), operations=("read", "write"))
    result = tools.search_context(token, ["apple"], client="reader")
    assert result["results"][0]["kind"] == "memory"
    assert result["results"][1]["id"].startswith("evt_")
    assert result["results"][0]["evidence_ids"] == []
    restricted = auth.create_grant("restricted", buckets=("Work",), sources=("claude_code",), operations=("read",))
    assert all(item.get("kind") != "memory" for item in tools.search_context(restricted, ["apple"], client="restricted")["results"])
    private = auth.create_grant("private", buckets=("Private",), operations=("read",))
    assert tools.search_context(private, ["apple"], client="private")["status"] == "no_results"
    assert memory_tools.update_memory(token, result["results"][0]["id"], "revoke", client="reader")["status"] == "ok"
    assert all(item.get("kind") != "memory" for item in tools.search_context(token, ["apple"], client="reader")["results"])
    assert get_conn().execute("SELECT result_count FROM mcp_audit WHERE tool='search_context' ORDER BY id LIMIT 1").fetchone()[0] == 2


def test_superseded_memory_not_returned_and_detail_truncated(databases):
    import_memmy(databases, get_conn())
    conn = get_conn()
    conn.execute("UPDATE memories SET detail = ? WHERE external_id = '1'", ("apple " + "x" * 3000,))
    conn.commit()
    token = auth.create_grant("reader", buckets=("Work",), operations=("read",))
    assert len(tools.search_context(token, ["apple"], client="reader")["results"][0]["detail"]) == 2000
    conn.execute("UPDATE memories SET superseded_by = (SELECT id FROM memories WHERE external_id = '2') WHERE external_id = '1'")
    conn.commit()
    assert tools.search_context(token, ["apple"], client="reader")["status"] == "no_results"


def test_evidence_backed_memory_obeys_requested_source(databases):
    conn = get_conn()
    event = insert_raw_event(RawEvent(source="claude_code", event_type="semantic_event", ts_start="2026-01-01", content_text="linked event", content_hash="linked", bucket="Work"))
    conn.execute("INSERT INTO evidence(event_id, locator, created_at) VALUES (?, 'synthetic', '2026-01-01')", (event,))
    evidence_id = f"evd_{conn.execute('SELECT max(id) FROM evidence').fetchone()[0]}"
    conn.commit()
    token = auth.create_grant("reader", buckets=("Work",), operations=("read", "write"))
    result = memory_tools.commit_memory(token, [{"type": "fact", "scope": "global", "content": "linked summary", "evidence_ids": [evidence_id]}], client="reader")
    assert result["status"] == "ok"
    found = tools.search_context(token, ["linked"], sources=["claude_code"], client="reader")
    assert found["results"][0]["evidence_ids"] == [evidence_id]
    denied = tools.search_context(token, ["linked"], sources=["memory"], client="reader")
    assert denied["status"] == "no_results"


def test_cli_dry_run(databases, monkeypatch, tmp_path):
    class Config:
        db_path_expanded = tmp_path / "penpal.sqlite"
    monkeypatch.setattr("penpal.cli.memory_import.get_config", lambda: Config())
    result = CliRunner().invoke(main, ["import", "memmy", "--db", str(databases), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "eligible: 3" in result.output and "imported: 2" in result.output
    assert get_conn().execute("SELECT count(*) FROM memories").fetchone()[0] == 0


def test_many_memories_leave_room_for_events(databases):
    token = auth.create_grant("reader", buckets=("Work",), operations=("read", "write"))
    for n in range(12):
        assert memory_tools.commit_memory(token, [{"type": "fact", "scope": "global", "content": f"banana note {n}", "bucket": "Work"}], client="reader")["status"] == "ok"
    for n in range(3):
        insert_raw_event(RawEvent(source="claude_code", event_type="semantic_event", ts_start="2026-01-01", content_text=f"banana event {n}", content_hash=f"banana-{n}", bucket="Work"))
    results = tools.search_context(token, ["banana"], limit=10, client="reader")["results"]
    assert [item.get("kind") == "memory" for item in results] == [True] * 5 + [False] * 3
    only_memories = tools.search_context(token, ["note"], limit=10, client="reader")["results"]
    assert len(only_memories) == 10 and all(item.get("kind") == "memory" for item in only_memories)
