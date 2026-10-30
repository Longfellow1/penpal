"""P0-04: bucket assignment (source default + app rules), v21 migration, grant scope."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from penpal.capture.policy import ALLOW, DENY, PolicyEngine
from penpal.config import Config
from penpal.i18n import T
from mcp_helpers import search
from penpal.mcp import auth
from penpal.mcp.cli import grant_output_lines, mcp_group
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.migrations import MIGRATIONS, SCHEMA_VERSION, run_migrations
from penpal.store.models import Policy, RawEvent
from penpal.store.repository import insert_policy, insert_raw_event, seed_policies_from_config


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Config.load() must never see a real ~/.penpal/config.toml or a cwd config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


_n = [0]


def _ev(source, text, ts="2026-03-01T09:00:00+00:00", app=None, bucket=None, event_type="semantic_event", metadata=None) -> int:
    _n[0] += 1
    return insert_raw_event(
        RawEvent(source=source, event_type=event_type, ts_start=ts, content_text=text, app_name=app,
                 content_hash=f"hh{_n[0]}", bucket=bucket,
                 metadata_json=json.dumps(metadata) if metadata is not None else None)
    )


def _bucket_of(row_id: int):
    return get_conn().execute("SELECT bucket FROM raw_events WHERE id = ?", (row_id,)).fetchone()[0]


@pytest.fixture
def db(tmp_path):
    buckets.reset_cache()
    init_db(tmp_path / "kp.db")
    yield tmp_path
    close()
    buckets.reset_cache()


# ── bucket assignment ────────────────────────────────────────────────────────────────────────
def test_source_defaults_and_unmapped_source_gets_no_bucket(db) -> None:
    assert _bucket_of(_ev("claude_code", "a")) == "Work"
    assert _bucket_of(_ev("chrome_history", "b")) == "Personal"
    assert _bucket_of(_ev("mystery_source", "c")) is None  # unmapped plugin source: never auto-placed in a shareable bucket
    assert _bucket_of(_ev("keyboard_chunk", "d")) == "Personal"  # capture stream, unknown app (D26)


def test_app_rule_routes_banking_app_to_financial_and_beats_source_default(db) -> None:
    insert_policy(Policy(scope_type="app", scope_value="Chase", mode=buckets.BUCKET_MODE,
                         config_json=json.dumps({"bucket": "Financial"}), priority=10))
    assert _bucket_of(_ev("knowledgec", "used app", app="Chase Mobile")) == "Financial"
    assert _bucket_of(_ev("keyboard_chunk", "typed", app="chase")) == "Financial"  # raw streams too
    assert _bucket_of(_ev("knowledgec", "used app", app="Notes")) == "Personal"


def test_rule_priority_and_disabled_rules(db) -> None:
    insert_policy(Policy(scope_type="app", scope_value="Bank", mode="bucket",
                         config_json=json.dumps({"bucket": "Private"}), priority=50))
    insert_policy(Policy(scope_type="app", scope_value="Bank", mode="bucket",
                         config_json=json.dumps({"bucket": "Financial"}), priority=5))
    insert_policy(Policy(scope_type="source", scope_value="git_log", mode="bucket",
                         config_json=json.dumps({"bucket": "Personal"}), enabled=False))
    assert _bucket_of(_ev("window", "x", app="MyBank")) == "Financial"  # lower priority number wins
    assert _bucket_of(_ev("git_log", "y")) == "Work"                    # disabled rule ignored


def test_project_rule_precedes_app_source_and_default_and_matches_exactly(db) -> None:
    insert_policy(Policy(scope_type="project", scope_value="life-admin", mode="bucket",
                         config_json=json.dumps({"bucket": "Financial"}), priority=100))
    insert_policy(Policy(scope_type="app", scope_value="Claude", mode="bucket",
                         config_json=json.dumps({"bucket": "Personal"}), priority=1))
    insert_policy(Policy(scope_type="source", scope_value="claude_code", mode="bucket",
                         config_json=json.dumps({"bucket": "Private"}), priority=1))

    exact = _ev("claude_code", "private message", app="Claude Code", metadata={"project": "LIFE-ADMIN"})
    partial = _ev("claude_code", "other project", app="Claude Code", ts="2026-03-01T10:00:00+00:00",
                  metadata={"project": "life-admin-tools"})
    source_only = _ev("claude_code", "source-scoped project", ts="2026-03-01T11:00:00+00:00",
                      metadata={"project": "unmatched"})
    assert _bucket_of(exact) == "Financial"  # project specificity beats lower numeric app/source priority
    assert _bucket_of(partial) == "Personal"  # exact project matching does not prefix-match
    assert _bucket_of(source_only) == "Private"  # source rule beats the claude_code Work default


def test_bucket_rules_do_not_disturb_privacy_policy_engine(db) -> None:
    insert_policy(Policy(scope_type="app", scope_value="Chase", mode="bucket",
                         config_json=json.dumps({"bucket": "Financial"}), priority=1))
    insert_policy(Policy(scope_type="app", scope_value="Chase", mode=DENY, priority=100))
    insert_policy(Policy(scope_type="app", scope_value="Safari", mode=ALLOW, priority=100))
    engine = PolicyEngine()
    engine.reload()
    ev = RawEvent(source="window", event_type="window_focus", ts_start="2026-03-01T00:00:00+00:00", app_name="Chase")
    assert engine.evaluate(ev) == DENY  # the bucket rule (priority 1) must not shadow the deny rule


def test_config_source_override_and_config_bucket_rule_seed(db, monkeypatch) -> None:
    cfg = Config.model_validate({
        "buckets": {"source_defaults": {"markdown_vault": "Work", "bogus": "Nope"}},
        "policies": [{"scope_type": "app", "scope_value": "1Password", "bucket": "private"}],
    })
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    buckets.reset_cache()
    seed_policies_from_config(cfg.policies)
    assert _bucket_of(_ev("markdown_vault", "note")) == "Work"
    assert _bucket_of(_ev("bogus", "z")) is None  # invalid override ignored
    assert _bucket_of(_ev("window", "pw", app="1Password 8")) == "Private"


def test_reapply_buckets_after_rule_change(db) -> None:
    rid = _ev("knowledgec", "late rule", app="Chase")
    assert _bucket_of(rid) == "Personal"
    insert_policy(Policy(scope_type="app", scope_value="Chase", mode="bucket",
                         config_json=json.dumps({"bucket": "Financial"})))
    assert buckets.reapply_buckets(get_conn()) == 1
    assert _bucket_of(rid) == "Financial"


def test_project_rule_rebucket_applies_to_legacy_project_dir_metadata(db) -> None:
    rid = _ev("claude_code", "stored therapy chat", metadata={"project_dir": "/tmp/claude/projects/-tmp-proj-life-admin"})
    assert _bucket_of(rid) == "Work"
    insert_policy(Policy(scope_type="project", scope_value="-tmp-proj-life-admin", mode="bucket",
                         config_json=json.dumps({"bucket": "Personal"})))
    assert buckets.reapply_buckets(get_conn()) == 1
    assert _bucket_of(rid) == "Personal"


def test_rules_cli_adds_bucket_rule(db, monkeypatch) -> None:
    from penpal import cli
    from penpal.cli import privacy as privacy_cli

    monkeypatch.setattr(privacy_cli, "get_config", lambda: Config())
    monkeypatch.setattr(privacy_cli, "require_db", lambda cfg: None)
    runner = CliRunner()
    ok = runner.invoke(cli.main, ["rules", "add", "--scope-type", "app", "--scope-value", "Chase", "--bucket", "financial"])
    assert ok.exit_code == 0, ok.output
    assert _bucket_of(_ev("window", "x", app="Chase")) == "Financial"
    assert runner.invoke(cli.main, ["rules", "add", "--scope-type", "window", "--scope-value", "x", "--bucket", "Work"]).exit_code != 0
    assert runner.invoke(cli.main, ["rules", "add", "--scope-type", "app", "--scope-value", "x", "--bucket", "Temporary"]).exit_code != 0
    assert runner.invoke(cli.main, ["rules", "add", "--scope-type", "app", "--scope-value", "x"]).exit_code != 0


def test_project_rules_cli_alias_and_projects_listing_are_read_only(db, monkeypatch) -> None:
    from penpal import cli
    from penpal.cli import privacy as privacy_cli

    monkeypatch.setattr(privacy_cli, "get_config", lambda: Config())
    monkeypatch.setattr(privacy_cli, "require_db", lambda cfg: None)
    _ev("claude_code", "must not appear in project listing", metadata={"project": "-tmp-proj-life-admin"})
    runner = CliRunner()
    result = runner.invoke(cli.main, [
        "rules", "add", "--scope", "project", "--value", "-tmp-proj-life-admin", "--bucket", "Personal",
    ])
    assert result.exit_code == 0, result.output
    listed = runner.invoke(cli.main, ["rules", "projects", "--plain"])
    assert listed.exit_code == 0, listed.output
    assert listed.output.strip() == "claude_code\t-tmp-proj-life-admin\t1\tWork"
    assert "must not appear" not in listed.output
    assert runner.invoke(cli.main, [
        "rules", "add", "--scope", "project", "--value", "life-admin", "--mode", "deny",
    ]).exit_code != 0


def test_rules_projects_folds_encoded_rows_into_decoded_name(db, monkeypatch) -> None:
    from penpal import cli
    from penpal.cli import privacy as privacy_cli

    monkeypatch.setattr(privacy_cli, "get_config", lambda: Config())
    monkeypatch.setattr(privacy_cli, "require_db", lambda cfg: None)

    # Decoded row: "life-admin" (2 events)
    _ev("claude_code", "event 1", metadata={"project": "life-admin"})
    _ev("claude_code", "event 2", metadata={"project": "life-admin"})

    # Encoded row matching "life-admin": "-Users-alice-Go-life-admin" (3 events)
    _ev("claude_code", "legacy 1", metadata={"project": "-Users-alice-Go-life-admin"})
    _ev("claude_code", "legacy 2", metadata={"project": "-Users-alice-Go-life-admin"})
    _ev("claude_code", "legacy 3", metadata={"project": "-Users-alice-Go-life-admin"})

    # Another encoded row matching "life-admin": "-tmp-proj-life-admin" (1 event)
    _ev("claude_code", "legacy 4", metadata={"project": "-tmp-proj-life-admin"})

    # Encoded row matching nothing: "-Users-alice-Go-unmatched-proj" (1 event)
    _ev("claude_code", "unmatched", metadata={"project": "-Users-alice-Go-unmatched-proj"})

    runner = CliRunner()
    result = runner.invoke(cli.main, ["rules", "projects", "--plain"])
    assert result.exit_code == 0, result.output

    lines = [line.strip() for line in result.output.strip().splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0] == "claude_code\t-Users-alice-Go-unmatched-proj\t1\tWork"
    assert lines[1] == "claude_code\tlife-admin\t6\tWork"

    # Also test table output
    table_result = runner.invoke(cli.main, ["rules", "projects"])
    assert table_result.exit_code == 0
    assert "life-admin" in table_result.output
    assert "6" in table_result.output
    assert "-Users-alice-Go-life-admin" not in table_result.output


def test_rules_projects_picks_longest_name_when_several_match(db, monkeypatch) -> None:
    from penpal import cli
    from penpal.cli import privacy as privacy_cli

    monkeypatch.setattr(privacy_cli, "get_config", lambda: Config())
    monkeypatch.setattr(privacy_cli, "require_db", lambda cfg: None)

    # Two decoded names where both match "-Users-alice-Go-life-admin":
    # "admin" (5 chars) and "life-admin" (10 chars)
    _ev("claude_code", "admin event", metadata={"project": "admin"})
    _ev("claude_code", "life-admin event", metadata={"project": "life-admin"})

    # Encoded row: "-Users-alice-Go-life-admin" (4 events)
    for i in range(4):
        _ev("claude_code", f"enc {i}", metadata={"project": "-Users-alice-Go-life-admin"})

    runner = CliRunner()
    result = runner.invoke(cli.main, ["rules", "projects", "--plain"])
    assert result.exit_code == 0, result.output

    lines = [line.strip() for line in result.output.strip().splitlines() if line.strip()]
    assert len(lines) == 2
    assert "claude_code\tadmin\t1\tWork" in lines
    assert "claude_code\tlife-admin\t5\tWork" in lines


# ── migration v21 ────────────────────────────────────────────────────────────────────────────
def test_v21_adds_grant_scope_backfills_buckets_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
    for i, step in enumerate(MIGRATIONS[:20], start=1):  # exactly v20
        if callable(step):
            step(conn)
        else:
            conn.executescript(step.strip())
        conn.execute("INSERT INTO _schema_version VALUES (?, 'x')", (i,))
    conn.execute("INSERT INTO access_grants(client_id, token_hash, created_at) VALUES ('old','h','2026-01-01')")
    for src, b in (("chrome_history", None), ("claude_code", "Work"), ("mystery", None), ("wechat", "Private")):
        conn.execute("INSERT INTO raw_events(source,event_type,ts_start,content_hash,created_at,bucket) "
                     "VALUES (?, 'semantic_event','2026-01-01','h'||?,'2026-01-01',?)", (src, src, b))
    conn.commit()

    run_migrations(conn)
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == SCHEMA_VERSION
    g = conn.execute("SELECT buckets, sources, operations, purpose, expires_at FROM access_grants").fetchone()
    assert (g["buckets"], g["sources"], g["operations"]) == ('["Work"]', None, '["read"]')  # legacy grant -> least privilege
    got = {r["source"]: r["bucket"] for r in conn.execute("SELECT source, bucket FROM raw_events")}
    assert got == {"chrome_history": "Personal", "claude_code": "Work", "mystery": None, "wechat": "Private"}

    from penpal.store.migration_v21 import migrate_v21

    migrate_v21(conn)  # rerun is harmless
    assert conn.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 4
    conn.close()


# ── grant scope ──────────────────────────────────────────────────────────────────────────────
def test_new_grant_defaults_to_work_only_read(db) -> None:
    grant = auth.authenticate(auth.create_grant("host"))
    assert grant.buckets == ("Work",) and grant.sources is None and grant.operations == ("read",)


def test_work_grant_notice_shows_once_only_when_enabled_coding_source_is_in_scope() -> None:
    notice = T(
        "Work 包含 Claude Code、Codex 等 AI 工具里的全部对话；私人项目可用 `penpal rules add --scope project …` 归到 Personal。",
        "Work includes all conversations in Claude Code, Codex and other AI tools; move a private project to Personal with `penpal rules add --scope project …`.",
    )
    shown = grant_output_lines(
        "claude", "buckets=Work", "kp_fake", "claude", "penpal", buckets=["Work"],
        enabled_sources=["claude_code", "codex_cli"],
    )
    hidden_personal = grant_output_lines(
        "claude", "buckets=Personal", "kp_fake", "claude", "penpal", buckets=["Personal"],
        enabled_sources=["claude_code", "codex_cli"],
    )
    hidden_source_scope = grant_output_lines(
        "claude", "buckets=Work, sources=git_log", "kp_fake", "claude", "penpal", buckets=["Work"],
        sources=["git_log"], enabled_sources=["claude_code", "codex_cli"],
    )
    hidden_disabled_sources = grant_output_lines(
        "claude", "buckets=Work", "kp_fake", "claude", "penpal", buckets=["Work"], enabled_sources=[],
    )
    assert sum(line == notice for line in shown) == 1
    assert all(not any(line == notice for line in output) for output in
               (hidden_personal, hidden_source_scope, hidden_disabled_sources))


def test_grant_validation(db) -> None:
    for kwargs in (dict(buckets=["Temporary"]), dict(buckets=["nope"]), dict(sources=[]),
                   dict(operations=["delete"]), dict(expires_at=datetime.now(timezone.utc) - timedelta(days=1))):
        with pytest.raises(ValueError):
            auth.create_grant("host", **kwargs)
    t = auth.create_grant("host", buckets=["work", "PERSONAL", "work"], sources=["git_log", " "], purpose="demo")
    g = auth.authenticate(t)
    assert g.buckets == ("Work", "Personal") and g.sources == ("git_log",) and g.purpose == "demo"


def test_expired_grant_is_rejected_like_an_unknown_token(db) -> None:
    token = auth.create_grant("host", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    _ev("claude_code", "pelican plan")
    assert search(token, ["pelican"])["status"] == "ok"
    get_conn().execute("UPDATE access_grants SET expires_at = ?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    get_conn().commit()
    assert auth.authenticate(token) is None
    assert search(token, ["pelican"]) == search("kp_unknown", ["pelican"])


def test_malformed_scope_fails_closed(db) -> None:
    token = auth.create_grant("host")
    get_conn().execute("UPDATE access_grants SET buckets = 'not json'")
    get_conn().commit()
    assert auth.authenticate(token) is None


def test_grant_without_read_operation_is_denied(db) -> None:
    token = auth.create_grant("host", operations=["write"])
    _ev("claude_code", "pelican plan")
    assert search(token, ["pelican"])["status"] == "permission_denied"


def test_scope_is_applied_before_limit_not_after(db) -> None:
    _ev("claude_code", "pelican old work note", ts="2026-01-01T00:00:00+00:00")
    for i in range(5):  # newer out-of-scope hits would fill LIMIT 1 if scope were a post-filter
        _ev("chrome_history", f"pelican newer personal {i}", ts=f"2026-02-0{i + 1}T00:00:00+00:00")
    token = auth.create_grant("host", buckets=["Work"])
    resp = search(token, ["pelican"], limit=1)
    assert resp["status"] == "ok" and [r["bucket"] for r in resp["results"]] == ["Work"]
    short = search(token, ["pe"], limit=1)  # LIKE path (<3 chars) is scoped too
    assert [r["bucket"] for r in short["results"]] == ["Work"]


def test_source_scope_intersects_requested_sources(db) -> None:
    _ev("claude_code", "pelican a")
    _ev("git_log", "pelican b")
    token = auth.create_grant("host", sources=["claude_code"])
    assert {r["source"] for r in search(token, ["pelican"])["results"]} == {"claude_code"}
    assert search(token, ["pelican"], sources=["git_log"])["status"] == "no_results"
    assert search(token, ["pelican"], sources=["git_log", "claude_code"])["status"] == "ok"


def test_multi_bucket_grant_and_cli(db, monkeypatch) -> None:
    _ev("claude_code", "pelican w")
    _ev("chrome_history", "pelican p")
    _ev("wechat", "pelican chat", bucket="Private")
    monkeypatch.setattr(Config, "db_path_expanded", property(lambda self: db / "kp.db"))
    runner = CliRunner()
    out = runner.invoke(mcp_group, ["grant", "claude", "--buckets", "Work,Personal", "--purpose", "demo", "--expires-in-days", "7"])
    assert out.exit_code == 0, out.output
    assert out.output.count(T(
        "Work 包含 Claude Code、Codex 等 AI 工具里的全部对话；私人项目可用 `penpal rules add --scope project …` 归到 Personal。",
        "Work includes all conversations in Claude Code, Codex and other AI tools; move a private project to Personal with `penpal rules add --scope project …`.",
    )) == 1
    token = next(line.strip() for line in out.output.splitlines() if line.strip().startswith("kp_"))
    assert {r["bucket"] for r in search(token, ["pelican"])["results"]} == {"Work", "Personal"}
    default = runner.invoke(mcp_group, ["grant", "plain"])
    token2 = next(line.strip() for line in default.output.splitlines() if line.strip().startswith("kp_"))
    assert {r["bucket"] for r in search(token2, ["pelican"])["results"]} == {"Work"}
    assert runner.invoke(mcp_group, ["grant", "bad", "--buckets", "Temporary"]).exit_code != 0
    listing = runner.invoke(mcp_group, ["list"]).output
    assert "Work" in listing and "expires" in listing and "kp_" not in listing
    assert T(
        "Work 包含 Claude Code、Codex 等 AI 工具里的全部对话；私人项目可用 `penpal rules add --scope project …` 归到 Personal。",
        "Work includes all conversations in Claude Code, Codex and other AI tools; move a private project to Personal with `penpal rules add --scope project …`.",
    ) in listing


def test_scoped_ranking_uses_relevance_not_just_recency_and_ignores_out_of_scope_data(db) -> None:
    _ev("claude_code", "pelican pelican pelican retry policy for pelican", ts="2026-01-01T00:00:00+00:00")
    _ev("claude_code", "long unrelated notes " * 20 + "pelican", ts="2026-02-01T00:00:00+00:00")
    _ev("claude_code", "pelican", ts="2026-01-15T00:00:00+00:00")
    token = auth.create_grant("host", buckets=["Work"])
    before = [r["snippet"] for r in search(token, ["pelican"])["results"]]
    assert before[0].startswith("pelican pelican pelican")  # most relevant first although oldest
    assert "unrelated" in before[-1]          # diluted by length, despite being newest
    for i in range(50):  # unrelated out-of-scope corpus must not change in-scope order
        _ev("chrome_history", f"pelican pelican pelican pelican {i}")
    assert [r["snippet"] for r in search(token, ["pelican"])["results"]] == before


def test_project_rule_by_folder_name_reaches_legacy_claude_rows_with_only_the_encoded_dir(db) -> None:
    # The user types the folder name they know; old rows only carry Claude's lossy encoded dir.
    rid = _ev("claude_code", "stored mortgage chat", metadata={"project_dir": "/tmp/claude/projects/-tmp-proj-life-admin"})
    other = _ev("claude_code", "stored work chat", ts="2026-03-01T12:00:00+00:00",
                metadata={"project_dir": "/tmp/claude/projects/-tmp-proj-life-admin-tools"})
    insert_policy(Policy(scope_type="project", scope_value="life-admin", mode="bucket",
                         config_json=json.dumps({"bucket": "Personal"})))
    buckets.reapply_buckets(get_conn())
    assert _bucket_of(rid) == "Personal"
    assert _bucket_of(other) == "Work"
