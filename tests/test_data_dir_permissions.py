"""Penpal's data directory stays owner-only, including existing loose directories."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from penpal.utils import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    target = tmp_path / "home"
    target.mkdir()
    monkeypatch.setenv("HOME", str(target))
    monkeypatch.setattr(Path, "home", lambda: target)
    return target


def test_new_data_dir_is_created_owner_only(home):
    data_dir = paths.get_data_dir()

    assert data_dir == home / ".penpal"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


def test_existing_data_dir_and_sensitive_files_are_tightened(home):
    data_dir = home / ".penpal"
    data_dir.mkdir()
    data_dir.chmod(0o755)
    run_dir = data_dir / "run"
    run_dir.mkdir()
    run_dir.chmod(0o755)
    sensitive = [
        data_dir / "penpal.db",
        data_dir / "penpal.db-wal",
        data_dir / "penpal.db-shm",
        data_dir / "penpal.db.v19-20260302T000000000000Z.bak",
        data_dir / "health.json",
        run_dir / "daemon.json",
    ]
    for path in sensitive:
        path.write_text("synthetic value", encoding="utf-8")
        path.chmod(0o644)
    unrelated = data_dir / "notes.txt"
    unrelated.write_text("synthetic note", encoding="utf-8")
    unrelated.chmod(0o644)

    assert paths.get_data_dir() == data_dir

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    for path in sensitive:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644


def test_secure_data_dir_accepts_an_explicit_penpal_directory(home):
    data_dir = home / ".penpal"
    data_dir.mkdir()
    data_dir.chmod(0o755)
    database = data_dir / "penpal.db"
    database.write_text("synthetic value", encoding="utf-8")
    database.chmod(0o644)
    outside = home / "outside.db"
    outside.write_text("synthetic value", encoding="utf-8")
    outside.chmod(0o644)

    assert paths.secure_data_dir(data_dir) == data_dir
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
