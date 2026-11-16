"""Synthetic coverage for memory provenance and confirmation."""
import sqlite3

import pytest
from click.testing import CliRunner

from penpal import i18n
from penpal.cli import main
from penpal.mcp import auth, memory_tools, tools
from penpal.memory.writer import Denied, _target
from penpal.store import migrations
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v37 import migrate_v37


@pytest.fixture
def db(tmp_path, monkeypatch):
    close()
    init_db(tmp_path / "test.sqlite")
    monkeypatch.setattr("penpal.cli.memory.require_db", lambda config: None)
    monkeypatch.setattr("penpal.cli.memory.get_config", lambda: None)
    yield get_conn()
    close()


def item(content, kind="fact", **kwargs):
    return {"type": kind, "content": content, "scope": "global", "bucket": "Work", **kwargs}


def commit(token, client, value):
    return memory_tools.commit_memory(token, [value], client=client)


def memory_hits(token, client, *keywords):
    result = tools.search_context(token, list(keywords), client=client)
    return [hit for hit in result["results"] if hit.get("kind") == "memory"]


def test_migration_backfills_pre_v37_and_is_idempotent(monkeypatch):
    conn = sqlite3.connect(":memory:")
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:-1])
        migrations.run_migrations(conn)
    for source in ("host", "memmy"):
        conn.execute("""INSERT INTO memories(type, content, scope, source_tool, evidence_ids, buckets,
                     content_hash, valid_from, status, created_at, confidence, source)
                     VALUES ('fact', ?, 'global', 'alpha', '[]', '[\"Work\"]', ?,
                             '2026-01-01', 'active', '2026-01-01', 1, ?)""", (source, source, source))
    conn.commit()
    migrations.run_migrations(conn)
    assert conn.execute("SELECT source, confirmed_at FROM memories ORDER BY id").fetchall() == [
        ("host", None), ("memmy", "2026-01-01")]
    conn.execute("UPDATE memories SET confirmed_at = NULL WHERE source = 'memmy'")
    migrate_v37(conn)
    assert conn.execute("SELECT confirmed_at FROM memories WHERE source = 'memmy'").fetchone()[0] is None
    conn.close()


def test_visibility_ranking_and_confirmation(db):
    alpha = auth.create_grant("alpha", buckets=["Work"], operations=("read", "write"))
    beta = auth.create_grant("beta", buckets=["Work"], operations=("read", "write"))
    procedure = commit(alpha, "alpha", item("synthetic procedure marker", "procedure"))["items"][0]["id"]
    fact = commit(alpha, "alpha", item("synthetic fact marker extra"))["items"][0]["id"]
    confirmed = commit(beta, "beta", item("synthetic fact marker"))["items"][0]["id"]
    assert CliRunner().invoke(main, ["memory", "confirm", confirmed]).exit_code == 0
    assert memory_hits(alpha, "alpha", "procedure")[0]["id"] == procedure
    assert memory_hits(beta, "beta", "procedure") == []
    with pytest.raises(Denied):
        _target(db, procedure, auth.authenticate(beta))
    hits = memory_hits(beta, "beta", "synthetic", "extra")
    assert [(hit["id"], hit["confirmed"]) for hit in hits] == [(confirmed, True), (fact, False)]
    result = CliRunner().invoke(main, ["memory", "confirm", procedure])
    assert result.exit_code == 0
    assert memory_hits(beta, "beta", "procedure")[0]["confirmed"] is True
    assert db.execute("SELECT result_count FROM mcp_audit WHERE tool='confirm_memory' ORDER BY id DESC").fetchone()[0] == 1


def test_ownership_and_duplicate_isolation(db):
    alpha = auth.create_grant("alpha", buckets=["Work"], operations=("read", "write"))
    beta = auth.create_grant("beta", buckets=["Work"], operations=("read", "write"))
    value = item("synthetic shared marker")
    original = commit(alpha, "alpha", value)["items"][0]["id"]
    duplicate = commit(beta, "beta", value)["items"][0]
    assert duplicate["id"] != original and duplicate["merged"] is False
    message = "memory was written by another tool or imported; save a new memory instead"
    for action in ("revoke", "invalidate", "correct"):
        result = memory_tools.update_memory(beta, original, action, item("synthetic replacement"), client="beta")
        assert result == {"status": "invalid_argument", "message": message}
    assert commit(beta, "beta", item("synthetic replacement", supersedes=original)) == {
        "status": "invalid_argument", "message": message}
    assert CliRunner().invoke(main, ["memory", "confirm", original]).exit_code == 0
    assert memory_tools.update_memory(beta, original, "revoke", client="beta")["message"] == message
    assert commit(beta, "beta", value)["items"][0] == {"id": original, "merged": True}
    existing = commit(beta, "beta", item("synthetic replacement"))["items"][0]["id"]
    assert CliRunner().invoke(main, ["memory", "confirm", existing]).exit_code == 0
    replacement = memory_tools.update_memory(alpha, original, "correct", item("synthetic replacement"), client="alpha")
    assert replacement["status"] == "ok" and replacement["id"] != existing
    row = db.execute("SELECT confirmed_at FROM memories WHERE id = ?", (int(replacement["id"][4:]),)).fetchone()
    assert row["confirmed_at"] is None
    assert memory_tools.update_memory(alpha, replacement["id"], "revoke", client="alpha")["status"] == "ok"


@pytest.mark.parametrize("language,pending,confirmed", [("en", "pending", "Confirmed"), ("zh", "待确认", "已确认")])
def test_cli_pending_and_confirm(db, monkeypatch, language, pending, confirmed):
    monkeypatch.setattr(i18n, "_LANG_CACHE", language)
    token = auth.create_grant("alpha", buckets=["Work"], operations=("read", "write"))
    memory_id = commit(token, "alpha", item("synthetic pending marker"))["items"][0]["id"]
    runner = CliRunner()
    result = runner.invoke(main, ["memory", "list", "--pending"])
    assert result.exit_code == 0 and memory_id in result.output and pending in result.output
    result = runner.invoke(main, ["memory", "confirm", memory_id, memory_id])
    assert result.exit_code == 0 and f"{confirmed}: 1" in result.output
    assert ("confirmed" if language == "en" else "已确认") in runner.invoke(main, ["memory", "list"]).output
    assert memory_id not in runner.invoke(main, ["memory", "list", "--pending", "--status", "all"]).output
    assert runner.invoke(main, ["memory", "confirm", "invalid"]).exit_code != 0
