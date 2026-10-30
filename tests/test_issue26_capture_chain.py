"""Issue #26 acceptance tests through the real capture main chain.

Path under test: watcher-style queue put -> CaptureManager._drain_queue -> _process_event (exclusion
gates, hygiene, policy, storage) -> search_context through the MCP server layer and grant checks.
Only a temp DB and a monkeypatched config are used; HOME is isolated. Synthetic data only.

AT18: user-excluded app (bundle id) and domain produce no content events; a control app does.
AT20: chat app is silent until opted in via `penpal privacy chat-apps allow`; then its screen text
      lands in Private and only a host granted Private can find it.
AC3:  foreground window and clipboard events are searchable through search_context.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner
from mcp.shared.memory import create_connected_server_and_client_session

from penpal.capture.manager import CaptureManager
from penpal.capture.normalizer import (
    normalize_ax_text_event,
    normalize_browser_tab_event,
    normalize_browser_url_event,
    normalize_clipboard_event,
    normalize_keyboard_chunk_event,
    normalize_window_event,
)
from penpal.cli import main
from penpal.config import Config
from penpal.mcp import auth
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from mcp_helpers import server_for

# Synthetic exclusion targets (not real apps or sites).
EXCLUDED_BUNDLE = "com.example.vault"
EXCLUDED_APP = dict(app_name="ExampleVault", process_name=EXCLUDED_BUNDLE)
EXCLUDED_DOMAIN = "clinic.example.test"
CONTROL_APP = dict(app_name="Notes", process_name="com.apple.Notes")
SLACK = dict(app_name="Slack", process_name="com.tinyspeck.slackmacgap")
TS = "2026-03-02T10:00:{:02d}+00:00"
PRIVATE_HOST = ["Private"]
WORK_PERSONAL_HOST = ["Work", "Personal"]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def db(tmp_path):
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    yield tmp_path
    close()
    buckets.reset_cache()


def _use_config(monkeypatch, cfg: Config) -> None:
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    buckets.reset_cache()


def _feed(mgr: CaptureManager, events) -> None:
    """Same entry the watchers use: emit onto the shared queue, then the manager drains it."""
    for ev in events:
        mgr._queue.put(ev)
    mgr._drain_queue()


def _rows() -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT source, app_name, bucket, content_text, window_title, metadata_json FROM raw_events "
        "WHERE source != 'idle' ORDER BY id")]


def _mcp_search(token: str, keywords: list[str]) -> dict:
    """search_context through the MCP protocol layer, authenticated by the host's token."""
    server = server_for(token)

    async def run():
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return await client.call_tool("search_context", {"keywords": keywords})

    result = asyncio.run(run())
    assert result.isError is False
    return json.loads(result.content[0].text)


def _hits(token: str, word: str) -> list[dict]:
    return _mcp_search(token, [word])["results"]


# ── AT18: user-excluded app and domain never reach storage ──────────────────────────────────
def _excluded_config() -> Config:
    return Config.model_validate({
        "privacy": {
            "blacklist_bundle_ids": [EXCLUDED_BUNDLE],
            "blacklist_patterns": [],
            "blacklist_app_names": [],
            "url_deny_hosts": [EXCLUDED_DOMAIN],
        }
    })


