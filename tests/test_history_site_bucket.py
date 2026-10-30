"""Browser-history plugins (chrome_history / safari_history) are bucketed by website, like browser_url.

Synthetic data only. Covers insert time (through the sink, so the plugin's full_url -> metadata url mapping is
exercised), the resolution order, and migration v27 on a v26 database.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from penpal.sources.sink import persist_semantic_event
from penpal.sources.types import SemanticEvent
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v27 import migrate_v27
from penpal.store.migrations import MIGRATIONS, SCHEMA_VERSION, run_migrations
from penpal.store.models import Policy
from penpal.store.repository import insert_policy


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Config.load() must never see a real ~/.penpal/config.toml or a cwd config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    buckets.reset_cache()
    yield
    buckets.reset_cache()
    close()


_visit_n = [0]


def _visit(source: str, full_url: str, minute: int = 0) -> int:
    """Persist one history visit exactly as the plugin emits it (artifact + metadata.full_url)."""
    _visit_n[0] += 1
    event = SemanticEvent(
        time=datetime(2026, 3, 2, 10, minute, tzinfo=timezone.utc),
        source=source,
        actor="user",
        intent="synthetic page title",
        artifact=full_url.split("?", 1)[0],
        raw_ref=f"synthetic:visit:{_visit_n[0]}",
        privacy_tier="standard",
        metadata={"profile": "Default", "full_url": full_url},
    )
    return persist_semantic_event(event, async_named_entities=False)


def _bucket_of(rid: int):
    return get_conn().execute("SELECT bucket FROM raw_events WHERE id = ?", (rid,)).fetchone()[0]


def test_history_rows_follow_the_site_at_insert(tmp_path):
    init_db(tmp_path / "kp.db")
    assert _bucket_of(_visit("chrome_history", "https://github.com/o/r?tab=readme", 0)) == "Work"
    assert _bucket_of(_visit("chrome_history", "https://open.feishu.cn/docs/x", 1)) == "Work"
    assert _bucket_of(_visit("chrome_history", "https://www.icbc.com.cn/account", 2)) == "Financial"
    assert _bucket_of(_visit("safari_history", "https://www.chase.com/", 3)) == "Financial"
    assert _bucket_of(_visit("safari_history", "https://github.com/o/r", 4)) == "Work"
    assert _bucket_of(_visit("chrome_history", "https://example.org/news", 5)) == "Personal"


def test_history_resolution_order_rule_over_config_over_preset(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[buckets.site_defaults]\n"github.com" = "Personal"\n')
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    assert _bucket_of(_visit("chrome_history", "https://github.com/o/r", 0)) == "Personal"   # config beats preset
    assert _bucket_of(_visit("chrome_history", "https://www.icbc.com.cn/", 1)) == "Financial"  # preset still applies
    insert_policy(Policy(scope_type="source", scope_value="chrome_history", mode=buckets.BUCKET_MODE,
                         config_json=json.dumps({"bucket": "Private"}), priority=1))
    assert _bucket_of(_visit("chrome_history", "https://www.icbc.com.cn/", 2)) == "Private"  # user rule wins


def test_history_reapply_is_site_aware(tmp_path):
    init_db(tmp_path / "kp.db")
    gh = _visit("chrome_history", "https://github.com/o/r", 0)
    bank = _visit("safari_history", "https://www.icbc.com.cn/", 1)
    conn = get_conn()
    conn.execute("UPDATE raw_events SET bucket = 'Personal'")
    conn.commit()
    assert buckets.reapply_buckets(conn) == 2
    assert (_bucket_of(gh), _bucket_of(bank)) == ("Work", "Financial")


# ── migration v27 ───────────────────────────────────────────────────────────────────────────
def _db_at_v26(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    for i, step in enumerate(MIGRATIONS[:26], start=1):
        step(conn) if callable(step) else conn.executescript(step.strip())
        conn.execute("INSERT INTO _schema_version VALUES (?, 'x')", (i,))
    conn.commit()
    return conn


_n = [0]


def _row(conn, source, *, url=None, bucket="Personal", app="Google Chrome", ts="2026-03-02T10:00:00+00:00") -> int:
    _n[0] += 1
    meta = json.dumps({"url": url, "profile": "Default"}) if url else None
    cur = conn.execute(
        "INSERT INTO raw_events(source,event_type,ts_start,app_name,content_text,content_hash,metadata_json,"
        "created_at,bucket) VALUES (?,?,?,?,?,?,?,'2026-01-01',?)",
        (source, source, ts, app, "synthetic title", f"hv27-{_n[0]}", meta, bucket),
    )
    return cur.lastrowid


def test_v27_rebuckets_personal_history_rows_by_url(tmp_path):
    conn = _db_at_v26(tmp_path / "old.db")
    gh = _row(conn, "chrome_history", url="https://github.com/o/r")
    feishu = _row(conn, "chrome_history", url="https://feishu.cn/wiki/x", ts="2026-03-02T10:01:00+00:00")
    bank = _row(conn, "safari_history", url="https://www.icbc.com.cn/", app="Safari", ts="2026-03-02T10:02:00+00:00")
    unknown = _row(conn, "chrome_history", url="https://example.org/", ts="2026-03-02T10:03:00+00:00")
    no_url = _row(conn, "chrome_history", url=None, ts="2026-03-02T10:04:00+00:00")
    manual_bank = _row(conn, "chrome_history", url="https://www.icbc.com.cn/", bucket="Private",
                       ts="2026-03-02T10:05:00+00:00")
    other = _row(conn, "claude_code", url="https://github.com/o/r", bucket="Personal", app=None,
                 ts="2026-03-02T10:06:00+00:00")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == SCHEMA_VERSION
    q = lambda rid: conn.execute("SELECT bucket FROM raw_events WHERE id = ?", (rid,)).fetchone()[0]
    assert q(gh) == "Work"
    assert q(feishu) == "Work"
    assert q(bank) == "Financial"
    assert q(unknown) == "Personal"
    assert q(no_url) == "Personal"
    assert q(manual_bank) == "Private"   # not on the old default: the user's choice stays
    assert q(other) == "Personal"        # not a history source: untouched
    conn.close()


def test_v27_is_idempotent(tmp_path):
    conn = _db_at_v26(tmp_path / "old.db")
    _row(conn, "chrome_history", url="https://github.com/o/r")
    _row(conn, "safari_history", url="https://example.org/")
    conn.commit()
    run_migrations(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM raw_events ORDER BY id")]
    migrate_v27(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM raw_events ORDER BY id")] == before
    conn.close()


def test_v27_is_atomic_with_version_stamp(tmp_path, monkeypatch):
    conn = _db_at_v26(tmp_path / "old.db")
    gh = _row(conn, "chrome_history", url="https://github.com/o/r")
    conn.commit()
    import penpal.store.migration_v27 as m

    real = m.resolve_bucket
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(m, "resolve_bucket", flaky)
    _row(conn, "chrome_history", url="https://www.icbc.com.cn/")
    conn.commit()
    steps = list(MIGRATIONS)
    steps[26] = m.migrate_v27
    monkeypatch.setattr("penpal.store.migrations.MIGRATIONS", steps)
    with pytest.raises(RuntimeError):
        run_migrations(conn)
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == 26
    assert conn.execute("SELECT bucket FROM raw_events WHERE id = ?", (gh,)).fetchone()[0] == "Personal"
    conn.close()
