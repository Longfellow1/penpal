"""Tests for penpal connect --auto, --list, disconnect, and install integration (issue #173)."""
from __future__ import annotations

import json
import sys

from click.testing import CliRunner

from penpal.cli import main
from penpal.mcp.auth import active_grants
from penpal.mcp.connect import (
    auto_connect,
    disconnect_all,
    disconnect_tool,
    list_tools,
)
from penpal.store.db import init_db
from penpal.config import Config


def _setup_db():
    cfg = Config.load()
    init_db(cfg.db_path_expanded)


def test_json_mcp_servers_lifecycle(tmp_path, monkeypatch):
    """Test json_mcpServers style: new file, idempotent rerun, and disconnect."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # Fake detected tool: claude_code
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"

    # Initially file does not exist
    assert not claude_json.exists()

    # 1. First run: writes config and skill
    results = auto_connect(env={"HOME": str(home)})
    claude_res = next(r for r in results if r.tool_id == "claude_code")
    assert claude_res.status == "connected"
    assert claude_json.exists()

    first_bytes = claude_json.read_bytes()
    data = json.loads(first_bytes)
    assert "penpal" in data["mcpServers"]
    assert data["mcpServers"]["penpal"]["args"] == ["mcp", "serve"]
    token1 = data["mcpServers"]["penpal"]["env"]["PENPAL_TOKEN"]
    assert token1.startswith("kp_")

    skill_file = home / ".claude" / "skills" / "penpal" / "SKILL.md"
    assert skill_file.is_file()
    assert "search_context" in skill_file.read_text(encoding="utf-8")
    assert "commit_memory" in skill_file.read_text(encoding="utf-8")

    # 2. Idempotent rerun: file bytes must NOT change
    rerun_results = auto_connect(env={"HOME": str(home)})
    rerun_res = next(r for r in rerun_results if r.tool_id == "claude_code")
    assert rerun_res.status == "already_connected"
    assert claude_json.read_bytes() == first_bytes

    # 3. Disconnect: since file did not exist originally and was not touched by user, it is removed
    d_res = disconnect_tool("claude_code", env={"HOME": str(home)})
    assert d_res.status == "restored_original"
    assert not claude_json.exists()
    assert not skill_file.exists()


def test_json_mcp_servers_preserve_existing_file(tmp_path, monkeypatch):
    """Existing JSON file with other keys must be preserved byte-for-byte on disconnect."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".cursor").mkdir(parents=True, exist_ok=True)
    cursor_json = home / ".cursor" / "mcp.json"

    original_content = (
        '{\n  "customOption": true,\n  "mcpServers": {\n    "otherServer": {\n      "command": "node"\n    }\n  }\n}\n'
    )
    cursor_json.write_text(original_content, encoding="utf-8")
    cursor_json.chmod(0o600)
    original_bytes = cursor_json.read_bytes()

    # Connect
    results = auto_connect(env={"HOME": str(home)})
    cursor_res = next(r for r in results if r.tool_id == "cursor")
    assert cursor_res.status == "connected"

    # Verify preserved mode 0o600
    assert (cursor_json.stat().st_mode & 0o777) == 0o600

    data = json.loads(cursor_json.read_text(encoding="utf-8"))
    assert data["customOption"] is True
    assert "otherServer" in data["mcpServers"]
    assert "penpal" in data["mcpServers"]
    assert data["mcpServers"]["penpal"]["type"] == "stdio"

    # Idempotent rerun
    auto_connect(env={"HOME": str(home)})

    # Disconnect
    d_res = disconnect_tool("cursor", env={"HOME": str(home)})
    assert d_res.status == "restored_original"
    assert cursor_json.read_bytes() == original_bytes


def test_toml_mcp_servers_lifecycle(tmp_path, monkeypatch):
    """Test toml_mcp_servers: preserves other sections/comments, byte-for-byte restored."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    codex_toml = codex_dir / "config.toml"

    original_content = """# Codex User Configuration
model = "o3-mini"

