from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from click.testing import CliRunner
from mcp.shared.memory import create_connected_server_and_client_session

from penpal.cli.sources import sources_group
from penpal.mcp import auth
from penpal.mcp.server import build_server
from penpal.sources import registry
from penpal.sources.cleaning.config import CleaningConfig
from penpal.sources.plugins.ai_tools import AiToolsSource
from penpal.store.db import close, get_conn, init_db


def test_mcp_recipe_end_to_end(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = home / "bin" / "orbit"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    history = home / ".orbit" / "history.jsonl"
    history.parent.mkdir()
    history.write_text('{"role":"user","body":"Please investigate the synthetic orbit project"}\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary.parent))
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: home / "penpal-data")
    monkeypatch.setattr("penpal.cli.sources.get_data_dir", lambda: home / "penpal-data")
    monkeypatch.setattr(registry, "load_cleaning_config", lambda: CleaningConfig())
    init_db(tmp_path / "test.db")
    source = AiToolsSource(catalog_dir=tmp_path / "empty", use_cursor=True)
    monkeypatch.setattr(registry, "_PLUGINS", {"ai_tools": source})
    token = auth.create_grant("host", operations=["write"])
    recipe = '''id = "orbit"
name = "Orbit"
kind = "agent"
[detect]
any = [{cli = "orbit"}]
[[history]]
format = "jsonl"
glob = "~/.orbit/*.jsonl"
timestamp = "file_mtime"
compression = "none"
[history.fields]
role = "role"
content = "body"
'''

    async def submit():
        server = build_server(token_provider=lambda: token, client_provider=lambda: "host")
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            return (await client.call_tool("propose_source_recipe", {"recipe_toml": recipe})).structuredContent

    result = asyncio.run(submit())
    assert result["status"] == "ok"
    assert (result["matched_files"], result["parsed_turns"]) == (1, 1)
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    end = datetime.max.replace(tzinfo=timezone.utc)
    assert len(list(registry.read_all(start, end, source="ai_tools"))) == 1
    assert list(registry.read_all(start, end, source="ai_tools")) == []
    assert get_conn().execute("SELECT count(*) FROM raw_events WHERE source='ai_tools'").fetchone()[0] == 1
    runner = CliRunner()
    # Use the already initialized database for the coverage command.
    monkeypatch.setattr("penpal.sources.coverage.init_db", lambda _: None)
    coverage = runner.invoke(sources_group, ["coverage"])
    assert coverage.exit_code == 0, coverage.output
    assert "orbit (AI 生成)" in coverage.output
    assert "enabled" in runner.invoke(sources_group, ["recipes"]).output
    assert runner.invoke(sources_group, ["disable", "orbit"]).exit_code == 0
    with history.open("a") as stream:
        stream.write('{"role":"user","body":"A second synthetic orbit question"}\n')
    assert list(registry.read_all(start, end, source="ai_tools")) == []
    assert "disabled" in runner.invoke(sources_group, ["recipes"]).output
    close()


def test_rejection_is_audited_without_sensitive_content(tmp_path, monkeypatch):
    import json

    from penpal.mcp.recipe_tools import propose_source_recipe

    home = tmp_path / "home"
    binary = home / "bin" / "orbit"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    sensitive = home / ".ssh"
    sensitive.mkdir()
    (sensitive / "history.jsonl").write_text('{"role":"user","body":"SYNTHETIC_CANARY"}\n')
    (home / ".orbit").symlink_to(sensitive, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary.parent))
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: home / "data")
    init_db(tmp_path / "test.db")
    token = auth.create_grant("host", operations=["write"])
    recipe = '''id = "orbit"
name = "Orbit"
kind = "agent"
[detect]
any = [{cli = "orbit"}]
[[history]]
format = "jsonl"
glob = "~/.orbit/*.jsonl"
timestamp = "file_mtime"
compression = "none"
[history.fields]
role = "role"
content = "body"
'''
    result = propose_source_recipe(token, recipe, client="host")
    assert result["status"] == "rejected" and "sensitive" in result["reason"]
    assert "SYNTHETIC_CANARY" not in json.dumps(result)
    assert not (home / "data" / "catalog" / "orbit.toml").exists()
    row = get_conn().execute("SELECT decision, reason_code FROM mcp_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("deny", "recipe_rejected")
    close()


def test_cli_propose_replace_rollback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = home / "bin" / "orbit"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    history = home / ".orbit" / "history.jsonl"
    history.parent.mkdir()
    history.write_text('{"role":"user","body":"synthetic message"}\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary.parent))
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: home / "data")
    recipe = '''id = "orbit"
name = "Orbit"
kind = "agent"
[detect]
any = [{cli = "orbit"}]
[[history]]
format = "jsonl"
glob = "~/.orbit/*.jsonl"
timestamp = "file_mtime"
compression = "none"
[history.fields]
role = "role"
content = "body"
'''
    file = tmp_path / "recipe.toml"
    file.write_text(recipe)
    runner = CliRunner()
    assert runner.invoke(sources_group, ["propose", str(file)]).exit_code == 0
    file.write_text(recipe.replace('name = "Orbit"', 'name = "Orbit Two"'))
    assert runner.invoke(sources_group, ["propose", str(file)]).exit_code == 0
    assert runner.invoke(sources_group, ["rollback", "orbit"]).exit_code == 0
    assert (home / "data" / "catalog" / "orbit.toml").read_text() == recipe
