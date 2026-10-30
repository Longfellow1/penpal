from __future__ import annotations

import plistlib
from pathlib import Path
from unittest.mock import patch

import pytest

from penpal.sources.recipes.catalog import DetectConfig, DetectRule, ToolRecipe
from penpal.sources.recipes.identity_gate import Identity, check_path, find_identity


def _tool(rule: DetectRule) -> ToolRecipe:
    return ToolRecipe(id="orbit", name="Orbit", kind="agent", detect=DetectConfig(any=[rule]))


def test_executable_identity_and_own_root(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "orbit"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    identity = find_identity(_tool(DetectRule(cli="orbit")), {"HOME": str(tmp_path), "PATH": str(executable.parent)})
    check_path(tmp_path / ".config" / "Or_bit" / "sessions", identity, tmp_path)
    with pytest.raises(ValueError, match="identity-derived"):
        check_path(tmp_path / ".config" / "other" / "sessions", identity, tmp_path)


def test_app_identity_comes_from_plist(tmp_path: Path) -> None:
    contents = tmp_path / "Applications" / "Orbit.app" / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": "example.orbit", "CFBundleName": "Orbit"}, stream)
    identity = find_identity(_tool(DetectRule(app_bundle="example.orbit")), {"HOME": str(tmp_path), "PATH": ""})
    check_path(tmp_path / "Library" / "Containers" / "example.orbit" / "sessions", identity, tmp_path)


@pytest.mark.parametrize("command", ["app", "data", "google", "sh"])
def test_generic_identity_rejected(tmp_path: Path, command: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        find_identity(_tool(DetectRule(cli=command)), {"HOME": str(tmp_path), "PATH": ""})


def test_system_executable_rejected(tmp_path: Path) -> None:
    with patch("penpal.sources.recipes.identity_gate.shutil.which", return_value="/usr/bin/printf"):
        with pytest.raises(ValueError, match="identity"):
            find_identity(_tool(DetectRule(cli="printf")), {"HOME": str(tmp_path), "PATH": ""})


def test_symlink_to_sensitive_path_rejected(tmp_path: Path) -> None:
    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()
    link = tmp_path / ".orbit"
    link.symlink_to(sensitive, target_is_directory=True)
    with pytest.raises(ValueError, match="sensitive"):
        check_path(link / "history.jsonl", Identity(("orbit",)), tmp_path)


def test_chat_app_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="chat"):
        check_path(tmp_path / ".config" / "Slack" / "sessions", Identity(("Slack",)), tmp_path)


def _recipe(path: str, *, field: str = "body") -> str:
    return f'''id = "orbit"
name = "Orbit"
kind = "agent"
[detect]
any = [{{cli = "orbit"}}]
[[history]]
format = "jsonl"
glob = "{path}"
timestamp = "file_mtime"
compression = "none"
[history.fields]
role = "role"
content = "{field}"
'''


def _setup(tmp_path: Path) -> tuple[dict[str, str], Path]:
    binary = tmp_path / "bin" / "orbit"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    history = tmp_path / ".orbit" / "history.jsonl"
    history.parent.mkdir()
    history.write_text('{"role":"user","body":"hello"}\n', encoding="utf-8")
    return {"HOME": str(tmp_path), "PATH": str(binary.parent)}, history


def test_save_replace_and_rollback(tmp_path: Path) -> None:
    from penpal.sources.recipes.identity_gate import rollback_recipe, save_recipe

    env, history = _setup(tmp_path)
    first = _recipe(str(history))
    second = first.replace('name = "Orbit"', 'name = "Orbit Two"')
    save_recipe(first, data_dir=tmp_path / "data", env=env)
    save_recipe(second, data_dir=tmp_path / "data", env=env)
    directory = tmp_path / "data" / "catalog"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "orbit.toml").stat().st_mode & 0o777 == 0o600
    rollback_recipe("orbit", data_dir=tmp_path / "data", env=env)
    assert (directory / "orbit.toml").read_text() == first


def test_probe_rejects_bad_mapping_and_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from penpal.sources.recipes.identity_gate import save_recipe

    env, history = _setup(tmp_path)
    with pytest.raises(ValueError, match="no session turn"):
        save_recipe(_recipe(str(history), field="missing"), data_dir=tmp_path / "data", env=env)
    monkeypatch.setattr("penpal.sources.recipes.identity_gate.MAX_PROBE_BYTES", 1)
    with pytest.raises(ValueError, match="size limit"):
        save_recipe(_recipe(str(history)), data_dir=tmp_path / "data", env=env)


@pytest.mark.parametrize("manager", ["brew", "npm", "pipx"])
def test_package_metadata_identity(tmp_path: Path, manager: str) -> None:
    import json

    env = {"HOME": str(tmp_path), "PATH": "", "HOMEBREW_PREFIX": str(tmp_path / "brew"),
           "NPM_CONFIG_PREFIX": str(tmp_path / "npm"), "PIPX_HOME": str(tmp_path / "pipx")}
    if manager == "brew":
        (tmp_path / "brew/Cellar/orbit/1.0").mkdir(parents=True)
    else:
        metadata = (tmp_path / "npm/lib/node_modules/orbit/package.json" if manager == "npm"
                    else tmp_path / "pipx/venvs/orbit/pipx_metadata.json")
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"name": "orbit"} if manager == "npm" else {"main_package": {"package": "orbit"}}))
    assert find_identity(_tool(DetectRule(cli="orbit")), env).names == ("orbit",)


