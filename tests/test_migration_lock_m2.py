from __future__ import annotations

import importlib
import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from penpal.config import Config
from penpal.foundation.runtime_identity import RuntimeIdentity
from penpal.foundation.runtime_record import read_record
from penpal.store import db, migrations


EVALUATOR = importlib.import_module("penpal.runtime.evaluate")
APP = importlib.import_module("penpal.app")


@pytest.fixture(autouse=True)
def reset_db():
    previous_path = db._db_path
    db.close()
    db._db_path = None
    yield
    db.close()
    db._db_path = previous_path


def _make_old_db(path: Path, monkeypatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:-1])
        conn = sqlite3.connect(path)
        try:
            migrations.run_migrations(conn)
        finally:
            conn.close()


def _migration_worker(path, start, ready, results):
    from penpal.store.db import close, get_conn, init_db

    ready.put(True)
    if not start.wait(20):
        results.put("start timeout")
        return
    try:
        init_db(Path(path))
        conn = get_conn()
        results.put((conn.execute("PRAGMA foreign_keys").fetchone()[0],
                     conn.execute("PRAGMA busy_timeout").fetchone()[0]))
    except Exception as exc:
        results.put(type(exc).__name__)
    finally:
        close()


def _lock_worker(path, ready, release):
    from penpal.store.db import _migration_lock

    with _migration_lock(Path(path)):
        ready.set()
        release.wait(20)


