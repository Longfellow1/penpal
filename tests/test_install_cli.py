from __future__ import annotations

import json
import plistlib
from pathlib import Path

from click.testing import CliRunner

from penpal.cli import install as cli
from penpal.cli import main


def test_install_help_exposes_lifecycle_commands():
    result = CliRunner().invoke(main, ["install", "--help"])

    assert result.exit_code == 0
    assert "init" in result.output
    assert "launchd" in result.output
    assert "doctor" in result.output
    assert "uninstall" in result.output


def test_install_script_unloads_legacy_services_before_data_migration():
    install_script = Path(__file__).parents[1] / "install.sh"
    source = install_script.read_text(encoding="utf-8")
    main_start = source.index("# Select Python before unloading old jobs")
    unload_call = source.index("unload_legacy_jobs()", main_start)
    migration_call = source.index("get_data_dir()", unload_call)

    assert unload_call < migration_call
    assert "launchctl" not in source
    assert '"$VENV_DIR/bin/penpal" install init' in source


def test_install_script_homebrew_path_drives_the_formula_only_through_its_cli():
    # README pipes install.sh from main while brew installs the latest release; an import of a module
    # that main added after that release fails on every fresh install.
    install_script = Path(__file__).parents[1] / "install.sh"
    source = install_script.read_text(encoding="utf-8")
    brew_start = source.index('if [[ "$DEV_MODE" != true ]]; then')
    brew_block = source[brew_start:source.index("exit 0", brew_start)]

    assert "check_data_dir_migration" not in brew_block
    assert "from penpal" not in brew_block
    assert "libexec/bin/python" not in brew_block
    assert "penpal install init" in brew_block


def test_install_script_homebrew_path_uses_qualified_names_and_uninstalls_old_formula_first():
    install_script = Path(__file__).parents[1] / "install.sh"
    source = install_script.read_text(encoding="utf-8")
    brew_start = source.index('header "3/4  Installing Penpal"')
    brew_block = source[brew_start:source.index('header "4/4  Initializing runtime"')]

    # Every install/upgrade/reinstall uses the fully qualified formula name.
    assert "brew install longfellow1/penpal/penpal" in brew_block
    assert "brew upgrade longfellow1/penpal/penpal || brew reinstall longfellow1/penpal/penpal" in brew_block
    assert "brew list --formula penpal" in brew_block
    assert "brew install penpal" not in brew_block
    assert "brew upgrade penpal" not in brew_block
    assert "brew reinstall penpal" not in brew_block
    assert "brew tap Longfellow1/penpal" not in brew_block

    # The old keypulse formula is removed before the old tap is untapped.
    uninstall_idx = brew_block.index("brew uninstall keypulse")
    untap_idx = brew_block.index("brew untap longfellow1/keypulse")
    assert uninstall_idx < untap_idx


def test_install_launchd_writes_plists_with_absolute_keypulse_path(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)

    result = CliRunner().invoke(main, ["install", "launchd", "--no-load"])

    assert result.exit_code == 0
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    daemon_plist = launch_agents / "com.penpal.daemon.plist"
    health_plist = launch_agents / "com.penpal.healthcheck.plist"
    for path in (daemon_plist, health_plist):
        assert path.exists()
    assert not (launch_agents / "com.keypulse.daemon.plist").exists()
    assert not (launch_agents / "com.keypulse.healthcheck.plist").exists()
    assert not (launch_agents / "com.keypulse.hud.plist").exists()
    assert not (launch_agents / "com.keypulse.obsidian-sync.plist").exists()
    assert not (launch_agents / "com.keypulse.obsidian-sync-hourly.plist").exists()

    payload = plistlib.loads(daemon_plist.read_bytes())
    assert payload["ProgramArguments"] == [str(executable), "serve"]
    assert payload["StandardOutPath"] == str(tmp_path / ".penpal" / "logs" / "daemon.log")


def test_install_launchd_removes_legacy_obsidian_plists(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    legacy = [
        launch_agents / "com.keypulse.daemon.plist",
        launch_agents / "com.keypulse.healthcheck.plist",
        launch_agents / "com.keypulse.hud.plist",
        launch_agents / "com.keypulse.obsidian-sync.plist",
        launch_agents / "com.keypulse.obsidian-sync-hourly.plist",
    ]
    for path in legacy:
        path.write_text("legacy", encoding="utf-8")

    control_preflights: list[str] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)
    monkeypatch.setattr("penpal.cli.install.unload_legacy_jobs", lambda: control_preflights.append("control") or [])

    result = CliRunner().invoke(main, ["install", "launchd", "--no-load"])

    assert result.exit_code == 0
    assert all(not path.exists() for path in legacy)
    assert control_preflights == ["control"]


