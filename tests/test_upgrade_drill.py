from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRILL = ROOT / "scripts" / "upgrade-drill.sh"


def _runtime_scenario_module():
    path = ROOT / "tests" / "test_runtime_scenarios.py"
    spec = importlib.util.spec_from_file_location("runtime_scenarios_for_upgrade_drill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_drill_workflow_modes_and_shared_read_only_commands():
    workflow = (ROOT / ".github" / "workflows" / "upgrade-drill.yml").read_text(encoding="utf-8")
    script = DRILL.read_text(encoding="utf-8")
    shared_commands = tuple(
        tuple(line.split())
        for line in (ROOT / "tests" / "read_only_commands.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert "matrix:" in workflow
    assert "mode: [upgrade, fresh]" in workflow
    assert '"${1:-upgrade}"' in script
    assert '"$REPO_DIR/tests/read_only_commands.txt"' in script
    assert tuple(tuple(command) for command in _runtime_scenario_module()._READ_ONLY_COMMANDS) == shared_commands


def test_upgrade_drill_refuses_to_run_without_ci_before_side_effects(tmp_path):
    workdir = tmp_path / "refusal-run"
    workdir.mkdir()
    home = workdir / "home"
    home.mkdir()
    env = os.environ.copy()
    env.pop("CI", None)
    env["GITHUB_ACTIONS"] = "true"
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(DRILL), "fresh"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "requires CI=true and GITHUB_ACTIONS=true" in result.stderr
    assert list(home.iterdir()) == []
    assert list(workdir.iterdir()) == [home]
