"""Memory management uses synthetic local records."""
import json

import pytest
from click.testing import CliRunner

from penpal.cli import main
from penpal.i18n import T
from penpal.mcp import auth, tools
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event


@pytest.fixture
def db(tmp_path, monkeypatch):
    close()
    init_db(tmp_path / "test.sqlite")
    monkeypatch.setattr("penpal.cli.memory.require_db", lambda cfg: None)
    monkeypatch.setattr("penpal.cli.memory.get_config", lambda: None)
    monkeypatch.setattr("penpal.cli.data.require_db", lambda cfg: None)
    monkeypatch.setattr("penpal.cli.data.get_config", lambda: None)
    yield get_conn()
    close()


def add(conn, text, *, source="host", tool="alpha", status="active", superseded_by=None,
        evidence=(), scope="global", project=None, date="2026-01-01"):
    cur = conn.execute("""INSERT INTO memories(type, content, scope, project, source_tool, evidence_ids,
        buckets, content_hash, valid_from, status, created_at, confidence, source, superseded_by)
        VALUES ('fact', ?, ?, ?, ?, ?, '[\"Work\"]', ?, ?, ?, ?, 1, ?, ?)""",
        (text, scope, project, tool, json.dumps(evidence), text, date, status, date, source, superseded_by))
    conn.commit()
    return cur.lastrowid


def invoke(*args, input=None):
    return CliRunner().invoke(main, list(args), input=input)


def test_list_filters_and_total(db):
    add(db, "older", status="invalid", date="2026-01-01")
    add(db, "project " + "x" * 90, source="memmy", tool="beta", scope="project", project="demo", date="2026-02-01")
    add(db, "newest", source="memmy", tool="alpha", date="2026-03-01")
    result = invoke("memory", "list", "--source", "memmy", "--limit", "1")
    assert result.exit_code == 0 and "newest" in result.output and "project" not in result.output
    assert f"{T('总数', 'Total')}: 2" in result.output
    result = invoke("memory", "list", "--tool", "beta")
    assert "project:demo" in result.output and "x" * 81 not in result.output
    assert "older" not in invoke("memory", "list").output
    assert "older" in invoke("memory", "list", "--status", "all", "--source", "host").output


def test_delete_chain_fts_search_and_audit(db):
    newest = add(db, "synthetic newest searchable")
    middle = add(db, "synthetic middle searchable", status="superseded", superseded_by=newest)
    add(db, "synthetic oldest searchable", status="superseded", superseded_by=middle)
    token = auth.create_grant("test", buckets=["Work"], sources=["memory"])
    assert tools.search_context(token, ["newest"], client="test")["results"]
    result = invoke("memory", "delete", f"mem_{newest}", "--dry-run")
    assert result.exit_code == 0 and "3" in result.output
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM mcp_audit WHERE tool='delete_memory'").fetchone()[0] == 0
    assert invoke("memory", "delete", f"mem_{newest}", input="n\n").exit_code == 0
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 3
    assert invoke("memory", "delete", f"mem_{newest}", "--yes").exit_code == 0
    assert db.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'searchable'").fetchone()[0] == 0
    assert tools.search_context(token, ["newest"], client="test")["results"] == []
    row = db.execute("SELECT * FROM mcp_audit WHERE tool='delete_memory'").fetchone()
    assert row["client_id"] == "cli" and row["result_count"] == 3 and "synthetic" not in str(tuple(row))


def test_delete_source_and_guard(db):
    add(db, "one", source="memmy")
    add(db, "two", source="memmy")
    add(db, "three")
    assert invoke("memory", "delete", "--yes").exit_code != 0
    assert invoke("memory", "delete", "mem_1", "--source", "memmy", "--yes").exit_code != 0
    assert invoke("memory", "delete", "--source", "memmy", "--yes").exit_code == 0
    assert [row["content"] for row in db.execute("SELECT * FROM memories")] == ["three"]


def test_purge_evidence_and_memmy(db):
    event = insert_raw_event(RawEvent(source="sample", event_type="semantic_event", ts_start="2026-01-01", bucket="Work"))
    ev = db.execute("INSERT INTO evidence(event_id, locator, created_at) VALUES (?, 'sample', '2026-01-01')", (event,)).lastrowid
    db.commit()
    new = add(db, "linked", evidence=[f"evd_{ev}"])
    add(db, "old", status="superseded", superseded_by=new)
    add(db, "imported", source="memmy")
    result = invoke("purge", "--source", "sample", "--dry-run")
    assert result.exit_code == 0 and "memories: 2" in result.output
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 3
    assert invoke("purge", "--source", "sample", "--confirm").exit_code == 0
    result = invoke("purge", "--source", "memmy", "--confirm")
    assert result.exit_code == 0 and "memories: 1" in result.output
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


def test_purge_rolls_back_memories_on_failure(db):
    event = insert_raw_event(RawEvent(source="sample", event_type="semantic_event", ts_start="2026-01-01", bucket="Work"))
    ev = db.execute("INSERT INTO evidence(event_id, locator, created_at) VALUES (?, 'sample', '2026-01-01')", (event,)).lastrowid
    db.commit()
    add(db, "rollback searchable", evidence=[f"evd_{ev}"])
    db.execute("CREATE TRIGGER reject_event_delete BEFORE DELETE ON raw_events BEGIN SELECT RAISE(ABORT, 'blocked'); END")
    db.commit()
    result = invoke("purge", "--source", "sample", "--confirm")
    assert result.exit_code != 0
    assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'rollback'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM evidence WHERE id = ?", (ev,)).fetchone()[0] == 1