def test_sync_rechecks_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from penpal.sources.plugins.ai_tools import AiToolsSource
    from penpal.sources.recipes.identity_gate import save_recipe

    env, history = _setup(tmp_path)
    data = tmp_path / "data"
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: data)
    save_recipe(_recipe(str(history.parent / "*.jsonl")), data_dir=data, env=env)
    source = AiToolsSource(catalog_dir=tmp_path / "empty", env=env)
    instance = source.discover()[0]
    history.unlink()
    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()
    (sensitive / "history.jsonl").write_text('{"role":"user","body":"secret"}\n')
    history.symlink_to(sensitive / "history.jsonl")
    assert source.discover() == []
    from datetime import datetime, timezone
    assert list(source.read(instance, datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc))) == []


def test_sqlite_probe_and_adapter_replacement(tmp_path: Path) -> None:
    import sqlite3
    from penpal.sources.recipes.identity_gate import save_recipe

    env, history = _setup(tmp_path)
    db = history.with_suffix(".sqlite")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE turns (id INTEGER, role TEXT, body TEXT)")
        conn.execute("INSERT INTO turns VALUES (1, 'user', 'hello')")
    content = _recipe(str(db)).replace('format = "jsonl"', 'format = "sqlite"\nquery = "SELECT * FROM turns"')
    content += 'message_id = "id"\n'
    save_recipe(content, data_dir=tmp_path / "data", env=env)
    with pytest.raises(ValueError, match="内置插件"):
        save_recipe(content.replace('id = "orbit"', 'id = "claude_code"'), data_dir=tmp_path / "data", env=env)


def test_submission_symlink_file_rejected(tmp_path: Path) -> None:
    from penpal.sources.recipes.identity_gate import save_recipe

    env, history = _setup(tmp_path)
    history.unlink()
    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()
    (sensitive / "data.jsonl").write_text('{"role":"user","body":"secret"}\n')
    history.symlink_to(sensitive / "data.jsonl")
    with pytest.raises(ValueError, match="sensitive"):
        save_recipe(_recipe(str(history.parent / "*.jsonl")), data_dir=tmp_path / "data", env=env)


def test_coverage_reports_rejected_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from penpal.sources.coverage import compute_tool_coverage
    from penpal.sources.recipes.identity_gate import save_recipe
    from penpal.store.db import init_db

    env, history = _setup(tmp_path)
    data = tmp_path / "data"
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: data)
    init_db(tmp_path / "coverage.db")
    save_recipe(_recipe(str(history.parent / "*.jsonl")), data_dir=data, env=env)
    history.unlink()
    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()
    (sensitive / "data.jsonl").write_text('{}\n')
    history.symlink_to(sensitive / "data.jsonl")
    rows, _ = compute_tool_coverage(catalog_dir=tmp_path / "empty", env=env)
    assert rows[0]["last_error"] == "sensitive path rejected"
    assert rows[0]["detected"] is False


def test_line_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from penpal.sources.recipes.identity_gate import save_recipe

    env, history = _setup(tmp_path)
    history.write_text('{"role":"user","body":"hello"}\n' * 2)
    monkeypatch.setattr("penpal.sources.recipes.identity_gate.MAX_PROBE_LINES", 1)
    with pytest.raises(ValueError, match="line limit"):
        save_recipe(_recipe(str(history)), data_dir=tmp_path / "data", env=env)


def test_user_recipe_sync_incremental(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone
    from penpal.sources.plugins.ai_tools import AiToolsSource
    from penpal.sources.recipes.identity_gate import save_recipe
    from penpal.sources.sink import persist_semantic_event
    from penpal.store.db import init_db

    env, history = _setup(tmp_path)
    data = tmp_path / "data"
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: data)
    init_db(tmp_path / "sync.db")
    save_recipe(_recipe(str(history.parent / "*.jsonl")), data_dir=data, env=env)
    source = AiToolsSource(catalog_dir=tmp_path / "empty", env=env, use_cursor=True)
    instance = source.discover()[0]
    start = datetime.min.replace(tzinfo=timezone.utc)
    end = datetime.max.replace(tzinfo=timezone.utc)
    events = list(source.read(instance, start, end))
    assert len(events) == 1 and events[0].intent == "hello"
    assert persist_semantic_event(events[0])
    source.commit_cursors()
    assert list(source.read(instance, start, end)) == []


def test_recursive_glob_rejected_before_sensitive_directory_walk(tmp_path: Path) -> None:
    from penpal.sources.recipes.identity_gate import save_recipe

    env, history = _setup(tmp_path)
    (history.parent / "linked").symlink_to(tmp_path / ".ssh", target_is_directory=True)
    with pytest.raises(ValueError, match="wildcards in directory"):
        save_recipe(_recipe(str(history.parent / "**" / "*.jsonl")), data_dir=tmp_path / "data", env=env)


@pytest.mark.parametrize("name", ["1password", "1Password", "bitwarden", "keepassxc", "dashlane"])
def test_password_manager_identity_rejected(tmp_path: Path, name: str) -> None:
    (tmp_path / f".{name}").mkdir()
    with pytest.raises(ValueError, match="identity|sensitive"):
        check_path(tmp_path / f".{name}" / "vault.json", Identity((name,)), tmp_path)
