"""Pure runtime identity data, code fingerprinting, and injectable change watcher."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import time
from dataclasses import dataclass
from typing import Callable


RELOAD_EXIT_CODE = 75
IDENTITY_CHECK_INTERVAL_SEC = 60.0
TEST_FINGERPRINT_ROOT_ENV = "PENPAL_TEST_FINGERPRINT_ROOT"
TEST_IDENTITY_INTERVAL_ENV = "PENPAL_TEST_IDENTITY_INTERVAL_SEC"


@dataclass(frozen=True)
class RuntimeIdentity:
    code_fingerprint: str
    package_version: str
    config_fingerprint: str
    database_schema_version: int | None
    supported_schema_version: int

    @property
    def schema_is_newer_than_code(self) -> bool:
        return (
            self.database_schema_version is not None
            and self.database_schema_version > self.supported_schema_version
        )


def _test_root() -> Path | None:
    test_root = os.environ.get(TEST_FINGERPRINT_ROOT_ENV)
    if os.environ.get("PENPAL_ISOLATED") == "1" and test_root:
        return Path(test_root).resolve()
    return None


def _loaded_package_root() -> Path:
    return _test_root() or Path(__file__).resolve().parents[1]


def installed_package_root() -> Path:
    """The penpal package the stable entry would load if exec'd now. A Homebrew upgrade installs a new keg
    and repoints the entry, and may keep the old keg (HOMEBREW_NO_INSTALL_CLEANUP), so the keg this process was
    loaded from is not the installed code. Wrapper scripts and editable installs fall back to the loaded root."""
    test_root = _test_root()
    if test_root is not None:
        return test_root
    try:
        from penpal.foundation.runtime_plist import resolve_penpal_executable

        entry = resolve_penpal_executable()
        real = entry.resolve()
        if real.parent.name == "bin":
            venv = real.parent.parent
            if (venv / "pyvenv.cfg").is_file():
                candidates = [
                    p.parent
                    for p in venv.glob("lib/python3*/site-packages/penpal/__init__.py")
                    if p.is_file()
                ]
                if len(candidates) == 1:
                    return candidates[0].resolve()
    except (OSError, ValueError, RuntimeError):
        pass
    return _loaded_package_root()



def _fingerprint(package: Path | None = None) -> str:
    package = package or installed_package_root()
    digest = hashlib.sha256()
    try:
        paths = sorted(package.rglob("*.py"))
    except OSError:
        paths = []
    for path in paths:
        try:
            relative = path.relative_to(package).as_posix()
        except ValueError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = b"<missing>"
        digest.update(content)
        digest.update(b"\n")
    return digest.hexdigest()


def _read_package_version(package: Path) -> str:
    source = ast.parse((package / "__init__.py").read_text(encoding="utf-8"))
    for statement in source.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.target.id == "__version__" and statement.value is not None:
                value = ast.literal_eval(statement.value)
                if isinstance(value, str):
                    return value
    raise ValueError("package version is not a string literal")


_LOADED_CODE_FINGERPRINT = _fingerprint(_loaded_package_root())
_LOADED_PACKAGE_VERSION = _read_package_version(Path(__file__).resolve().parents[1])


def code_fingerprint() -> str:
    """Fingerprint the code loaded by this process when its start identity is recorded."""
    return _LOADED_CODE_FINGERPRINT


def installed_fingerprint() -> str:
    """Recompute the on-disk identity so long-running processes can detect upgrades."""
    return _fingerprint(installed_package_root())


def loaded_package_version() -> str:
    """Return the package version captured from this process's loaded source."""
    return _LOADED_PACKAGE_VERSION


def installed_package_version() -> str:
    """Read the installed package version, falling back if an upgrade is mid-write."""
    try:
        return _read_package_version(installed_package_root())
    except (OSError, SyntaxError, ValueError):
        return _LOADED_PACKAGE_VERSION


def make_identity(
    *,
    code_fingerprint: str,
    package_version: str,
    config_fingerprint: str,
    database_schema_version: int | None,
    supported_schema_version: int,
) -> RuntimeIdentity:
    return RuntimeIdentity(
        code_fingerprint=code_fingerprint,
        package_version=package_version,
        config_fingerprint=config_fingerprint,
        database_schema_version=database_schema_version,
        supported_schema_version=supported_schema_version,
    )


class IdentityWatcher:
    """Compare one start identity with the installed identity on an injected monotonic clock."""

    def __init__(
        self,
        started: RuntimeIdentity,
        reader: Callable[[], RuntimeIdentity],
        *,
        interval_sec: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        test_interval = os.environ.get(TEST_IDENTITY_INTERVAL_ENV)
        if interval_sec is None and os.environ.get("PENPAL_ISOLATED") == "1" and test_interval:
            try:
                interval_sec = float(test_interval)
            except ValueError:
                interval_sec = None
        self.started = started
        self.reader = reader
        self.interval_sec = max(0.05, interval_sec or IDENTITY_CHECK_INTERVAL_SEC)
        self.clock = clock
        self._next_check = clock() + self.interval_sec
        self.last_identity: RuntimeIdentity | None = started

    def seconds_until_check(self) -> float:
        return max(0.0, self._next_check - self.clock())

    def check(self) -> bool:
        now = self.clock()
        if now < self._next_check:
            return False
        self._next_check = now + self.interval_sec
        current = self.reader()
        self.last_identity = current
        return current != self.started
