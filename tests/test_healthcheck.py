from __future__ import annotations

import pytest

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from penpal.cli import main
from penpal.health.alerts import render_alert
from penpal.health.product_delivery import DeliveryHealth
from penpal.store.db import close, init_db
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event
from penpal.runtime import RuntimeState, RuntimeStatus
from penpal.runtime.evaluate import Issue


@pytest.fixture(autouse=True)
def _runtime_status(monkeypatch):
    import penpal.health.check as check

    status = RuntimeStatus(RuntimeState.RUNNING, pid=69943)
    monkeypatch.setattr(check, "evaluate", lambda **_kwargs: status)
    return status


def _set_runtime(monkeypatch, state: RuntimeState, pid: int | None = 69943):
    import penpal.health.check as check

    issues = () if state in {RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.STARTING} else (
        Issue("daemon", "error", "后台服务未运行。", "The background service is not running.", "penpal start"),
    )
    status = RuntimeStatus(state, issues=issues, pid=pid)
    monkeypatch.setattr(check, "evaluate", lambda **_kwargs: status)
    return status


def _utc_iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


def _make_config(db_path: Path, vault_path: Path, **watcher_flags):
    watchers = {
        "window": False,
        "idle": False,
        "clipboard": False,
        "manual": False,
        "browser": False,
        "ax_text": False,
        "ocr": False,
    }
    watchers.update(watcher_flags)
    return type(
        "Cfg",
        (),
        {
            "db_path_expanded": db_path,
            "obsidian": type("ObsidianCfg", (), {"vault_path": str(vault_path)})(),
            "watchers": type("WatchersCfg", (), watchers)(),
        },
    )()


def _seed_event(db_path: Path, *, source: str, event_type: str, ts_start: str, speaker: str = "system") -> None:
    init_db(db_path)
    insert_raw_event(
        RawEvent(
            source=source,
            event_type=event_type,
            ts_start=ts_start,
            speaker=speaker,
        )
    )


def test_healthcheck_reports_alive_daemon_and_writes_atomic_json(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    daily_dir = vault_path / "Daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-04-22.md").write_text("daily note")
    config = _make_config(db_path, vault_path, window=True)
    out_path = tmp_path / "health.json"

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()

    assert result["overall"] in {"ok", "alert"}
    assert result["daemon"]["alive"] is True
    assert result["daemon"]["pid"] == 69943
    assert result["daemon"]["state"] == "running"
    assert result["integrity"]["key_watchers_enabled"] == ["window"]
    assert "product_status" in result
    assert out_path.exists()
    assert json.loads(out_path.read_text()) == result
    assert not out_path.with_name(out_path.name + ".tmp").exists()
    assert not (out_path.parent / "alerts.json").exists()


def test_healthcheck_reports_shared_not_running_state(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    config = _make_config(db_path, vault_path, window=True)
    out_path = tmp_path / "health.json"

    _set_runtime(monkeypatch, RuntimeState.NOT_RUNNING, None)
    monkeypatch.setattr("penpal.health.check.Config.load", lambda: config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()

    assert result["daemon"]["alive"] is False
    assert result["daemon"]["state"] == "not_running"
    assert any(alert["code"] == "not_running" for alert in result["alerts"])


def test_healthcheck_does_not_treat_quiet_event_stream_as_daemon_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    daily_dir = vault_path / "Daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-04-22.md").write_text("daily note")
    config = _make_config(db_path, vault_path, window=True)
    out_path = tmp_path / "health.json"
    stale_ts = _utc_iso(timedelta(hours=30))

    _seed_event(db_path, source="window", event_type="window_focus", ts_start=stale_ts)

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()

    assert result["daemon"]["alive"] is True
    assert result["daemon"]["state"] == "running"
    assert not any(alert["code"] in {"STALE_EVENT_STREAM", "not_running", "hung"} for alert in result["alerts"])


def test_healthcheck_flags_speaker_mislabel_spike_and_ignores_low_ratio(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    daily_dir = vault_path / "Daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-04-22.md").write_text("daily note")
    out_path = tmp_path / "health.json"

    high_ratio_config = _make_config(db_path, vault_path, window=True)

    for index in range(10):
        _seed_event(
            db_path,
            source="clipboard",
            event_type="clipboard_copy",
            ts_start=_utc_iso(timedelta(minutes=1, seconds=index)),
            speaker="system" if index < 3 else "user",
        )

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: high_ratio_config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()
    assert result["integrity"]["speaker_ratio_system_in_user_source_last_1h"] > 0.10
    assert any(alert["code"] == "SPEAKER_MISLABEL_SPIKE" for alert in result["alerts"])

    close()
    low_ratio_db = tmp_path / "low-ratio.db"
    low_ratio_config = _make_config(low_ratio_db, vault_path, window=True)
    low_out = tmp_path / "health-low.json"

    for index in range(50):
        _seed_event(
            low_ratio_db,
            source="clipboard",
            event_type="clipboard_copy",
            ts_start=_utc_iso(timedelta(minutes=1, seconds=index)),
            speaker="system" if index == 0 else "user",
        )

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: low_ratio_config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", low_out)

    low_result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()
    assert low_result["integrity"]["speaker_ratio_system_in_user_source_last_1h"] == 0.02
    assert not any(alert["code"] == "SPEAKER_MISLABEL_SPIKE" for alert in low_result["alerts"])


def test_healthcheck_reports_config_load_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    _make_config(db_path, vault_path, window=True)
    out_path = tmp_path / "health.json"

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: (_ for _ in ()).throw(RuntimeError("bad config")))
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()

    assert result["integrity"]["config_loadable"] is False
    assert any(alert["code"] == "CONFIG_LOAD_FAILED" for alert in result["alerts"])