def test_concurrent_upgrade_runs_each_migration_once(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    _make_old_db(path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    start, ready, results = ctx.Event(), ctx.Queue(), ctx.Queue()
    workers = [ctx.Process(target=_migration_worker, args=(str(path), start, ready, results))
               for _ in range(6)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20) is True
        start.set()
        assert [results.get(timeout=40) for _ in workers] == [(1, 15000)] * len(workers)
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
    finally:
        start.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        ready.close()
        results.close()

    with sqlite3.connect(path) as conn:
        versions = conn.execute("SELECT version, COUNT(*) FROM _schema_version GROUP BY version ORDER BY version").fetchall()
    assert versions == [(version, 1) for version in range(1, migrations.SCHEMA_VERSION + 1)]
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_latest_schema_initializes_connection_pragmas(tmp_path):
    path = tmp_path / "memory.db"
    db.init_db(path)
    db.close()
    db.init_db(path)
    conn = db.get_conn()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    assert not list(tmp_path.glob("*.bak"))


def test_get_conn_propagates_schema_database_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", tmp_path / "empty.db")
    with pytest.raises(sqlite3.OperationalError):
        db.get_conn()


@pytest.mark.parametrize("exists", [False, True])
def test_paused_does_not_create_files_or_migrate(tmp_path, monkeypatch, exists):
    path = tmp_path / "memory.db"
    if exists:
        _make_old_db(path, monkeypatch)
        conn = sqlite3.connect(path)
        conn.execute("INSERT OR REPLACE INTO app_state(key, value, updated_at) VALUES ('status', 'paused', 'test')")
        conn.commit()
        conn.close()
    monkeypatch.setattr(db, "_db_path", path)
    before_files = {entry.name for entry in tmp_path.iterdir()}
    assert EVALUATOR._paused() is exists
    assert {entry.name for entry in tmp_path.iterdir()} == before_files
    assert db.read_schema_version(path) == (migrations.SCHEMA_VERSION - 1 if exists else 0)


def test_paused_reads_uncheckpointed_wal_without_migrating(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    _make_old_db(path, monkeypatch)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT OR REPLACE INTO app_state(key, value, updated_at) VALUES ('status', 'paused', 'test')")
        conn.commit()
        monkeypatch.setattr(db, "_db_path", path)
        before_files = {entry.name for entry in tmp_path.iterdir()}
        assert EVALUATOR._paused() is True
        assert {entry.name for entry in tmp_path.iterdir()} == before_files
        assert db.read_schema_version(path) == migrations.SCHEMA_VERSION - 1
    finally:
        conn.close()


def test_migration_lock_times_out_when_other_process_holds_it(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_lock_worker, args=(str(path), ready, release))
    worker.start()
    try:
        assert ready.wait(20)
        monkeypatch.setattr(db, "MIGRATION_LOCK_TIMEOUT_SEC", 0.1)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="migration lock"):
            db.init_db(path)
        assert 0.1 <= time.monotonic() - started < 3
        assert not path.exists()
    finally:
        release.set()
        worker.join(timeout=20)
        if worker.is_alive():
            worker.terminate()
            worker.join()
    assert worker.exitcode == 0


def test_new_schema_daemon_suspends_and_evaluator_suggests_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE _schema_version (version INTEGER, applied_at TEXT)")
        conn.execute("INSERT INTO _schema_version VALUES (?, 'test')", (migrations.SCHEMA_VERSION + 1,))
    config = Config()
    config.app.db_path = str(path)
    monkeypatch.setattr(APP, "setup_logging", lambda *_args: None)
    monkeypatch.setattr(APP, "cap_launchd_output_logs", lambda: [])
    monkeypatch.setattr(APP.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(APP, "_reload_in_place", lambda: pytest.fail("unexpected reload"))
    sleep_calls = []
    original_sleep = APP._sleep_on_schema_too_new
    monkeypatch.setattr(APP, "_sleep_on_schema_too_new", lambda cfg, cfg_path, lock: original_sleep(
        cfg, cfg_path, lock, sleep=sleep_calls.append, max_iterations=2,
    ))

    assert APP.run(config) is None
    assert sleep_calls == [10, 10]
    record = read_record()
    assert record is not None
    assert record.database_schema_version == migrations.SCHEMA_VERSION + 1
    assert record.components["storage"].detail == (
        f"schema_too_new: recorded={migrations.SCHEMA_VERSION + 1} supported={migrations.SCHEMA_VERSION}"
    )
    identity = RuntimeIdentity("code", "1.0", "config", migrations.SCHEMA_VERSION + 1, migrations.SCHEMA_VERSION)
    status = EVALUATOR.evaluate(
        identity_reader=lambda: identity,
        record_reader=lambda: record,
        launchd_probe=lambda: {"loaded": True},
        hud_record_reader=lambda: None,
        wake_time_reader=lambda: None,
    )
    assert status.state is EVALUATOR.RuntimeState.SCHEMA_TOO_NEW
    assert status.issues[0].fix_command == "brew upgrade longfellow1/penpal/penpal"
    assert status.fix_command == status.issues[0].fix_command


def test_daemon_suspends_if_schema_changes_between_probe_and_lock(tmp_path, monkeypatch):
    config = Config()
    config.app.db_path = str(tmp_path / "memory.db")
    monkeypatch.setattr(APP, "setup_logging", lambda *_args: None)
    monkeypatch.setattr(APP, "cap_launchd_output_logs", lambda: [])
    monkeypatch.setattr(APP, "read_schema_version", lambda _path: 0)
    monkeypatch.setattr("penpal.permissions.trigger_required_prompts", lambda _keys: {})
    monkeypatch.setattr(APP, "init_db", lambda _path: (_ for _ in ()).throw(
        db.DatabaseSchemaTooNewError(migrations.SCHEMA_VERSION + 1)
    ))
    sleeps = []
    monkeypatch.setattr(APP, "_sleep_on_schema_too_new", lambda *_args: sleeps.append(True))
    assert APP.run(config) is None
    assert sleeps == [True]
    record = read_record()
    assert record is not None
    assert record.database_schema_version == migrations.SCHEMA_VERSION + 1


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_status_commands_show_upgrade_without_opening_new_schema(tmp_path, monkeypatch, command):
    from penpal import cli
    from penpal.cli import daemon, doctor

    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE _schema_version (version INTEGER, applied_at TEXT)")
        conn.execute("INSERT INTO _schema_version VALUES (?, 'test')", (migrations.SCHEMA_VERSION + 1,))
    identity = RuntimeIdentity("code", "1.0", "config", migrations.SCHEMA_VERSION + 1, migrations.SCHEMA_VERSION)
    runtime_status = EVALUATOR.evaluate(
        identity_reader=lambda: identity, record_reader=lambda: None,
        launchd_probe=lambda: {"loaded": True}, hud_record_reader=lambda: None,
        wake_time_reader=lambda: None,
    )
    monkeypatch.setattr(daemon, "evaluate", lambda: runtime_status)
    monkeypatch.setattr(doctor, "evaluate", lambda: runtime_status)
    monkeypatch.setattr(daemon, "require_db", lambda _cfg: pytest.fail("unexpected database initialization"))
    monkeypatch.setattr(doctor, "require_db", lambda _cfg: pytest.fail("unexpected database initialization"))
    before = {entry.name for entry in tmp_path.iterdir()}
    result = CliRunner().invoke(cli.main, [command, "--plain"])
    assert result.exit_code == 0, result.output
    assert "schema_too_new" in result.output
    assert "brew upgrade longfellow1/penpal/penpal" in result.output
    assert {entry.name for entry in tmp_path.iterdir()} == before


def test_healthcheck_reports_new_schema_without_opening_for_writes(tmp_path, monkeypatch):
    from penpal.health import check

    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE _schema_version (version INTEGER, applied_at TEXT)")
        conn.execute("INSERT INTO _schema_version VALUES (?, 'test')", (migrations.SCHEMA_VERSION + 1,))
    config = Config()
    config.app.db_path = str(path)
    identity = RuntimeIdentity("code", "1.0", "config", migrations.SCHEMA_VERSION + 1, migrations.SCHEMA_VERSION)
    runtime_status = EVALUATOR.evaluate(
        identity_reader=lambda: identity, record_reader=lambda: None,
        launchd_probe=lambda: {"loaded": True}, hud_record_reader=lambda: None,
        wake_time_reader=lambda: None,
    )
    monkeypatch.setattr(check, "_load_config", lambda _path: (config, True))
    monkeypatch.setattr(check, "evaluate", lambda **_kwargs: runtime_status)
    monkeypatch.setattr(check, "init_db", lambda _path: pytest.fail("unexpected database initialization"))
    monkeypatch.setattr(check, "run_watchdog", lambda *_args, **_kwargs: {"action": "report_only"})
    monkeypatch.setattr(check, "write_health_report", lambda *_args: None)
    monkeypatch.setattr("penpal.foundation.runtime_logs.cap_launchd_output_logs", lambda: [])
    result = check.run_healthcheck()
    assert result["daemon"]["state"] == "schema_too_new"
    assert result["overall"] == "alert"
    assert result["integrity"]["schema_version"] == migrations.SCHEMA_VERSION + 1
    assert result["product_status"]["watcher_emit_counts_24h"] is None
    assert any("请升级 Penpal" in alert["message"] for alert in result["alerts"])


def test_watchdog_does_not_restart_new_schema(tmp_path):
    from penpal.health.self_heal import run_watchdog

    status = EVALUATOR.RuntimeStatus(EVALUATOR.RuntimeState.SCHEMA_TOO_NEW, managed=True)
    result = run_watchdog(status, state_path=tmp_path / "watchdog.json",
                          restart_control=lambda *_args: pytest.fail("unexpected restart"))
    assert result["action"] == "report_only"


@pytest.mark.parametrize("language,expected", [("zh", "请升级 Penpal"), ("en", "please upgrade Penpal")])
def test_mcp_new_schema_exits_one_with_bilingual_upgrade_message(monkeypatch, language, expected):
    from penpal import i18n
    from penpal.mcp import cli

    monkeypatch.setattr(i18n, "_LANG_CACHE", language)
    monkeypatch.setattr(cli, "init_db", lambda _path: (_ for _ in ()).throw(
        db.DatabaseSchemaTooNewError(migrations.SCHEMA_VERSION + 1)
    ))
    result = CliRunner().invoke(cli.mcp_group, ["serve"])
    assert result.exit_code == 1
    assert expected in result.output