def test_at18_excluded_app_and_domain_produce_no_content_events(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _feed(mgr, [
        normalize_window_event("window_focus_session", "ExampleVault", "zebracorn vault", EXCLUDED_BUNDLE,
                               ts_start=TS.format(1), ts_end=TS.format(9)),
        normalize_keyboard_chunk_event("zebracorn typed secret entry", ts_start=TS.format(2), **EXCLUDED_APP),
        normalize_clipboard_event("zebracorn copied from vault", app_name="ExampleVault", ts_start=TS.format(3),
                                 process_name=EXCLUDED_BUNDLE),  # watcher sends the active bundle id
        normalize_ax_text_event("zebracorn screen text in vault", window_title="Vault", ts_start=TS.format(4), **EXCLUDED_APP),
        normalize_browser_url_event(f"https://{EXCLUDED_DOMAIN}/visit/1", "zebracorn clinic", "Google Chrome",
                                    ts_start=TS.format(5)),
        normalize_browser_url_event(f"https://portal.{EXCLUDED_DOMAIN}/x", "zebracorn portal", "Google Chrome",
                                    ts_start=TS.format(6)),
    ])
    assert [r for r in _rows() if r["content_text"] or r["window_title"]] == []
    assert not any("zebracorn" in str(v) for v in mgr.health()["gate_hits"])

    everything = auth.create_grant("all-buckets-host", buckets=["Work", "Personal", "Private"])
    assert _mcp_search(everything, ["zebracorn"])["status"] == "no_results"


def test_at18_control_app_and_domain_are_stored_and_searchable(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _feed(mgr, [
        normalize_window_event("window_focus_session", "ExampleVault", "zebracorn vault", EXCLUDED_BUNDLE,
                               ts_start=TS.format(1), ts_end=TS.format(9)),
        normalize_keyboard_chunk_event("zebracorn typed secret entry", ts_start=TS.format(2), **EXCLUDED_APP),
        normalize_window_event("window_focus_session", "Notes", "Zebracorn roadmap", CONTROL_APP["process_name"],
                               ts_start=TS.format(3), ts_end=TS.format(9)),
        normalize_keyboard_chunk_event("zebracorn control note text here", ts_start=TS.format(4), **CONTROL_APP),
        normalize_clipboard_event("zebracorn control clipboard", app_name="Notes", ts_start=TS.format(5)),
        normalize_browser_url_event("https://docs.example.org/zebracorn", "zebracorn docs", "Google Chrome",
                                    ts_start=TS.format(6)),
    ])
    stored_apps = {r["app_name"] for r in _rows()}
    assert stored_apps == {"Notes", "Google Chrome"}

    host = auth.create_grant("work-host", buckets=WORK_PERSONAL_HOST)
    sources = {r["source"] for r in _mcp_search(host, ["zebracorn"])["results"]}
    assert {"window", "keyboard_chunk", "clipboard", "browser_url"} <= sources


# ── AC3: foreground window and clipboard are retrievable via search_context ─────────────────
def test_ac3_foreground_window_and_clipboard_retrievable_through_mcp(db, monkeypatch):
    cfg = Config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _feed(mgr, [
        normalize_window_event("window_focus_session", "Notes", "Quokka planning doc", CONTROL_APP["process_name"],
                               ts_start=TS.format(1), ts_end=TS.format(20)),
        normalize_clipboard_event("quokka invoice number 4471", app_name="Notes", ts_start=TS.format(2)),
    ])
    host = auth.create_grant("work-host", buckets=WORK_PERSONAL_HOST)
    by_source = {r["source"]: r for r in _hits(host, "quokka")}
    assert {"window", "clipboard"} <= set(by_source)
    assert by_source["window"]["bucket"] in WORK_PERSONAL_HOST


# ── AT20: chat app opt-in via the CLI path, then Private-only visibility ────────────────────
def _seed_slack(mgr: CaptureManager) -> None:
    _feed(mgr, [
        normalize_ax_text_event("zebracorn screen text from the channel", window_title="general",
                                ts_start=TS.format(2), **SLACK),
        normalize_keyboard_chunk_event("zebracorn typed reply to the team", ts_start=TS.format(3), **SLACK),
        normalize_clipboard_event("zebracorn copied from slack", app_name="Slack", ts_start=TS.format(4)),
    ])


def test_at20_chat_app_not_opted_in_has_no_content_events(db, monkeypatch):
    _use_config(monkeypatch, Config())
    _seed_slack(CaptureManager(Config()))
    assert [r for r in _rows() if r["content_text"]] == []
    for host in (auth.create_grant("private-host", buckets=PRIVATE_HOST),
                 auth.create_grant("work-host", buckets=WORK_PERSONAL_HOST)):
        assert _mcp_search(host, ["zebracorn"])["status"] == "no_results"


def test_at20_opt_in_via_cli_lands_in_private_and_is_scoped_by_grant(db, monkeypatch):
    # Opt in through the same command a user runs: `penpal privacy chat-apps allow Slack`.
    result = CliRunner().invoke(main, ["privacy", "chat-apps", "allow", "Slack"])
    assert result.exit_code == 0, result.output

    buckets.reset_cache()
    cfg = Config.load()
    assert cfg.privacy.chat_apps_opt_in == ["Slack"]
    _use_config(monkeypatch, cfg)
    _seed_slack(CaptureManager(cfg))

    rows = _rows()
    assert {r["source"] for r in rows} >= {"ax_text", "keyboard_chunk", "clipboard"}
    assert {r["bucket"] for r in rows} == {"Private"}

    work_personal = auth.create_grant("work-personal-host", buckets=WORK_PERSONAL_HOST)
    assert _mcp_search(work_personal, ["zebracorn"])["status"] == "no_results"

    private = auth.create_grant("private-host", buckets=PRIVATE_HOST)
    found = _mcp_search(private, ["zebracorn"])["results"]
    assert {r["bucket"] for r in found} == {"Private"}
    assert {"ax_text", "keyboard_chunk", "clipboard"} <= {r["source"] for r in found}


# ── AT18 on the sources path: browser history from an excluded domain is never stored ───────
WEBKIT_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000


def _webkit_us(day: int) -> int:
    from datetime import datetime, timezone

    return int(datetime(2026, 3, day, 10, 0, tzinfo=timezone.utc).timestamp() * 1_000_000) + WEBKIT_EPOCH_OFFSET_US


@pytest.fixture
def chrome_history(tmp_path, monkeypatch):
    """Synthetic Chrome profile under a temp HOME; only the chrome_history plugin is registered."""
    import sqlite3
    from pathlib import Path

    from penpal.sources import registry
    from penpal.sources.cleaning.config import CleaningConfig
    from penpal.sources.plugins.chrome_history import ChromeHistorySource

    home = tmp_path / "chrome-home"
    history = home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "History"
    history.parent.mkdir(parents=True)
    conn = sqlite3.connect(history)
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT)")
    conn.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER)")
    rows = [
        (1, f"https://{EXCLUDED_DOMAIN}/visit/1", "zebracorn clinic booking"),
        (2, f"https://portal.{EXCLUDED_DOMAIN}/x", "zebracorn patient portal"),
        (3, "https://docs.example.org/zebracorn", "zebracorn public handbook"),
    ]
    for i, (uid, url, title) in enumerate(rows, start=1):
        conn.execute("INSERT INTO urls(id, url, title) VALUES (?, ?, ?)", (uid, url, title))
        conn.execute("INSERT INTO visits(id, url, visit_time) VALUES (?, ?, ?)", (10 + i, uid, _webkit_us(i)))
    conn.commit()
    conn.close()

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(registry, "_PLUGINS", {"chrome_history": ChromeHistorySource()})
    monkeypatch.setattr(registry, "load_cleaning_config", lambda: CleaningConfig())
    return registry