def test_healthcheck_flags_missing_watchers(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    config = _make_config(db_path, vault_path)
    out_path = tmp_path / "health.json"

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()

    assert result["integrity"]["key_watchers_enabled"] == []
    assert any(alert["code"] == "CRITICAL_WATCHERS_DISABLED" for alert in result["alerts"])


def test_healthcheck_cli_exit_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "penpal.health.run_healthcheck",
        lambda config_path=None: {
            "schema_version": 1,
            "checked_at": "2026-04-22T10:50:00+00:00",
            "overall": "alert",
            "daemon": {"alive": True, "pid": 1, "state": "running"},
            "integrity": {"schema_version": 11, "config_loadable": True, "key_watchers_enabled": ["window"], "speaker_ratio_system_in_user_source_last_1h": 0.02},
            "sync": {"last_daily_sync_at": None, "last_daily_file_date": None},
            "alerts": [{"severity": "warn", "code": "ONLY_WARN", "message": "warn"}],
        },
    )

    result = CliRunner().invoke(main, ["healthcheck"])
    assert result.exit_code == 0

    monkeypatch.setattr(
        "penpal.health.run_healthcheck",
        lambda config_path=None: {
            "schema_version": 1,
            "checked_at": "2026-04-22T10:50:00+00:00",
            "overall": "alert",
            "daemon": {"alive": False, "pid": None, "state": "not_running"},
            "integrity": {"schema_version": 11, "config_loadable": False, "key_watchers_enabled": [], "speaker_ratio_system_in_user_source_last_1h": 0.0},
            "sync": {"last_daily_sync_at": None, "last_daily_file_date": None},
            "alerts": [{"severity": "error", "code": "BAD", "message": "bad"}],
        },
    )

    result = CliRunner().invoke(main, ["healthcheck"])
    assert result.exit_code == 1


def test_healthcheck_reports_delivery_alerts_without_self_heal_state(monkeypatch, tmp_path):
    db_path = tmp_path / "penpal.db"
    vault_path = tmp_path / "vault"
    config = _make_config(db_path, vault_path, window=True)
    out_path = tmp_path / "health.json"

    monkeypatch.setattr("penpal.health.check.Config.load", lambda: config)
    monkeypatch.setattr("penpal.health.check.HEALTH_JSON_PATH", out_path)

    monkeypatch.setattr(
        "penpal.health.check.evaluate_delivery_health",
        lambda **_kwargs: DeliveryHealth(
            alerts=[render_alert(level="critical", source="daily", message="今日日报未生成", suggested_action="补跑")],
            watcher_counts={"keyboard_chunk": 0, "clipboard": 0},
            degraded=True,
        ),
    )
    result = __import__("penpal.health.check", fromlist=["run_healthcheck"]).run_healthcheck()

    assert any(alert["code"] == "PRODUCT_DAILY" for alert in result["alerts"])
    assert "self_heal" not in result
