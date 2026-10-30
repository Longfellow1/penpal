"""Issue #30: `penpal purge` deletes raw_events AND everything derived from them.

Covers the store-layer cascade (raw text, FTS, evidence, MCP ledgers, search docs, sessions), the
exact --app / --source selectors, dry run, atomicity, the CLI prompt and per-table output, and the
MCP read path (search_context / get_evidence) after deletion. Synthetic data only.
"""
from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

import penpal.cli.data as cli_module
from penpal.capture.normalizer import (
    normalize_clipboard_event,
    normalize_keyboard_chunk_event,
    normalize_manual_event,
)
from penpal.config import Config
from penpal.mcp import auth, tools
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.models import RawEvent, SearchDoc, Session
from penpal.store.repository import insert_raw_event, insert_search_doc, purge_raw_events, upsert_session
from mcp_helpers import search

T1 = "2026-03-02T10:01:00+00:00"
T2 = "2026-03-02T10:02:00+00:00"
T3 = "2026-03-02T10:03:00+00:00"
ALL_BUCKETS = ["Work", "Personal", "Financial", "Private"]
CLIENT = "h"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def seeded(tmp_path):
    buckets.reset_cache()
    db_path = tmp_path / "kp.db"
    init_db(db_path)
    ids = {}
    ids["notes_kb"] = insert_raw_event(normalize_keyboard_chunk_event(
        "zebracorn draft typed in the notes app", app_name="Notes", ts_start=T1))
    ids["notes_clip"] = insert_raw_event(normalize_clipboard_event(
        "zebraclip copied from notes", app_name="Notes", ts_start=T2))
    ids["notes_manual"] = insert_raw_event(normalize_manual_event("zebramanual standalone note", app_name="Notes"))
    ids["notes_pro"] = insert_raw_event(normalize_keyboard_chunk_event(
        "zebrapro contract text", app_name="Notes Pro", ts_start=T1))
    ids["sticky"] = insert_raw_event(normalize_keyboard_chunk_event(
        "zebrasticky reminder", app_name="StickyNotes", ts_start=T1))
    ids["cursor"] = insert_raw_event(normalize_keyboard_chunk_event(
        "zebracursor code comment", app_name="Cursor", ts_start=T3))
    # Session linked only to Notes events: must disappear. Session with a Cursor event: must stay.
    ids["sess_notes_event"] = insert_raw_event(RawEvent(
        source="window", event_type="window_focus_session", ts_start=T1, app_name="Notes",
        window_title="zebrawindow title", session_id="sess-notes"))
    ids["sess_mixed_notes"] = insert_raw_event(RawEvent(
        source="window", event_type="window_focus_session", ts_start=T2, app_name="Notes",
        window_title="mixed", session_id="sess-mixed"))
    ids["sess_mixed_cursor"] = insert_raw_event(RawEvent(
        source="window", event_type="window_focus_session", ts_start=T3, app_name="Cursor",
        window_title="mixed", session_id="sess-mixed"))
    for sid, app in (("sess-notes", "Notes"), ("sess-mixed", "Notes")):
        upsert_session(Session(id=sid, started_at=T1, ended_at=T3, app_name=app))

    # search_docs: manual and clipboard docs point at raw event ids (as capture writes them).
    insert_search_doc(SearchDoc(ref_type="manual", ref_id=str(ids["notes_manual"]), title="m",
                                body="zebramanual standalone note", created_at=T1))
    insert_search_doc(SearchDoc(ref_type="clipboard", ref_id=str(ids["notes_clip"]), title="c",
                                body="zebraclip copied from notes", created_at=T2))
    insert_search_doc(SearchDoc(ref_type="clipboard", ref_id=str(ids["cursor"]), title="c2",
                                body="zebracursor code comment", created_at=T3))

    # MCP ledgers: what a client was shown and the verdicts it filed about it.
    conn = get_conn()
    ev_notes = conn.execute("SELECT id FROM evidence WHERE event_id = ?", (ids["notes_kb"],)).fetchone()[0]
    ev_cursor = conn.execute("SELECT id FROM evidence WHERE event_id = ?", (ids["cursor"],)).fetchone()[0]
    ids["evidence_notes"] = ev_notes
    ids["evidence_cursor"] = ev_cursor
    for event_id, evidence_id, query in ((ids["notes_kb"], ev_notes, "zebracorn"),
                                         (ids["cursor"], ev_cursor, "zebracursor")):
        conn.execute(
            "INSERT INTO mcp_returned_results(client_id, event_id, evidence_id, returned_at, query) VALUES (?,?,?,?,?)",
            (CLIENT, event_id, evidence_id, T1, query),
        )
        conn.execute(
            "INSERT INTO mcp_feedback(created_at, client_id, verdict, event_id, evidence_id, query, note, grant_buckets)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (T1, CLIENT, "wrong", event_id, evidence_id, query, "note about " + query, '["Work"]'),
        )
    conn.commit()
    yield ids
    close()
    buckets.reset_cache()