def test_at18_browser_history_from_excluded_domain_is_not_stored(db, monkeypatch, chrome_history):
    from datetime import datetime, timezone

    from penpal.sources.sink import flush_named_entity_backfill

    _use_config(monkeypatch, _excluded_config())
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    until = datetime(2026, 3, 31, tzinfo=timezone.utc)
    yielded = list(chrome_history.read_all(since, until, source="chrome_history"))
    flush_named_entity_backfill()

    assert {e.metadata["full_url"].split("/")[2] for e in yielded} == {"docs.example.org"}
    stored = [r["content_text"] for r in _rows()]
    assert stored and all("zebracorn" in (t or "") for t in stored)
    assert "zebracorn clinic booking" not in stored and "zebracorn patient portal" not in stored
    assert _mcp_search(auth.create_grant("work-host", buckets=WORK_PERSONAL_HOST), ["zebracorn"])["results"]
    assert _mcp_search(auth.create_grant("all-host", buckets=["Work", "Personal", "Private"]),
                       ["clinic"])["status"] == "no_results"


# ── AT18 across streams: text typed, read or copied in an excluded-domain browser tab ───────
CHROME = dict(app_name="Google Chrome", process_name="com.google.Chrome")
SAFARI = dict(app_name="Safari", process_name="com.apple.Safari")
ALLOWED_URL = "https://docs.example.org/zebracorn"


def _page(mgr: CaptureManager, url: str, title: str, browser: dict = CHROME, n: int = 1) -> None:
    """Browser URL watcher event (emitted on tab / URL change), through the queue."""
    _feed(mgr, [normalize_browser_url_event(url, title, browser["app_name"], ts_start=TS.format(n))])


def _typed(mgr: CaptureManager, text: str, n: int, browser: dict = CHROME) -> None:
    _feed(mgr, [
        normalize_keyboard_chunk_event(text, ts_start=TS.format(n), **browser),
        normalize_ax_text_event(text, window_title="Tab", ts_start=TS.format(n + 1), **browser),
        # Distinct wording: identical text is merged by the fusion step into the AX row (existing behaviour).
        normalize_clipboard_event("zebracorn quarterly figure copied", app_name=browser["app_name"], ts_start=TS.format(n + 2),
                                  process_name=browser["process_name"]),
    ])


def _content_for(text: str) -> list[dict]:
    return [r for r in _rows() if r["content_text"] and text in r["content_text"]]


