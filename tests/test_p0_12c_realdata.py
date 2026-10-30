"""P0-12c (issue #53): zsh unmetafy, system overlay apps, transcript noise backfill, input placeholders,
zero-width characters, browser content bucketed by website (D28), migration v26. Synthetic data only."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from penpal.foundation.capture_hygiene import DROP, capture_gate, is_placeholder_text, is_system_overlay
from penpal.capture.manager import CaptureManager
from penpal.capture.normalizer import (
    normalize_ax_text_event,
    normalize_browser_url_event,
    normalize_clipboard_event,
    normalize_keyboard_chunk_event,
    normalize_window_event,
)
from penpal.config import Config
from penpal.sources.plugins.zsh_history import ZshHistorySource, unmetafy
from penpal.sources.registry import read_all
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v26 import migrate_v26
from penpal.store.migrations import MIGRATIONS, run_migrations
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.store.site_categories import browser_family, classify_host, classify_url, host_of, normalize_site_key
from penpal.utils.text_filters import strip_invisible

ZW = "​‌‍‎‏‪‮⁠⁤﻿­"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    buckets.reset_cache()
    yield
    close()
    buckets.reset_cache()


def metafy(data: bytes) -> bytes:
    """What zsh writes: 0x83 + (byte ^ 0x20) for 0x83 and bytes in 0x80-0x9f."""
    out = bytearray()
    for b in data:
        if b == 0x83 or 0x80 <= b <= 0x9F:
            out += bytes([0x83, b ^ 0x20])
        else:
            out.append(b)
    return bytes(out)


# ── 1. zsh ───────────────────────────────────────────────────────────────────────────────────
def test_unmetafy_roundtrip_chinese():
    text = "echo 你好世界 测试"
    raw = text.encode("utf-8")
    assert metafy(raw) != raw  # the sample really contains bytes zsh escapes
    assert unmetafy(metafy(raw)).decode("utf-8") == text
    assert unmetafy(b"plain ascii") == b"plain ascii"
    assert unmetafy(b"tail\x83") == b"tail\x83"  # lone trailing marker survives


def _zsh_history(tmp_path, lines: list[bytes]) -> Path:
    path = tmp_path / "home" / ".zsh_history"
    path.write_bytes(b"".join(lines))
    return path


def test_zsh_reads_metafied_chinese_and_strips_continuation(tmp_path):
    path = _zsh_history(tmp_path, [
        b": 1774855914:0;" + metafy("git commit -m '修复登录问题'".encode()) + b"\n",
        b": 1774855915:0;echo first \\\n",
        b"second line of the command\n",
        b": 1774855916:0;ls -la\n",
    ])
    src = ZshHistorySource(history_path=path)
    inst = src.discover()[0]
    events = list(src.read(inst, datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2999, 1, 1, tzinfo=timezone.utc)))
    intents = [e.intent for e in events]
    assert "git commit -m '修复登录问题'" in intents
    assert "echo first" in intents                       # trailing continuation backslash removed
    assert not any("�" in i for i in intents)       # no mojibake / replacement characters


def test_zsh_first_sync_reads_whole_file_then_only_the_window(tmp_path):
    path = _zsh_history(tmp_path, [
        b": 1000000000:0;echo ancient command\n",
        b": 1774855916:0;echo recent command\n",
    ])
    init_db(tmp_path / "kp.db")
    src = ZshHistorySource(history_path=path, use_cursor=True)
    inst = src.discover()[0]
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    until = datetime(2999, 1, 1, tzinfo=timezone.utc)
    first = [e.intent for e in src.read(inst, since, until)]
    assert first == ["echo ancient command", "echo recent command"]  # no cursor yet: full re-read
    src.commit_cursors()
    second = [e.intent for e in src.read(inst, since, until)]
    assert second == ["echo recent command"]                          # cursor set: normal window


def test_zsh_resync_is_idempotent_through_the_registry(tmp_path):
    path = _zsh_history(tmp_path, [b": 1774855916:0;" + metafy("echo 你好".encode()) + b"\n"])
    init_db(tmp_path / "kp.db")
    from penpal.sources import registry

    plugin = ZshHistorySource(history_path=path, use_cursor=True)
    registry._PLUGINS["zsh_history"] = plugin
    since, until = datetime(2000, 1, 1, tzinfo=timezone.utc), datetime(2999, 1, 1, tzinfo=timezone.utc)
    list(read_all(since, until, source="zsh_history"))
    list(read_all(since, until, source="zsh_history"))
    rows = get_conn().execute("SELECT content_text FROM raw_events WHERE source = 'zsh_history'").fetchall()
    assert [r[0] for r in rows] == ["echo 你好"]


# ── 2. system overlay apps ───────────────────────────────────────────────────────────────────
def test_setup_assistant_is_a_system_overlay():
    for name in ("Setup Assistant", "设置助理", "SetupAssistant", "loginwindow"):
        assert is_system_overlay(name)
    assert is_system_overlay(None, "com.apple.SetupAssistant")
    assert is_system_overlay("x", "com.apple.SetupAssistant.SetupAssistantMac")
    assert not is_system_overlay("Notes", "com.apple.Notes")
    verdict = capture_gate(source="window", app_name="设置助理", process_name=None, window_title="欢迎", content_text=None)
    assert verdict is not None and (verdict.action, verdict.reason) == (DROP, "system_overlay")


def test_manager_drops_overlay_windows_and_unattributes_other_streams(tmp_path):
    init_db(tmp_path / "kp.db")
    m = CaptureManager(Config())
    m._process_event(normalize_window_event("window_focus_session", "Setup Assistant", "Welcome", None,
                                            ts_start="2026-03-02T10:00:01+00:00", ts_end="2026-03-02T10:00:30+00:00"))
    m._process_event(normalize_clipboard_event("copied while setting up the laptop", app_name="Setup Assistant",
                                               process_name="com.apple.SetupAssistant", ts_start="2026-03-02T10:00:05+00:00"))
    m._process_event(normalize_keyboard_chunk_event("typed this sentence during setup", app_name="设置助理",
                                                    ts_start="2026-03-02T10:00:06+00:00"))
    rows = [dict(r) for r in get_conn().execute("SELECT * FROM raw_events ORDER BY id")]
    assert [r["source"] for r in rows] == ["clipboard", "keyboard_chunk"]      # no window session
    for r in rows:
        assert r["app_name"] is None and r["process_name"] is None
        assert r["bucket"] == "Personal" and r["content_text"]


# ── 4. placeholders ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "问问 ChatGPT", "发消息或按住空格说话", "Message ChatGPT", "Ask anything", "有问题，尽管问", "输入消息",
    "Send a message", "  ask anything…  ", "MESSAGE CHATGPT",
])
def test_placeholder_texts_are_app_chrome(text):
    assert is_placeholder_text(text)
    verdict = capture_gate(source="ax_text", app_name="Google Chrome", process_name=None, window_title="Some page", content_text=text)
    assert verdict is not None and (verdict.action, verdict.reason) == (DROP, "app_chrome")


def test_placeholder_is_whole_text_match_only():
    for text in ("Ask anything about the quarterly numbers please", "please send a message to Bob about lunch"):
        assert not is_placeholder_text(text)
        assert capture_gate(source="ax_text", app_name="Notes", process_name=None, window_title="Doc", content_text=text) is None
    # clipboard is not an AX field: not gated by the placeholder rule
    assert capture_gate(source="clipboard", app_name="Notes", process_name=None, window_title=None, content_text="Ask anything") is None


# ── 5. zero-width ────────────────────────────────────────────────────────────────────────────
def test_strip_invisible_covers_the_listed_ranges():
    assert strip_invisible(f"a{ZW}b") == "ab"
    assert strip_invisible(None) is None and strip_invisible("") == ""
    assert strip_invisible("正常 text") == "正常 text"


def test_insert_raw_event_strips_invisible_and_fts_follows(tmp_path):
    init_db(tmp_path / "kp.db")
    rid = insert_raw_event(RawEvent(source="manual", event_type="manual_save", ts_start="2026-03-02T10:00:00+00:00",
                                    content_text=f"zebra{ZW}corn note", window_title=f"ti{ZW}tle", content_hash="z1"))
    row = get_conn().execute("SELECT content_text, window_title FROM raw_events WHERE id = ?", (rid,)).fetchone()
    assert (row[0], row[1]) == ("zebracorn note", "title")
    hit = get_conn().execute("SELECT rowid FROM raw_events_fts WHERE raw_events_fts MATCH '\"zebracorn\"'").fetchall()
    assert [h[0] for h in hit] == [rid]


def test_gate_sees_through_zero_width_privacy_keyword():
    verdict = capture_gate(source="keyboard_chunk", app_name="Notes", process_name=None, window_title=None,
                           content_text=f"the pass{ZW}word is hunter22 ok")
    assert verdict is not None and verdict.reason == "privacy_keyword"


# ── 6. site table ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,bucket", [
    ("https://github.com/org/repo", "Work"),
    ("https://gist.github.com/x", "Work"),
    ("https://my-team.feishu.cn/docx/abc", "Work"),
    ("https://acme.atlassian.net/browse/KP-1", "Work"),
    ("https://chatgpt.com/c/123", "Work"),
    ("https://claude.ai/chat/1", "Work"),
    ("https://www.notion.so/page", "Work"),
    ("https://stackoverflow.com/questions/1", "Work"),
    ("https://console.aws.amazon.com/ec2", "Work"),
    ("https://docs.python.org/3/", "Work"),
    ("https://developer.apple.com/documentation", "Work"),
    ("https://foo.readthedocs.io/en/latest/", "Work"),
    ("https://arxiv.org/abs/1", "Work"),
    ("https://www.alipay.com/", "Financial"),
    ("https://www.paypal.com/signin", "Financial"),
    ("https://www.icbc.com.cn/", "Financial"),
    ("https://www.schwab.com/", "Financial"),
    ("https://docs.mybank.example/guide", "Financial"),   # Financial is checked before the docs.* rule
    ("https://www.bilibili.com/video/x", None),
    ("https://news.example.org/", None),
    ("not a url", None),
])
def test_classify_url_preset(url, bucket):
    assert classify_url(url) == bucket


def test_config_overrides_beat_preset_and_normalise_keys():
    assert classify_host("github.com", {"github.com": "Personal"}) == "Personal"
    assert classify_host("www.corp.example", {"corp.example": "Work"}) == "Work"
    assert classify_host("a.b.example", {"example": "Work", "b.example": "Financial"}) == "Financial"  # longest suffix
    assert normalize_site_key("*.Notion.so") == "notion.so"
    assert normalize_site_key("https://Notion.so/x") == "notion.so"
    assert host_of("HTTPS://GitHub.com:443/a?b=1") == "github.com"


def test_browser_family():
    assert browser_family("Google Chrome") == "chrome" and browser_family(None, "com.google.Chrome") == "chrome"
    assert browser_family("Safari") == "safari" and browser_family("Arc") == "arc"
    assert browser_family("Microsoft Edge") == "edge" and browser_family("Firefox") == "firefox"
    assert browser_family("Brave Browser") == "brave"
    assert browser_family("Notes") is None and browser_family("Archive Utility") is None


# ── 6. live bucketing ────────────────────────────────────────────────────────────────────────
def _bucket_of(rid: int):
    return get_conn().execute("SELECT bucket FROM raw_events WHERE id = ?", (rid,)).fetchone()[0]


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-03-02T10:{minute:02d}:{second:02d}+00:00"


def _insert(ev: RawEvent) -> int:
    ev.content_hash = ev.content_hash or f"h-{ev.source}-{ev.ts_start}-{ev.app_name}"
    return insert_raw_event(ev)


def _url_event(url, minute, browser="Google Chrome", n=0):
    ev = normalize_browser_url_event(url, "Page", browser, ts_start=_ts(minute, n))
    return _insert(ev)


def _ax(text, minute, app="Google Chrome", second=0):
    return _insert(normalize_ax_text_event(text, app_name=app, window_title="Page - 已固定 - 内存用量高 - 1.8 GB - " + app, ts_start=_ts(minute, second)))


def test_browser_events_follow_the_site(tmp_path):
    init_db(tmp_path / "kp.db")
    assert _bucket_of(_url_event("https://github.com/o/r", 0)) == "Work"
    assert _bucket_of(_url_event("https://www.bilibili.com/v/1", 1)) == "Personal"
    assert _bucket_of(_url_event("https://www.icbc.com.cn/login", 2)) == "Financial"
    # AX text in Chrome inherits the nearest earlier URL of the same browser
    assert _bucket_of(_ax("reading the repository readme file", 0, second=30)) == "Work"
    assert _bucket_of(_ax("watching a cat video here today", 1, second=30)) == "Personal"
    assert _bucket_of(_ax("statement balance detail for march", 2, second=30)) == "Financial"
    clip = _insert(normalize_clipboard_event("copied from the bank page", app_name="Google Chrome",
                                             process_name="com.google.Chrome", ts_start=_ts(3)))
    assert _bucket_of(clip) == "Financial"
    win = _insert(normalize_window_event("window_focus_session", "Google Chrome", "Page - Google Chrome", None, ts_start=_ts(4), ts_end=_ts(5)))
    assert _bucket_of(win) == "Financial"


def test_url_lookup_is_same_browser_at_or_before_and_within_ten_minutes(tmp_path):
    init_db(tmp_path / "kp.db")
    _url_event("https://github.com/o/r", 0, browser="Google Chrome")
    _url_event("https://www.paypal.com/", 1, browser="Safari")
    assert _bucket_of(_ax("chrome text within window ok", 9, app="Google Chrome")) == "Work"
    assert _bucket_of(_ax("chrome text after ten minutes", 11, app="Google Chrome")) == "Personal"   # expired
    assert _bucket_of(_ax("safari text uses its own tab", 2, app="Safari")) == "Financial"          # not Chrome's URL
    assert _bucket_of(_ax("firefox has no url events yet", 3, app="Firefox")) == "Personal"        # none -> Personal
    assert _bucket_of(_ax("text before any url was seen", 0, app="Google Chrome", second=0)) == "Work"  # at (==) counts
    # a non-browser app never inherits a URL
    assert _bucket_of(_ax("editor text typed while chrome is on github", 2, app="Notes")) == "Personal"


XHS_TITLE = "🇳🇿春节新西兰南岛13天-自驾攻略 - 小红书"


def _titled_ax(text, minute, title, app="Google Chrome"):
    return _insert(normalize_ax_text_event(text, app_name=app, window_title=title, ts_start=_ts(minute)))


def test_window_title_must_match_the_page_title_else_personal(tmp_path):
    init_db(tmp_path / "kp.db")
    _insert(normalize_browser_url_event("https://www.xiaohongshu.com/explore/1", XHS_TITLE, "Google Chrome", ts_start=_ts(0)))
    _insert(normalize_browser_url_event("https://chatgpt.com/c/1", "Trip planner", "Google Chrome", ts_start=_ts(5)))
    # tab-switch case: the last URL (chatgpt, Work) is not the page this text came from
    mismatch = _titled_ax("读到的帖子正文内容在这里", 6, f"​{XHS_TITLE} - Google Chrome")
    assert _bucket_of(mismatch) == "Personal"
    # decorated titles still match their own page (latest matching row, not the latest row)
    assert _bucket_of(_titled_ax("planning the trip with the assistant", 7,
                                 "Trip planner - 已固定 - 内存用量高 - 1.8 GB - Google Chrome")) == "Work"
    assert _bucket_of(_titled_ax("text on the earlier work tab again", 8, "Trip planner - Audio playing - Google Chrome")) == "Work"
    # title unknown anywhere -> no URL -> Personal, never "nearest"
    assert _bucket_of(_titled_ax("some unrelated page body text", 8, "Totally different - Google Chrome")) == "Personal"
    # a prefix without the " - " boundary is not a match
    assert _bucket_of(_titled_ax("prefix boundary check text", 8, "Trip planner 2 - Google Chrome")) == "Personal"
    # no window title (typical clipboard row): nearest-in-time behaviour is kept
    clip = _insert(normalize_clipboard_event("copied text from somewhere", app_name="Google Chrome",
                                             process_name="com.google.Chrome", ts_start=_ts(9)))
    assert _bucket_of(clip) == "Work"
    # same rules in the bulk (rebucket / v26) path
    conn = get_conn()
    conn.execute("UPDATE raw_events SET bucket = 'Private'")
    conn.commit()
    buckets.reapply_buckets(conn)
    assert _bucket_of(mismatch) == "Personal" and _bucket_of(clip) == "Work"


def test_resolution_order_user_rule_over_config_over_preset(tmp_path, monkeypatch):
    from penpal.store.models import Policy
    from penpal.store.repository import insert_policy

    cfg = tmp_path / "config.toml"
    cfg.write_text('[buckets.site_defaults]\n"github.com" = "Personal"\n"corp.example" = "Work"\n')
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    assert _bucket_of(_url_event("https://github.com/o/r", 0)) == "Personal"           # config beats preset
    assert _bucket_of(_url_event("https://wiki.corp.example/x", 1)) == "Work"           # config adds a site
    assert _bucket_of(_url_event("https://www.icbc.com.cn/", 2)) == "Financial"         # preset still applies
    insert_policy(Policy(scope_type="app", scope_value="Safari", mode=buckets.BUCKET_MODE,
                         config_json=json.dumps({"bucket": "Private"}), priority=1))
    assert _bucket_of(_url_event("https://github.com/o/r", 3, browser="Safari")) == "Private"  # user rule wins


def test_reapply_buckets_is_site_aware(tmp_path):
    init_db(tmp_path / "kp.db")
    u = _url_event("https://github.com/o/r", 0)
    a = _ax("a long enough sentence typed here", 1)
    conn = get_conn()
    conn.execute("UPDATE raw_events SET bucket = 'Personal'")
    conn.commit()
    assert buckets.reapply_buckets(conn) == 2
    assert (_bucket_of(u), _bucket_of(a)) == ("Work", "Work")


# ── 7. migration v26 ─────────────────────────────────────────────────────────────────────────
def _db_at_v25(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    for i, step in enumerate(MIGRATIONS[:25], start=1):
        step(conn) if callable(step) else conn.executescript(step.strip())
        conn.execute("INSERT INTO _schema_version VALUES (?, 'x')", (i,))
    conn.commit()
    return conn


def _row(conn, source, text, *, app=None, proc=None, title=None, ts="2026-03-02T10:00:00+00:00", bucket="Personal",
         etype=None, meta=None, n=[0]) -> int:
    n[0] += 1
    cur = conn.execute(
        "INSERT INTO raw_events(source,event_type,ts_start,app_name,process_name,window_title,content_text,content_hash,"
        "metadata_json,created_at,bucket) VALUES (?,?,?,?,?,?,?,?,?,'2026-01-01',?)",
        (source, etype or source, ts, app, proc, title, text, f"h{n[0]}", meta, bucket))
    return cur.lastrowid


def _seed_v25(path: Path) -> tuple[sqlite3.Connection, dict]:
    conn = _db_at_v25(path)
    ids: dict[str, int] = {}
    ids["zw"] = _row(conn, "claude_code", f"zebra{ZW}corn plan", bucket="Work")
    ids["zw_title"] = _row(conn, "window", None, app="Notes", title=f"Ti{ZW}tle", etype="window_focus_session")
    ids["ov_window"] = _row(conn, "window", None, app="Setup Assistant", title="Welcome", etype="window_focus_session")
    ids["ov_clip"] = _row(conn, "clipboard", "copied during setup phase", app="设置助理", etype="clipboard_copy")
    ids["ov_key"] = _row(conn, "keyboard_chunk", "typed during setup phase", app="Setup Assistant", proc="com.apple.SetupAssistant")
    ids["ov_manual"] = _row(conn, "clipboard", "set by hand to Work", app="Setup Assistant", bucket="Work", etype="clipboard_copy")
    ids["noise_all"] = _row(conn, "claude_code", "<system-reminder>\nhost injected text\n</system-reminder>", bucket="Work")
    ids["noise_cmd"] = _row(conn, "codex_cli", "<environment_context>cwd</environment_context>", bucket="Work")
    ids["noise_inline"] = _row(conn, "claude_code", "real question here<system-reminder>injected</system-reminder> and more", bucket="Work")
    ids["clean"] = _row(conn, "claude_code", "an ordinary message", bucket="Work")
    ids["ph"] = _row(conn, "ax_text", "问问 ChatGPT", app="Google Chrome", etype="ax_text_capture")
    ids["ph_real"] = _row(conn, "ax_text", "Ask anything about pelicans in detail", app="Google Chrome", etype="ax_text_capture")
    ids["zsh"] = _row(conn, "zsh_history", "echo å¥½", bucket="Work")
    # D28 fixtures: Chrome urls + text, Safari
    ids["u_gh"] = _row(conn, "browser_url", "t", app="Google Chrome", etype="browser_url_capture", ts="2026-03-02T11:00:00+00:00",
                       meta=json.dumps({"url": "https://github.com/o/r"}))
    ids["ax_gh"] = _row(conn, "ax_text", "reading code review comments", app="Google Chrome", etype="ax_text_capture", ts="2026-03-02T11:03:00+00:00")
    ids["ax_gh_manual"] = _row(conn, "ax_text", "manually moved to Private", app="Google Chrome", etype="ax_text_capture",
                               ts="2026-03-02T11:04:00+00:00", bucket="Private")
    ids["ax_gh_stale"] = _row(conn, "ax_text", "an hour later this is stale", app="Google Chrome", etype="ax_text_capture", ts="2026-03-02T12:30:00+00:00")
    ids["u_bank"] = _row(conn, "browser_url", "t", app="Safari", etype="browser_url_capture", ts="2026-03-02T11:00:00+00:00",
                         meta=json.dumps({"url": "https://www.icbc.com.cn/x"}))
    ids["clip_bank"] = _row(conn, "clipboard", "account number copied here", app="Safari", proc="com.apple.Safari",
                            etype="clipboard_copy", ts="2026-03-02T11:01:00+00:00")
    ids["kb_notes"] = _row(conn, "keyboard_chunk", "typed in notes during the github visit", app="Notes", ts="2026-03-02T11:02:00+00:00")
    for rid in ids.values():
        if conn.execute("SELECT source FROM raw_events WHERE id=?", (rid,)).fetchone()[0] != "claude_code":
            conn.execute("INSERT INTO evidence(event_id,locator,available,created_at) VALUES (?,?,1,'x')",
                         (rid, f"penpal://event/{rid}"))
    conn.execute("INSERT INTO sessions(id,started_at,ended_at,app_name,created_at,updated_at) VALUES "
                 "('s1','2026-03-02','2026-03-02','Setup Assistant','x','x'),('s2','2026-03-02','2026-03-02','Notes','x','x')")
    src = conn.execute("INSERT INTO sources(type,locator,created_at) VALUES ('zsh_history','/p/.zsh_history','x')").lastrowid
    conn.execute("INSERT INTO sync_cursors(source_id,cursor) VALUES (?, '{\"decoded\": 1}')", (src,))
    conn.commit()
    return conn, ids


def test_v26_migration_end_to_end_and_idempotent(tmp_path):
    conn, ids = _seed_v25(tmp_path / "old.db")
    run_migrations(conn)
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == 34
    assert list(tmp_path.glob("old.db.v25-*.bak"))
    q = lambda col, rid: conn.execute(f"SELECT {col} FROM raw_events WHERE id = ?", (rid,)).fetchone()
    exists = lambda rid: q("id", rid) is not None
    fts = lambda s: [r[0] for r in conn.execute("SELECT rowid FROM raw_events_fts WHERE raw_events_fts MATCH ?", (s,))]

    # 5 zero-width
    assert q("content_text", ids["zw"])[0] == "zebracorn plan" and q("window_title", ids["zw_title"])[0] == "Title"
    assert fts('"zebracorn"') == [ids["zw"]]
    # 2 overlay
    assert not exists(ids["ov_window"])
    assert conn.execute("SELECT app_name FROM sessions").fetchall()[0][0] == "Notes" and conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    for k in ("ov_clip", "ov_key"):
        r = conn.execute("SELECT app_name, process_name, bucket, content_text FROM raw_events WHERE id = ?", (ids[k],)).fetchone()
        assert (r[0], r[1], r[2]) == (None, None, "Personal") and r[3]
    assert q("bucket", ids["ov_manual"])[0] == "Work" and q("app_name", ids["ov_manual"])[0] is None   # manual bucket kept
    # 3 noise
    assert not exists(ids["noise_all"]) and not exists(ids["noise_cmd"])
    assert q("content_text", ids["noise_inline"])[0] == "real question here and more"
    assert q("content_text", ids["clean"])[0] == "an ordinary message"
    assert fts('"injected"') == [] and fts('"host injected"') == []
    # 4 placeholder
    assert not exists(ids["ph"]) and exists(ids["ph_real"])
    # evidence follows deletes
    ev = {r[0] for r in conn.execute("SELECT event_id FROM evidence")}
    assert {ids["ov_window"], ids["ph"], ids["zsh"]}.isdisjoint(ev) and ids["ph_real"] in ev
    # 1 zsh
    assert not exists(ids["zsh"])
    assert conn.execute("SELECT count(*) FROM sync_cursors").fetchone()[0] == 0
    # 6 site buckets
    assert q("bucket", ids["u_gh"])[0] == "Work" and q("bucket", ids["u_bank"])[0] == "Financial"
    assert q("bucket", ids["ax_gh"])[0] == "Work"                  # inherits github url
    assert q("bucket", ids["ax_gh_manual"])[0] == "Private"        # user's choice untouched
    assert q("bucket", ids["ax_gh_stale"])[0] == "Personal"        # > 10 minutes after the url
    assert q("bucket", ids["clip_bank"])[0] == "Financial"
    assert q("bucket", ids["kb_notes"])[0] == "Personal"           # not a browser

    before = [tuple(r) for r in conn.execute("SELECT * FROM raw_events ORDER BY id")]
    migrate_v26(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM raw_events ORDER BY id")] == before
    conn.close()


def test_v26_is_atomic_on_failure(tmp_path, monkeypatch):
    conn, ids = _seed_v25(tmp_path / "old.db")
    import penpal.store.migration_v26 as m

    monkeypatch.setattr(m, "_rebucket_by_site", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    steps = list(MIGRATIONS)
    steps[-1] = m.migrate_v26  # the registered function calls the patched module global
    monkeypatch.setattr("penpal.store.migrations.MIGRATIONS", steps)
    with pytest.raises(RuntimeError):
        run_migrations(conn)
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == 25
    assert conn.execute("SELECT content_text FROM raw_events WHERE id = ?", (ids["noise_all"],)).fetchone() is not None
    assert conn.execute("SELECT count(*) FROM raw_events WHERE source = 'zsh_history'").fetchone()[0] == 1
    conn.close()


@pytest.mark.slow
def test_v26_is_fast_on_25k_rows(tmp_path):
    conn = _db_at_v25(tmp_path / "big.db")
    apps = ["Google Chrome", "Safari", "Notes", "Cursor", "Setup Assistant"]
    sources = ["ax_text", "clipboard", "keyboard_chunk", "window", "browser_url"]
    urls = ["https://github.com/o/r", "https://www.bilibili.com/v", "https://www.icbc.com.cn/", "https://docs.python.org/3/"]
    rows = []
    for i in range(25_000):
        src, app = sources[i % 5], apps[(i // 5) % 5]
        text = (f"sentence number {i} about nothing in particular{ZW}" if i % 7 == 0 else
                "Ask anything" if i % 11 == 0 else f"sentence number {i} about nothing in particular")
        meta = json.dumps({"url": urls[i % 4]}) if src == "browser_url" else None
        rows.append((src, src, f"2026-03-{1 + i // 1000:02d}T{(i // 40) % 24:02d}:{i % 60:02d}:00+00:00", app, text, f"h{i}", meta))
    rows += [("claude_code", "claude_code", f"2026-04-01T00:00:{i % 60:02d}+00:00", None,
              "<system-reminder>x</system-reminder>" if i % 3 == 0 else "hello there friend", f"c{i}", None) for i in range(2000)]
    conn.executemany(
        "INSERT INTO raw_events(source,event_type,ts_start,app_name,content_text,content_hash,metadata_json,created_at,bucket) "
        "VALUES (?,?,?,?,?,?,?,'2026-01-01','Personal')", rows)
    conn.commit()
    start = time.perf_counter()
    run_migrations(conn)
    elapsed = time.perf_counter() - start
    print(f"v26 migration {len(rows)} rows: {elapsed:.2f}s")
    assert elapsed < 30
    assert conn.execute("SELECT count(*) FROM raw_events WHERE content_text LIKE '%' || char(8203) || '%'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM raw_events WHERE bucket = 'Work' AND source = 'browser_url'").fetchone()[0] > 0
    conn.close()
