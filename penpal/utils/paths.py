from __future__ import annotations

import errno
import os
import pwd
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MIGRATION_MARKER = "MIGRATED_TO_PENPAL"
LEGACY_DATA_DIRNAME = ".keypulse"
CONFIG_BACKUP_NAME = "config.toml.bak-pre-penpal"


class DataDirMigrationError(RuntimeError):
    """The old data directory cannot be moved safely to the Penpal location."""


def _guard_penpal_path(path: Path) -> Path:
    """Reject Penpal-owned paths in the real user home during isolated work."""
    if os.environ.get("PENPAL_ISOLATED") != "1":
        return path
    try:
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=False)
    except (KeyError, OSError) as exc:
        raise RuntimeError("PENPAL_ISOLATED=1 cannot verify the real user home") from exc

    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_relative_to(real_home):
        return path

    home_value = os.environ.get("HOME")
    isolated_home = Path(home_value).resolve(strict=False) if home_value else None
    if isolated_home is not None and isolated_home != real_home:
        try:
            real_home.relative_to(isolated_home)
        except ValueError:
            pass
        else:
            return path
    raise RuntimeError(f"PENPAL_ISOLATED=1 refuses Penpal path inside the real user home: {resolved}")


def _find_legacy_mcp_serve_processes() -> list[str]:
    """Find identifiable old ``keypulse mcp serve`` processes when pgrep exists."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", r"keypulse.*mcp serve"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"Warning: could not check for old KeyPulse MCP processes ({exc}); continuing.", file=sys.stderr)
        return []

    if result.returncode == 1:
        return []
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        print(
            f"Warning: pgrep could not check for old KeyPulse MCP processes{suffix}; continuing.",
            file=sys.stderr,
        )
        return []

    current_pid = os.getpid()
    processes = []
    for line in (result.stdout or "").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != current_pid:
            processes.append(f"keypulse mcp serve (PID {pid})")
    return processes


def _running_legacy_processes(old: Path) -> list[str]:
    return _find_legacy_mcp_serve_processes()


def is_migration_marker_dir(old: Path, new: Path) -> bool:
    if old.is_symlink():
        return False
    try:
        children = list(old.iterdir())
        if (len(children) != 1 or children[0].name != MIGRATION_MARKER
                or children[0].is_symlink() or not children[0].is_file()):
            return False
        lines = children[0].read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    return any(line.startswith("migrated_at=") for line in lines) and f"new_path={new}" in lines


def migrate_legacy_data_dir(home: Path | None = None) -> bool:
    """Atomically move ``~/.keypulse`` to ``~/.penpal`` once.

    Returns True only when this call moved the directory. It never copies, merges, or
    replaces data. If marker creation fails, it attempts to put the moved directory
    back at the original path before reporting the error.
    """
    home = home if home is not None else Path.home()
    old = _guard_penpal_path(home / ".keypulse")
    new = _guard_penpal_path(home / ".penpal")

    if new.is_symlink():
        raise DataDirMigrationError(f"Cannot use {new}: expected a real Penpal data directory, not a symbolic link.")
    if new.exists() and not new.is_dir():
        raise DataDirMigrationError(f"Cannot use {new}: expected a Penpal data directory.")

    if new.exists():
        if not old.exists() and not old.is_symlink():
            return False
        if is_migration_marker_dir(old, new):
            return False
        raise DataDirMigrationError(
            f"Both {old} and {new} exist, and {old} is not Penpal's migration marker directory. "
            "No files were changed. Resolve the directory conflict manually, then retry."
        )

    if old.is_symlink():
        raise DataDirMigrationError(f"Cannot migrate {old}: expected a real directory, not a symbolic link.")
    if not old.exists():
        return False
    if is_migration_marker_dir(old, new):
        raise DataDirMigrationError(
            f"{old} contains a Penpal migration marker, but the destination {new} is missing. "
            "Restore the data directory or remove the marker only after confirming the data is safe."
        )
    if not old.is_dir():
        raise DataDirMigrationError(f"Cannot migrate {old}: expected a real directory.")

    running_processes = _running_legacy_processes(old)
    if running_processes:
        details = ", ".join(running_processes)
        raise DataDirMigrationError(
            f"Cannot migrate {old} while an old KeyPulse process is running ({details}). "
            "Stop the old service and exit AI clients using KeyPulse MCP, then retry; "
            "or run `bash install.sh` to unload old launchd services before migration."
        )

    try:
        os.rename(old, new)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise DataDirMigrationError(
                f"Cannot migrate {old} to {new}: they are on different filesystems. "
                "Penpal requires an atomic same-filesystem rename and will not copy the data."
            ) from exc
        raise DataDirMigrationError(f"Could not atomically rename {old} to {new}: {exc}") from exc

    marker_dir_created = False
    try:
        old.mkdir()
        marker_dir_created = True
        marker = old / MIGRATION_MARKER
        marker.write_text(
            f"migrated_at={datetime.now(timezone.utc).isoformat()}\nnew_path={new}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        if marker_dir_created:
            try:
                marker = old / MIGRATION_MARKER
                if marker.exists():
                    marker.unlink()
                old.rmdir()
                os.rename(new, old)
            except Exception as rollback_exc:
                raise DataDirMigrationError(
                    f"Migration marker creation failed ({exc}); automatic rollback also failed "
                    f"({rollback_exc}). The data remains at {new}; do not delete either directory."
                ) from exc
        elif not old.exists():
            try:
                os.rename(new, old)
            except Exception as rollback_exc:
                raise DataDirMigrationError(
                    f"Migration marker creation failed ({exc}); automatic rollback also failed "
                    f"({rollback_exc}). The data remains at {new}; do not delete either directory."
                ) from exc
        else:
            raise DataDirMigrationError(
                f"The data moved to {new}, but {old} became occupied before the migration marker "
                "could be created. Neither directory was overwritten; resolve this conflict manually."
            ) from exc
        raise DataDirMigrationError(
            f"Migration marker creation failed ({exc}); the data directory was rolled back to {old}."
        ) from exc

    return True


def resolve_legacy_data_path(value: str, home: Path | None = None) -> str:
    """Map a path under the legacy ``~/.keypulse`` directory to the same place under ``~/.penpal``.

    Handles ``~/.keypulse/...`` and absolute ``<home>/.keypulse/...``. Any other value is returned unchanged.
    Applied when config is loaded, so a config that still names the legacy directory keeps working.
    """
    home = home if home is not None else Path.home()
    tilde_old = f"~/{LEGACY_DATA_DIRNAME}"
    if value == tilde_old or value.startswith(tilde_old + "/"):
        return "~/.penpal" + value[len(tilde_old):]
    absolute_old = str(_guard_penpal_path(home / LEGACY_DATA_DIRNAME))
    if value == absolute_old or value.startswith(absolute_old + "/"):
        return str(_guard_penpal_path(home / ".penpal")) + value[len(absolute_old):]
    return value


def get_data_dir() -> Path:
    d = _guard_penpal_path(Path.home() / ".penpal")
    moved = migrate_legacy_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    if moved:
        print(
            "Penpal moved ~/.keypulse to ~/.penpal. The moved developer venv contains scripts "
            "with an obsolete absolute path; run `bash install.sh --dev` to rebuild it.",
            file=sys.stderr,
        )
    try:
        from penpal.config import rewrite_legacy_config_paths

        rewrite_legacy_config_paths(d)
    except Exception as exc:
        print(f"Warning: could not update legacy paths in {d / 'config.toml'} ({exc}); continuing.", file=sys.stderr)
    return _guard_penpal_path(d)


def get_codex_home() -> Path:
    """Return the current user's Codex data directory."""
    return Path.home() / ".codex"


