"""M2 (issue #57): every source plugin writes an evidence locator at ingest, get_evidence resolves it,
and migration v28 backfills existing rows from stored metadata only. Synthetic fixtures only."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from penpal.mcp import auth, tools
from penpal.sources import sink
from penpal.sources.plugins import spotlight as spotlight_mod
from penpal.sources.plugins.approved_sqlite import ApprovedSqliteSource
from penpal.sources.plugins.chrome_history import ChromeHistorySource
from penpal.sources.plugins.knowledgec import KnowledgeCSource
from penpal.sources.plugins.markdown_vault import MarkdownVaultSource
from penpal.sources.plugins.safari_history import SafariHistorySource
from penpal.sources.plugins.spotlight import SpotlightSource
from penpal.sources.plugins.zsh_history import ZshHistorySource
from penpal.sources.types import DataSourceInstance
from penpal.store import evidence_original
from penpal.store.buckets import BUCKETS
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v28 import migrate_v28
from penpal.store.migrations import MIGRATIONS, _apply_transactional
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from mcp_helpers import client_of

SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)
UNTIL = datetime(2030, 1, 1, tzinfo=timezone.utc)
WEBKIT_US = 11_644_473_600 * 1_000_000
CF_S = 978_307_200
UNAVAILABLE_ZH = "原始出处已不可访问"


@pytest.fixture
def db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "t.db")
    yield tmp_path
    close()


def _get(token, evidence_id):
    return tools.get_evidence(token, evidence_id, client=client_of(token))


def _ingest(events) -> list[int]:
    ids = [sink.persist_semantic_event(e, async_named_entities=False) for e in events]
    assert all(i > 0 for i in ids)
    return ids


def _evd(event_id: int) -> str:
    return f"evd_{get_conn().execute('SELECT id FROM evidence WHERE event_id = ?', (event_id,)).fetchone()[0]}"


def _evidence_row(event_id: int):
    return get_conn().execute("SELECT * FROM evidence WHERE event_id = ?", (event_id,)).fetchall()


def _token() -> str:
    return auth.create_grant("host", buckets=list(BUCKETS))


# ── zsh_history ──────────────────────────────────────────────────────────────────────────────
def test_zsh_history_round_trip_and_missing_file(db):
    hist = db / "zsh_hist"
    hist.write_text(": 1790000000:0;echo first\n: 1790000100:3;ls -la notes\n")
    src = ZshHistorySource(history_path=hist)
    events = list(src.read(src.discover()[0], SINCE, UNTIL))
    rid = _ingest(events)[1]
    row = _evidence_row(rid)[0]
    assert row["locator"] == str(hist.resolve()) and row["offset_or_line"] == 2 and row["available"] == 1
    resp = _get(_token(), _evd(rid))
    assert resp["status"] == "ok"
    assert resp["evidence"]["locator"] == {"type": "file", "path": str(hist.resolve()), "line": 2}
    assert "ls -la notes" in resp["evidence"]["excerpt"]
    hist.unlink()
    gone = _get(_token(), _evd(rid))
    assert gone["status"] == "source_unavailable" and UNAVAILABLE_ZH in gone["message"]
    assert gone["evidence"]["locator"] is None


# ── markdown_vault ───────────────────────────────────────────────────────────────────────────
def test_markdown_vault_round_trip_and_missing_file(db):
    vault = db / "vault"
    vault.mkdir()
    note = vault / "idea.md"
    note.write_text("---\ntags: x\n---\n\n# Quokka plan\nbody\n")
    src = MarkdownVaultSource(roots=[vault])
    events = list(src.read(src.discover()[0], SINCE, UNTIL))
    rid = _ingest(events)[0]
    row = _evidence_row(rid)[0]
    assert row["locator"] == str(note.resolve()) and row["offset_or_line"] == 5
    resp = _get(_token(), _evd(rid))
    assert resp["status"] == "ok" and resp["evidence"]["locator"]["line"] == 5
    assert "Quokka plan" in resp["evidence"]["excerpt"]
    note.unlink()
    assert _get(_token(), _evd(rid))["status"] == "source_unavailable"


# ── chrome_history / safari_history ──────────────────────────────────────────────────────────
def _chrome_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
    c.execute("INSERT INTO urls VALUES (1, 'https://example.com/page?x=1', 'Synthetic Page')")
    ts = datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc).timestamp()
    c.execute("INSERT INTO visits VALUES (10, 1, ?)", (int(ts * 1_000_000) + WEBKIT_US,))
    c.commit()
    c.close()


def _safari_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE history_items (id INTEGER PRIMARY KEY, url TEXT)")
    c.execute("CREATE TABLE history_visits (id INTEGER PRIMARY KEY, history_item INTEGER, title TEXT, visit_time REAL)")
    c.execute("INSERT INTO history_items VALUES (1, 'https://example.org/doc')")
    ts = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc).timestamp()
    c.execute("INSERT INTO history_visits VALUES (20, 1, 'Synthetic Doc', ?)", (ts - CF_S,))
    c.commit()
    c.close()


def test_chrome_round_trip_locked_db_and_missing(db):
    hdb = db / "chrome" / "Default" / "History"
    _chrome_db(hdb)
    src = ChromeHistorySource(profiles_root=db / "chrome")
    rid = _ingest(list(src.read(src.discover()[0], SINCE, UNTIL)))[0]
    row = _evidence_row(rid)[0]
    assert row["locator"] == f"{hdb.resolve()}#visit:10" and row["available"] == 1
    resp = _get(_token(), _evd(rid))
    assert resp["status"] == "ok"
    ev = resp["evidence"]
    assert ev["locator"] == {"type": "browser_visit", "db": str(hdb.resolve()), "visit_id": 10}
    assert ev["original"] == {
        "kind": "browser_visit", "title": "Synthetic Page", "url": "https://example.com/page?x=1",
        "visited_at": "2026-04-28T01:00:00+00:00",
    }
    # Browser running: an exclusive lock makes the in-place read-only open fail; the temp copy still works.
    locker = sqlite3.connect(hdb, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        from penpal.foundation.sqlite_rules import is_blocked_sqlite
        from penpal.sources.cleaning.path_filter import is_excluded_path

        original = evidence_original.read_original(
            "chrome_history",
            row["locator"],
            is_blocked_sqlite=is_blocked_sqlite,
            is_excluded_path=is_excluded_path,
        )
        assert original["title"] == "Synthetic Page"
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    # The browser DB itself is never modified by the read.
    before = hdb.read_bytes()
    _get(_token(), _evd(rid))
    assert hdb.read_bytes() == before
    hdb.unlink()
    gone = _get(_token(), _evd(rid))
    assert gone["status"] == "source_unavailable" and "original" not in gone["evidence"]


def test_chrome_visit_row_removed_is_unavailable_and_stored(db):
    hdb = db / "chrome" / "Default" / "History"
    _chrome_db(hdb)
    src = ChromeHistorySource(profiles_root=db / "chrome")
    rid = _ingest(list(src.read(src.discover()[0], SINCE, UNTIL)))[0]
    c = sqlite3.connect(hdb)
    c.execute("DELETE FROM visits")
    c.commit()
    c.close()
    assert _get(_token(), _evd(rid))["status"] == "source_unavailable"
    assert _evidence_row(rid)[0]["available"] == 0


def test_safari_round_trip_and_missing(db):
    hdb = db / "safari" / "History.db"
    _safari_db(hdb)
    src = SafariHistorySource(history_path=hdb)
    rid = _ingest(list(src.read(src.discover()[0], SINCE, UNTIL)))[0]
    assert _evidence_row(rid)[0]["locator"] == f"{hdb.resolve()}#visit:20"
    ev = _get(_token(), _evd(rid))["evidence"]
    assert ev["original"]["title"] == "Synthetic Doc" and ev["original"]["url"] == "https://example.org/doc"
    assert ev["original"]["visited_at"] == "2026-04-28T02:00:00+00:00"
    hdb.unlink()
    assert _get(_token(), _evd(rid))["status"] == "source_unavailable"


def test_browser_original_url_is_redacted(db):
    hdb = db / "chrome" / "Default" / "History"
    _chrome_db(hdb)
    c = sqlite3.connect(hdb)
    c.execute("UPDATE urls SET url = 'https://example.com/?u=someone@example.com'")
    c.commit()
    c.close()
    src = ChromeHistorySource(profiles_root=db / "chrome")
    rid = _ingest(list(src.read(src.discover()[0], SINCE, UNTIL)))[0]
    assert "someone@example.com" not in json.dumps(_get(_token(), _evd(rid)))


# ── spotlight ────────────────────────────────────────────────────────────────────────────────
def _spotlight_events(monkeypatch, root: Path, files: list[Path]):
    monkeypatch.setattr(spotlight_mod, "_run_mdfind", lambda **_: [f.resolve() for f in files])
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(spotlight_mod, "_run_mdls", lambda p: {
        "kMDItemDisplayName": p.name, "kMDItemContentModificationDate": now, "kMDItemPath": str(p),
    })
    inst = DataSourceInstance(plugin="spotlight", locator=str(root), label="Documents", metadata={})
    return list(SpotlightSource().read(inst, SINCE, datetime.now(timezone.utc) + timedelta(days=1)))


def test_spotlight_round_trip_text_binary_and_missing(db, monkeypatch):
    root = db / "Documents"
    root.mkdir()
    txt = root / "meeting.md"
    txt.write_text("# Quokka sync\nagenda items\n")
    big = root / "big.txt"
    big.write_text("x" * (evidence_original.MAX_TEXT_FILE_BYTES + 10))
    binary = root / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02binary")
    nul_txt = root / "nul.txt"
    nul_txt.write_bytes(b"abc\x00def")
    files = [txt, big, binary, nul_txt]
    ids = _ingest(_spotlight_events(monkeypatch, root, files))
    by_name = {Path(_evidence_row(i)[0]["locator"]).name: i for i in ids}
    assert set(by_name) == {"meeting.md", "big.txt", "blob.bin", "nul.txt"}
    assert _evidence_row(by_name["meeting.md"])[0]["offset_or_line"] is None

    token = _token()
    ev = _get(token, _evd(by_name["meeting.md"]))["evidence"]
    assert ev["locator"]["type"] == "file"
    assert ev["original"]["exists"] is True and ev["original"]["size_bytes"] == txt.stat().st_size
    assert ev["original"]["modified_at"] and "Quokka sync" in ev["original"]["text"]
    for name in ("big.txt", "blob.bin", "nul.txt"):
        orig = _get(token, _evd(by_name[name]))["evidence"]["original"]
        assert orig["exists"] is True and orig["text"] is None, name
    txt.unlink()
    assert _get(token, _evd(by_name["meeting.md"]))["status"] == "source_unavailable"


def test_spotlight_text_is_redacted(db, monkeypatch):
    root = db / "Documents"
    root.mkdir()
    f = root / "note.txt"
    f.write_text("deploy key: sk-ant-api03-" + "A" * 40 + "\n")
    rid = _ingest(_spotlight_events(monkeypatch, root, [f]))[0]
    text = _get(_token(), _evd(rid))["evidence"]["original"]["text"]
    assert "sk-ant-api03-" + "A" * 40 not in (text or "")


# ── knowledgec / approved_sqlite: not resolvable, no error ───────────────────────────────────
def test_knowledgec_not_resolvable(db):
    kdb = db / "knowledgeC.db"
    c = sqlite3.connect(kdb)
    c.execute("CREATE TABLE ZOBJECT (Z_PK INTEGER PRIMARY KEY, ZSTREAMNAME TEXT, ZSTARTDATE REAL, ZENDDATE REAL, ZVALUESTRING TEXT)")
    start = datetime(2026, 4, 28, 1, 0, tzinfo=timezone.utc).timestamp() - CF_S
    c.execute("INSERT INTO ZOBJECT VALUES (1, '/app/usage', ?, ?, 'com.example.Synthetic')", (start, start + 600))
    c.commit()
    c.close()
    src = KnowledgeCSource(db_path=kdb)
    rid = _ingest(list(src.read(src.discover()[0], SINCE, UNTIL)))[0]
    row = _evidence_row(rid)[0]
    assert row["locator"] == "unresolvable://knowledgec" and row["available"] == 0
    resp = _get(_token(), _evd(rid))
    assert resp["status"] == "not_resolvable" and "knowledgeC" in resp["message"]
    assert resp["evidence"]["locator"] is None and resp["evidence"]["available"] is False
    assert resp["evidence"]["excerpt"]
    assert _evidence_row(rid)[0]["available"] == 0  # a refresh never flips it


def test_approved_sqlite_not_resolvable(db):
    from penpal.foundation.approval_store import ApprovalStore
    from penpal.sources.discoverers import CandidateSource

    adb = db / "app.db"
    c = sqlite3.connect(adb)
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, timestamp INTEGER, text TEXT)")
    c.execute("INSERT INTO messages VALUES (1, ?, 'synthetic approved row')",
              (int(datetime(2026, 4, 28, tzinfo=timezone.utc).timestamp()),))
    c.commit()
    c.close()
    store = ApprovalStore(path=db / "approval.json")
    store.approve(CandidateSource(discoverer="sqlite", path=str(adb.resolve()), app_hint="Cursor",
                                  schema_signature="messages", shape="tabular_rows",
                                  hint_tables=["messages"], confidence="high"), note="t")
    src = ApprovedSqliteSource(approval_store=store)
    events = list(src.read(src.discover()[0], SINCE, UNTIL))
    assert events
    rid = _ingest(events)[0]
    assert _evidence_row(rid)[0]["locator"] == "unresolvable://approved_sqlite"
    # approved_sqlite has no default bucket (rows are unbucketed, so no MCP grant reaches them until the
    # user's rules bucket them); give it one here to exercise the get_evidence path.
    get_conn().execute("UPDATE raw_events SET bucket = 'Work' WHERE id = ?", (rid,))
    get_conn().commit()
    resp = _get(_token(), _evd(rid))
    assert resp["status"] == "not_resolvable" and resp["evidence"]["available"] is False


# ── migration v28 ────────────────────────────────────────────────────────────────────────────
def _raw(source, raw_ref, meta=None, *, source_id=None, external_id=None, text="synthetic", n=[0]) -> int:
    n[0] += 1
    meta = {"raw_ref": raw_ref, **(meta or {})}
    return insert_raw_event(RawEvent(
        source=source, event_type="semantic_event", ts_start=f"2026-03-01T00:00:{n[0] % 60:02d}+00:00",
        content_text=text, content_hash=f"v28-{n[0]}", metadata_json=json.dumps(meta),
        source_id=source_id, external_id=external_id, bucket="Work",
    ))


def _src(kind, locator) -> int:
    c = get_conn()
    c.execute("INSERT INTO sources(type, locator, created_at) VALUES (?,?,'x')", (kind, locator))
    return c.execute("SELECT id FROM sources WHERE type=? AND locator=?", (kind, locator)).fetchone()[0]


def _locator_of(event_id):
    r = _evidence_row(event_id)
    return (r[0]["locator"], r[0]["offset_or_line"], r[0]["available"]) if r else None


def test_v28_backfills_from_stored_metadata_and_counts_the_rest(db, caplog):
    home = Path.home()
    _src("zsh_history", "/h/.zsh_history")
    claude_src = _src("claude_code", "/h/.claude/projects/-proj-a")
    codex_root = _src("codex_cli", "/h/.codex/sessions")
    codex_hist = _src("codex_cli", "/h/.codex/history.jsonl")
    git_src = _src("git_log", "/r/repo")
    ids = {
        "zsh": _raw("zsh_history", "zsh:line:7"),
        "chrome": _raw("chrome_history", "chrome:visit:42", {"profile": "Profile 1"}),
        "chrome_noprofile": _raw("chrome_history", "chrome:visit:43"),
        "safari": _raw("safari_history", "safari:visit:9"),
        "spotlight": _raw("spotlight", "spotlight:/Users/x/Documents/a.pdf"),
        "spotlight_rel": _raw("spotlight", "spotlight:relative.pdf"),
        "vault_abs": _raw("markdown_vault", "markdown_vault:v:n.md", {"path": "/v/n.md"}),
        "vault_rel": _raw("markdown_vault", "markdown_vault:v:n.md", {"vault_name": "v"}),
        "claude": _raw("claude_code", "claude:projects:-proj-a:sess.jsonl:12", source_id=claude_src, external_id="c1"),
        "claude_meta": _raw("claude_code", "claude:projects:-proj-b:s2.jsonl:3", {"project_dir": "/h/.claude/projects/-proj-b"}),
        "claude_mismatch": _raw("claude_code", "claude:projects:-other:s.jsonl:3", {"project_dir": "/h/.claude/projects/-proj-b"}),
        "codex": _raw("codex_cli", "codex:sessions:2026/03/01/r.jsonl:5", source_id=codex_root, external_id="x1"),
        "codex_hist": _raw("codex_cli", "codex:history:4", source_id=codex_hist, external_id="x2"),
        "codex_nosrc": _raw("codex_cli", "codex:sessions:r.jsonl:5"),
        "git": _raw("git_log", "git:repo:" + "ab" * 20, source_id=git_src, external_id="ab" * 20),
        "git_meta": _raw("git_log", "git:r2:" + "cd" * 20, {"repo_path": "/r/two", "full_hash": "cd" * 20}),
        "git_bad": _raw("git_log", "git:r3:zz", {"repo_path": "/r/three", "full_hash": "not-hex"}),
        "kc": _raw("knowledgec", "knowledgec:/app/usage:1"),
        "asq": _raw("approved_sqlite", "approved_sqlite:c:t:1"),
        "other": _raw("claude_code", "claude:projects:-p:f.jsonl:1", {"project_dir": "/x/-p"}),
    }
    get_conn().execute("DELETE FROM evidence")
    get_conn().commit()
    # An existing evidence row is left alone.
    insert_before = ids["other"]
    get_conn().execute("INSERT INTO evidence(event_id, locator, offset_or_line, available, created_at) VALUES (?,?,?,?,?)",
                       (insert_before, "/keep/me.jsonl", 1, 1, "x"))
    get_conn().commit()

    with caplog.at_level("INFO", logger="penpal.store.migration_v28"):
        stats = migrate_v28(get_conn())
    get_conn().commit()

    assert _locator_of(ids["zsh"]) == ("/h/.zsh_history", 7, 1)  # the registered sources row wins
    chrome_db = home / "Library" / "Application Support" / "Google" / "Chrome" / "Profile 1" / "History"
    assert _locator_of(ids["chrome"]) == (f"{chrome_db}#visit:42", None, 1)
    assert _locator_of(ids["chrome_noprofile"]) is None
    assert _locator_of(ids["safari"]) == (f"{home / 'Library' / 'Safari' / 'History.db'}#visit:9", None, 1)
    assert _locator_of(ids["spotlight"]) == ("/Users/x/Documents/a.pdf", None, 1)
    assert _locator_of(ids["spotlight_rel"]) is None
    assert _locator_of(ids["vault_abs"]) == ("/v/n.md", None, 1)
    assert _locator_of(ids["vault_rel"]) is None
    assert _locator_of(ids["claude"]) == ("/h/.claude/projects/-proj-a/sess.jsonl", 12, 1)
    assert _locator_of(ids["claude_meta"]) == ("/h/.claude/projects/-proj-b/s2.jsonl", 3, 1)
    assert _locator_of(ids["claude_mismatch"]) is None
    assert _locator_of(ids["codex"]) == ("/h/.codex/sessions/2026/03/01/r.jsonl", 5, 1)
    assert _locator_of(ids["codex_hist"]) == ("/h/.codex/history.jsonl", 4, 1)
    assert _locator_of(ids["codex_nosrc"]) is None
    assert _locator_of(ids["git"]) == ("/r/repo@" + "ab" * 20, None, 1)
    assert _locator_of(ids["git_meta"]) == ("/r/two@" + "cd" * 20, None, 1)
    assert _locator_of(ids["git_bad"]) is None
    assert _locator_of(ids["kc"]) == ("unresolvable://knowledgec", None, 0)
    assert _locator_of(ids["asq"]) == ("unresolvable://approved_sqlite", None, 0)
    assert _locator_of(ids["other"]) == ("/keep/me.jsonl", 1, 1)

    created = sum(c for c, _ in stats.values())
    skipped = sum(s for _, s in stats.values())
    assert (created, skipped) == (13, 6)
    assert any("13 created, 6 left without evidence" in r.getMessage() for r in caplog.records)


def test_v28_zsh_prefers_the_registered_source_path(db):
    _src("zsh_history", "/custom/hist")
    rid = _raw("zsh_history", "zsh:line:3")
    get_conn().execute("DELETE FROM evidence")
    migrate_v28(get_conn())
    assert _locator_of(rid) == ("/custom/hist", 3, 1)


def test_v28_is_idempotent(db):
    rid = _raw("spotlight", "spotlight:/a/b.txt")
    unresolved = _raw("chrome_history", "chrome:visit:1")  # no profile: stays without evidence
    get_conn().execute("DELETE FROM evidence")
    migrate_v28(get_conn())
    first = get_conn().execute("SELECT id, event_id, locator FROM evidence ORDER BY id").fetchall()
    stats = migrate_v28(get_conn())
    assert get_conn().execute("SELECT id, event_id, locator FROM evidence ORDER BY id").fetchall() == first
    assert "spotlight" not in stats  # already has evidence: not even a candidate
    assert _locator_of(unresolved) is None and _locator_of(rid) is not None


def test_v28_never_touches_source_files(db, monkeypatch):
    f = db / "present.txt"
    f.write_text("x")
    rid = _raw("spotlight", f"spotlight:{f}")
    get_conn().execute("DELETE FROM evidence")
    import builtins, os

    def boom(*a, **k):
        raise AssertionError("migration touched the filesystem")

    conn = get_conn()
    with monkeypatch.context() as m:
        m.setattr(builtins, "open", boom)
        for name in ("stat", "lstat", "listdir", "scandir"):
            m.setattr(os, name, boom)
        migrate_v28(conn)
    assert _locator_of(rid)[0] == str(f)


def test_v28_is_transactional(db):
    _raw("spotlight", "spotlight:/a/b.txt")
    conn = get_conn()
    conn.execute("DELETE FROM evidence")
    conn.commit()

    def failing(c):
        migrate_v28(c)
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        _apply_transactional(conn, 99, failing)
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == len(MIGRATIONS)


def test_v28_backfilled_row_resolves_through_get_evidence(db):
    f = db / "doc.md"
    f.write_text("# Old doc\nhello\n")
    rid = _raw("spotlight", f"spotlight:{f.resolve()}", text="old doc")
    get_conn().execute("DELETE FROM evidence")
    migrate_v28(get_conn())
    get_conn().commit()
    ev = _get(_token(), _evd(rid))["evidence"]
    assert ev["original"]["text"].startswith("# Old doc")


@pytest.mark.slow
def test_v28_runtime_on_25k_rows(db):
    conn = get_conn()
    kinds = [("zsh_history", "zsh:line:%d", {}), ("chrome_history", "chrome:visit:%d", {"profile": "Default"}),
             ("spotlight", "spotlight:/u/f%d.txt", {}), ("claude_code", "claude:projects:-p:s.jsonl:%d", {"project_dir": "/c/-p"}),
             ("knowledgec", "knowledgec:/app/usage:%d", {})]
    rows = []
    for i in range(25_000):
        source, ref, meta = kinds[i % len(kinds)]
        rows.append((source, "semantic_event", f"2026-03-01T00:00:00.{i:06d}+00:00", f"synthetic {i}", f"h{i}",
                     json.dumps({"raw_ref": ref % (i + 1), **meta}), "Work"))
    conn.executemany("INSERT INTO raw_events(source, event_type, ts_start, content_text, content_hash, metadata_json, bucket, created_at) "
                     "VALUES (?,?,?,?,?,?,?,'x')", rows)
    conn.execute("DELETE FROM evidence")
    conn.commit()
    t0 = time.perf_counter()
    stats = migrate_v28(conn)
    conn.commit()
    elapsed = time.perf_counter() - t0
    print(f"v28 on 25k rows: {elapsed:.2f}s")
    assert sum(c for c, _ in stats.values()) == 25_000
    assert elapsed < 15