def _grant():
    return auth.create_grant(CLIENT, buckets=list(ALL_BUCKETS))


def _count(sql: str, params=()) -> int:
    return get_conn().execute(sql, params).fetchone()[0]


def _fts(term: str) -> int:
    return _count("SELECT COUNT(*) FROM raw_events_fts WHERE raw_events_fts MATCH ?", (f'"{term}"',))


def _search_docs(term: str) -> int:
    return _count("SELECT COUNT(*) FROM search_docs_fts WHERE search_docs_fts MATCH ?", (f'"{term}"',))


def _raw_text(term: str) -> int:
    like = f"%{term}%"
    return _count(
        "SELECT COUNT(*) FROM raw_events WHERE content_text LIKE ? OR window_title LIKE ? OR app_name LIKE ?",
        (like, like, like),
    )


def _snapshot() -> dict[str, int]:
    tables = ("raw_events", "evidence", "mcp_returned_results", "mcp_feedback", "search_docs", "sessions")
    return {t: _count(f"SELECT COUNT(*) FROM {t}") for t in tables}


# ── store-layer cascade ─────────────────────────────────────────────────────────────────────
def test_app_delete_removes_raw_text_fts_evidence_ledgers_docs_and_sessions(seeded):
    counts = purge_raw_events(app_name="Notes")

    assert counts == {
        "raw_events": 5,  # notes_kb, notes_clip, notes_manual, sess_notes_event, sess_mixed_notes
        "raw_events_fts": 5,
        "evidence": 4,  # keyboard, clipboard and both window sessions; the manual note has none
        "mcp_returned_results": 1,
        "mcp_feedback": 1,
        "search_docs": 2,  # manual doc + clipboard doc for Notes
        "sessions": 1,  # sess-notes only; sess-mixed still has a Cursor event
    }
    for term in ("zebracorn", "zebraclip", "zebramanual", "zebrawindow"):
        assert _raw_text(term) == 0, term
        assert _fts(term) == 0, term
    for term in ("zebramanual", "zebraclip"):
        assert _search_docs(term) == 0, term
    assert _count("SELECT COUNT(*) FROM evidence WHERE id = ?", (seeded["evidence_notes"],)) == 0
    assert _count("SELECT COUNT(*) FROM mcp_returned_results WHERE event_id = ?", (seeded["notes_kb"],)) == 0
    assert _count("SELECT COUNT(*) FROM mcp_feedback WHERE event_id = ?", (seeded["notes_kb"],)) == 0
    assert _count("SELECT COUNT(*) FROM sessions WHERE id = 'sess-notes'") == 0
    assert _count("SELECT COUNT(*) FROM sessions WHERE id = 'sess-mixed'") == 1