[mcp_servers.other]
command = "other_agent"
"""
    codex_toml.write_text(original_content, encoding="utf-8")
    orig_bytes = codex_toml.read_bytes()

    # 1. Connect
    results = auto_connect(env={"HOME": str(home)})
    codex_res = next(r for r in results if r.tool_id == "codex_cli")
    assert codex_res.status == "connected"

    after_text = codex_toml.read_text(encoding="utf-8")
    assert "# Codex User Configuration" in after_text
    assert 'model = "o3-mini"' in after_text
    assert "[mcp_servers.penpal]" in after_text
    assert "[mcp_servers.other]" in after_text
    first_bytes = codex_toml.read_bytes()

    # Skill check
    skill_file = home / ".agents" / "skills" / "penpal" / "SKILL.md"
    assert skill_file.is_file()

    # 2. Idempotent rerun
    rerun_results = auto_connect(env={"HOME": str(home)})
    rerun_res = next(r for r in rerun_results if r.tool_id == "codex_cli")
    assert rerun_res.status == "already_connected"
    assert codex_toml.read_bytes() == first_bytes

    # 3. Disconnect
    d_res = disconnect_tool("codex_cli", env={"HOME": str(home)})
    assert d_res.status == "restored_original"
    assert codex_toml.read_bytes() == orig_bytes
    assert not skill_file.exists()


def test_yaml_mcp_servers_lifecycle(tmp_path, monkeypatch):
    """Test yaml_mcp_servers: preserves formatting and comments, byte-for-byte restored."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True, exist_ok=True)
    hermes_yaml = hermes_dir / "config.yaml"

    original_content = """# Hermes Configuration
model: gpt-4
mcp_servers:
  other:
    command: other_service
"""
    hermes_yaml.write_text(original_content, encoding="utf-8")
    orig_bytes = hermes_yaml.read_bytes()

    # Connect
    results = auto_connect(env={"HOME": str(home)})
    hermes_res = next(r for r in results if r.tool_id == "hermes")
    assert hermes_res.status == "connected"

    after_text = hermes_yaml.read_text(encoding="utf-8")
    assert "# Hermes Configuration" in after_text
    assert "penpal:" in after_text
    assert "other:" in after_text
    first_bytes = hermes_yaml.read_bytes()

    # Idempotent
    auto_connect(env={"HOME": str(home)})
    assert hermes_yaml.read_bytes() == first_bytes

    # Disconnect
    d_res = disconnect_tool("hermes", env={"HOME": str(home)})
    assert d_res.status == "restored_original"
    assert hermes_yaml.read_bytes() == orig_bytes


def test_corrupted_config_files_not_touched(tmp_path, monkeypatch):
    """Corrupted config files must not be touched and parse error must be reported."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # 1. Corrupted JSON for claude_code
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"
    broken_json = '{"mcpServers": { broken json ...'
    claude_json.write_text(broken_json, encoding="utf-8")

    # 2. Corrupted TOML for codex_cli
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    codex_toml = home / ".codex" / "config.toml"
    broken_toml = "[mcp_servers\ninvalid syntax ===\n"
    codex_toml.write_text(broken_toml, encoding="utf-8")

    # 3. Corrupted YAML for hermes
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    hermes_yaml = home / ".hermes" / "config.yaml"
    broken_yaml = "mcp_servers:\n  - item1\n   bad_indent: ["
    hermes_yaml.write_text(broken_yaml, encoding="utf-8")

    results = auto_connect(env={"HOME": str(home)})
    res_map = {r.tool_id: r for r in results}

    assert res_map["claude_code"].status == "parse_error"
    assert res_map["codex_cli"].status == "parse_error"
    assert res_map["hermes"].status == "parse_error"

    # Assert files were untouched
    assert claude_json.read_text(encoding="utf-8") == broken_json
    assert codex_toml.read_text(encoding="utf-8") == broken_toml
    assert hermes_yaml.read_text(encoding="utf-8") == broken_yaml


def test_user_modified_after_connect_only_removes_penpal(tmp_path, monkeypatch):
    """When user edits config after connect, disconnect must only delete Penpal entry."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # Claude Code (JSON)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"
    claude_json.write_text('{"mcpServers": {}}', encoding="utf-8")

    auto_connect(env={"HOME": str(home)})
    assert "penpal" in json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"]

    # User modifies file afterwards
    user_modified = json.loads(claude_json.read_text(encoding="utf-8"))
    user_modified["userAddedKey"] = "my_value"
    claude_json.write_text(json.dumps(user_modified, indent=2) + "\n", encoding="utf-8")

    d_res = disconnect_tool("claude_code", env={"HOME": str(home)})
    assert d_res.status == "entry_removed_modified"
    final_data = json.loads(claude_json.read_text(encoding="utf-8"))
    assert "penpal" not in final_data.get("mcpServers", {})
    assert final_data.get("userAddedKey") == "my_value"

    # Codex CLI (TOML)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    codex_toml = home / ".codex" / "config.toml"
    codex_toml.write_text('model = "default"\n', encoding="utf-8")

    auto_connect(env={"HOME": str(home)})
    # User edits afterwards
    toml_text = codex_toml.read_text(encoding="utf-8") + '\n[user_section]\nfoo = "bar"\n'
    codex_toml.write_text(toml_text, encoding="utf-8")

    d_toml = disconnect_tool("codex_cli", env={"HOME": str(home)})
    assert d_toml.status == "entry_removed_modified"
    after_toml = codex_toml.read_text(encoding="utf-8")
    assert "[mcp_servers.penpal]" not in after_toml
    assert "[user_section]" in after_toml


