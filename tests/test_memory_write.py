"""Memory writes use synthetic evidence and isolated grants."""
import importlib
import json

import pytest

from penpal.mcp import auth, memory_tools
from penpal.memory.writer import visible
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.store.migration_v35 import migrate_v35


@pytest.fixture
def db(tmp_path):
    close()
    init_db(tmp_path / "memory.sqlite")
    yield
    close()


def grant(name="claude_code", buckets=("Work",), sources=None, operations=("read", "write")):
    return auth.create_grant(name, buckets=buckets, sources=sources, operations=operations)


def evidence(bucket="Work", source="claude_code"):
    event = insert_raw_event(RawEvent(source=source, event_type="semantic_event", ts_start="2026-01-01", content_text="synthetic evidence", content_hash=f"{bucket}-{source}", bucket=bucket))
    conn = get_conn()
    conn.execute("INSERT INTO evidence(event_id, locator, created_at) VALUES (?, 'synthetic', '2026-01-01')", (event,))
    conn.commit()
    return f"evd_{conn.execute('SELECT max(id) FROM evidence').fetchone()[0]}"


def item(**kw):
    return {"type": "fact", "content": "A synthetic fact.", "scope": "global", **kw}


def write(token, *items, client="claude_code"):
    return memory_tools.commit_memory(token, list(items), client=client)


def test_writes_merges_and_history(db):
    t = grant()
    a, b = evidence(), evidence(source="codex_cli")
    first = write(t, item(evidence_ids=[a]))
    assert first["status"] == "ok"
    mid = first["items"][0]["id"]
    assert write(t, item(content="  A SYNTHETIC FACT. ", evidence_ids=[b]))["items"][0] == {"id": mid, "merged": True}
    row = get_conn().execute("SELECT * FROM memories").fetchone()
    assert set(json.loads(row["evidence_ids"])) == {a, b}
    replacement = write(t, item(content="A revised fact.", supersedes=mid))
    assert replacement["status"] == "ok"
    assert get_conn().execute("SELECT superseded_by FROM memories WHERE id = 1").fetchone()[0] == 2
    assert memory_tools.update_memory(t, "mem_2", "invalidate", client="claude_code")["status"] == "ok"
    assert get_conn().execute("SELECT status FROM memories WHERE id = 2").fetchone()[0] == "invalid"
    assert get_conn().execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'revised'").fetchone()[0] == 1
    migrate_v35(get_conn())


def test_rejects_unauthorized_without_disclosure(db):
    t = grant()
    private = evidence("Private")
    assert write(t, item(evidence_ids=[private])) == write(t, item(evidence_ids=["evd_999999"]))
    assert write(t, item(bucket="Private"))["status"] == "not_found"
    read_only = grant("reader", operations=("read",))
    assert write(read_only, item(), client="reader")["status"] == "permission_denied"
    assert get_conn().execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    assert get_conn().execute("SELECT count(*) FROM mcp_audit WHERE decision='deny'").fetchone()[0] == 4


def test_multibucket_and_source_visibility(db):
    t = grant(buckets=("Work", "Private"))
    a, b = evidence(), evidence("Private", "codex_cli")
    mid = write(t, item(evidence_ids=[a, b]))["items"][0]["id"]
    row = get_conn().execute("SELECT * FROM memories").fetchone()
    assert visible(row, auth.authenticate(t))
    limited = grant("limited", sources=("claude_code",))
    assert not visible(row, auth.authenticate(limited))
    assert memory_tools.update_memory(limited, mid, "revoke", client="limited")["status"] == "not_found"
    scoped = grant("scoped", buckets=("Work", "Private"), sources=("claude_code",))
    assert not visible(row, auth.authenticate(scoped))
    assert write(scoped, item(), client="scoped")["status"] == "not_found"
    memory_only = grant("memory-only", sources=("memory",))
    assert write(memory_only, item(), client="memory-only")["status"] == "ok"


def test_project_secret_and_correction(db):
    t = grant()
    assert write(t, item(scope="project"))["status"] == "invalid_argument"
    assert write(t, item(content="token: " + "sk-" + "a" * 48))["status"] == "invalid_argument"
    mid = write(t, item(scope="project", project="/tmp/example"))["items"][0]["id"]
    assert get_conn().execute("SELECT project FROM memories").fetchone()[0] == "example"
    assert memory_tools.update_memory(t, mid, "correct", item(content="Corrected."), client="claude_code")["status"] == "ok"


def test_no_evidence_bucket_resolution(db, monkeypatch):
    from penpal.store import buckets

    monkeypatch.setattr(buckets, "_overrides", {"ai_tools": "Personal", "claude_code": "Private"})
    cursor = grant("cursor", buckets=("Work", "Personal"))
    assert write(cursor, item(), client="cursor")["status"] == "ok"
    assert json.loads(get_conn().execute("SELECT buckets FROM memories").fetchone()[0]) == ["Personal"]
    claude = grant("claude_code", buckets=("Work", "Private"))
    assert write(claude, item(content="Other fact"))["status"] == "ok"
    assert json.loads(get_conn().execute("SELECT buckets FROM memories WHERE id = 2").fetchone()[0]) == ["Private"]
    denied = grant("codex_cli", buckets=("Personal",))
    assert write(denied, item(), client="codex_cli")["status"] == "not_found"
    single = grant("custom", buckets=("Private",))
    assert write(single, item(content="Custom fact"), client="custom")["status"] == "ok"
    multi = grant("custom-multi", buckets=("Work", "Personal"))
    result = write(multi, item(), client="custom-multi")
    assert result["status"] == "invalid_argument"
    assert all(value in result["message"] for value in ("bucket", "Work", "Personal"))
    assert write(multi, item(bucket="Personal"), client="custom-multi")["status"] == "ok"
    assert write(multi, item(bucket="Private"), client="custom-multi")["status"] == "not_found"


def test_no_model_dependencies():
    module = importlib.import_module("penpal.memory.writer")
    assert not any(name.startswith(("openai", "anthropic", "litellm")) for name in vars(module))


def test_a_duplicate_in_another_bucket_never_hides_the_first_writers_memory(db):
    work_only = grant("claude_code", buckets=("Work",))
    wide = grant("codex_cli", buckets=("Work", "Personal"))
    first = write(work_only, item(bucket="Work"))["items"][0]["id"]
    second = write(wide, item(bucket="Personal"), client="codex_cli")["items"][0]
    assert second["merged"] is False and second["id"] != first
    row = get_conn().execute("SELECT * FROM memories WHERE id = ?", (int(first[4:]),)).fetchone()
    assert json.loads(row["buckets"]) == ["Work"]
    assert visible(row, auth.authenticate(work_only))