def test_exact_app_match_leaves_similar_app_names_alone(seeded):
    purge_raw_events(app_name="Notes")

    assert _raw_text("zebrapro") == 1 and _fts("zebrapro") == 1
    assert _raw_text("zebrasticky") == 1 and _fts("zebrasticky") == 1
    assert _fts("zebracursor") == 1
    assert _count("SELECT COUNT(*) FROM evidence WHERE id = ?", (seeded["evidence_cursor"],)) == 1
    assert _count("SELECT COUNT(*) FROM mcp_feedback WHERE event_id = ?", (seeded["cursor"],)) == 1


def test_app_match_is_case_insensitive_but_not_substring(seeded):
    counts = purge_raw_events(app_name="notes")
    assert counts["raw_events"] == 5  # "notes" matches Notes, not Notes Pro / StickyNotes
    assert _raw_text("zebrapro") == 1
    assert _raw_text("zebrasticky") == 1


def test_source_selector_deletes_only_that_source(seeded):
    counts = purge_raw_events(source="clipboard")

    assert counts["raw_events"] == 1
    assert counts["search_docs"] == 1  # the clipboard doc for notes_clip
    assert _raw_text("zebraclip") == 0 and _fts("zebraclip") == 0
    assert _fts("zebracorn") == 1  # keyboard chunk from Notes untouched
    assert _count("SELECT COUNT(*) FROM search_docs WHERE ref_type = 'clipboard' AND ref_id = ?",
                  (str(seeded["cursor"]),)) == 1


def test_keyboard_chunk_rows_are_deleted_with_their_derived_data(seeded):
    purge_raw_events(source="keyboard_chunk")

    assert _count("SELECT COUNT(*) FROM raw_events WHERE source = 'keyboard_chunk'") == 0
    assert _fts("zebracorn") == 0 and _fts("zebracursor") == 0
    assert _count("SELECT COUNT(*) FROM evidence WHERE event_id IN (?, ?)",
                  (seeded["notes_kb"], seeded["cursor"])) == 0
    assert _count("SELECT COUNT(*) FROM mcp_feedback WHERE event_id = ?", (seeded["cursor"],)) == 0
    assert _fts("zebraclip") == 1  # other sources untouched


def test_time_range_selector_is_inclusive_on_ts_start(seeded):
    counts = purge_raw_events(since=T2, until=T2)
    assert counts["raw_events"] == 2  # notes_clip (T2) and sess_mixed_notes (T2)
    assert _fts("zebraclip") == 0
    assert _fts("zebracorn") == 1


def test_dry_run_reports_the_same_counts_and_deletes_nothing(seeded):
    before = _snapshot()
    dry = purge_raw_events(app_name="Notes", dry_run=True)
    assert _snapshot() == before
    assert _fts("zebracorn") == 1

    real = purge_raw_events(app_name="Notes")
    assert dry == real
    assert _snapshot() != before


def test_empty_selector_is_refused(seeded):
    before = _snapshot()
    with pytest.raises(ValueError):
        purge_raw_events()
    assert _snapshot() == before


