"""Connect rollback and locking tests; all host configs and tool data are synthetic."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

from penpal.config import Config
from penpal.mcp import auth, connect
from penpal.mcp.auth import authenticate, create_grant
from penpal.mcp.connect import auto_connect, disconnect_tool
from penpal.store.db import init_db


def _setup(tmp_path: Path, monkeypatch, tool_ids: tuple[str, ...]) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    init_db(Config.load().db_path_expanded)

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    for tool_id in tool_ids:
        recipe = (
            f'id = "{tool_id}"\n'
            f'name = "Tool {tool_id}"\n'
            'kind = "agent"\n'
            '\n[detect]\n'
            f'any = [{{ dir = "${{HOME}}/.{tool_id}" }}]\n'
            '\n[mcp]\n'
            f'config = "${{HOME}}/{tool_id}.json"\n'
            'style = "json_mcpServers"\n'
        )
        (catalog / f"{tool_id}.toml").write_text(recipe, encoding="utf-8")
        (home / f".{tool_id}").mkdir()
    return home, catalog


def _penpal_entry(token: str, client: str) -> dict:
    return {
        "command": "penpal",
        "args": ["mcp", "serve"],
        "env": {"PENPAL_TOKEN": token, "PENPAL_CLIENT": client},
    }


def _written_token(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["mcpServers"]["penpal"]["env"]["PENPAL_TOKEN"]


def test_config_write_failure_revokes_only_new_grant(tmp_path, monkeypatch):
    home, catalog = _setup(tmp_path, monkeypatch, ("alpha",))
    config = home / "alpha.json"
    old_token = create_grant("alpha", operations=["read", "write"])
    config.write_text(
        json.dumps({"mcpServers": {"penpal": _penpal_entry(old_token, "alpha")}}, indent=2) + "\n",
        encoding="utf-8",
    )
    original_bytes = config.read_bytes()

    real_create = auth.create_grant
    new_tokens: list[str] = []

    def _capture_create(*args, **kwargs):
        token = real_create(*args, **kwargs)
        new_tokens.append(token)
        return token

    def _fail_write(*args, **kwargs):
        raise OSError("simulated permission failure")

    monkeypatch.setattr(connect, "create_grant", _capture_create)
    monkeypatch.setattr(connect, "_update_config_file", _fail_write)

    results = auto_connect(catalog_dir=catalog, env={"HOME": str(home)})
    result = next(r for r in results if r.tool_id == "alpha")

    assert (result.status, result.error) == ("parse_error", "config_failed")
    assert result.message == connect._CONNECT_FAILURE_MESSAGE
    assert config.read_bytes() == original_bytes
    assert len(new_tokens) == 1
    assert authenticate(new_tokens[0]) is None
    old_grant = authenticate(old_token)
    assert old_grant is not None
    assert old_grant.client_id == "alpha"
    assert connect._load_all_states() == {}


def test_skill_write_failure_restores_config_and_keeps_other_tool(tmp_path, monkeypatch):
    home, catalog = _setup(tmp_path, monkeypatch, ("alpha", "beta"))
    alpha_config = home / "alpha.json"
    alpha_config.write_text('{"keep": true}\n', encoding="utf-8")
    alpha_bytes = alpha_config.read_bytes()

    real_update = connect._update_config_file
    real_skill = connect._write_skill_file
    new_tokens: list[str] = []

    def _capture_update(path, *args, **kwargs):
        result = real_update(path, *args, **kwargs)
        if path == alpha_config:
            new_tokens.append(_written_token(path))
        return result

    def _fail_skill(tool_id, **kwargs):
        if tool_id == "alpha":
            raise OSError("simulated skill failure")
        return real_skill(tool_id, **kwargs)

    monkeypatch.setattr(connect, "_update_config_file", _capture_update)
    monkeypatch.setattr(connect, "_write_skill_file", _fail_skill)

    results = auto_connect(catalog_dir=catalog, env={"HOME": str(home)})
    result_map = {r.tool_id: r for r in results}

    assert (result_map["alpha"].status, result_map["alpha"].error) == ("parse_error", "skill_failed")
    assert result_map["alpha"].message == connect._CONNECT_FAILURE_MESSAGE
    assert result_map["beta"].status == "connected"
    assert alpha_config.read_bytes() == alpha_bytes
    assert len(new_tokens) == 1
    assert authenticate(new_tokens[0]) is None

    states = connect._load_all_states()
    assert "alpha" not in states
    assert "beta" in states
    assert authenticate(_written_token(home / "beta.json")) is not None


def test_state_save_failure_restores_config_and_revokes_grant(tmp_path, monkeypatch):
    home, catalog = _setup(tmp_path, monkeypatch, ("alpha",))
    config = home / "alpha.json"
    config.write_text('{"keep": 1}\n', encoding="utf-8")
    original_bytes = config.read_bytes()

    real_update = connect._update_config_file
    new_tokens: list[str] = []

    def _capture_update(path, *args, **kwargs):
        result = real_update(path, *args, **kwargs)
        new_tokens.append(_written_token(path))
        return result

    def _fail_save(*args, **kwargs):
        raise OSError("simulated state failure")

    monkeypatch.setattr(connect, "_update_config_file", _capture_update)
    monkeypatch.setattr(connect, "_save_all_states", _fail_save)

    results = auto_connect(catalog_dir=catalog, env={"HOME": str(home)})

    assert (results[0].status, results[0].error) == ("parse_error", "state_failed")
    assert results[0].message == connect._CONNECT_FAILURE_MESSAGE
    assert config.read_bytes() == original_bytes
    assert len(new_tokens) == 1
    assert authenticate(new_tokens[0]) is None
    assert not connect._state_file_path().exists()


def test_concurrent_auto_connect_serializes_state(tmp_path, monkeypatch):
    home, catalog = _setup(tmp_path, monkeypatch, ("alpha",))
    config = home / "alpha.json"
    config.write_text('{"keep": true}\n', encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    second_lock_attempt = threading.Event()
    real_update = connect._update_config_file
    real_lock = connect._connect_state_lock
    writes: list[Path] = []
    lock_attempts: list[int] = []

    def _slow_update(path, *args, **kwargs):
        writes.append(path)
        if len(writes) == 1:
            entered.set()
            assert release.wait(5)
        return real_update(path, *args, **kwargs)

    @contextmanager
    def _tracking_lock():
        lock_attempts.append(1)
        if len(lock_attempts) == 2:
            second_lock_attempt.set()
        with real_lock():
            yield

    monkeypatch.setattr(connect, "_update_config_file", _slow_update)
    monkeypatch.setattr(connect, "_connect_state_lock", _tracking_lock)

    first_results: list = []
    second_results: list = []
    errors: list[Exception] = []

    def _run(store):
        try:
            store.extend(auto_connect(catalog_dir=catalog, env={"HOME": str(home)}))
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=_run, args=(first_results,))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=_run, args=(second_results,))
    second.start()
    assert second_lock_attempt.wait(5)
    release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(writes) == 1
    assert first_results[0].status == "connected"
    assert second_results[0].status == "already_connected"

    state = connect._load_all_states()["alpha"]
    assert state.written_hash == connect._file_hash(config)
    assert authenticate(_written_token(config)) is not None
    assert len([g for g in auth.active_grants() if g["client_id"] == "alpha"]) == 1


def test_lock_timeout_reports_error_without_writing(tmp_path, monkeypatch):
    home, catalog = _setup(tmp_path, monkeypatch, ("alpha",))
    monkeypatch.setattr(connect, "_CONNECT_LOCK_TIMEOUT_SEC", 0.1)

    with connect._connect_state_lock():
        results = auto_connect(catalog_dir=catalog, env={"HOME": str(home)})
        disconnect_result = disconnect_tool("alpha", catalog_dir=catalog, env={"HOME": str(home)})

    assert len(results) == 1
    assert results[0].status == "parse_error"
    assert results[0].error == "lock_timeout"
    assert results[0].message == connect._CONNECT_LOCK_TIMEOUT_MESSAGE
    assert disconnect_result.status == "error"
    assert disconnect_result.message == connect._DISCONNECT_LOCK_TIMEOUT_MESSAGE
    assert not (home / "alpha.json").exists()
    assert auth.active_grants() == []
    assert connect._load_all_states() == {}
