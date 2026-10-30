"""G1 越权泄露 = 0 (Eval PRD §3.1.3) -- BLOCKING. Synthetic data only.

Three parts:
  1. canary scan   : unique strings planted in every out-of-scope bucket / source, hit with an
                     adversarial query battery; the canary must never appear in any response.
  2. differential  : same grant, same queries, two DBs that differ only by out-of-scope data;
                     responses (ids / timestamps stripped) must be byte-identical.  A deliberately
                     injected count leak must be caught (the harness is itself tested).
  3. coverage guard: MCP tools and response fields must be enumerated in G1_COVERED_* below; adding
                     a tool or field without extending this suite fails the build.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_helpers import search, server_for
from penpal.mcp import auth, tools
from penpal.mcp.server import build_server
from penpal.store.db import close, get_conn, init_db
from penpal.store.capture_events import CAPTURE_SEARCHABLE_EVENT_TYPES
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.store.sync_repo import add_evidence


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Config.load() must never see a real ~/.penpal/config.toml or a cwd config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


# ── coverage list: UPDATE THIS (and the batteries) when the MCP surface changes ──────────────
G1_COVERED_TOOLS = {
    "search_context": {"keywords", "time_range", "sources", "limit", "include_penpal_echoes", "include_keypulse_echoes"},
    "get_evidence": {"evidence_id"},
    "get_timeline": {"start", "end", "sources", "limit", "cursor", "include_penpal_echoes", "include_keypulse_echoes"},
    # M2-3: its G1 tests (unseen / other-bucket ids, differential) live in tests/test_feedback.py
    "rate_result": {"result_id", "verdict", "note", "query"},
    "commit_memory": {"items"},
    "update_memory": {"memory_id", "action", "item"},
}
G1_COVERED_RESPONSE_FIELDS = {
    "status", "reason_code", "message", "next_action",
    "results", "results[].id", "results[].snippet", "results[].occurred_at", "results[].source",
    "results[].bucket", "results[].evidence_id", "results[].evidence_available", "results[].match", "results[].match_reason",
    "results[].occurrences", "results[].first_seen", "results[].last_seen",
    # cross-source fold: other in-grant sources that recorded the same page (names only, never out-of-grant)
    "results[].also_seen_in",
    "coverage", "coverage.partial", "coverage.note", "coverage.time_range",
    "coverage.time_range.start", "coverage.time_range.end", "coverage.buckets", "coverage.sources",
    # M2-2: per-source span inside the grant (names / times of in-scope sources only; NO excluded counts)
    "recorded",  # rate_result: {"status": "ok", "recorded": true}
    # issue #22: in-grant sources that could not be read (fixed reason enum; names inside the grant only)
    "coverage.unavailable_sources", "coverage.unavailable_sources[].source", "coverage.unavailable_sources[].reason",
    "coverage.by_source", "coverage.by_source[].source", "coverage.by_source[].earliest",
    "coverage.by_source[].latest", "coverage.by_source[].has_events_in_range",
    # search_context per-keyword feedback: index = caller's own position (no text echoed); in_scope /
    # in_results are counts or booleans computed inside the grant only; hint is a fixed enum
    "keywords", "keywords[].index", "keywords[].in_scope", "keywords[].in_results", "hint",
    # get_timeline (results[].* above are shared)
    "has_more", "next_cursor",
    # get_evidence
    "evidence", "evidence.id", "evidence.event_id", "evidence.source", "evidence.bucket", "evidence.occurred_at",
    "evidence.excerpt", "evidence.available", "evidence.locator", "evidence.locator.type", "evidence.locator.path",
    "evidence.locator.line", "evidence.locator.repo", "evidence.locator.commit",
    # capture events (D26): locator {"type": "penpal", "event_id": ...}
    "evidence.locator.event_id",
}

# ── synthetic corpus ─────────────────────────────────────────────────────────────────────────
SHARED_WORD = "pelican"  # present in-scope AND out-of-scope, so queries on it exercise the filter

IN_SCOPE = [  # (source, bucket, text, ts[, event_type])
    ("claude_code", "Work", f"{SHARED_WORD} migration plan for the billing service", "2026-03-01T09:00:00+00:00"),
    ("claude_code", "Work", f"follow-up on {SHARED_WORD} rollout checklist", "2026-03-02T09:00:00+00:00"),
    ("git_log", "Work", f"fix {SHARED_WORD} retry loop", "2026-03-03T09:00:00+00:00"),
    # a capture-stream row (D26) inside the granted scope: reachable, with penpal evidence
    ("claude_code", "Work", f"typed {SHARED_WORD} outline in the editor", "2026-03-03T11:00:00+00:00", "keyboard_chunk"),
]

# Each out-of-scope row carries its own canary. (source, bucket, event_type, canary)
OUT_OF_SCOPE = [
    ("chrome_history", "Personal", "semantic_event", "KPCANARY1personalbrowse"),
    ("wechat", "Private", "semantic_event", "KPCANARY2privatechat"),
    ("knowledgec", "Financial", "semantic_event", "KPCANARY3bankstatement"),
    ("mystery_source", None, "semantic_event", "KPCANARY4nobucketrow"),
    ("codex_cli", "Work", "semantic_event", "KPCANARY5workothersource"),   # in bucket, out of source scope
    ("ocr_text", "Work", "ocr_text_capture", "KPCANARY6ocrneversearchable"),  # not a searchable capture type
    ("claude_code", "Personal", "semantic_event", "KPCANARY7samesrcotherbucket"),
    # capture streams (D26) in buckets / sources the grant does not cover
    ("keyboard_chunk", "Financial", "keyboard_chunk", "KPCANARY8keyboardbank"),    # typing in a banking app
    ("keyboard_chunk", "Work", "keyboard_chunk", "KPCANARY9keyboardwork"),         # in bucket, out of source scope
    ("clipboard", "Personal", "clipboard_copy", "KPCANARY10clipboardpersonal"),
    ("window", "Financial", "window_focus_session", "KPCANARY11windowbank"),
    ("ax_text", "Private", "ax_text_capture", "KPCANARY12axprivate"),
    ("browser_url", "Personal", "browser_url_capture", "KPCANARY13browserpersonal"),
    ("keyboard_chunk", None, "keyboard_chunk", "KPCANARY14capturenobucket"),       # capture row with no bucket
]
CANARIES = [c for *_, c in OUT_OF_SCOPE]
SOURCE_DEPENDENT = {"KPCANARY5workothersource", "KPCANARY9keyboardwork"}  # in-scope for a Work grant with no source list
OUT_SOURCES = sorted({s for s, *_ in OUT_OF_SCOPE})
OUT_BUCKETS = ["Personal", "Private", "Financial", "Temporary"]


def _insert(source, bucket, text, ts, event_type="semantic_event", n=[0]) -> int:
    n[0] += 1
    window = event_type == "window_focus_session"  # real window rows carry the text in window_title only
    row_id = insert_raw_event(
        RawEvent(
            source=source, event_type=event_type, ts_start=ts, content_text=None if window else text,
            window_title=text if window else None, app_name="SomeApp" if event_type != "semantic_event" else None,
            content_hash=f"h{n[0]}-{hash(text) & 0xFFFF}-{source}-{ts}", bucket=bucket or "",
        )
    )
    if bucket is None:  # force a genuinely NULL bucket (insert_raw_event would resolve one)
        get_conn().execute("UPDATE raw_events SET bucket = NULL WHERE id = ?", (row_id,))
        get_conn().commit()
    if event_type not in CAPTURE_SEARCHABLE_EVENT_TYPES:  # capture rows get their penpal evidence from the repository
        add_evidence(row_id, f"/synthetic/{source}.jsonl", 1, None)
    return row_id


def _is_in_scope(canary: str, scope: str) -> bool:
    return canary in SOURCE_DEPENDENT and "sources" not in GRANT_SCOPES[scope]


def _canaries_out_of_scope(scope: str) -> list[str]:
    return [c for c in CANARIES if not _is_in_scope(c, scope)]


def _seed(db: Path, *, with_out_of_scope: bool, scope: str) -> None:
    close()
    init_db(db)
    for source, bucket, text, ts, *event_type in IN_SCOPE:
        _insert(source, bucket, text, ts, *event_type)
    for source, bucket, event_type, canary in OUT_OF_SCOPE:
        if _is_in_scope(canary, scope):  # identical in both DBs: part of the granted data
            _insert(source, bucket, f"{SHARED_WORD} secret note {canary} end", "2026-03-02T10:00:00+00:00", event_type)
    if with_out_of_scope:
        for source, bucket, event_type, canary in OUT_OF_SCOPE:
            if _is_in_scope(canary, scope):
                continue
            _insert(source, bucket, f"{SHARED_WORD} secret note {canary} end", "2026-03-02T10:00:00+00:00", event_type)
        for i in range(30):  # volume, so counts/limits would differ if they leaked
            _insert("chrome_history", "Personal", f"{SHARED_WORD} bulk {i}", f"2026-03-04T09:{i:02d}:00+00:00")


GRANT_SCOPES = {
    "work_all_sources": dict(buckets=["Work"]),
    "work_claude_only": dict(buckets=["Work"], sources=["claude_code"]),
}


# ── query batteries ──────────────────────────────────────────────────────────────────────────
def _adversarial_queries() -> list[dict]:
    qs: list[dict] = []
    for c in CANARIES:
        mid = len(c) // 2
        for q in (c, c.lower(), c[:8], c[-8:], c[mid - 3 : mid + 3], f"{c}*", f"^{c}", f"-{c}",
                  f"content_text:{c}", f"{{content_text window_title}}:{c}", f'"{c}"', f'"{c}',
                  f"{c} OR {SHARED_WORD}", f"{SHARED_WORD} OR {c}", f"{SHARED_WORD} NOT {c}",
                  f"NEAR({SHARED_WORD} {c})", f"NEAR({SHARED_WORD} {c}, 5)", f"{c})", f"({c}",
                  f"%{c}%", f"_{c[1:]}", c[:3], c[:2]):
            qs.append({"query": q})
        qs.append({"query": c, "time_range": {"start": "1970-01-01", "end": "2999-12-31"}})
        qs.append({"query": c, "time_range": {"start": c}})        # garbage bound containing the canary
        qs.append({"query": c, "sources": OUT_SOURCES})
        qs.append({"query": c, "sources": [c]})
        qs.append({"query": c, "limit": 100})
    for q in ("*", "**", "OR", "AND", "NOT", "NEAR", "NEAR(", '"', '""', "'", "(", ")", ":", "content_text:", "^",
              "KPCANARY", "KPCANARY*", "kpcanary", "canary", "%", "_", "\\", "a" * 1000, "a" * 5000,
              "KPCANARY " * 400, "\x00", "🙂", "密码"):
        qs.append({"query": q})
        qs.append({"query": q, "sources": OUT_SOURCES})
    for src in OUT_SOURCES + ["no_such_source", ""]:
        qs.append({"query": SHARED_WORD, "sources": [src]})
    qs.append({"query": SHARED_WORD, "sources": []})
    qs.append({"query": SHARED_WORD, "sources": OUT_SOURCES + ["claude_code"]})
    # multi-keyword OR: canaries / out-of-scope words mixed with in-scope ones, plus malformed lists
    for c in CANARIES:
        qs.append({"keywords": [c, SHARED_WORD]})
        qs.append({"keywords": [SHARED_WORD, "billing", c, c.lower()]})
        qs.append({"keywords": [c, "billing"], "sources": OUT_SOURCES})
        qs.append({"keywords": [c + "?"]})            # sentence-like keyword -> fixed invalid_argument
        qs.append({"keywords": [f"what is {c} about in the billing notes please tell me"]})
    qs.append({"keywords": [SHARED_WORD, "billing"]})  # strong tier
    qs.append({"keywords": [SHARED_WORD, "zzz-none"]})  # single-keyword coverage
    qs.append({"keywords": []})
    qs.append({"keywords": [""]})
    qs.append({"keywords": SHARED_WORD})
    qs.append({"keywords": [1, None]})
    # D27: the echo switch only widens visibility inside the grant, never beyond it
    for q in list(qs[::9]):
        qs.append({**q, "include_penpal_echoes": True})
    qs.append({"keywords": [SHARED_WORD], "include_penpal_echoes": "yes"})
    return qs


def _differential_queries() -> list[dict]:
    qs = _adversarial_queries()
    for limit in (1, 2, 3, 10, 100):
        qs.append({"query": SHARED_WORD, "limit": limit})
    qs += [
        {"query": "billing"}, {"query": "retry loop"}, {"query": "nothing-matches-this"},
        {"keywords": [SHARED_WORD, "retry", "billing", "rollout"]}, {"keywords": ["retry loop", "checklist"]},
        {"query": SHARED_WORD, "time_range": {"start": "2026-03-02", "end": "2026-03-03"}},
        {"query": SHARED_WORD, "time_range": {"end": "2026-02-01"}},
        {"query": SHARED_WORD, "time_range": {"start": "bad"}},
        {"query": SHARED_WORD, "sources": ["claude_code"]},
        {"query": "  "}, {"query": ""},
    ]
    return qs


def _keywords_of(q: dict) -> list:
    """Battery entries carry explicit `keywords` or a free-text `query` split on whitespace (as the CLI does)."""
    return q["keywords"] if "keywords" in q else q["query"].split()


def _run(token, q: dict) -> dict:
    return search(token, _keywords_of(q), q.get("time_range"), q.get("sources"), q.get("limit", 10),
                  q.get("include_penpal_echoes", False))


# ── 1. canary scan ───────────────────────────────────────────────────────────────────────────
@pytest.fixture
def canary_db(tmp_path, request):
    scope = request.node.callspec.params.get("scope", "work_claude_only") if hasattr(request.node, "callspec") else "work_claude_only"
    _seed(tmp_path / "canary.db", with_out_of_scope=True, scope=scope)
    yield scope
    close()


def _assert_no_canary(text: str, context: str, scope: str = "work_claude_only") -> None:
    for c in _canaries_out_of_scope(scope):
        assert c not in text, f"G1 FAIL: canary {c} leaked ({context})"
        assert c.lower() not in text.lower(), f"G1 FAIL: canary {c} leaked case-folded ({context})"


@pytest.mark.parametrize("scope", GRANT_SCOPES)
def test_g1_canary_scan_tools_layer(canary_db, scope) -> None:
    token = auth.create_grant("host", **GRANT_SCOPES[scope])
    for q in _adversarial_queries():
        resp = _run(token, q)
        _assert_no_canary(json.dumps(resp, ensure_ascii=False), repr(q)[:80], scope)
        assert resp["status"] in {"ok", "no_results", "invalid_argument"}
        if resp["status"] == "ok":  # only planted in-scope rows may ever come back
            assert all(SHARED_WORD in r["snippet"] for r in resp["results"])
            assert {r["bucket"] for r in resp["results"]} == {"Work"}


@pytest.mark.parametrize("scope", GRANT_SCOPES)
def test_g1_canary_scan_through_mcp_transport(canary_db, scope) -> None:
    """Same scan through the real MCP server: covers error text and protocol-level validation errors."""
    token = auth.create_grant("host", **GRANT_SCOPES[scope])
    server = server_for(token)
    c = CANARIES[0]
    calls = [{"keywords": _keywords_of(q), **{k: v for k, v in q.items() if k not in ("query", "keywords")}}
             for q in _adversarial_queries()[::7]]
    calls += [
        {"keywords": [c], "limit": c},           # wrong type carrying the canary
        {"keywords": [c], "sources": c},
        {"keywords": [c], "time_range": c},
        {"keywords": [c], "time_range": {"start": c, "end": c}},
        {"keywords": [c], "unexpected": c},
        {"keywords": c},
        {"keywords": [[c]]},
        {"keywords": [c + "?"]},
    ]

    async def run():
        out = []
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            for args in calls:
                res = await client.call_tool("search_context", args)
                out.append(res.model_dump_json())
        return out

    for args, text in zip(calls, asyncio.run(run())):
        _assert_no_canary(text, repr(args)[:80], scope)


def _evidence_ids_all() -> list[str]:
    """Every evidence id in the DB (in AND out of scope), then guesses past them."""
    top = get_conn().execute("SELECT COALESCE(MAX(id), 0) FROM evidence").fetchone()[0]
    return [f"evd_{i}" for i in range(0, top + 40)]


def _evidence_junk() -> list:
    junk = ["", "evd_", "evd_-1", "evd_0", "evd_1e3", "evd_1.0", "evd_ 1", "evd_1 OR 1=1", "1", "evd_" + "9" * 30,
            "../etc/passwd", "evt_1", "EVD_1", "\x00", "evd_\u0663", " evd_1", "evd_1\n", 5, None, ["evd_1"]]
    return junk + [f"evd_{c}" for c in CANARIES] + CANARIES


def _timeline_battery(with_id_cursors: bool = True) -> list[dict]:
    wide = {"start": "1970-01-01", "end": "2999-12-31"}
    qs: list[dict] = [
        dict(wide), dict(wide, limit=1), dict(wide, limit=200), dict(wide, limit=5000), dict(wide, limit=0),
        dict(wide, sources=OUT_SOURCES), dict(wide, sources=OUT_SOURCES + ["claude_code"]), dict(wide, sources=[]),
        dict(wide, sources=["codex_cli"]), dict(wide, sources=["claude_code"]),
        {"start": "2026-03-02", "end": "2026-03-02"},
        {"start": "2026-03-02T10:00:00+00:00", "end": "2026-03-02T10:00:00+00:00"},
        {"start": "2026-03-04", "end": "2026-03-06"}, {"start": "2026-03-02", "end": "2026-03-01"},
        {"start": None, "end": None}, {"start": 1, "end": 2}, dict(wide, limit="x"), dict(wide, sources="chrome_history"),
        dict(wide, include_penpal_echoes=True), dict(wide, include_penpal_echoes="yes"),
    ]
    for c in CANARIES:
        qs += [dict(wide, sources=[c]), {"start": c, "end": "2999-12-31"}, {"start": "1970-01-01", "end": c},
               dict(wide, cursor=c), dict(wide, cursor=f"{c}|1")]
    if with_id_cursors:  # row ids differ between DBs, so these are canary-scan only
        for ident in (0, 1, 3, 4, 5, 10, 50, 99999):
            qs.append(dict(wide, limit=2, cursor=f"2026-03-01T00:00:00+00:00|{ident}"))
            qs.append(dict(wide, limit=2, cursor=f"2026-03-04T09:00:00+00:00|{ident}"))  # inside the out-of-scope bulk range
    return qs


def _run_timeline(token, q: dict, client: str = "host") -> dict:
    q = dict(q)
    return tools.get_timeline(token, q.pop("start"), q.pop("end"), client=client, **q)


def test_g1_canary_scan_get_evidence_and_get_timeline(canary_db) -> None:
    for scope_name in GRANT_SCOPES:
        token = auth.create_grant("host", **GRANT_SCOPES[scope_name])
        ghost = tools.get_evidence(token, "evd_987654321", client="host")
        assert ghost["status"] == "not_found"
        for eid in _evidence_ids_all() + _evidence_junk():
            resp = tools.get_evidence(token, eid, client="host")
            _assert_no_canary(json.dumps(resp, ensure_ascii=False), f"get_evidence {eid!r}", scope_name)
            if resp["status"] in ("ok", "source_unavailable"):
                assert resp["evidence"]["bucket"] == "Work" and SHARED_WORD in resp["evidence"]["excerpt"]
            else:
                assert resp == ghost or resp["status"] == "invalid_argument", (eid, resp)
        for q in _timeline_battery():
            resp = _run_timeline(token, q)
            _assert_no_canary(json.dumps(resp, ensure_ascii=False), f"get_timeline {str(q)[:80]}", scope_name)
            assert resp["status"] in {"ok", "no_results", "invalid_argument"}
            if resp["status"] == "ok":
                assert {r["bucket"] for r in resp["results"]} == {"Work"}
                assert all(SHARED_WORD in r["snippet"] for r in resp["results"])
        auth.revoke_grants("host")


def test_g1_canary_scan_new_tools_through_mcp_transport(canary_db) -> None:
    token = auth.create_grant("host", **GRANT_SCOPES["work_claude_only"])
    server = server_for(token)
    c = CANARIES[0]
    calls = [("get_evidence", {"evidence_id": e}) for e in (_evidence_ids_all() + _evidence_junk())[::4]]
    calls += [("get_evidence", {"evidence_id": c}), ("get_evidence", {"evidence_id": [c]}), ("get_evidence", {"evidence_id": 3}),
              ("get_evidence", {}), ("get_evidence", {"evidence_id": "evd_1", "unexpected": c})]
    calls += [("get_timeline", q) for q in _timeline_battery()[::2]]
    calls += [("get_timeline", {"start": c, "end": c}), ("get_timeline", {"start": [c], "end": "2026-01-01"}),
              ("get_timeline", {"start": "2026-01-01", "end": "2026-02-01", "limit": c}),
              ("get_timeline", {"start": "2026-01-01", "end": "2026-02-01", "cursor": {"x": c}}), ("get_timeline", {})]

    async def run():
        out = []
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            for name, args in calls:
                out.append((await client.call_tool(name, args)).model_dump_json())
        return out

    for (name, args), text in zip(calls, asyncio.run(run())):
        _assert_no_canary(text, f"{name} {str(args)[:80]}")


def test_g1_memory_write_scope_through_mcp_transport(canary_db) -> None:
    from penpal.mcp import memory_tools

    owner = auth.create_grant("owner", buckets=["Private"], operations=["write"])
    private = memory_tools.commit_memory(owner, [{"type": "fact", "scope": "global", "content": CANARIES[1]}], client="owner")
    memory_id = private["items"][0]["id"]
    token = auth.create_grant("host", buckets=["Work"], operations=["write"])
    server = server_for(token)
    outside = get_conn().execute(
        "SELECT evidence.id FROM evidence JOIN raw_events ON raw_events.id = evidence.event_id WHERE raw_events.bucket = 'Private' LIMIT 1"
    ).fetchone()[0]

    async def run():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            results = []
            for evidence_id in (f"evd_{outside}", "evd_999999999"):
                args = {"items": [{"type": "fact", "scope": "global", "content": "synthetic", "evidence_ids": [evidence_id]}]}
                results.append(json.loads((await client.call_tool("commit_memory", args)).content[0].text))
            for ident in (memory_id, "mem_999999999"):
                results.append(json.loads((await client.call_tool("update_memory", {"memory_id": ident, "action": "revoke"})).content[0].text))
            return results

    results = asyncio.run(run())
    assert results[0] == results[1] == results[2] == results[3]
    _assert_no_canary(json.dumps(results), "memory writes")


def test_g1_canary_scan_denied_and_failure_paths(canary_db, monkeypatch) -> None:
    token = auth.create_grant("host", buckets=["Work"])
    for bad in (None, "", "kp_wrong", CANARIES[0]):
        _assert_no_canary(json.dumps(search(bad, [CANARIES[0]])), "bad token")
    auth.revoke_grants("host")
    _assert_no_canary(json.dumps(search(token, [CANARIES[0]])), "revoked")

    token2 = auth.create_grant("host2", buckets=["Work"])

    def boom(*a, **k):
        raise sqlite3.OperationalError(f"fts5: syntax error near {CANARIES[0]} in table raw_events_fts")

    monkeypatch.setattr(tools, "search_events", boom)
    resp = search(token2, [CANARIES[0]])
    assert resp["status"] == "error"
    _assert_no_canary(json.dumps(resp), "sqlite error")
    assert "raw_events" not in json.dumps(resp)


def test_g1_sanity_canaries_are_findable_with_enough_scope(canary_db) -> None:
    """Guards against a vacuous scan: the planted data IS reachable when the scope allows it."""
    token = auth.create_grant("wide", buckets=["Work", "Personal", "Private", "Financial"])
    for canary in ("KPCANARY1personalbrowse", "KPCANARY2privatechat", "KPCANARY3bankstatement",
                   "KPCANARY5workothersource", "KPCANARY7samesrcotherbucket", "KPCANARY8keyboardbank",
                   "KPCANARY9keyboardwork", "KPCANARY10clipboardpersonal", "KPCANARY11windowbank",
                   "KPCANARY12axprivate", "KPCANARY13browserpersonal"):
        assert search(token, [canary])["status"] == "ok", canary
    # never reachable by any grant: NULL bucket (plugin or capture) and capture types outside D26
    for canary in ("KPCANARY4nobucketrow", "KPCANARY6ocrneversearchable", "KPCANARY14capturenobucket"):
        assert search(token, [canary])["status"] == "no_results", canary


def test_at05_work_only_host_sees_only_work_in_mixed_hits(canary_db) -> None:
    token = auth.create_grant("work-host", buckets=["Work"])
    resp = search(token, [SHARED_WORD], limit=100)
    assert resp["status"] == "ok"
    assert {r["bucket"] for r in resp["results"]} == {"Work"}
    assert {r["source"] for r in resp["results"]} == {"claude_code", "git_log", "codex_cli", "keyboard_chunk"}
    # 4 in-scope + the Work/codex_cli row + the Work keyboard row; not the 30 Personal bulk rows
    assert len(resp["results"]) == 6
    assert not any("secret note KPCANARY7" in r["snippet"] for r in resp["results"])


def test_g1_history_row_on_bank_site_is_invisible_to_work_grant(tmp_path) -> None:
    """A chrome_history visit to a bank is classified Financial at insert (site table, D28), so a Work-only
    grant must not see it, even though the same grant reads Work rows that share its keyword."""
    from datetime import datetime, timezone

    from penpal.sources.sink import persist_semantic_event
    from penpal.sources.types import SemanticEvent

    canary = "KPCANARY15historybankvisit"
    _seed(tmp_path / "g1_history.db", with_out_of_scope=False, scope="work_claude_only")
    row_id = persist_semantic_event(
        SemanticEvent(
            time=datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc), source="chrome_history", actor="user",
            intent=f"{SHARED_WORD} account page {canary}", artifact="https://www.icbc.com.cn/account",
            raw_ref="synthetic:g1:visit:1", privacy_tier="standard",
            metadata={"profile": "Default", "full_url": "https://www.icbc.com.cn/account?sid=1"},
        ),
        async_named_entities=False,
    )
    assert get_conn().execute("SELECT bucket FROM raw_events WHERE id = ?", (row_id,)).fetchone()[0] == "Financial"
    token = auth.create_grant("host", **GRANT_SCOPES["work_all_sources"])
    for q in ({"query": canary}, {"query": f"{SHARED_WORD} {canary}"}, {"query": SHARED_WORD}, {"keywords": [canary, SHARED_WORD]}):
        resp = _run(token, q)
        assert canary not in json.dumps(resp, ensure_ascii=False), q
        assert {r["bucket"] for r in resp.get("results", [])} <= {"Work"}
    close()


# ── 2. differential ──────────────────────────────────────────────────────────────────────────
_STRIP = {"id", "evidence_id", "occurred_at", "event_id", "next_cursor"}


def _normalize(obj):
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items() if k not in _STRIP}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def _responses_for(db: Path, with_out_of_scope: bool, scope_name: str) -> list[bytes]:
    _seed(db, with_out_of_scope=with_out_of_scope, scope=scope_name)
    token = auth.create_grant("host", **GRANT_SCOPES[scope_name])
    out = [json.dumps(_normalize(_run(token, q)), sort_keys=True, ensure_ascii=False).encode() for q in _differential_queries()]
    dump = lambda r: json.dumps(_normalize(r), sort_keys=True, ensure_ascii=False).encode()
    # In-scope rows are inserted first, so their evidence ids match in both DBs; every id beyond them
    # (extra DB: out-of-scope evidence, clean DB: nonexistent) must be answered identically.
    in_scope_rows = len(IN_SCOPE) + sum(1 for *_, canary in OUT_OF_SCOPE if _is_in_scope(canary, scope_name))
    out += [dump(tools.get_evidence(token, f"evd_{i}", client="host")) for i in range(0, in_scope_rows + 80)]
    out += [dump(tools.get_evidence(token, e, client="host")) for e in _evidence_junk()]
    out += [dump(_run_timeline(token, q)) for q in _timeline_battery(with_id_cursors=False)]
    out.append(dump(tools.get_evidence("kp_nope", "evd_1", client="host")))
    out.append(dump(tools.get_timeline("kp_nope", "2026-01-01", "2026-02-01", client="host")))
    # denied path must also be identical
    out.append(json.dumps(search("kp_nope", [SHARED_WORD]), sort_keys=True).encode())
    close()
    return out


def _differential_mismatches(tmp_path: Path, scope: str) -> list[int]:
    clean = _responses_for(tmp_path / "clean.db", False, scope)
    extra = _responses_for(tmp_path / "extra.db", True, scope)
    assert len(clean) == len(extra)
    return [i for i, (a, b) in enumerate(zip(clean, extra)) if a != b]


@pytest.mark.parametrize("scope", GRANT_SCOPES)
def test_g1_differential_identical_with_and_without_out_of_scope_data(tmp_path, scope) -> None:
    mismatches = _differential_mismatches(tmp_path, scope)
    assert mismatches == [], f"G1 FAIL: {len(mismatches)} responses differ, e.g. query #{mismatches[:3]}"


def test_g1_differential_catches_injected_count_leak(tmp_path, monkeypatch) -> None:
    """The harness must fail when coverage leaks a total count (proves the differential has teeth)."""
    real = tools._coverage

    def leaky(grant, start, end):
        cov = real(grant, start, end)
        cov["total_events"] = get_conn().execute("SELECT count(*) FROM raw_events").fetchone()[0]
        return cov

    monkeypatch.setattr(tools, "_coverage", leaky)
    assert _differential_mismatches(tmp_path, "work_all_sources") != []


def test_g1_differential_catches_injected_evidence_existence_hint(tmp_path, monkeypatch) -> None:
    """get_evidence must not answer differently for 'exists but out of scope' vs 'does not exist'."""
    real = tools.get_evidence

    def hinting(token, evidence_id, **k):
        resp = real(token, evidence_id, **k)
        if resp.get("status") == "not_found" and isinstance(evidence_id, str) and evidence_id[4:].isdigit() and len(evidence_id) < 15:
            n = get_conn().execute("SELECT count(*) FROM evidence WHERE id = ?", (int(evidence_id[4:]),)).fetchone()[0]
            resp = {**resp, "message": resp["message"] + (" (exists elsewhere)" if n else "")}
        return resp

    monkeypatch.setattr(tools, "get_evidence", hinting)
    assert _differential_mismatches(tmp_path, "work_all_sources") != []


def test_g1_differential_catches_injected_timeline_count_leak(tmp_path, monkeypatch) -> None:
    real = tools.get_timeline

    def leaky(*a, **k):
        resp = real(*a, **k)
        if "results" in resp:
            resp["total"] = get_conn().execute("SELECT count(*) FROM raw_events").fetchone()[0]
        return resp

    monkeypatch.setattr(tools, "get_timeline", leaky)
    assert _differential_mismatches(tmp_path, "work_all_sources") != []


def test_g1_differential_catches_injected_existence_hint(tmp_path, monkeypatch) -> None:
    real = tools.search_context

    def hinting(token, keywords, *a, **k):
        resp = real(token, keywords, *a, **k)
        query = keywords[0] if keywords else ""
        if resp["status"] == "no_results":  # "more results exist elsewhere" style hint
            n = get_conn().execute("SELECT count(*) FROM raw_events WHERE content_text LIKE ?", (f"%{query}%",)).fetchone()[0]
            resp["message"] = "no results" if n == 0 else "no results in your scope"
        return resp

    monkeypatch.setattr(tools, "search_context", hinting)
    assert _differential_mismatches(tmp_path, "work_all_sources") != []


# ── 3. coverage guard ────────────────────────────────────────────────────────────────────────
def _field_paths(obj, prefix="") -> set[str]:
    paths: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            paths.add(p)
            paths |= _field_paths(v, p)
    elif isinstance(obj, list):
        for item in obj:
            paths |= _field_paths(item, prefix + "[]")
    return paths


def _observed_response_fields(monkeypatch=None) -> set[str]:
    token = auth.create_grant("guard", buckets=["Work"], sources=["claude_code"])
    base = Path(get_conn().execute("PRAGMA database_list").fetchone()[2]).parent
    real_file = base / "guard.jsonl"
    real_file.write_text("x\n")
    file_event = _insert("claude_code", "Work", f"{SHARED_WORD} guard row", "2026-03-09T09:00:00+00:00")
    add_evidence(file_event, str(real_file), 1, None)
    repo = base / "guard-repo"
    repo.mkdir()
    genv = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "--allow-empty", "-m", "m"]):
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args], cwd=repo,
                       check=True, capture_output=True, env=genv)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, env=genv).stdout.strip()
    commit_event = _insert("claude_code", "Work", f"{SHARED_WORD} guard commit", "2026-03-09T10:00:00+00:00")
    add_evidence(commit_event, f"{repo}@{sha}", None, None)

    def evd(event_id):
        return "evd_%d" % get_conn().execute("SELECT MAX(id) FROM evidence WHERE event_id = ?", (event_id,)).fetchone()[0]

    capture_event = _insert("claude_code", "Work", f"{SHARED_WORD} guard keystrokes", "2026-03-09T11:00:00+00:00", "keyboard_chunk")
    unavailable = get_conn().execute("SELECT id FROM evidence WHERE locator LIKE '/synthetic/claude_code%' LIMIT 1").fetchone()[0]
    ev = lambda i: tools.get_evidence(token, i, client="guard")
    tl = lambda *a, **k: tools.get_timeline(token, *a, client="guard", **k)
    responses = [
        ev(evd(file_event)), ev(evd(commit_event)),                   # ok: file locator / repo+commit locator
        ev(evd(capture_event)),                                       # ok: penpal locator (capture event)
        ev(f"evd_{unavailable}"),                                     # source_unavailable
        ev("evd_99999"), ev(5),                                       # not_found / invalid_argument
        tl("2026-03-01", "2026-03-10", limit=1),                      # ok with has_more + next_cursor
        tl("2026-03-01", "2026-03-10"), tl("2030-01-01", "2030-01-02"), tl("bad", "worse"),
        tools.get_evidence("kp_nope", "evd_1", client="guard"),
        tools.get_timeline("kp_nope", "2026-01-01", "2026-01-02", client="guard"),
        search(token, [SHARED_WORD]),                                 # ok
        search(token, ["zzz-no-such-thing"]),                         # no_results
        search(token, [SHARED_WORD], time_range={"start": "bad"}),    # invalid_argument
        search("kp_nope", [SHARED_WORD]),                               # permission_denied
    ]
    fields: set[str] = set()
    for r in responses:
        fields |= _field_paths(r)
    return fields


def test_g1_coverage_guard_tools_and_arguments(canary_db) -> None:
    server = build_server(token_provider=lambda: None)

    async def listing():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return (await client.list_tools()).tools

    registered = {t.name: set(t.inputSchema.get("properties", {})) for t in asyncio.run(listing())}
    assert registered == G1_COVERED_TOOLS, (
        "MCP tool surface changed: extend the G1 canary/differential batteries in tests/test_g1_leakage.py "
        f"and update G1_COVERED_TOOLS. registered={registered}"
    )


def test_g1_coverage_guard_response_fields(canary_db) -> None:
    observed = _observed_response_fields()
    uncovered = observed - G1_COVERED_RESPONSE_FIELDS
    assert not uncovered, f"new MCP response fields not covered by G1: {sorted(uncovered)}"


def test_g1_coverage_guard_fails_on_new_field(canary_db, monkeypatch) -> None:
    real = tools._coverage
    monkeypatch.setattr(tools, "_coverage", lambda g, s, e: {**real(g, s, e), "hit_total": 3})
    observed = _observed_response_fields()
    assert "coverage.hit_total" in observed - G1_COVERED_RESPONSE_FIELDS


# ── 4. D28: browser content follows the site, never the browser app ──────────────────────────
def _assert_no_canary_value(text: str, canary: str) -> None:
    assert canary not in text and canary.lower() not in text.lower(), f"G1 FAIL: {canary} leaked to a Work-only grant"


def test_g1_canary_financial_site_in_browser_is_unreachable_for_work_grant(tmp_path) -> None:
    """A banking tab in Chrome: the page text, clipboard, window and typing that follow the browser_url
    row are Financial, so a Work-only grant must not find them, while the same browser on a Work site is
    found. Rows are inserted with no preset bucket, so the live resolution path itself is under test."""
    from penpal.capture.normalizer import (
        normalize_ax_text_event, normalize_browser_url_event, normalize_clipboard_event,
        normalize_keyboard_chunk_event, normalize_window_event,
    )

    close()
    init_db(tmp_path / "d28.db")

    def put(ev):
        ev.content_hash = f"d28-{ev.source}-{ev.ts_start}-{ev.app_name}"
        return insert_raw_event(ev)

    bank = ["KPCANARY20bankax", "KPCANARY21bankclip", "KPCANARY22bankwin", "KPCANARY23bankkey", "KPCANARY24bankdocs"]
    put(normalize_browser_url_event("https://www.icbc.com.cn/login?x=1", "Online banking", "Google Chrome", ts_start="2026-03-02T10:00:00+00:00"))
    put(normalize_ax_text_event(f"{SHARED_WORD} balance {bank[0]} shown", app_name="Google Chrome", window_title="Online banking - Google Chrome", ts_start="2026-03-02T10:01:00+00:00"))
    put(normalize_clipboard_event(f"{SHARED_WORD} copied {bank[1]}", app_name="Google Chrome", process_name="com.google.Chrome", ts_start="2026-03-02T10:02:00+00:00"))
    put(normalize_window_event("window_focus_session", "Google Chrome", f"Online banking - {SHARED_WORD} {bank[2]}", None, ts_start="2026-03-02T10:03:00+00:00", ts_end="2026-03-02T10:04:00+00:00"))
    put(normalize_keyboard_chunk_event(f"{SHARED_WORD} typed {bank[3]} here", app_name="Google Chrome", ts_start="2026-03-02T10:05:00+00:00"))
    # a docs.* host must not outrank Financial
    put(normalize_browser_url_event("https://docs.mybank.example/guide", "Docs", "Safari", ts_start="2026-03-02T10:00:00+00:00"))
    put(normalize_ax_text_event(f"{SHARED_WORD} guide {bank[4]} text", app_name="Safari", window_title="Docs - Safari", ts_start="2026-03-02T10:01:00+00:00"))
    # a Work site in the same browser stays reachable (the scan is not vacuous)
    put(normalize_browser_url_event("https://github.com/org/repo", "repo", "Google Chrome", ts_start="2026-03-02T11:00:00+00:00"))
    put(normalize_ax_text_event(f"{SHARED_WORD} review KPCANARYWORKOK comments", app_name="Google Chrome", window_title="repo - Google Chrome", ts_start="2026-03-02T11:01:00+00:00"))
    # a stale URL (> 10 min) must not lend its bucket: this text falls back to Personal
    put(normalize_ax_text_event(f"{SHARED_WORD} later KPCANARY25stale text", app_name="Google Chrome", window_title="repo - Google Chrome", ts_start="2026-03-02T11:30:00+00:00"))

    work = auth.create_grant("work-host", buckets=["Work"])
    for canary in bank + ["KPCANARY25stale"]:
        for q in (canary, canary.lower(), canary[:10], f"{SHARED_WORD} {canary}"):
            _assert_no_canary_value(json.dumps(search(work, q.split(), limit=100), ensure_ascii=False), canary)
    ok = search(work, ["KPCANARYWORKOK"])
    assert ok["status"] == "ok" and {r["bucket"] for r in ok["results"]} == {"Work"}
    wide = auth.create_grant("wide", buckets=["Work", "Personal", "Financial"])
    for canary in bank:
        assert search(wide, [canary])["status"] == "ok", canary
    close()