def test_typing_in_excluded_domain_tab_is_not_stored_as_content(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _page(mgr, f"https://{EXCLUDED_DOMAIN}/visit/1", "clinic booking")  # dropped itself, but tracked
    _typed(mgr, "zebracorn typed patient note", n=2)

    assert _content_for("zebracorn") == []
    kb = [r for r in _rows() if r["source"] == "keyboard_chunk"]
    assert len(kb) == 1 and kb[0]["app_name"] == "Google Chrome"  # app and time kept, as for private windows
    assert kb[0]["window_title"] is None
    assert json.loads(kb[0]["metadata_json"])["text_dropped"] == "denied_domain"
    assert not any(r["source"] == "browser_url" for r in _rows())  # the URL event itself is still dropped
    assert _mcp_search(auth.create_grant("all-host", buckets=["Work", "Personal", "Private"]),
                       ["zebracorn"])["status"] == "no_results"


def test_same_browser_on_allowed_domain_stores_text(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _page(mgr, ALLOWED_URL, "zebracorn docs")
    _typed(mgr, "zebracorn typed a sprint note", n=2)

    assert {r["source"] for r in _content_for("zebracorn typed")} >= {"keyboard_chunk", "ax_text"}
    assert {r["source"] for r in _content_for("quarterly figure")} == {"clipboard"}


def test_switching_tabs_updates_the_page_used_for_the_check(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _page(mgr, ALLOWED_URL, "zebracorn docs", n=1)
    _typed(mgr, "zebracorn first tab text", n=2)
    _page(mgr, f"https://{EXCLUDED_DOMAIN}/visit/1", "clinic", n=5)  # switch to excluded tab
    _typed(mgr, "zebracorn second tab text", n=6)
    _page(mgr, ALLOWED_URL, "zebracorn docs", n=9)  # back to the allowed tab
    _typed(mgr, "zebracorn third tab text", n=10)

    assert _content_for("first tab")
    assert _content_for("third tab")
    assert _content_for("second tab") == []


def test_unknown_page_is_not_blocked(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    # No URL seen for Chrome yet (URL watcher off or not polled yet): unknown, so not blocked.
    _typed(mgr, "zebracorn typed before any url", n=2)
    assert _content_for("before any url")
    # A URL for another browser does not apply to Chrome.
    _page(mgr, f"https://{EXCLUDED_DOMAIN}/visit/1", "clinic", browser=SAFARI, n=5)
    _typed(mgr, "zebracorn chrome text", n=6)
    assert _content_for("chrome text")


def test_browser_tab_watcher_event_also_sets_the_page(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _feed(mgr, [normalize_browser_tab_event(f"https://{EXCLUDED_DOMAIN}/visit/2", "clinic", "Google Chrome",
                                            ts_start=TS.format(1))])
    _typed(mgr, "zebracorn tab watcher text", n=2)
    assert _content_for("tab watcher") == []


# ── Window events: the foreground title of an excluded-domain browser tab is not stored ───────
def _window_on(browser: dict, title: str, n: int):
    """Session event as the window watcher emits it: the title is in window_title and in metadata."""
    return normalize_window_event("window_focus_session", browser["app_name"], title, browser["process_name"],
                                  ts_start=TS.format(n), ts_end=TS.format(n + 1),
                                  metadata={"duration_sec": 1, "primary_title": title})


def test_window_title_on_excluded_domain_tab_is_not_stored(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _page(mgr, f"https://{EXCLUDED_DOMAIN}/visit/1", "clinic booking")
    _feed(mgr, [_window_on(CHROME, "zebracorn clinic booking", n=2)])

    win = [r for r in _rows() if r["source"] == "window"]
    assert len(win) == 1
    assert win[0]["app_name"] == "Google Chrome"  # app and time kept, as for private windows
    assert win[0]["window_title"] is None
    assert "zebracorn" not in (win[0]["metadata_json"] or "")  # the watcher's primary_title copy is dropped too
    assert json.loads(win[0]["metadata_json"])["text_dropped"] == "denied_domain"
    assert _mcp_search(auth.create_grant("all-host", buckets=["Work", "Personal", "Private"]),
                       ["zebracorn"])["status"] == "no_results"


def test_window_title_on_allowed_domain_tab_is_stored(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    _page(mgr, ALLOWED_URL, "zebracorn docs")
    _feed(mgr, [_window_on(CHROME, "zebracorn sprint docs", n=2)])

    win = [r for r in _rows() if r["source"] == "window"]
    assert len(win) == 1 and win[0]["window_title"] == "zebracorn sprint docs"
    host = auth.create_grant("work-host", buckets=WORK_PERSONAL_HOST)
    assert "window" in {r["source"] for r in _hits(host, "sprint")}


def test_window_title_with_unknown_page_is_not_blocked(db, monkeypatch):
    cfg = _excluded_config()
    _use_config(monkeypatch, cfg)
    mgr = CaptureManager(cfg)
    # No URL seen for Chrome yet: unknown, so the title is kept (same policy as typed text).
    _feed(mgr, [_window_on(CHROME, "zebracorn before any url", n=2)])
    assert [r["window_title"] for r in _rows() if r["source"] == "window"] == ["zebracorn before any url"]