def get_db_path() -> Path:
    current = _guard_penpal_path(get_data_dir() / "penpal.db")
    legacy = current.with_name("keypulse.db")
    return _guard_penpal_path(legacy if not current.exists() and legacy.exists() else current)


def get_pid_path() -> Path:
    return _guard_penpal_path(get_data_dir() / "penpal.pid")


def get_hud_pid_path() -> Path:
    return _guard_penpal_path(get_data_dir() / "penpal-hud.pid")


def get_daemon_lock_path() -> Path:
    return _guard_penpal_path(get_run_dir() / "daemon.lock")


def get_hud_lock_path() -> Path:
    return _guard_penpal_path(get_run_dir() / "hud.lock")


def get_run_dir() -> Path:
    return _guard_penpal_path(Path.home() / ".penpal" / "run")


def get_daemon_record_path() -> Path:
    return _guard_penpal_path(get_run_dir() / "daemon.json")


def get_hud_record_path() -> Path:
    return _guard_penpal_path(get_run_dir() / "hud.json")


def get_launchd_daemon_plist_path() -> Path:
    return _guard_penpal_path(get_launchd_plist_path("com.penpal.daemon"))


def get_launchd_agents_dir() -> Path:
    return _guard_penpal_path(Path.home() / "Library" / "LaunchAgents")


def get_launchd_plist_path(label: str) -> Path:
    return _guard_penpal_path(get_launchd_agents_dir() / f"{label}.plist")


def get_logs_dir() -> Path:
    return _guard_penpal_path(get_data_dir() / "logs")


def get_log_path(configured_path: str | Path | None = None) -> Path:
    """Resolve the app log path and perform the one-time KeyPulse log rename."""
    data_dir = get_data_dir()
    current = _guard_penpal_path(data_dir / "penpal.log")
    if configured_path is not None:
        configured = _guard_penpal_path(Path(configured_path).expanduser())
        configured = Path(resolve_legacy_data_path(str(configured)))
        # Old config files can still name ~/.penpal/keypulse.log after their
        # directory path has been rewritten. Keep that migration here only.
        if configured.parent != data_dir or configured.name not in {"penpal.log", "keypulse.log"}:
            return configured

    legacy = _guard_penpal_path(data_dir / "keypulse.log")
    if not current.exists() and legacy.is_file() and not legacy.is_symlink():
        try:
            legacy.rename(current)
        except OSError as exc:
            # Logging can proceed at the canonical path; doctor will report the
            # legacy file if the one-time rename could not be completed.
            if not (current.exists() and not legacy.exists()):
                print(f"Warning: could not rename legacy log {legacy} to {current} ({exc}).", file=sys.stderr)
    return current


def get_config_path() -> Path:
    return _guard_penpal_path(get_data_dir() / "config.toml")


def get_health_report_path() -> Path:
    return _guard_penpal_path(Path.home() / ".penpal" / "health.json")


def get_launcher_stamp_path() -> Path:
    return _guard_penpal_path(get_run_dir() / "launcher.json")


def get_apps_dir() -> Path:
    return _guard_penpal_path(Path.home() / "Applications")


def get_legacy_venv_dir() -> Path:
    return _guard_penpal_path(Path.home() / ".keypulse" / "venv")


def get_ax_spike_dir() -> Path:
    return _guard_penpal_path(Path.home() / ".penpal" / "spike")


def get_wechat_authorization_path() -> Path:
    return _guard_penpal_path(Path.home() / ".penpal" / "wechat-authorized")
