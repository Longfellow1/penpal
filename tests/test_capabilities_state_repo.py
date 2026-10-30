"""Lock the architectural invariants of the capability/health gateway.

Specifically:
1. The state repo is the runtime source of truth for capability health.
2. The daemon supervisor never writes health.json directly.
3. The healthcheck CLI is the sole writer of health.json and embeds a
   capability snapshot read from the state repo.
4. registry.monitor_all isolates per-capability exceptions.
5. HUD reads the state repo first, falling back to health.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


from penpal.capabilities.base import HealthState
from penpal.capabilities.registry import CapabilityRegistry
from penpal.capabilities.store import load_states, save_states
from penpal.store.db import close, init_db
from penpal.store.repository import get_state, set_state


def _init_test_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "penpal.db"
    init_db(db_path)
    return db_path


def test_save_and_load_states_roundtrip(tmp_path) -> None:
    _init_test_db(tmp_path)
    try:
        original = {
            "alpha": HealthState(ok=True, code="ok", last_checked=10.0, detail=None),
            "beta": HealthState(ok=False, code="boom", last_checked=20.5, detail="explosion"),
        }
        save_states(original)
        recovered = load_states()
        assert recovered == original
    finally:
        close()


def test_supervisor_does_not_write_health_json(tmp_path, monkeypatch) -> None:
    """Daemon supervisor must persist via state repo only."""
    _init_test_db(tmp_path)
    try:
        out_path = tmp_path / "health.json"
        # Redirect every consumer of HEALTH_JSON_PATH so the test stays sandboxed
        monkeypatch.setattr("penpal.health.report.HEALTH_JSON_PATH", out_path)
        monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

        from penpal.app import _run_capability_self_check
        from penpal.capabilities.registry import build_default_registry

        registry = build_default_registry()
        _run_capability_self_check(registry)

        assert not out_path.exists(), "supervisor must not write health.json"
        # But state repo MUST hold the capabilities now
        assert load_states(), "supervisor must persist capabilities to state repo"
    finally:
        close()


def test_healthcheck_embeds_capabilities_from_state_repo(tmp_path, monkeypatch) -> None:
    """healthcheck is the sole writer of health.json; capability data flows in
    from the state repo, not from a direct registry probe."""
    _init_test_db(tmp_path)
    try:
        # Pre-populate state repo with a known capability snapshot.
        save_states({
            "fake_cap": HealthState(ok=False, code="fake_boom", last_checked=1234.5, detail="msg"),
        })
        set_state("status", "running")

        out_path = tmp_path / "health.json"
        vault_path = tmp_path / "vault"
        (vault_path / "Daily").mkdir(parents=True)

        config = type(
            "Cfg",
            (),
            {
                "db_path_expanded": tmp_path / "penpal.db",
                "obsidian": type("Obs", (), {"vault_path": str(vault_path)})(),
                "watchers": type("W", (), {
                    "window": False, "idle": False, "clipboard": False, "manual": False,
                    "browser": False, "ax_text": False, "ocr": False,
                })(),
            },
        )()

        monkeypatch.setattr("penpal.health.check.Config.load", lambda: config)
        monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)
        from penpal.runtime import RuntimeState, RuntimeStatus
        monkeypatch.setattr("penpal.health.check.evaluate", lambda **_kwargs: RuntimeStatus(RuntimeState.RUNNING, pid=1))

        from penpal.health.check import run_healthcheck

        result = run_healthcheck()
        assert "capabilities" in result
        assert "fake_cap" in result["capabilities"]
        assert result["capabilities"]["fake_cap"]["code"] == "fake_boom"
        assert result["paused"] is False

        on_disk = json.loads(out_path.read_text())
        assert on_disk["capabilities"]["fake_cap"]["code"] == "fake_boom"
    finally:
        close()


def test_registry_monitor_isolates_failing_capability() -> None:
    """A capability that raises must not block the rest of the registry."""

    class BoomCap:
        name = "boom"
        level_when_failed = "err"
        label_when_failed = "boom"

        def precheck(self):
            from penpal.capabilities.base import CheckResult
            return CheckResult(ok=True, code="ok")

        def monitor(self):
            raise RuntimeError("intentional crash")

        def diagnose(self, state):
            from penpal.capabilities.base import Signal
            return Signal(level="err", label="boom", hint=state.detail, action=None)

    class GoodCap:
        name = "good"
        level_when_failed = "ok"
        label_when_failed = "ok"

        def precheck(self):
            from penpal.capabilities.base import CheckResult
            return CheckResult(ok=True, code="ok")

        def monitor(self):
            return HealthState(ok=True, code="ok", last_checked=time.time())

        def diagnose(self, state):
            from penpal.capabilities.base import Signal
            return Signal(level="ok", label="ok", hint=None, action=None)

    registry = CapabilityRegistry()
    registry.register(BoomCap())
    registry.register(GoodCap())

    states = registry.monitor_all()

    assert states["boom"].ok is False
    assert states["boom"].code == "capability_error"
    assert "intentional crash" in (states["boom"].detail or "")
    assert states["good"].ok is True


def test_ax_denied_forces_ax_dependent_watcher_facts_false(tmp_path) -> None:
    _init_test_db(tmp_path)
    try:
        from penpal.app import _run_capability_self_check

        class _FakeRegistry:
            def monitor_all(self):
                now = time.time()
                return {
                    "accessibility_permission": HealthState(ok=False, code="ax_denied", last_checked=now, detail="denied"),
                    "ax_text_watcher": HealthState(ok=True, code="ok", last_checked=now, detail=None),
                    "keyboard_chunk_watcher": HealthState(ok=True, code="ok", last_checked=now, detail=None),
                }

            def select_failure(self, states, names=None):
                selected = {
                    name: state
                    for name, state in states.items()
                    if state.ok is False and (names is None or name in names)
                }
                if not selected:
                    return None
                name = sorted(selected.keys())[0]
                return type("Failure", (), {"state": selected[name]})()

        _run_capability_self_check(_FakeRegistry())

        states = load_states()
        assert states["ax_text_watcher"].ok is False
        assert states["ax_text_watcher"].code == "ax_denied"
        assert states["keyboard_chunk_watcher"].ok is False
        assert states["keyboard_chunk_watcher"].code == "ax_denied"
        assert get_state("capture_error_code") == "ax_denied"
    finally:
        close()
