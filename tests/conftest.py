from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
_SESSION_HOME: str | None = None
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_OPT_IN_MARKERS = {
    "slow": ("PENPAL_RUN_SLOW", "opt-in performance benchmark; set PENPAL_RUN_SLOW=1"),
    "system_smoke": (
        "PENPAL_RUN_SYSTEM_SMOKE",
        "opt-in system smoke; set PENPAL_RUN_SYSTEM_SMOKE=1",
    ),
}


def pytest_configure(config):
    """Set the process-wide isolation boundary before test modules import Penpal."""
    global _SESSION_HOME
    _SESSION_HOME = tempfile.mkdtemp(prefix="penpal-pytest-home-")
    os.environ["HOME"] = _SESSION_HOME
    os.environ["PENPAL_ISOLATED"] = "1"


def pytest_unconfigure(config):
    if _SESSION_HOME is not None:
        shutil.rmtree(_SESSION_HOME, ignore_errors=True)


def pytest_collection_modifyitems(items):
    for item in items:
        for marker, (env_var, reason) in _OPT_IN_MARKERS.items():
            if marker in item.keywords and os.environ.get(env_var) != "1":
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def _isolated_home_and_launchd(tmp_path, monkeypatch):
    """Keep tests away from user data and the host's launchd domain."""
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    monkeypatch.setenv("PENPAL_RUNTIME_DISABLE_LAUNCHD_PROBE", "1")


@pytest.fixture(scope="session", autouse=True)
def _no_object_object_artifacts() -> None:
    for path in ROOT.glob("<object object*"):
        path.unlink(missing_ok=True)
    yield
    leaked = sorted(path.name for path in ROOT.glob("<object object*"))
    assert leaked == [], f"unexpected root artifacts: {', '.join(leaked)}"


@pytest.fixture(autouse=True)
def _pinned_ui_language(monkeypatch):
    """Tests assert the Chinese copy. Pin it so a runner with an English locale (CI) sees the same text;
    tests about language detection override or delete PENPAL_LANG themselves."""
    from penpal.i18n import set_lang

    monkeypatch.setenv("PENPAL_LANG", "zh")
    set_lang()
    yield
    set_lang()


@pytest.fixture(autouse=True)
def _mcp_capture_health_off_by_default(monkeypatch):
    """MCP tests assert exact payloads; runtime adapter tests exercise capture_health explicitly."""
    monkeypatch.setattr("penpal.mcp.server.current_capture_health", lambda: None)


@pytest.fixture(autouse=True)
def _no_host_legacy_process_scan(monkeypatch):
    """Data-dir migration refuses to run while old `keypulse mcp serve` processes are alive. That scan
    looks at the whole machine, so on a developer box with KeyPulse running it would block every
    migration test. tests/test_data_dir_migration.py exercises the real scan explicitly."""
    from penpal.utils import paths

    monkeypatch.setattr(paths, "_find_legacy_mcp_serve_processes", lambda: [])