def test_token_per_tool_and_revocation(tmp_path, monkeypatch):
    """Each tool gets an independent token, revoked on disconnect."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    auto_connect(env={"HOME": str(home)})

    claude_json = home / ".claude.json"
    claude_token = json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"]["penpal"]["env"]["PENPAL_TOKEN"]
    assert claude_token.startswith("kp_")

    codex_toml = home / ".codex" / "config.toml"
    assert "PENPAL_TOKEN" in codex_toml.read_text(encoding="utf-8")

    grants = {g["client_id"]: g for g in active_grants()}
    assert "claude_code" in grants
    assert "codex_cli" in grants

    # Disconnect claude_code
    disconnect_tool("claude_code", env={"HOME": str(home)})

    grants_after = {g["client_id"]: g for g in active_grants()}
    assert "claude_code" not in grants_after
    assert "codex_cli" in grants_after


def test_dry_run_writes_no_files(tmp_path, monkeypatch):
    """Dry-run must not write any files or issue grants."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    results = auto_connect(dry_run=True, env={"HOME": str(home)})
    dry_map = {r.tool_id: r for r in results}
    assert dry_map["claude_code"].status == "dry_run"
    assert dry_map["codex_cli"].status == "dry_run"

    assert not (home / ".claude.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
    assert not (home / ".claude" / "skills").exists()
    assert len(active_grants()) == 0


def test_manual_style_not_written(tmp_path, monkeypatch):
    """Tools with manual style (e.g. opencode) must not be written and give instructions."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # Detect opencode
    (home / ".local" / "share" / "opencode").mkdir(parents=True, exist_ok=True)

    results = auto_connect(env={"HOME": str(home)})
    opencode_res = next((r for r in results if r.tool_id == "opencode"), None)
    assert opencode_res is not None
    assert opencode_res.status == "manual"
    assert "手动" in opencode_res.message


def test_connect_list(tmp_path, monkeypatch):
    """Test connect --list outputs all status states accurately."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # 1. claude_code connected
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    auto_connect(env={"HOME": str(home)})

    # 2. codex_cli installed but parse_error
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "config.toml").write_text("invalid_toml [[", encoding="utf-8")

    statuses = {s.tool_id: s for s in list_tools(env={"HOME": str(home)})}
    assert statuses["claude_code"].state == "已接入"
    assert statuses["codex_cli"].state == "解析失败"
    assert statuses["chatgpt"].state in ("未安装", "不支持 MCP")


def test_cli_connect_and_disconnect(tmp_path, monkeypatch):
    """Test CLI commands penpal connect --auto, --list, disconnect."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)

    runner = CliRunner()

    # penpal connect (without args)
    res_help = runner.invoke(main, ["connect"])
    assert res_help.exit_code == 0
    assert "--auto" in res_help.output

    # penpal connect --dry-run --auto
    res_dry = runner.invoke(main, ["connect", "--auto", "--dry-run"])
    assert res_dry.exit_code == 0
    assert "[演练]" in res_dry.output
    assert not (home / ".claude.json").exists()

    # penpal connect --auto
    res_auto = runner.invoke(main, ["connect", "--auto"])
    assert res_auto.exit_code == 0
    assert "已接入 Claude Code" in res_auto.output
    assert (home / ".claude.json").exists()

    # penpal connect --list
    res_list = runner.invoke(main, ["connect", "--list"])
    assert res_list.exit_code == 0
    assert "Claude Code" in res_list.output
    assert "已接入" in res_list.output

    # penpal disconnect claude_code
    res_disc = runner.invoke(main, ["disconnect", "claude_code"])
    assert res_disc.exit_code == 0
    assert "已断开" in res_disc.output

    # penpal disconnect --all directly and via CLI
    d_all_res = disconnect_all(env={"HOME": str(home)})
    assert isinstance(d_all_res, list)

    res_all = runner.invoke(main, ["disconnect", "--all"])
    assert res_all.exit_code == 0


def test_four_tools_skill_mappings(tmp_path, monkeypatch):
    """Verify all four tools receive skill files at their specified locations."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # Detect claude_code, codex_cli, antigravity, cursor
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "antigravity").mkdir(parents=True, exist_ok=True)
    (home / ".cursor").mkdir(parents=True, exist_ok=True)

    auto_connect(env={"HOME": str(home)})

    claude_skill = home / ".claude" / "skills" / "penpal" / "SKILL.md"
    codex_skill = home / ".agents" / "skills" / "penpal" / "SKILL.md"
    agy_skill = home / ".gemini" / "config" / "skills" / "penpal" / "SKILL.md"
    cursor_skill = home / ".cursor" / "skills" / "penpal" / "SKILL.md"

    for p in (claude_skill, codex_skill, agy_skill, cursor_skill):
        assert p.is_file(), f"Missing skill file: {p}"
        content = p.read_text(encoding="utf-8")
        assert "search_context" in content
        assert "commit_memory" in content

    (home / ".cline").mkdir(parents=True, exist_ok=True)
    assert not (home / ".cline" / "skills").exists()


