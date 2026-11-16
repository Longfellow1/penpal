from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from penpal.cli.sources import sources_group
from penpal.i18n import T
from penpal.mcp import auth
from penpal.mcp.recipe_tools import propose_source_recipe
from penpal.sources.recipes.catalog import load_combined_catalog
from penpal.sources.recipes.identity_gate import save_recipe
from penpal.store.db import close, get_conn, init_db


def _recipe(name: str = "Orbit") -> str:
    return f'''id = "orbit"
name = "{name}"
kind = "agent"
[detect]
any = [{{cli = "orbit"}}]
[[history]]
format = "jsonl"
glob = "~/.orbit/*.jsonl"
timestamp = "file_mtime"
compression = "none"
[history.fields]
role = "role"
content = "body"
'''


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    binary = home / "bin" / "orbit"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    history = home / ".orbit" / "history.jsonl"
    history.parent.mkdir()
    history.write_text('{"role":"user","body":"Synthetic orbit prompt"}\n', encoding="utf-8")
    data_dir = home / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary.parent))
    monkeypatch.setattr("penpal.utils.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("penpal.cli.sources.get_data_dir", lambda: data_dir)
    init_db(tmp_path / "test.db")
    return data_dir


def test_mcp_proposal_stages_without_enabling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_env(tmp_path, monkeypatch)
    try:
        token = auth.create_grant("host", operations=["write"])
        result = propose_source_recipe(token, _recipe(), client="host")
        assert result["status"] == "pending_approval"
        assert result["id"] == "orbit"
        assert result["name"] == "Orbit"
        assert (result["matched_files"], result["parsed_turns"]) == (1, 1)
        assert "penpal sources approve" in result["message"]

        catalog_dir = data_dir / "catalog"
        proposed = catalog_dir / "orbit.proposed"
        assert catalog_dir.stat().st_mode & 0o777 == 0o700
        assert proposed.is_file()
        assert proposed.stat().st_mode & 0o777 == 0o600
        assert not (catalog_dir / "orbit.toml").exists()
        assert not any(t.id == "orbit" for t in load_combined_catalog(catalog_dir=tmp_path / "empty"))

        row = get_conn().execute("SELECT decision, reason_code FROM mcp_audit ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(row) == ("allow", "pending_approval")
    finally:
        close()


def test_mcp_proposal_preserves_disabled_and_existing_recipes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_env(tmp_path, monkeypatch)
    try:
        original = _recipe("Orbit Original")
        save_recipe(original, data_dir=data_dir)
        catalog_dir = data_dir / "catalog"
        disabled_marker = catalog_dir / "orbit.disabled"
        disabled_marker.touch(mode=0o600)

        token = auth.create_grant("host", operations=["write"])
        res1 = propose_source_recipe(token, _recipe("Orbit Proposed V1"), client="host")
        assert res1["status"] == "pending_approval"
        assert disabled_marker.exists()
        assert (catalog_dir / "orbit.toml").read_text(encoding="utf-8") == original

        # A newer proposal for the same id replaces the older proposal without touching .toml or .disabled.
        v2 = _recipe("Orbit Proposed V2")
        res2 = propose_source_recipe(token, v2, client="host")
        assert res2["status"] == "pending_approval"
        assert (catalog_dir / "orbit.proposed").read_text(encoding="utf-8") == v2
        assert disabled_marker.exists()
        assert (catalog_dir / "orbit.toml").read_text(encoding="utf-8") == original
    finally:
        close()


def test_approve_without_yes_and_decline_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_env(tmp_path, monkeypatch)
    try:
        token = auth.create_grant("host", operations=["write"])
        propose_source_recipe(token, _recipe(), client="host")

        runner = CliRunner()
        res = runner.invoke(sources_group, ["approve", "orbit"], input="n\n")
        assert res.exit_code == 0, res.output
        assert "orbit" in res.output
        assert "Orbit" in res.output
        assert str(data_dir / "catalog" / "orbit.proposed") in res.output
        assert "~/.orbit/*.jsonl" in res.output
        assert (data_dir / "catalog" / "orbit.proposed").exists()
        assert not (data_dir / "catalog" / "orbit.toml").exists()
    finally:
        close()


def test_approve_enables_and_removes_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_env(tmp_path, monkeypatch)
    try:
        save_recipe(_recipe("Orbit Old"), data_dir=data_dir)
        token = auth.create_grant("host", operations=["write"])
        updated = _recipe("Orbit Approved")
        propose_source_recipe(token, updated, client="host")

        # Existing .toml stays unchanged until user approves.
        assert 'name = "Orbit Old"' in (data_dir / "catalog" / "orbit.toml").read_text(encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(sources_group, ["approve", "orbit", "--yes"])
        assert res.exit_code == 0, res.output
        assert "orbit\tenabled" in res.output
        assert not (data_dir / "catalog" / "orbit.proposed").exists()
        assert (data_dir / "catalog" / "orbit.toml").read_text(encoding="utf-8") == updated
        assert 'name = "Orbit Old"' in (data_dir / "catalog" / "orbit.previous").read_text(encoding="utf-8")
    finally:
        close()


def test_reject_removes_proposal_and_errors_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_env(tmp_path, monkeypatch)
    try:
        token = auth.create_grant("host", operations=["write"])
        propose_source_recipe(token, _recipe(), client="host")

        runner = CliRunner()
        res = runner.invoke(sources_group, ["reject", "orbit"])
        assert res.exit_code == 0, res.output
        assert "orbit\trejected" in res.output
        assert not (data_dir / "catalog" / "orbit.proposed").exists()

        missing = runner.invoke(sources_group, ["reject", "orbit"])
        assert missing.exit_code != 0
    finally:
        close()


@pytest.mark.parametrize("cmd", ["approve", "reject"])
@pytest.mark.parametrize("bad_id", ["../evil", "BadID", "has space", "a/b"])
def test_invalid_id_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str, bad_id: str) -> None:
    _setup_env(tmp_path, monkeypatch)
    try:
        runner = CliRunner()
        args = [cmd, bad_id] + (["--yes"] if cmd == "approve" else [])
        res = runner.invoke(sources_group, args)
        assert res.exit_code != 0
    finally:
        close()


def test_recipes_lists_pending_proposals_after_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_env(tmp_path, monkeypatch)
    try:
        save_recipe(_recipe("Orbit Active"), data_dir=data_dir)
        token = auth.create_grant("host", operations=["write"])
        propose_source_recipe(token, _recipe("Orbit Pending"), client="host")

        runner = CliRunner()
        res = runner.invoke(sources_group, ["recipes"])
        assert res.exit_code == 0, res.output
        lines = [line for line in res.output.splitlines() if line.strip()]
        assert lines == [
            "orbit\tOrbit Active\tenabled",
            f"orbit\tOrbit Pending\t{T('待批准', 'pending')}",
        ]
    finally:
        close()


def test_recipe_id_with_trailing_newline_is_rejected():
    from penpal.sources.recipes.catalog import parse_recipe_dict

    with pytest.raises(ValueError):
        parse_recipe_dict({"id": "orbit\n"}, origin="<test>")
