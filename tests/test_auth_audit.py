"""P0-05: client whitelist, rotation, audit log, uniform denial (AT15) and credential scan (G5)."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from mcp.shared.memory import create_connected_server_and_client_session

from penpal.config import Config
from penpal.mcp import audit, auth, tools
from penpal.mcp.cli import mcp_group
from penpal.mcp.server import build_server
from penpal.store import buckets
from penpal.store.db import close, get_conn, init_db
from penpal.store.migrations import run_migrations
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.utils.logging import setup_logging

WORD = "pelican"
SECRET_QUERY = "needle-query-text-9137"  # must never appear in the audit table


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PENPAL_TOKEN", raising=False)
    monkeypatch.delenv("PENPAL_CLIENT", raising=False)
    buckets.reset_cache()
    init_db(Config.load().db_path_expanded)
    insert_raw_event(RawEvent(source="claude_code", event_type="semantic_event", ts_start="2026-03-01T09:00:00+00:00",
                              content_text=f"{WORD} rollout checklist", content_hash="h1", bucket="Work"))
    yield h
    close()
    buckets.reset_cache()


def call(token, client, keywords=(WORD,)):
    return tools.search_context(token, [keywords] if isinstance(keywords, str) else list(keywords), client=client)


def audit_rows():
    return [dict(r) for r in get_conn().execute("SELECT * FROM mcp_audit ORDER BY id")]


def last_audit():
    return audit_rows()[-1]


# ── AT15 ─────────────────────────────────────────────────────────────────────────────────────
def test_valid_token_and_whitelisted_client_allowed_and_audited(home) -> None:
    token = auth.create_grant("claude", buckets=["Work", "Personal"])
    resp = call(token, "claude")
    assert resp["status"] == "ok" and resp["results"]
    row = last_audit()
    assert (row["client_id"], row["tool"], row["decision"], row["reason_code"]) == ("claude", "search_context", "allow", "ok")
    assert row["scope"] == "Work,Personal" and row["result_count"] == len(resp["results"])
    call(token, "claude", "zzzz-nothing")
    assert (last_audit()["reason_code"], last_audit()["result_count"]) == ("no_results", 0)


def _scenario_missing_token(t):
    return None, "claude", "token_missing"


def _scenario_wrong_token(t):
    return "kp_not-a-real-token", "claude", "token_unknown"


def _scenario_non_whitelisted(t):
    get_conn().execute("DELETE FROM client_whitelist WHERE client_id = 'claude'")
    get_conn().commit()
    return t, "claude", "client_not_whitelisted"


def _scenario_client_mismatch(t):
    auth.create_grant("codex")  # codex is whitelisted, but presents claude's token
    return t, "codex", "client_mismatch"


def _scenario_client_missing(t):
    return t, None, "client_missing"


def _scenario_client_garbage(t):
    return t, "not a valid id!", "client_invalid"


def _scenario_revoked(t):
    auth.revoke_grants("claude")
    return t, "claude", "token_revoked"


def _scenario_rotated_old(t):
    auth.rotate_grant("claude")
    return t, "claude", "token_unknown"


def _scenario_expired(t):
    get_conn().execute("UPDATE access_grants SET expires_at = ? WHERE client_id = 'claude'",
                       ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
    get_conn().commit()
    return t, "claude", "token_expired"


def _scenario_no_read_right(t):
    w = auth.create_grant("writer", operations=["write"])
    return w, "writer", "operation_denied"


SCENARIOS = [_scenario_missing_token, _scenario_wrong_token, _scenario_non_whitelisted, _scenario_client_mismatch,
             _scenario_client_missing, _scenario_client_garbage, _scenario_revoked, _scenario_rotated_old,
             _scenario_expired, _scenario_no_read_right]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("_scenario_"))
def test_at15_every_denial_has_identical_body_and_an_audit_row(home, scenario) -> None:
    good = auth.create_grant("claude")
    baseline = call("kp_nope", None)
    token, client, reason = scenario(good)
    before = len(audit_rows())
    resp = call(token, client, SECRET_QUERY)
    assert resp == baseline == tools.permission_denied()
    assert "results" not in resp and "coverage" not in resp
    rows = audit_rows()
    assert len(rows) == before + 1
    row = rows[-1]
    assert (row["decision"], row["reason_code"], row["tool"]) == ("deny", reason, "search_context")
    assert row["result_count"] is None
    assert row["client_id"] == (client if client in ("claude", "codex", "writer") else "unknown")


def test_denied_before_auth_is_audited_as_unknown_client(home) -> None:
    call(None, None)
    row = last_audit()
    assert (row["client_id"], row["decision"], row["reason_code"], row["scope"]) == ("unknown", "deny", "token_missing", None)


def test_denial_bodies_identical_through_the_mcp_transport(home) -> None:
    good = auth.create_grant("claude")
    auth.create_grant("codex")
    attempts = [(None, "claude"), ("kp_x", "claude"), (good, "codex"), (good, None), (good, "evil")]

    async def run():
        out = []
        for token, client in attempts:
            server = build_server(token_provider=lambda t=token: t, client_provider=lambda c=client: c)
            async with create_connected_server_and_client_session(server._mcp_server) as c:
                out.append((await c.call_tool("search_context", {"keywords": [WORD]})).model_dump_json())
        return out

    bodies = asyncio.run(run())
    assert len(set(bodies)) == 1 and "permission_denied" in bodies[0] and WORD not in bodies[0]
    assert len(audit_rows()) == len(attempts)


def test_revoke_is_immediate_and_removes_whitelist(home) -> None:
    token = auth.create_grant("claude")
    assert call(token, "claude")["status"] == "ok"
    assert auth.is_whitelisted("claude")
    auth.revoke_grants("claude")
    assert not auth.is_whitelisted("claude")
    assert call(token, "claude")["status"] == "permission_denied"  # very next call, no caching
    assert last_audit()["reason_code"] == "token_revoked"


def test_other_client_unaffected_by_revoke(home) -> None:
    a, b = auth.create_grant("claude"), auth.create_grant("codex")
    auth.revoke_grants("claude")
    assert call(b, "codex")["status"] == "ok" and call(a, "claude")["status"] == "permission_denied"


# ── rotation ─────────────────────────────────────────────────────────────────────────────────
def test_rotate_keeps_grant_and_scope_and_kills_old_token(home) -> None:
    old = auth.create_grant("claude", buckets=["Work", "Personal"], sources=["claude_code"], purpose="demo")
    grant_before = auth.authenticate(old)
    new = auth.rotate_grant("claude")
    assert new != old and new.startswith("kp_")
    assert call(old, "claude")["status"] == "permission_denied"
    assert call(new, "claude")["status"] == "ok"
    g = auth.authenticate(new)
    assert (g.id, g.buckets, g.sources, g.purpose) == (grant_before.id, grant_before.buckets, grant_before.sources, "demo")
    rows = get_conn().execute("SELECT count(*), max(rotated_at IS NOT NULL) FROM access_grants").fetchone()
    assert tuple(rows) == (1, 1)  # still ONE grant row = one active token per grant
    assert new not in json.dumps(audit_rows())


def test_rotate_rejects_unknown_revoked_expired_and_ambiguous(home) -> None:
    with pytest.raises(ValueError):
        auth.rotate_grant("nobody")
    auth.create_grant("claude")
    auth.revoke_grants("claude")
    with pytest.raises(ValueError):
        auth.rotate_grant("claude")
    auth.create_grant("dup")
    auth.create_grant("dup")
    with pytest.raises(ValueError):
        auth.rotate_grant("dup")


def test_token_shaped_client_id_is_refused_and_never_audited(home) -> None:
    with pytest.raises(ValueError):
        auth.create_grant("kp_looks-like-a-token")
    token = auth.create_grant("claude")
    call(token, token)  # Host config mistake: token pasted into PENPAL_CLIENT
    assert last_audit()["client_id"] == "unknown"
    assert token not in json.dumps(audit_rows())


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────
def test_cli_grant_rotate_revoke_audit(home) -> None:
    r = CliRunner()
    out = r.invoke(mcp_group, ["grant", "claude", "--buckets", "Work,Personal"])
    assert out.exit_code == 0, out.output
    token1 = next(w for w in out.output.split() if w.startswith("kp_"))
    assert f"-e PENPAL_TOKEN={token1} -e PENPAL_CLIENT=claude --" in out.output
    assert '"PENPAL_CLIENT": "claude"' in out.output
    assert auth.is_whitelisted("claude")

    rot = r.invoke(mcp_group, ["rotate", "claude"])
    assert rot.exit_code == 0, rot.output
    token2 = next(w for w in rot.output.split() if w.startswith("kp_"))
    assert token2 != token1 and token1 not in rot.output
    assert "-e PENPAL_CLIENT=claude" in rot.output and '"PENPAL_CLIENT": "claude"' in rot.output
    assert call(token1, "claude")["status"] == "permission_denied" and call(token2, "claude")["status"] == "ok"

    shown = r.invoke(mcp_group, ["audit", "--limit", "5"])
    assert shown.exit_code == 0
    assert "deny" in shown.output and "token_unknown" in shown.output and "allow" in shown.output
    assert "scope=Work,Personal" in shown.output
    assert len(r.invoke(mcp_group, ["audit", "--limit", "1"]).output.strip().splitlines()) == 1

    assert r.invoke(mcp_group, ["rotate", "ghost"]).exit_code != 0
    assert r.invoke(mcp_group, ["revoke", "claude"]).exit_code == 0
    assert call(token2, "claude")["status"] == "permission_denied"


# ── migration ────────────────────────────────────────────────────────────────────────────────
def test_v22_migration_backfills_whitelist_from_active_grants(tmp_path) -> None:
    from penpal.store import migrations

    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute("DROP TABLE client_whitelist")
    conn.execute("DROP TABLE mcp_audit")
    conn.execute("DELETE FROM _schema_version")
    conn.execute("INSERT INTO _schema_version(version, applied_at) VALUES (21, 'x')")
    conn.execute("INSERT INTO access_grants(client_id, token_hash, created_at) VALUES ('live','h1','2026-01-01')")
    conn.execute("INSERT INTO access_grants(client_id, token_hash, created_at, revoked_at) VALUES ('dead','h2','2026-01-01','2026-01-02')")
    conn.commit()
    run_migrations(conn)
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == migrations.SCHEMA_VERSION
    assert [r[0] for r in conn.execute("SELECT client_id FROM client_whitelist")] == ["live"]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mcp_audit)")}
    assert cols == {"id", "ts", "client_id", "tool", "decision", "reason_code", "scope", "result_count"}
    conn.close()


# ── G5: credential exposure scan ─────────────────────────────────────────────────────────────
def test_g5_no_plaintext_token_or_hash_in_db_wal_logs_audit_or_cli_output(home, tmp_path) -> None:
    log_file = tmp_path / "penpal.log"
    kp_logger = setup_logging(log_file, "DEBUG")
    handler = kp_logger.handlers[-1]
    mcp_logger = logging.getLogger("mcp")
    old_level = mcp_logger.level
    mcp_logger.addHandler(handler)
    mcp_logger.setLevel(logging.DEBUG)
    try:
        runner = CliRunner()
        printed = []  # outputs allowed to hold a token (one-time grant / rotate print)
        tokens = []
        for client in ("claude", "codex"):
            out = runner.invoke(mcp_group, ["grant", client])
            assert out.exit_code == 0, out.output
            tokens.append(next(w for w in out.output.split() if w.startswith("kp_")))
            printed.append(out.output)
        rot = runner.invoke(mcp_group, ["rotate", "claude"])
        tokens.append(next(w for w in rot.output.split() if w.startswith("kp_")))
        printed.append(rot.output)
        old_claude, codex, new_claude = tokens

        # allowed + every kind of denied call, direct and through the MCP transport
        call(new_claude, "claude")
        call(codex, "codex")
        for bad in ((old_claude, "claude"), (new_claude, "codex"), (None, None), ("kp_wrong", "claude"), (new_claude, new_claude)):
            call(*bad)

        async def via_transport():
            server = build_server(token_provider=lambda: old_claude, client_provider=lambda: "claude")
            async with create_connected_server_and_client_session(server._mcp_server) as c:
                await c.call_tool("search_context", {"keywords": [WORD]})

        asyncio.run(via_transport())
        auth.revoke_grants("codex")
        call(codex, "codex")
        audit.record(client="claude", tool="search_context", allowed=True, reason_code="ok")

        # other CLI surfaces + diagnostics (runs under the isolated HOME, so it never touches real state)
        from penpal import cli as kp_cli

        outputs = {
            "mcp list": runner.invoke(mcp_group, ["list"]).output,
            "mcp audit": runner.invoke(mcp_group, ["audit", "--limit", "100"]).output,
            "mcp revoke": runner.invoke(mcp_group, ["revoke", "claude"]).output,
            "doctor": runner.invoke(kp_cli.main, ["doctor", "--plain"]).output,
            "healthcheck": runner.invoke(kp_cli.main, ["healthcheck"]).output,
        }
        get_conn().commit()
        for h in kp_logger.handlers + mcp_logger.handlers:
            h.flush()

        assert len(tokens) == len(set(tokens)) == 3
        assert all(len(t) > 20 for t in tokens)
        hashes = [auth.hash_token(t) for t in tokens]
        needles = [t.encode() for t in tokens]

        # 1) every file under HOME and tmp (db, -wal, -shm, log, health.json, diagnostics): 0 hits
        scanned = []
        for path in tmp_path.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                scanned.append(path.name)
                for n in needles:
                    assert n not in data, f"G5 FAIL: plaintext token in {path}"
        assert any(n.endswith(".db") for n in scanned) and "penpal.log" in scanned

        # 2) audit rows + non-print CLI outputs: no plaintext token, no hash
        haystacks = {"audit rows": json.dumps(audit_rows()), "log": log_file.read_text(encoding="utf-8"), **outputs}
        for name, text in haystacks.items():
            for t, h in zip(tokens, hashes):
                assert t not in text, f"G5 FAIL: token in {name}"
                assert h not in text, f"G5 FAIL: token hash in {name}"
        # query text never reaches the audit table
        call(new_claude, "claude", SECRET_QUERY)
        assert SECRET_QUERY not in json.dumps(audit_rows())

        # 3) sanity: the DB holds only hashes (so the scan above is meaningful), the print holds the tokens
        stored = {r[0] for r in get_conn().execute("SELECT token_hash FROM access_grants")}
        assert auth.hash_token(new_claude) in stored and auth.hash_token(old_claude) not in stored
        assert all(t in "".join(printed) for t in tokens)
    finally:
        mcp_logger.removeHandler(handler)
        mcp_logger.setLevel(old_level)
        kp_logger.removeHandler(handler)
        handler.close()


def test_keypulse_token_in_ingested_text_is_redacted() -> None:
    # A host transcript can contain the `claude mcp add -e PENPAL_TOKEN=...` command (B6).
    import secrets

    from penpal.foundation.desensitizer import redact_secrets

    token = "kp_" + secrets.token_urlsafe(32)
    out = redact_secrets(f"claude mcp add penpal -e PENPAL_TOKEN={token} -- penpal mcp serve")
    assert token not in out and "[REDACTED]" in out