def test_install_doctor_reports_old_launchd_and_stale_moved_venv(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from penpal.cli import install as cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(cli, "_penpal_executable_path", lambda: executable)
    monkeypatch.setattr(cli, "get_config_path", lambda: tmp_path / ".penpal" / "config.toml")
    monkeypatch.setattr(cli.Config, "load", lambda: SimpleNamespace(db_path_expanded=tmp_path / ".penpal" / "penpal.db", watchers=SimpleNamespace(keyboard_chunk=True)))
    monkeypatch.setattr(cli, "_launchd_label_loaded", lambda _label: False)

    config = tmp_path / ".penpal" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("", encoding="utf-8")
    database = tmp_path / ".penpal" / "penpal.db"
    database.write_text("", encoding="utf-8")
    legacy_plist = tmp_path / "Library" / "LaunchAgents" / "com.keypulse.daemon.plist"
    legacy_plist.parent.mkdir(parents=True)
    legacy_plist.write_text("legacy", encoding="utf-8")
    legacy_hud_plist = legacy_plist.with_name("com.keypulse.hud.plist")
    legacy_hud_plist.write_text("legacy", encoding="utf-8")
    old_entry = tmp_path / ".penpal" / "venv" / "bin" / "keypulse"
    old_entry.parent.mkdir(parents=True)
    old_entry.write_text(f"#!{tmp_path}/.keypulse/venv/bin/python\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["install", "doctor", "--plain"])

    assert result.exit_code == 1
    assert "legacy_launchd.com.keypulse.daemon=FAIL" in result.output
    assert "legacy_launchd.com.keypulse.hud=FAIL" in result.output
    assert "dev_venv=FAIL" in result.output
    assert "run install.sh --dev" in result.output


def _prepare_main_doctor(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from penpal.cli import install as cli
    from penpal.health import capture_health

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    config = home / ".penpal" / "config.toml"
    config.parent.mkdir()
    config.write_text("", encoding="utf-8")
    database = home / ".penpal" / "penpal.db"
    monkeypatch.setattr(cli.Config, "load", lambda: SimpleNamespace(db_path_expanded=database, watchers=SimpleNamespace(keyboard_chunk=True)))
    monkeypatch.setattr(cli, "get_config_path", lambda: config)
    monkeypatch.setattr(cli, "_launchd_label_loaded", lambda _label: False)
    monkeypatch.setattr(capture_health, "permission_report", lambda *_args, **_kwargs: [])
    return home


def test_doctor_detects_legacy_claude_and_codex_mcp_configs_read_only(monkeypatch, tmp_path):
    home = _prepare_main_doctor(monkeypatch, tmp_path)
    claude_config = home / ".claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "projects": {
                    "/tmp/project": {
                        "mcpServers": {
                            "keypulse": {"command": "~/.local/bin/keypulse", "args": ["mcp", "serve"]}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text(
        '[mcp_servers.penpal]\ncommand = "~/.local/bin/keypulse"\nargs = ["mcp", "serve"]\n',
        encoding="utf-8",
    )
    claude_before = claude_config.read_bytes()
    codex_before = codex_config.read_bytes()

    result = CliRunner().invoke(main, ["doctor", "--plain"])

    assert "MCP host 配置已迁移为 penpal: FAIL" in result.output
    assert "legacy_mcp_config=~/.claude.json: server=keypulse, command=keypulse" in result.output
    assert "legacy_mcp_config=~/.codex/config.toml: command=keypulse" in result.output
    assert "change the MCP server name and command to `penpal`" in result.output
    assert claude_config.read_bytes() == claude_before
    assert codex_config.read_bytes() == codex_before


def test_doctor_accepts_mcp_configs_already_using_penpal(monkeypatch, tmp_path):
    home = _prepare_main_doctor(monkeypatch, tmp_path)
    claude_config = home / ".claude.json"
    claude_config.write_text(
        json.dumps({"mcpServers": {"penpal": {"command": "~/.local/bin/penpal", "args": ["mcp", "serve"]}}}),
        encoding="utf-8",
    )
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir()
    codex_config.write_text(
        '[mcp_servers.penpal]\ncommand = "~/.local/bin/penpal"\nargs = ["mcp", "serve"]\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--plain"])

    assert "MCP host 配置已迁移为 penpal: OK" in result.output
    assert "legacy_mcp_config=" not in result.output


def test_install_launchd_preserves_custom_plist_without_force(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    custom = launch_agents / "com.penpal.daemon.plist"
    custom.write_text("custom plist", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)

    result = CliRunner().invoke(main, ["install", "launchd", "--no-load"])

    assert result.exit_code != 0
    assert "already exists and differs" in result.output
    assert custom.read_text(encoding="utf-8") == "custom plist"


def test_install_init_creates_runtime_without_overwriting_config(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    config_path = tmp_path / ".penpal" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[app]\nretention_days = 99\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)

    result = CliRunner().invoke(main, ["install", "init", "--no-launchd"])

    assert result.exit_code == 0
    assert (tmp_path / ".penpal" / "logs").exists()
    assert (tmp_path / ".penpal" / "penpal.db").exists()
    assert config_path.read_text(encoding="utf-8") == "[app]\nretention_days = 99\n"
    assert "penpal doctor" in result.output


def test_executable_path_keeps_homebrew_symlink_not_versioned_cellar(monkeypatch, tmp_path):
    # `brew upgrade` deletes the old Cellar dir; launchd jobs must point at the stable bin/ symlink.
    cellar = tmp_path / "Cellar" / "penpal" / "0.2.0" / "bin" / "penpal"
    cellar.parent.mkdir(parents=True)
    cellar.write_text("#!/bin/sh\n")
    link = tmp_path / "bin" / "penpal"
    link.parent.mkdir()
    link.symlink_to(cellar)
    monkeypatch.setenv("PENPAL_EXECUTABLE", str(link))

    assert cli._penpal_executable_path() == link


def test_install_launchd_replaces_own_plist_pinned_to_cellar_without_force(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    stale = launch_agents / "com.penpal.daemon.plist"
    stale.write_bytes(plistlib.dumps({
        "Label": "com.penpal.daemon",
        "ProgramArguments": ["/opt/homebrew/Cellar/penpal/0.2.0/libexec/bin/penpal", "serve"],
    }))

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)

    result = CliRunner().invoke(main, ["install", "launchd", "--no-load"])

    assert result.exit_code == 0, result.output
    assert plistlib.loads(stale.read_bytes())["ProgramArguments"][0] == str(executable)


def test_install_launchd_replaces_own_plist_from_an_older_template_without_force(monkeypatch, tmp_path):
    # Upgrading after the template changed (e.g. KeepAlive) must not stop `penpal install init`.
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    older = launch_agents / "com.penpal.daemon.plist"
    older.write_bytes(plistlib.dumps({
        "Label": "com.penpal.daemon",
        "ProgramArguments": [str(executable), "serve"],
        "KeepAlive": True,
    }))

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)

    result = CliRunner().invoke(main, ["install", "init", "--no-load"])

    assert result.exit_code == 0, result.output
    assert plistlib.loads(older.read_bytes())["KeepAlive"] == {"SuccessfulExit": False}


def test_install_launchd_preserves_a_plist_naming_another_job_without_force(monkeypatch, tmp_path):
    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    foreign = launch_agents / "com.penpal.daemon.plist"
    foreign.write_bytes(plistlib.dumps({"Label": "com.example.other", "ProgramArguments": ["/bin/true"]}))
    before = foreign.read_bytes()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)

    result = CliRunner().invoke(main, ["install", "launchd", "--no-load"])

    assert result.exit_code != 0
    assert "already exists and differs" in result.output
    assert foreign.read_bytes() == before


def test_install_init_with_load_reloads_healthcheck_with_replace_even_when_unchanged(monkeypatch, tmp_path):
    from penpal.runtime.control import ControlResult

    executable = tmp_path / "bin" / "penpal"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("penpal.cli.install._penpal_executable_path", lambda: executable)
    monkeypatch.setattr("penpal.cli.install.unload_legacy_jobs", lambda: [])
    monkeypatch.setattr("penpal.cli.install.control_restart", lambda *args, **kwargs: ControlResult(True, "restart"))
    monkeypatch.setattr("penpal.cli.install.launchd_label_loaded", lambda _label: True)

    # First, write unchanged plists via --no-load
    runner = CliRunner()
    result = runner.invoke(main, ["install", "launchd", "--no-load"])
    assert result.exit_code == 0

    load_calls = []
    monkeypatch.setattr(cli, "_launchd_load_plist", lambda path, **kwargs: load_calls.append((path, kwargs)) or True)

    # Run install init with load (default is load=True)
    result = runner.invoke(main, ["install", "init"])
    assert result.exit_code == 0

    assert len(load_calls) == 1
    path, kwargs = load_calls[0]
    assert path.name == "com.penpal.healthcheck.plist"
    assert kwargs.get("replace") is True
