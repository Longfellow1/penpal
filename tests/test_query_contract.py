"""P0-05b: the query contract. The AI host extracts keywords; the server matches (OR), ranks by
keyword coverage, tiers results strong/weak, and refuses sentence-like keywords. Synthetic data only."""
from __future__ import annotations

import asyncio

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_helpers import search, server_for
from penpal.mcp import auth, tools
from penpal.store.db import close, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event


@pytest.fixture
def token(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    close()
    init_db(tmp_path / "q.db")
    rows = [
        ("2026-05-01T09:00:00+00:00", "visa checklist for the trip 签证材料 list"),   # visa + 签证 + checklist
        ("2026-05-02T09:00:00+00:00", "visa appointment booked"),                     # visa only
        ("2026-05-03T09:00:00+00:00", "unrelated grocery note"),
        ("2026-05-04T09:00:00+00:00", "护照 renewal form"),                           # 2-char CJK only
    ]
    for i, (ts, text) in enumerate(rows):
        insert_raw_event(RawEvent(source="claude_code", event_type="semantic_event", ts_start=ts,
                                  content_text=text, content_hash=f"h{i}", bucket="Work"))
    yield auth.create_grant("host", buckets=["Work"])
    close()


def test_or_matching_ranks_by_keyword_coverage_then_recency(token) -> None:
    resp = search(token, ["visa", "签证", "checklist"])
    assert resp["status"] == "ok"
    snippets = [r["snippet"] for r in resp["results"]]
    assert snippets[0].startswith("visa checklist")   # 3 keywords beats 1 keyword although older
    assert snippets[1].startswith("visa appointment")
    assert len(snippets) == 2                          # OR, not AND: partial matches are returned


def test_two_tiers_strong_and_weak(token) -> None:
    resp = search(token, ["visa", "checklist"])
    assert [r["match"] for r in resp["results"]] == ["strong", "weak"]
    assert resp["results"][0]["match_reason"] == "keyword match: 2 of 2 keywords"
    only = search(token, ["visa"])                     # a single keyword that matched is a clear hit
    assert {r["match"] for r in only["results"]} == {"strong"}


def test_match_reason_never_echoes_keywords(token) -> None:
    resp = search(token, ["visa", "checklist"])
    assert all("visa" not in r["match_reason"] and "checklist" not in r["match_reason"] for r in resp["results"])


def test_two_char_cjk_keyword_matches(token) -> None:
    assert search(token, ["护照"])["status"] == "ok"
    assert search(token, ["护照", "nothing-else"])["results"][0]["match"] == "weak"


@pytest.mark.parametrize("bad", [
    "这个功能当时边界是怎么定的呢我想知道具体原因", "what was the name of the thing I looked at last week",
    "上次的签证材料？", "上次的签证材料呢?", "x" * 101,
])
def test_sentence_like_keyword_is_refused_with_fixed_text(token, bad) -> None:
    resp = search(token, [bad])
    assert resp == {"status": "invalid_argument", "message": tools.SENTENCE_KEYWORD_MESSAGE}
    assert bad not in str(resp)
    assert search(token, ["visa", bad])["status"] == "invalid_argument"


@pytest.mark.parametrize("bad", [[], "visa", None, [""], ["  "], [1], [["visa"]], ["k"] * 11])
def test_malformed_keywords_are_refused_with_fixed_text(token, bad) -> None:
    assert search(token, bad) == {"status": "invalid_argument", "message": tools.KEYWORDS_MESSAGE}


def test_schema_requires_keywords_and_describes_the_contract(token) -> None:
    server = server_for(token)

    async def listing():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return (await client.list_tools()).tools, client.get_server_capabilities()

    tool_list, _ = asyncio.run(listing())
    tool = next(t for t in tool_list if t.name == "search_context")
    assert tool.inputSchema["required"] == ["keywords"]
    assert tool.inputSchema["properties"]["keywords"]["type"] == "array"
    for must in ("keywords", "Chinese and English", "time_range", "zsh_history", "retry", "签证"):
        assert must in tool.description
    assert "keywords" in server.instructions and "retry" in server.instructions
