from __future__ import annotations

import sqlite3

import pytest

from penpal.config import config_fingerprint
from penpal.foundation.runtime_identity import (
    IdentityWatcher,
    RuntimeIdentity,
    TEST_FINGERPRINT_ROOT_ENV,
    installed_fingerprint,
    installed_package_version,
)
from penpal.foundation.runtime_record import _record_from_dict
from penpal.store.db import DatabaseSchemaTooNewError, close, get_conn, init_db, read_schema_version
from penpal.store.migrations import SCHEMA_VERSION


def _identity(**overrides) -> RuntimeIdentity:
    values = {
        "code_fingerprint": "code-a",
        "package_version": "1.0",
        "config_fingerprint": "config-a",
        "database_schema_version": SCHEMA_VERSION,
        "supported_schema_version": SCHEMA_VERSION,
    }
    values.update(overrides)
    return RuntimeIdentity(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("code_fingerprint", "code-b"),
        ("package_version", "1.1"),
        ("config_fingerprint", "config-b"),
        ("database_schema_version", SCHEMA_VERSION + 1),
    ],
)
def test_identity_watcher_detects_code_config_and_schema_changes(field, value):
    clock = [0.0]
    current = [_identity()]
    watcher = IdentityWatcher(_identity(), lambda: current[0], interval_sec=5, clock=lambda: clock[0])

    assert watcher.check() is False
    clock[0] = 5.0
    current[0] = _identity(**{field: value})
    assert watcher.check() is True


def test_installed_fingerprint_tracks_test_package_contents(monkeypatch, tmp_path):
    package = tmp_path / "installed" / "penpal"
    package.mkdir(parents=True)
    source = package / "entry.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    monkeypatch.setenv(TEST_FINGERPRINT_ROOT_ENV, str(package))

    before = installed_fingerprint()
    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert installed_fingerprint() != before


def test_installed_package_version_reads_installed_source(monkeypatch, tmp_path):
    package = tmp_path / "installed" / "penpal"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    monkeypatch.setenv(TEST_FINGERPRINT_ROOT_ENV, str(package))

    assert installed_package_version() == "0.1.0"
    source.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    assert installed_package_version() == "0.2.0"


def test_config_fingerprint_hashes_exact_config_content(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[privacy]\nredact_emails = true\n", encoding="utf-8")
    before = config_fingerprint(path)
    path.write_text("[privacy]\nredact_emails = false\n", encoding="utf-8")
    assert config_fingerprint(path) != before


def test_schema_reader_and_store_reject_newer_schema_without_migrating(tmp_path):
    path = tmp_path / "newer.db"
    init_db(path)
    close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO _schema_version VALUES (?, 'synthetic')", (SCHEMA_VERSION + 1,))
    conn.commit()
    conn.close()

    assert read_schema_version(path) == SCHEMA_VERSION + 1
    with pytest.raises(DatabaseSchemaTooNewError):
        init_db(path)
    with pytest.raises(DatabaseSchemaTooNewError):
        get_conn()
    close()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == SCHEMA_VERSION + 1
    conn.close()


def test_runtime_record_reader_tolerates_pr1_format_for_one_release():
    legacy = {
        "generation": "g1", "pid": 10, "process_start_time": "start", "version": "1.0",
        "code_fingerprint": "code-a", "started_at": "start", "written_at": "now",
        "progress": {}, "components": {},
    }
    record = _record_from_dict(legacy)
    assert record is not None
    assert record.record_format_version == 1
    assert record.config_fingerprint == ""
    assert record.database_schema_version == 0