def test_failure_mid_cascade_rolls_back_everything(seeded):
    # Make one derived delete fail: the whole purge must be undone, including raw_events.
    get_conn().execute(
        "CREATE TRIGGER boom BEFORE DELETE ON mcp_feedback BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    get_conn().commit()
    before = _snapshot()
    with pytest.raises(sqlite3.DatabaseError):
        purge_raw_events(app_name="Notes")
    assert _snapshot() == before
    assert _fts("zebracorn") == 1


# ── MCP read path after deletion ────────────────────────────────────────────────────────────
def test_search_context_returns_nothing_for_deleted_content(seeded):
    token = _grant()
    assert search(token, ["zebracorn"], limit=20)["results"]
    assert search(token, ["zebraclip"], limit=20)["results"]

    purge_raw_events(app_name="Notes")

    for term in ("zebracorn", "zebraclip", "zebramanual", "zebrawindow"):
        assert search(token, [term], limit=20)["results"] == [], term
    assert search(token, ["zebracursor"], limit=20)["results"]  # other app still searchable


def test_get_evidence_for_deleted_id_is_not_found(seeded):
    token = _grant()
    evd = f"evd_{seeded['evidence_notes']}"
    before = tools.get_evidence(token, evd, client=CLIENT)
    assert before["status"] == "ok"

    purge_raw_events(app_name="Notes")

    after = tools.get_evidence(token, evd, client=CLIENT)
    assert after["status"] == "not_found"
    assert "evidence" not in after


def test_get_timeline_hides_deleted_events_but_keeps_other_apps(seeded):
    token = _grant()

    before = tools.get_timeline(
        token, "2026-03-02", "2026-03-02", sources=["keyboard_chunk"], client=CLIENT
    )
    assert before["status"] == "ok"
    before_ids = {row["id"] for row in before["results"]}
    deleted_id = f"evt_{seeded['notes_kb']}"
    retained_id = f"evt_{seeded['cursor']}"
    assert deleted_id in before_ids
    assert retained_id in before_ids

    purge_raw_events(app_name="Notes")

    after = tools.get_timeline(
        token, "2026-03-02", "2026-03-02", sources=["keyboard_chunk"], client=CLIENT
    )
    assert after["status"] == "ok"
    after_ids = {row["id"] for row in after["results"]}
    assert deleted_id not in after_ids
    assert retained_id in after_ids


# ── CLI ────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def cli_db(seeded, tmp_path, monkeypatch):
    cfg = Config.model_validate({"app": {"db_path": str(tmp_path / "kp.db")}})
    monkeypatch.setattr(cli_module, "get_config", lambda: cfg)
    return seeded


def test_cli_dry_run_prints_counts_and_deletes_nothing(cli_db):
    before = _snapshot()
    result = CliRunner().invoke(cli_module.main, ["purge", "--app", "Notes", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "raw_events: 5" in result.output
    assert "evidence: 4" in result.output
    assert "Dry run" in result.output
    assert _snapshot() == before


def test_cli_prompt_declined_deletes_nothing(cli_db):
    before = _snapshot()
    result = CliRunner().invoke(cli_module.main, ["purge", "--app", "Notes"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    assert _snapshot() == before


def test_cli_confirmed_app_purge_prints_per_table_counts(cli_db):
    result = CliRunner().invoke(cli_module.main, ["purge", "--app", "Notes", "--confirm"])
    assert result.exit_code == 0, result.output
    assert "Deleted:" in result.output
    for line in ("raw_events: 5", "evidence: 4","search_docs: 2", "sessions: 1"):
        assert line in result.output
    assert _raw_text("zebrapro") == 1
    assert _raw_text("zebracorn") == 0


def test_cli_source_flag_deletes_only_that_source(cli_db):
    result = CliRunner().invoke(cli_module.main, ["purge", "--source", "clipboard", "--confirm"])
    assert result.exit_code == 0, result.output
    assert _fts("zebraclip") == 0
    assert _fts("zebracorn") == 1


def test_cli_requires_a_selector(cli_db):
    result = CliRunner().invoke(cli_module.main, ["purge", "--confirm"])
    assert result.exit_code != 0
    assert _snapshot()["raw_events"] > 0


def test_partially_emptied_session_is_rebuilt_without_deleted_text(seeded):
    get_conn().execute("UPDATE sessions SET primary_window_title = 'zebrasecret title', summary = 'zebrasecret summary', "
                       "event_count = 2 WHERE id = 'sess-mixed'")
    get_conn().execute("UPDATE raw_events SET window_title = 'zebrasecret title' WHERE id = ?", (seeded["sess_mixed_notes"],))
    get_conn().execute("UPDATE raw_events SET window_title = 'cursor editor' WHERE id = ?", (seeded["sess_mixed_cursor"],))
    get_conn().commit()
    purge_raw_events(app_name="Notes")
    row = get_conn().execute("SELECT event_count, primary_window_title, summary FROM sessions WHERE id = 'sess-mixed'").fetchone()
    assert tuple(row) == (1, "cursor editor", None)
