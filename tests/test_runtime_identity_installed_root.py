from __future__ import annotations

from pathlib import Path
import pytest

from penpal.foundation.runtime_identity import (
    TEST_FINGERPRINT_ROOT_ENV,
    _fingerprint,
    code_fingerprint,
    installed_fingerprint,
    installed_package_version,
    loaded_package_version,
)


def _setup_keg(keg_dir: Path, version: str, marker_val: str) -> Path:
    libexec = keg_dir / "libexec"
    (libexec / "bin").mkdir(parents=True)
    (libexec / "bin" / "penpal").write_text("#!/bin/sh\n", encoding="utf-8")
    (libexec / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    pkg = libexec / "lib" / "python3.13" / "site-packages" / "penpal"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (pkg / "x.py").write_text(f'X = "{marker_val}"\n', encoding="utf-8")
    return pkg


def test_homebrew_layout_follows_stable_entry_and_repointing(monkeypatch, tmp_path):
    keg_old = tmp_path / "Cellar" / "penpal" / "0.3.0"
    keg_new = tmp_path / "Cellar" / "penpal" / "0.4.0"
    pkg_old = _setup_keg(keg_old, "0.3.0", "old")
    pkg_new = _setup_keg(keg_new, "0.4.0", "new")

    stable_dir = tmp_path / "opt" / "homebrew" / "bin"
    stable_dir.mkdir(parents=True)
    stable = stable_dir / "penpal"
    stable.symlink_to(keg_new / "libexec" / "bin" / "penpal")

    monkeypatch.setenv("PENPAL_EXECUTABLE", str(stable))
    monkeypatch.delenv(TEST_FINGERPRINT_ROOT_ENV, raising=False)

    # installed_* reflects the NEW keg even though both kegs exist
    assert installed_package_version() == "0.4.0"
    assert installed_fingerprint() == _fingerprint(pkg_new)

    # Repointing the symlink to the old keg flips them back
    stable.unlink()
    stable.symlink_to(keg_old / "libexec" / "bin" / "penpal")

    assert installed_package_version() == "0.3.0"
    assert installed_fingerprint() == _fingerprint(pkg_old)


def test_wrapper_editable_layout_falls_back_to_loaded_root(monkeypatch, tmp_path):
    wrapper = tmp_path / "local" / "bin" / "penpal"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\nexec ~/.penpal/venv/bin/penpal "$@"\n', encoding="utf-8")

    monkeypatch.setenv("PENPAL_EXECUTABLE", str(wrapper))
    monkeypatch.delenv(TEST_FINGERPRINT_ROOT_ENV, raising=False)

    assert installed_fingerprint() == code_fingerprint()
    assert installed_package_version() == loaded_package_version()


def test_missing_entry_falls_back_without_raising(monkeypatch, tmp_path):
    missing = tmp_path / "bin" / "missing_penpal"
    monkeypatch.setenv("PENPAL_EXECUTABLE", str(missing))
    monkeypatch.delenv(TEST_FINGERPRINT_ROOT_ENV, raising=False)

    assert installed_fingerprint() == code_fingerprint()
    assert installed_package_version() == loaded_package_version()


def test_fingerprint_file_deleted_between_listing_and_reading_does_not_raise(tmp_path, monkeypatch):
    pkg = tmp_path / "penpal"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "0.9.0"\n', encoding="utf-8")
    vanishing = pkg / "vanishing.py"
    vanishing.write_text("VALUE = 42\n", encoding="utf-8")

    intact_fp = _fingerprint(pkg)

    orig_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:
        if self.resolve() == vanishing.resolve():
            vanishing.unlink(missing_ok=True)
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{self}'")
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    # Must not raise FileNotFoundError, and should be deterministically different
    fp_vanished = _fingerprint(pkg)
    assert isinstance(fp_vanished, str)
    assert len(fp_vanished) == 64
    assert fp_vanished != intact_fp


def test_isolated_test_override_takes_precedence_first(monkeypatch, tmp_path):
    keg = tmp_path / "Cellar" / "penpal" / "0.4.0"
    _setup_keg(keg, "0.4.0", "keg")
    stable = tmp_path / "bin" / "penpal"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(keg / "libexec" / "bin" / "penpal")

    override_pkg = tmp_path / "override" / "penpal"
    override_pkg.mkdir(parents=True)
    (override_pkg / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")

    monkeypatch.setenv("PENPAL_EXECUTABLE", str(stable))
    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    monkeypatch.setenv(TEST_FINGERPRINT_ROOT_ENV, str(override_pkg))

    assert installed_package_version() == "9.9.9"
    assert installed_fingerprint() == _fingerprint(override_pkg)
