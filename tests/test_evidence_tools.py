"""P0-08: evidence availability, get_evidence, get_timeline, AT21 (retention), AT04, AT15-style denial."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from penpal.mcp import audit, auth, tools
from penpal.store import evidence as evidence_mod
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.store.sync_repo import add_evidence
from mcp_helpers import client_of
from test_mainline_codex_git import _read_all, env  # noqa: F401  (fixture reused: claude + codex + repo)

UNAVAILABLE_ZH = "原始出处已不可访问"


def get_evidence(token, evidence_id):
    return tools.get_evidence(token, evidence_id, client=client_of(token))


def timeline(token, start, end, **kwargs):
    return tools.get_timeline(token, start, end, client=client_of(token), **kwargs)


def _evidence_id_for(token, word, source):
    data = tools.search_context(token, [word], sources=[source], client=client_of(token))
    assert data["status"] == "ok", data
    return data["results"][0]


# ── availability checker ─────────────────────────────────────────────────────────────────────
def test_locator_exists_file_line_and_git(tmp_path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text("a\nb\nc\n", encoding="utf-8")
    assert evidence_mod.locator_exists(str(f))
    assert evidence_mod.locator_exists(str(f), 3)
    assert not evidence_mod.locator_exists(str(f), 4)            # file shrank: line gone
    assert evidence_mod.locator_exists(str(f), 4, deep=False)     # cheap check is path-only
    assert not evidence_mod.locator_exists(str(tmp_path / "gone.jsonl"), 1)
    assert not evidence_mod.locator_exists(str(tmp_path))         # a directory is not a file

    repo = tmp_path / "repo"
    repo.mkdir()
    genv = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    run = lambda *a: subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *a],
                                    cwd=repo, check=True, capture_output=True, env=genv)
    run("init", "-q", "-b", "main")
    (repo / "x").write_text("x")
    run("add", ".")
    run("commit", "-q", "-m", "m")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, env=genv).stdout.strip()
    assert evidence_mod.locator_exists(f"{repo}@{commit}")
    assert not evidence_mod.locator_exists(f"{repo}@{'0' * 40}")
    assert not evidence_mod.locator_exists(f"{tmp_path}/nope@{commit}")
    assert not evidence_mod.locator_exists(f"{repo}@--upload-pack=x")   # not hex: never reaches the command line


def test_git_check_times_out_as_unavailable(tmp_path, monkeypatch) -> None:
    (tmp_path / "r").mkdir()

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=k.get("timeout", 0))

    monkeypatch.setattr(evidence_mod.subprocess, "run", slow)
    assert evidence_mod.locator_exists(f"{tmp_path}/r@{'a' * 40}") is False


# ── AT04 + AT21 over the real mainline (Claude + Codex + Git) ────────────────────────────────
def test_at04_get_evidence_returns_location_for_claude_codex_and_git(env) -> None:  # noqa: F811
    _read_all()
    token = auth.create_grant("claude")
    seen = {}
    for source in ("claude_code", "codex_cli", "git_log"):
        hit = _evidence_id_for(token, "narwhalquux", source)
        assert hit["evidence_available"] is True
        resp = get_evidence(token, hit["evidence_id"])
        assert resp["status"] == "ok"
        ev = resp["evidence"]
        assert ev["available"] is True and ev["source"] == source and ev["bucket"] == "Work"
        assert ev["event_id"] == hit["id"] and ev["occurred_at"] == hit["occurred_at"]
        assert "narwhalquux" in ev["excerpt"]
        seen[source] = ev["locator"]
    assert seen["claude_code"] == {"type": "file", "path": str(env.claude.resolve()), "line": 1}
    assert seen["codex_cli"]["type"] == "file" and seen["codex_cli"]["path"] == str(env.rollout.resolve())
    assert seen["codex_cli"]["line"] == 6
    assert seen["git_log"]["type"] == "git" and seen["git_log"]["repo"] == str(env.repo.resolve())
    assert len(seen["git_log"]["commit"]) == 40


def test_at21_source_deleted_event_still_found_but_evidence_marked_unavailable(env) -> None:  # noqa: F811
    _read_all()
    token = auth.create_grant("claude")
    before = {s: _evidence_id_for(token, "narwhalquux", s) for s in ("claude_code", "codex_cli")}
    assert all(h["evidence_available"] for h in before.values())

    env.claude.unlink()                         # Claude Code retention expired
    shutil.rmtree(env.sessions)                 # Codex rollouts gone

    for source, old in before.items():
        hit = _evidence_id_for(token, "narwhalquux", source)   # still searchable
        assert hit["id"] == old["id"] and hit["evidence_available"] is False
        resp = get_evidence(token, hit["evidence_id"])
        assert resp["status"] == "source_unavailable"
        assert UNAVAILABLE_ZH in resp["message"] and "no longer accessible" in resp["message"]
        ev = resp["evidence"]
        assert ev["available"] is False and "narwhalquux" in ev["excerpt"]      # stored excerpt kept
        assert ev["locator"] is None                                            # nothing openable
        assert str(env.tmp) not in json.dumps(resp)                             # no path leaks either
    row = get_conn().execute("SELECT available FROM evidence WHERE id = ?",
                             (int(before["claude_code"]["evidence_id"].removeprefix("evd_")),)).fetchone()
    assert row["available"] == 0                                                # updated lazily


def test_at21_git_commit_gone_and_line_gone_detected_by_get_evidence(env) -> None:  # noqa: F811
    _read_all()
    token = auth.create_grant("claude")
    hit = _evidence_id_for(token, "narwhalquux", "codex_cli")
    assert hit["evidence_available"] is True
    env.rollout.write_text("only one line\n", encoding="utf-8")     # file exists, line 6 does not
    assert _evidence_id_for(token, "narwhalquux", "codex_cli")["evidence_available"] is True  # cheap check: path only
    resp = get_evidence(token, hit["evidence_id"])
    assert resp["status"] == "source_unavailable"
    assert _evidence_id_for(token, "narwhalquux", "codex_cli")["evidence_available"] is False  # verdict now stored

    gh = _evidence_id_for(token, "narwhalquux", "git_log")
    shutil.rmtree(env.repo)
    assert get_evidence(token, gh["evidence_id"])["status"] == "source_unavailable"


def test_available_evidence_never_returns_more_than_the_stored_excerpt(env) -> None:  # noqa: F811
    _read_all()
    token = auth.create_grant("claude")
    hit = _evidence_id_for(token, "narwhalquux", "claude_code")
    env.claude.write_text("RAW-FILE-ONLY-TEXT-should-never-be-returned\n", encoding="utf-8")
    resp = get_evidence(token, hit["evidence_id"])
    assert resp["status"] == "ok" and "RAW-FILE-ONLY-TEXT" not in json.dumps(resp)
    assert len(resp["evidence"]["excerpt"]) <= tools.EXCERPT_CHARS


# ── get_evidence scope / validation / audit ──────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "t.db")
    yield tmp_path
    close()


def _event(source, bucket, text, ts, locator=None, line=1, event_type="semantic_event", n=[0]):
    n[0] += 1
    rid = insert_raw_event(RawEvent(source=source, event_type=event_type, ts_start=ts, content_text=text,
                                    content_hash=f"hh{n[0]}", bucket=bucket))
    if locator:
        add_evidence(rid, locator, line, None)
    return rid


def _evd(event_id):
    return f"evd_{get_conn().execute('SELECT id FROM evidence WHERE event_id = ?', (event_id,)).fetchone()[0]}"


def test_out_of_scope_evidence_is_indistinguishable_from_nonexistent(db) -> None:
    f = db / "a.txt"
    f.write_text("x\n")
    mine = _event("claude_code", "Work", "alpha work note", "2026-03-01T09:00:00+00:00", str(f))
    other_bucket = _event("chrome_history", "Personal", "alpha personal", "2026-03-01T10:00:00+00:00", str(f))
    other_source = _event("codex_cli", "Work", "alpha codex", "2026-03-01T11:00:00+00:00", str(f))
    raw_stream = _event("claude_code", "Work", "alpha keyboard", "2026-03-01T12:00:00+00:00", str(f), event_type="ocr_text_capture")
    token = auth.create_grant("host", buckets=["Work"], sources=["claude_code"])
    assert get_evidence(token, _evd(mine))["status"] == "ok"
    ghost = get_evidence(token, "evd_99999")
    assert ghost["status"] == "not_found"
    for eid in (other_bucket, other_source, raw_stream):
        assert get_evidence(token, _evd(eid)) == ghost
    for bad in ("evd_abc", "evd_", "evd_-1", "evd_1.0", "evd_٣", "evd_" + "9" * 40, "123", "", "evd_1 OR 1=1", "../etc/passwd"):
        assert get_evidence(token, bad) == ghost, bad
    assert get_evidence(token, 5)["status"] == "invalid_argument"
    assert get_evidence(token, None)["status"] == "invalid_argument"


def test_unavailable_body_does_not_reveal_scope_difference(db) -> None:
    """A missing original on an in-scope event is a different answer from not_found (by design),
    but only for events the grant may read."""
    gone = _event("claude_code", "Work", "beta note", "2026-03-01T09:00:00+00:00", str(db / "missing.jsonl"))
    out = _event("chrome_history", "Personal", "beta private", "2026-03-01T09:00:00+00:00", str(db / "missing.jsonl"))
    token = auth.create_grant("host", buckets=["Work"])
    assert get_evidence(token, _evd(gone))["status"] == "source_unavailable"
    assert get_evidence(token, _evd(out)) == get_evidence(token, "evd_424242")


def test_get_evidence_and_timeline_audited_without_content(db) -> None:
    f = db / "a.txt"
    f.write_text("x\n")
    eid = _event("claude_code", "Work", "gamma note", "2026-03-01T09:00:00+00:00", str(f))
    token = auth.create_grant("host", buckets=["Work"])
    get_evidence(token, _evd(eid))
    get_evidence(token, "evd_777")
    timeline(token, "2026-03-01", "2026-03-02")
    timeline(token, "nonsense", "2026-03-02")
    rows = audit.recent(10)[::-1]
    got = [(r["tool"], r["decision"], r["reason_code"], r["result_count"], r["scope"]) for r in rows]
    assert got == [
        ("get_evidence", "allow", "ok", 1, "Work"),
        ("get_evidence", "allow", "not_found", 0, "Work"),
        ("get_timeline", "allow", "ok", 1, "Work"),
        ("get_timeline", "allow", "invalid_argument", 0, "Work"),
    ]
    assert "gamma" not in json.dumps(rows) and "evd_" not in json.dumps(rows)


# ── get_timeline ─────────────────────────────────────────────────────────────────────────────
def test_timeline_is_ordered_by_occurred_at_not_ingest_order_and_scoped(db) -> None:
    _event("claude_code", "Work", "third", "2026-03-03T09:00:00+00:00")
    _event("claude_code", "Work", "first", "2026-03-01T09:00:00+00:00", str(db / "x"))
    _event("git_log", "Work", "second", "2026-03-02T09:00:00+00:00")
    _event("chrome_history", "Personal", "hidden", "2026-03-02T10:00:00+00:00")
    _event("claude_code", "Work", "raw", "2026-03-02T11:00:00+00:00", event_type="ocr_text_capture")
    token = auth.create_grant("host", buckets=["Work"])
    resp = timeline(token, "2026-03-01", "2026-03-03")
    assert resp["status"] == "ok" and resp["has_more"] is False
    assert [r["snippet"] for r in resp["results"]] == ["first", "second", "third"]
    first = resp["results"][0]
    assert set(first) == {"id", "occurred_at", "source", "bucket", "snippet", "evidence_id"}
    assert first["evidence_id"].startswith("evd_") and resp["results"][1]["evidence_id"] is None
    assert get_evidence(token, first["evidence_id"])["evidence"]["event_id"] == first["id"]
    # date-only end covers the whole day; source filter is intersected with the grant
    assert [r["snippet"] for r in timeline(token, "2026-03-03", "2026-03-03")["results"]] == ["third"]
    assert [r["snippet"] for r in timeline(token, "2026-03-01", "2026-03-03", sources=["git_log"])["results"]] == ["second"]
    assert timeline(token, "2026-03-01", "2026-03-03", sources=["chrome_history"])["status"] == "no_results"
    assert timeline(token, "2025-01-01", "2025-01-02")["status"] == "no_results"


def test_timeline_limit_default_max_and_cursor_pagination(db) -> None:
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for i in range(230):
        _event("claude_code", "Work", f"e{i:03d}", (base + timedelta(minutes=i)).isoformat())
    _event("claude_code", "Work", "same-ts-a", base.isoformat())
    _event("claude_code", "Work", "same-ts-b", base.isoformat())
    token = auth.create_grant("host", buckets=["Work"])
    first = timeline(token, "2026-03-01", "2026-03-02")
    assert len(first["results"]) == 50 and first["has_more"] is True and first["next_cursor"]
    assert len(timeline(token, "2026-03-01", "2026-03-02", limit=1000)["results"]) == 200   # clamped to max
    seen, cursor = [], None
    while True:
        page = timeline(token, "2026-03-01", "2026-03-02", limit=40, cursor=cursor)
        seen += [r["id"] for r in page["results"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) == 232 and len(set(seen)) == 232           # same-timestamp rows neither lost nor duplicated


def test_timeline_validation_uses_fixed_text(db) -> None:
    token = auth.create_grant("host", buckets=["Work"])
    secret = "SECRETVALUE9"
    cases = [dict(start=secret, end="2026-03-02"), dict(start="2026-03-02", end="2026-03-01"),
             dict(start=None, end="2026-03-02"), dict(start=5, end="2026-03-02"),
             dict(start="2026-03-01", end="2026-03-02", cursor=secret),
             dict(start="2026-03-01", end="2026-03-02", cursor=f"{secret}|1"),
             dict(start="2026-03-01", end="2026-03-02", cursor=7)]
    for c in cases:
        resp = timeline(token, c.pop("start"), c.pop("end"), **c)
        assert resp["status"] == "invalid_argument" and secret not in json.dumps(resp)
    assert timeline(token, "2026-03-01", "2026-03-02", limit="5")["status"] == "invalid_argument"
    assert timeline(token, "2026-03-01", "2026-03-02", limit=True)["status"] == "invalid_argument"
    assert timeline(token, "2026-03-01", "2026-03-02", sources="git_log")["status"] == "invalid_argument"


# ── AT15-style uniform denial for the new tools ──────────────────────────────────────────────
def test_at15_denial_uniform_across_all_tools_and_audited(db) -> None:
    good = auth.create_grant("claude")
    auth.create_grant("codex")
    attempts = [(None, "claude"), ("kp_x", "claude"), (good, "codex"), (good, None), (good, "evil")]
    calls = {
        "search_context": ("search_context", {"keywords": ["pelican"]}),
        "get_evidence": ("get_evidence", {"evidence_id": "evd_1"}),
        "get_timeline": ("get_timeline", {"start": "2026-03-01", "end": "2026-03-02"}),
    }

    async def run():
        out = {name: set() for name in calls}
        for token, client in attempts:
            server = build_server_for(token, client)
            async with create_connected_server_and_client_session(server._mcp_server) as c:
                for name, (tool, args) in calls.items():
                    out[name].add((await c.call_tool(tool, args)).model_dump_json())
        return out

    bodies = asyncio.run(run())
    assert len({b for s in bodies.values() for b in s}) == 1                    # one body for every tool and reason
    assert "permission_denied" in next(iter(bodies["get_evidence"]))
    rows = [r for r in audit.recent(100) if r["decision"] == "deny"]
    assert len(rows) == len(attempts) * len(calls)
    for tool in calls:
        assert sum(1 for r in rows if r["tool"] == tool) == len(attempts)
    assert all(r["result_count"] is None for r in rows)


def build_server_for(token, client):
    from penpal.mcp.server import build_server
    return build_server(token_provider=lambda: token, client_provider=lambda: client)


def test_tool_descriptions_state_the_contract() -> None:
    from penpal.mcp.server import EVIDENCE_DESCRIPTION, INSTRUCTIONS, TIMELINE_DESCRIPTION
    for must in ("evidence_id", "source_unavailable", "no longer accessible", "not_found"):
        assert must in EVIDENCE_DESCRIPTION
    for must in ("start", "end", "next_cursor", "oldest first", "ISO"):
        assert must in TIMELINE_DESCRIPTION
    assert "get_evidence" in INSTRUCTIONS and "get_timeline" in INSTRUCTIONS
    # AT13 (contract level): history is presented as the user's record, not as a current rule
    assert "not current rules" in INSTRUCTIONS and "your history shows" in INSTRUCTIONS