def test_install_failure_does_not_fail_install(tmp_path, monkeypatch):
    """When connect has an unexpected failure during install, install succeeds."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    def _broken_connect(*args, **kwargs):
        raise RuntimeError("simulated connect crash")

    monkeypatch.setattr("penpal.mcp.connect.auto_connect", _broken_connect)

    runner = CliRunner()
    res = runner.invoke(main, ["install", "init", "--no-launchd"])
    assert res.exit_code == 0
    assert "警告" in res.output or "connect_error" in res.output


def test_install_init_connect_integration(tmp_path, monkeypatch):
    """Test that install init auto-connects by default and --no-connect skips."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"

    runner = CliRunner()

    res_no = runner.invoke(main, ["install", "init", "--no-launchd", "--no-connect"])
    assert res_no.exit_code == 0
    assert not claude_json.exists()

    res_init = runner.invoke(main, ["install", "init", "--no-launchd"])
    assert res_init.exit_code == 0
    assert claude_json.exists()
    assert "penpal" in json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"]


def test_penpal_install_subcommand_default(tmp_path, monkeypatch):
    """Running `penpal install` directly triggers init and auto-connect."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)
    monkeypatch.setattr("penpal.cli.install._install_launchd_jobs", lambda **kwargs: [])

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"

    runner = CliRunner()
    res = runner.invoke(main, ["install"])
    assert res.exit_code == 0
    assert claude_json.exists()
    assert "penpal" in json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"]


# ── Tests for the 7 review items ──────────────────────────────────────────────

def test_item1_missing_yaml_does_not_break_other_tools(tmp_path, monkeypatch):
    """Item 1: When yaml module is missing, hermes reports missing yaml, other tools connect normally."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    # Simulate yaml module unavailable
    monkeypatch.setattr("penpal.mcp.connect._get_yaml_module", lambda: None)
    monkeypatch.setitem(sys.modules, "yaml", None)

    # Detect both claude_code and hermes; an existing hermes config stays byte-identical.
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    hermes_yaml = home / ".hermes" / "config.yaml"
    hermes_yaml.write_text("mcp_servers:\n  other:\n    command: other_service\n", encoding="utf-8")
    before = hermes_yaml.read_bytes()

    results = auto_connect(env={"HOME": str(home)})
    res_map = {r.tool_id: r for r in results}

    # Not a parse failure of the user's file: report it as not connected, file untouched.
    assert (res_map["hermes"].status, res_map["hermes"].error) == ("manual", "missing_yaml")
    assert "缺少 YAML 解析库" in res_map["hermes"].message
    assert hermes_yaml.read_bytes() == before

    # Claude Code connected normally despite missing yaml!
    assert res_map["claude_code"].status == "connected"
    assert (home / ".claude.json").exists()

    # List shows missing yaml
    statuses = {s.tool_id: s for s in list_tools(env={"HOME": str(home)})}
    assert statuses["hermes"].state == "未接入"
    assert "缺少 YAML 解析库" in statuses["hermes"].detail


def test_item2_permissions_boundary(tmp_path, monkeypatch):
    """Item 2: New files 0600; existing 0600 preserved; backup/state 0600; dirs 0700."""
    from penpal.utils.paths import get_data_dir

    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    # Create existing codex config with 0600
    codex_toml = home / ".codex" / "config.toml"
    codex_toml.write_text('model = "o3"\n', encoding="utf-8")
    codex_toml.chmod(0o600)

    auto_connect(env={"HOME": str(home)})

    # 1. Newly created claude.json has mode 0600
    claude_json = home / ".claude.json"
    assert (claude_json.stat().st_mode & 0o777) == 0o600

    # 2. Existing codex.toml retains 0600
    assert (codex_toml.stat().st_mode & 0o777) == 0o600

    # 3. Connect dir and backup dir have mode 0700
    data_dir = get_data_dir()
    connect_dir = data_dir / "connect"
    backup_dir = connect_dir / "backups"
    assert (connect_dir.stat().st_mode & 0o777) == 0o700
    assert (backup_dir.stat().st_mode & 0o777) == 0o700

    # 4. State file has mode 0600
    state_file = connect_dir / "state.json"
    assert (state_file.stat().st_mode & 0o777) == 0o600

    # 5. Backup file has mode 0600
    backup_file = backup_dir / f"codex_cli_{codex_toml.name}"
    assert backup_file.is_file()
    assert (backup_file.stat().st_mode & 0o777) == 0o600


