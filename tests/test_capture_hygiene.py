"""D27 capture hygiene: shared write gate, keyboard tap fixes, exclusion coverage, v24 migration,
fusion dedup, card-number detectors. Synthetic data only."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from penpal.capture.fusion import CaptureFusionEngine
from penpal.foundation.capture_hygiene import DROP, METADATA_ONLY, capture_gate
from penpal.capture.manager import CaptureManager
from penpal.capture.normalizer import (
    normalize_ax_text_event,
    normalize_browser_url_event,
    normalize_clipboard_event,
    normalize_keyboard_chunk_event,
    normalize_window_event,
)
from penpal.capture.watchers import keyboard_native
from penpal.capture.watchers.keyboard_chunk import KeyboardChunkBuffer
from penpal.config import Config
from penpal.foundation.desensitizer import desensitize
from penpal.foundation.detectors import luhn_valid, valid_id_card
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v24 import migrate_v24
from penpal.store.migrations import MIGRATIONS, SCHEMA_VERSION, run_migrations

VALID_ID = "110101199003071233"  # synthetic, passes GB 11643
VALID_CARD = "6222021234567890128"  # synthetic, passes Luhn


def _gate(source="keyboard_chunk", text=None, app="Notes", proc="com.apple.Notes", title=None, **kw):
    v = capture_gate(source=source, app_name=app, process_name=proc, window_title=title, content_text=text, **kw)
    return None if v is None else (v.action, v.reason)


# ---- gate rules -------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("hello world this is a test", None),                     # spaces survive, English sentence kept
    ("hello world", None),                                    # legacy entropy 3.0 would have dropped this
    ("我们明天开会吧", None),
    ("好的", None),
    ("nihaoshijiezhendeshiyigeceshi", (DROP, "ime_blob")),
    ("a", (DROP, "short_input")),
    ("aaaaaaaaaaaa", (DROP, "low_entropy")),
    ("asdfasdfasdf", (DROP, "low_entropy")),
    ("验证码 482913", (DROP, "privacy_keyword")),
    ("我的银行密码是 8827361", (DROP, "privacy_keyword")),
    ("my footprint is large", None),                          # "otp" inside a word is not a keyword
    ("   ", (DROP, "empty")),
])
def test_keyboard_gate(text, expected):
    assert _gate(text=text) == expected


def test_keyboard_in_terminal_is_metadata_only():
    assert _gate(text="git commit -m wip", app="Terminal", proc="com.apple.Terminal") == (METADATA_ONLY, "terminal_app")
    assert _gate(text="git commit -m wip", app="Ghostty", proc="com.mitchellh.ghostty") == (METADATA_ONLY, "terminal_app")


def test_login_title_is_metadata_only_for_every_stream():
    for src in ("keyboard_chunk", "ax_text", "clipboard", "window"):
        text = None if src == "window" else "some ordinary working sentence"
        assert _gate(src, text, app="Safari", title="Sign in - Google Accounts") == (METADATA_ONLY, "title_deny")
    assert _gate("window", None, app="Safari", title="Reset password - Example")[0] == METADATA_ONLY


def test_clipboard_keeps_short_text_but_applies_privacy():
    assert _gate("clipboard", "kubectl") is None
    assert _gate("clipboard", "密码是 correcthorse") == (DROP, "privacy_keyword")


def test_window_overlay_dropped():
    assert _gate("window", None, app="loginwindow", title="Login") == (DROP, "system_overlay")
    assert _gate("window", None, app="Figma", title="Mockups") is None


def test_ax_text_app_chrome_and_normal():
    assert _gate("ax_text", "Quarterly Plan", app="Notes", title="Quarterly Plan") == (DROP, "app_chrome")
    assert _gate("ax_text", "The quarterly roadmap review notes are here", title="Doc") is None


def test_user_title_deny_patterns_are_configurable():
    text = "a normal long sentence here"
    assert _gate(text=text, title="Acme Payroll", title_deny_patterns=[r"payroll"]) == (METADATA_ONLY, "title_deny")
    assert _gate(text=text, title="Acme Payroll", title_deny_patterns=[]) is None
    assert _gate(text=text, title="x", title_deny_patterns=["("]) is None  # bad regex ignored


@pytest.mark.parametrize("title", [
    "Inbox - Google Chrome (Incognito)", "新建无痕窗口", "Bing - InPrivate", "Start Page - Private Browsing", "隐私浏览 - Safari",
])
def test_private_windows_are_metadata_only(title):
    assert _gate("window", None, app="Google Chrome", title=title) == (METADATA_ONLY, "private_window")
    assert _gate("browser_url", "Inbox", app="Google Chrome", title=title) == (METADATA_ONLY, "private_window")


@pytest.mark.parametrize("source,title,url", [
    ("browser_url", "Settings", "chrome://settings/"),
    ("browser_url", "Extensions", "chrome-extension://abc/page.html"),
    ("browser_url", "New Tab", "about:blank"),
    ("browser_url", "Edge", "edge://flags"),
    ("window", "chrome://newtab", None),
    ("browser_url", "", "file:///Users/x/doc.html"),
])
def test_browser_internal_pages_dropped(source, title, url):
    text = "x" if source == "browser_url" else None
    assert _gate(source, text, app="Google Chrome", title=title, url=url) == (DROP, "browser_internal_page")


def test_file_url_with_real_title_is_kept():
    assert _gate("browser_url", "Notes", app="Google Chrome", title="Notes", url="file:///Users/x/doc.html") is None


# ---- keyboard tap (F0) ------------------------------------------------------------------------
class _FakeQuartz:
    kCGEventKeyDown = 10
    kCGSessionEventTap = 1
    kCGHeadInsertEventTap = 0
    kCGEventTapOptionListenOnly = 1
    kCGEventFlagMaskCommand = 0x100000
    kCGEventFlagMaskControl = 0x40000
    kCGEventFlagMaskAlternate = 0x80000
    kCGEventFlagMaskShift = 0x20000

    def __init__(self):
        self.handler = None

    def CGEventTapCreate(self, *args):
        self.handler = args[4]
        return None  # run loop exits as "permission_denied"; handler stays callable

    def CGEventKeyboardGetUnicodeString(self, event, n, a, b):
        return (len(event["text"]), event["text"])

    def CGEventGetFlags(self, event):
        return event.get("flags", 0)


def _tap_texts(events, monkeypatch):
    quartz = _FakeQuartz()
    source = keyboard_native.MacOSKeyboardInputSource(quartz_module=quartz)
    monkeypatch.setattr(keyboard_native, "_get_frontmost_app", lambda: ("Notes", "Doc", "com.apple.Notes"))
    monkeypatch.setattr(keyboard_native, "_is_secure_input_enabled", lambda *a, **k: False)
    got = []
    source._running.set()
    source._run_loop_forever(lambda text, **kw: got.append(text))
    for ev in events:
        quartz.handler(None, 0, ev, None)
    return got


def test_tap_keeps_space_and_enter_and_ignores_command_chords(monkeypatch):
    q = _FakeQuartz
    events = [{"text": c} for c in "hello"] + [{"text": " "}] + [{"text": c} for c in "world"]
    events += [{"text": "\r"}, {"text": "c", "flags": q.kCGEventFlagMaskCommand},
               {"text": "a", "flags": q.kCGEventFlagMaskControl}, {"text": "å", "flags": q.kCGEventFlagMaskAlternate},
               {"text": "H", "flags": q.kCGEventFlagMaskShift}]
    got = _tap_texts(events, monkeypatch)
    assert "".join(got) == "hello world\nH"
    buf = KeyboardChunkBuffer()
    for i, t in enumerate(got):
        buf.add_text(t, "Notes", None, now=i * 0.01)
    assert buf.flush_if_due(now=500).content_text == "hello world\nH"


def test_english_sentence_through_buffer_passes_gate_and_pinyin_does_not():
    def chunk(s):
        buf = KeyboardChunkBuffer()
        for i, ch in enumerate(s):
            buf.add_text(ch, "Notes", None, now=i * 0.01)
        return buf.flush_if_due(now=100).content_text

    assert chunk("hello world this is a test") == "hello world this is a test"
    assert _gate(text=chunk("hello world this is a test")) is None
    assert _gate(text=chunk("nihaoshijiezhendeshiyigeceshi")) == (DROP, "ime_blob")


def test_force_flush_default_is_sentence_length():
    assert Config().keyboard_chunk.force_flush_sec == 20.0
    assert Config().keyboard_chunk.silence_sec == 2.0
    buf = KeyboardChunkBuffer()
    for i in range(30):
        buf.add_text("x", "Notes", None, now=i * 1.0)
    assert buf.flush_if_due(now=10.5) is None       # typing continues, 20s not reached
    assert buf.flush_if_due(now=29.5) is not None   # forced after 20s of continuous typing


# ---- manager integration ----------------------------------------------------------------------
@pytest.fixture
def mgr(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    yield CaptureManager(Config())
    close()
    buckets.reset_cache()


def _stored():
    return [dict(r) for r in get_conn().execute("SELECT * FROM raw_events ORDER BY id")]


def _ts(n):
    return f"2026-03-02T10:00:{n:02d}+00:00"


def test_manager_end_to_end_audit_cases(mgr):
    events = [
        normalize_keyboard_chunk_event("nihaoshijiezhendeshiyigeceshi", app_name="Notes", process_name="com.apple.Notes", ts_start=_ts(1)),
        normalize_keyboard_chunk_event("验证码 482913", app_name="Notes", process_name="com.apple.Notes", ts_start=_ts(2)),
        normalize_keyboard_chunk_event("晚上吃什么", app_name="微信", process_name="com.tencent.xinWeChat", ts_start=_ts(3)),
        normalize_clipboard_event("meeting notes copied from chat", app_name="WeChat", ts_start=_ts(4)),
        normalize_clipboard_event("今天开会的结论是需求确认", app_name="微信", ts_start=_ts(5)),
        normalize_clipboard_event("Tr0ub4dor&3xKcd", app_name="1Password 7", ts_start=_ts(6)),
        normalize_clipboard_event("copied from a chat app", app_name="Whatever", process_name="com.tinyspeck.slackmacgap", ts_start=_ts(7)),
        normalize_clipboard_event("密码是 correcthorse", app_name="Notes", ts_start=_ts(8)),
        normalize_browser_url_event("https://www.bilibili.com/video/x", "t", "Google Chrome", ts_start=_ts(9)),
        normalize_window_event("window_focus_session", "loginwindow", "Login", None, ts_start=_ts(10), ts_end=_ts(30)),
        normalize_keyboard_chunk_event("hello world this is a test", app_name="Notes", process_name="com.apple.Notes", ts_start=_ts(11)),
        normalize_clipboard_event("kubectl", app_name="Notes", ts_start=_ts(12)),
    ]
    for ev in events:
        mgr._process_event(ev)
    assert [r["content_text"] for r in _stored()] == ["hello world this is a test", "kubectl"]
    hits = mgr.health()["gate_hits"]
    assert hits["keyboard_chunk:ime_blob"] == 1 and hits["keyboard_chunk:privacy_keyword"] == 1
    assert hits["window:system_overlay"] == 1
    assert "hello" not in json.dumps(hits)  # counters hold reasons, never content


def test_manager_metadata_only_keeps_app_and_time(mgr):
    mgr._process_event(normalize_keyboard_chunk_event("git commit -m wip", app_name="Terminal", process_name="com.apple.Terminal", ts_start=_ts(1)))
    mgr._process_event(normalize_keyboard_chunk_event("alice typed some words", app_name="Safari", window_title="Sign in - Google Accounts", process_name="com.apple.Safari", ts_start=_ts(2)))
    mgr._process_event(normalize_browser_url_event("https://accounts.example.com/signin?token=abc123", "Sign in", "Google Chrome", ts_start=_ts(3)))
    rows = _stored()
    assert [r["app_name"] for r in rows] == ["Terminal", "Safari", "Google Chrome"]
    for r in rows:
        assert r["content_text"] is None and r["content_hash"] is None
        assert json.loads(r["metadata_json"])["text_dropped"]
    assert rows[1]["window_title"] is None and rows[2]["window_title"] is None
    assert "abc123" not in rows[2]["metadata_json"]


def test_manager_private_and_internal_pages(mgr):
    mgr._process_event(normalize_browser_url_event("chrome://settings/", "Settings", "Google Chrome", ts_start=_ts(2)))
    mgr._process_event(normalize_window_event("window_focus_session", "Google Chrome", "Inbox (Incognito)", "com.google.Chrome", ts_start=_ts(3), ts_end=_ts(20)))
    rows = _stored()
    assert [r["event_type"] for r in rows] == ["window_focus_session"]
    assert rows[0]["window_title"] is None and rows[0]["app_name"] == "Google Chrome"


def test_manager_dedup_ax_vs_clipboard_and_near_duplicates(mgr):
    mgr._process_event(normalize_ax_text_event("The quarterly roadmap review notes are here", app_name="Notes", window_title="Doc", process_name="com.apple.Notes", ts_start=_ts(1)))
    mgr._process_event(normalize_clipboard_event("The quarterly roadmap review notes are here", app_name="Notes", ts_start=_ts(2)))
    mgr._process_event(normalize_ax_text_event("Line A\nLine B\nLine C\nLine X", app_name="Notes", window_title="Doc2", process_name="com.apple.Notes", ts_start=_ts(3)))
    mgr._process_event(normalize_ax_text_event("Line A\nLine B\nLine C\nLine X\nLine D", app_name="Notes", window_title="Doc2", process_name="com.apple.Notes", ts_start=_ts(40)))
    mgr._process_event(normalize_keyboard_chunk_event("same sentence typed twice", app_name="Notes", ts_start=_ts(41)))
    mgr._process_event(normalize_keyboard_chunk_event("same sentence typed twice", app_name="Notes", ts_start=_ts(50)))
    assert len(_stored()) == 3


def test_blacklist_app_names_and_bundle_ids():
    cfg = Config()
    assert "com.tinyspeck.slackmacgap" in cfg.privacy.blacklist_bundle_ids
    assert "com.electron.lark" in cfg.privacy.blacklist_bundle_ids
    m = CaptureManager(cfg)
    for name in ("微信", "WeChat", "企业微信", "钉钉", "DingTalk", "飞书", "Lark", "Feishu", "1Password 7", "Keychain Access"):
        assert m._is_blacklisted(normalize_clipboard_event("x", app_name=name)), name
    assert not m._is_blacklisted(normalize_clipboard_event("x", app_name="Notes"))


def test_url_deny_hosts_apply_to_browser_url():
    from penpal.capture.manager_events import _should_drop_browser_event

    ev = normalize_browser_url_event("https://www.bilibili.com/v", "t", "Google Chrome")
    assert _should_drop_browser_event(ev, ["bilibili.com"]) is True


def test_fusion_relaxed_title_and_bounded_memory():
    eng = CaptureFusionEngine()
    a = normalize_ax_text_event("Line A\nLine B\nLine C", app_name="Notes", window_title="Doc", ts_start=_ts(1))
    b = normalize_clipboard_event("Line A\nLine B\nLine C", app_name="Notes", ts_start=_ts(5))
    assert eng.fuse(a).persist and not eng.fuse(b).persist
    for i in range(1200):
        eng.fuse(normalize_ax_text_event(f"unique{i} words", app_name="Notes", window_title=f"T{i}", ts_start=_ts(6)))
    assert len(eng._canonicals) <= 500


# ---- detectors --------------------------------------------------------------------------------
def test_checksums():
    assert valid_id_card(VALID_ID) and not valid_id_card("110101199003071234")
    assert luhn_valid(VALID_CARD) and not luhn_valid("6222021234567890123")


def test_id_and_card_redacted_next_to_cjk_with_labels():
    assert desensitize(f"身份证{VALID_ID}，请核对") == "身份证[ID_CARD]，请核对"
    assert desensitize(f"卡号{VALID_CARD}给你") == "卡号[BANK_CARD]给你"
    assert desensitize("ID " + VALID_ID + " ok") == "ID [ID_CARD] ok"


def test_invalid_checksums_and_longer_digit_runs_not_redacted():
    for text in ("身份证110101199003071234，请核对", "卡号6222021234567890123给你",
                 "order 1700000000123456789 shipped", "timestamp 1700000000123457"):
        assert desensitize(text) == text
    assert desensitize("1" + VALID_ID) == "1" + VALID_ID  # embedded in a longer digit run


# ---- migration v24 ----------------------------------------------------------------------------
def _db_at_v23(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    for i, step in enumerate(MIGRATIONS[:23], start=1):
        step(conn) if callable(step) else conn.executescript(step.strip())
        conn.execute("INSERT INTO _schema_version VALUES (?, 'x')", (i,))
    conn.commit()
    return conn


def _row(conn, n, source, etype, app, text, title=None, proc=None, meta=None):
    cur = conn.execute(
        "INSERT INTO raw_events(source,event_type,ts_start,app_name,process_name,window_title,content_text,content_hash,"
        "metadata_json,created_at,bucket) VALUES (?,?,?,?,?,?,?,?,?,'2026-01-01','Work')",
        (source, etype, f"2026-01-01T00:00:00+00:00#{n}", app, proc, title, text, f"h{n}", meta))
    rid = cur.lastrowid
    if source != "claude_code":
        conn.execute("INSERT INTO evidence(event_id,locator,available,created_at) VALUES (?,?,1,'x')",
                     (rid, f"penpal://event/{rid}"))
    return rid


def test_v24_scrubs_existing_rows_transactionally_and_idempotently(tmp_path):
    conn = _db_at_v23(tmp_path / "old.db")
    keep = _row(conn, 1, "keyboard_chunk", "keyboard_chunk", "Notes", "hello world this is a test")
    pinyin = _row(conn, 2, "keyboard_chunk", "keyboard_chunk", "Notes", "nihaoshijiezhendeshiyigeceshi zebracorn")
    term = _row(conn, 3, "keyboard_chunk", "keyboard_chunk", "Terminal", "git commit -m zebracornwip", proc="com.apple.Terminal")
    wechat = _row(conn, 4, "clipboard", "clipboard_copy", "微信", "zebracorn chat paste text")
    login = _row(conn, 5, "window", "window_focus_session", "Safari", None, title="Sign in - Example")
    overlay = _row(conn, 6, "window", "window_focus_session", "loginwindow", None, title="Login")
    bili = _row(conn, 7, "browser_url", "browser_url_capture", "Google Chrome", "zebracorn page", title="t - Google Chrome",
                meta=json.dumps({"url": "https://www.bilibili.com/v/zebracorn"}))
    plugin = _row(conn, 8, "claude_code", "semantic_event", "Cursor", "nihaoshijiezhendeshiyigeceshi")  # not a capture row
    conn.commit()

    run_migrations(conn)
    ids = {r["id"] for r in conn.execute("SELECT id FROM raw_events")}
    assert ids == {keep, term, login, plugin}
    t = conn.execute("SELECT * FROM raw_events WHERE id=?", (term,)).fetchone()
    assert t["content_text"] is None and t["content_hash"] is None and t["app_name"] == "Terminal"
    assert json.loads(t["metadata_json"])["text_dropped"] == "terminal_app"
    lg = conn.execute("SELECT * FROM raw_events WHERE id=?", (login,)).fetchone()
    assert lg["window_title"] is None and lg["app_name"] == "Safari"
    fts = lambda q: [r[0] for r in conn.execute("SELECT rowid FROM raw_events_fts WHERE raw_events_fts MATCH ?", (q,))]
    assert fts('"zebracorn"') == []                                     # FTS follows deletes and nulling
    assert fts('"hello world"') == [keep]
    ev = {r["event_id"] for r in conn.execute("SELECT event_id FROM evidence")}
    assert {pinyin, wechat, overlay, bili}.isdisjoint(ev) and keep in ev  # evidence of deleted events is gone
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == SCHEMA_VERSION

    before = [tuple(r) for r in conn.execute("SELECT * FROM raw_events ORDER BY id")]
    migrate_v24(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM raw_events ORDER BY id")] == before
    assert list(tmp_path.glob("old.db.v23-*.bak"))                      # auto backup taken
    conn.close()


@pytest.mark.slow
def test_v24_is_fast_on_25k_rows(tmp_path):
    conn = _db_at_v23(tmp_path / "big.db")
    apps = ["Cursor", "Safari", "Terminal", "Notes", "微信", "Figma", "Preview", "Chrome", "Warp", "Slack"]
    texts = ["typed some ordinary words about project %d and lunch plans", "nihaoshijiezhende%dshiyigeceshi",
             "验证码 %d", "a", "aaaaaaaaaaaa%d"]
    conn.executemany(
        "INSERT INTO raw_events(source,event_type,ts_start,app_name,window_title,content_text,content_hash,created_at,bucket) "
        "VALUES (?,?,?,?,?,?,?,'2026-01-01','Work')",
        [("keyboard_chunk", "keyboard_chunk", f"2026-01-01T00:00:00+00:00#{i}", apps[i % 10], "title %d" % (i % 50),
          texts[i % 5].replace("%d", str(i)), f"h{i}") for i in range(25_000)])
    conn.commit()
    start = time.perf_counter()
    run_migrations(conn)
    elapsed = time.perf_counter() - start
    print(f"v24 gate migration 25k rows: {elapsed:.2f}s")
    assert elapsed < 30
    assert conn.execute("SELECT count(*) FROM raw_events WHERE content_text LIKE 'nihao%'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM raw_events").fetchone()[0] < 25_000
    conn.close()


def test_v25_hides_pre_fix_keyboard_rows_from_search_but_keeps_them(tmp_path):
    from penpal.search.engine import _NOT_ECHO_SQL

    conn = _db_at_v23(tmp_path / "old.db")
    kb = _row(conn, 1, "keyboard_chunk", "keyboard_chunk", "Notes", "hello world this is a test")
    clip = _row(conn, 2, "clipboard", "clipboard_copy", "Notes", "hello world copied text")
    conn.commit()

    run_migrations(conn)
    visible = {r[0] for r in conn.execute(f"SELECT e.id FROM raw_events e WHERE {_NOT_ECHO_SQL}")}
    assert kb not in visible and clip in visible                       # hidden from search ...
    assert conn.execute("SELECT content_text FROM raw_events WHERE id=?", (kb,)).fetchone()[0]  # ... but kept

    new_kb = _row(conn, 3, "keyboard_chunk", "keyboard_chunk", "Notes", "typed after the tap fix")
    assert conn.execute(f"SELECT 1 FROM raw_events e WHERE e.id=? AND {_NOT_ECHO_SQL}", (new_kb,)).fetchone()