def test_item3_concurrency_conflict_aborts_without_overwrite(tmp_path, monkeypatch):
    """Item 3: Concurrent edit by external process causes connect to abort and notify."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"
    claude_json.write_text('{"mcpServers": {}}', encoding="utf-8")

    from penpal.mcp import connect as connect_module
    real_update = connect_module._update_config_file

    def _simulated_concurrent_edit(path, *args, **kwargs):
        if path == claude_json:
            claude_json.write_text('{"mcpServers": {"concurrent": true}}', encoding="utf-8")
        return real_update(path, *args, **kwargs)

    monkeypatch.setattr(connect_module, "_update_config_file", _simulated_concurrent_edit)

    results = auto_connect(env={"HOME": str(home)})
    claude_res = next(r for r in results if r.tool_id == "claude_code")

    assert claude_res.status == "parse_error"
    assert "配置文件正被其他程序修改，请稍后重试" in claude_res.message
    # File content not overwritten by connect!
    assert "concurrent" in claude_json.read_text(encoding="utf-8")
    assert "penpal" not in claude_json.read_text(encoding="utf-8")


def test_item4_toml_subtable_removal(tmp_path, monkeypatch):
    """Item 4: Existing manual TOML config with subtables [mcp_servers.penpal.env] is cleaned cleanly."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".codex").mkdir(parents=True, exist_ok=True)
    codex_toml = home / ".codex" / "config.toml"

    # User manually configured penpal with subtable
    manual_toml = """model = "o3-mini"

[mcp_servers.penpal]
command = "manual_penpal"
args = ["mcp", "serve"]

[mcp_servers.penpal.env]
OLD_KEY = "old_value"

[mcp_servers.other]
command = "other_tool"
"""
    codex_toml.write_text(manual_toml, encoding="utf-8")

    # Connect should replace it without duplicate key error
    results = auto_connect(env={"HOME": str(home)})
    codex_res = next(r for r in results if r.tool_id == "codex_cli")
    assert codex_res.status == "connected"

    new_toml = codex_toml.read_text(encoding="utf-8")
    assert "[mcp_servers.penpal]" in new_toml
    assert "[mcp_servers.penpal.env]" not in new_toml
    assert "[mcp_servers.other]" in new_toml

    # Verify tomllib parses cleanly
    import tomllib
    parsed = tomllib.loads(new_toml)
    assert parsed["mcp_servers"]["penpal"]["command"] != "manual_penpal"
    assert parsed["mcp_servers"]["other"]["command"] == "other_tool"


def test_item5_takeover_manual_config_and_restore(tmp_path, monkeypatch):
    """Item 5: Takeover existing manual config, mark in list, and restore original byte-for-byte on disconnect."""
    _setup_db()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    (home / ".claude").mkdir(parents=True, exist_ok=True)
    claude_json = home / ".claude.json"

    # Manual pre-existing penpal config
    manual_content = '{\n  "mcpServers": {\n    "penpal": {\n      "command": "/manual/penpal"\n    }\n  }\n}\n'
    claude_json.write_text(manual_content, encoding="utf-8")
    orig_bytes = claude_json.read_bytes()

    # 1. Connect takes over
    results = auto_connect(env={"HOME": str(home)})
    claude_res = next(r for r in results if r.tool_id == "claude_code")
    assert claude_res.status == "connected"
    assert "已接管现有手动配置" in claude_res.message

    # 2. list_tools displays takeover note
    statuses = {s.tool_id: s for s in list_tools(env={"HOME": str(home)})}
    assert statuses["claude_code"].state == "已接入"
    assert statuses["claude_code"].detail == "接管了已有的手动配置"

    # 3. Disconnect restores original manual config byte-for-byte
    d_res = disconnect_tool("claude_code", env={"HOME": str(home)})
    assert d_res.status == "restored_original"
    assert claude_json.read_bytes() == orig_bytes
