"""Identity and path checks for proposed source recipes.

This module does not enable or execute recipes; callers must also validate parsing.
"""
from __future__ import annotations

import os
import json
import sqlite3
import plistlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from penpal.foundation.chat_apps import CHAT_APPS, chat_app_key
from penpal.sources.recipes.catalog import ToolRecipe, expand_vars_and_tilde

_GENERIC = frozenset({
    "app", "application", "applications", "data", "cache", "config", "configuration",
    "apple", "google", "microsoft", "system", "library", "local", "share", "support",
    "user", "users", "home", "desktop", "browser", "client", "server", "tool", "tools",
})
# Password managers keep credential vaults in their own directories, so their identity alone is refused.
_DENIED_IDENTITIES = frozenset({
    "1password", "bitwarden", "keepassxc", "keepassx", "dashlane", "enpass", "lastpass", "strongbox",
})
_SYSTEM_ROOTS = tuple(Path(item) for item in ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/System"))
_SENSITIVE = (
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config/gcloud", ".config/gh",
    ".netrc", ".npmrc", ".penpal", ".keypulse", ".claude", ".codex", ".cursor", ".memmy",
    "Library/Keychains", "Library/Mail", "Library/Messages", "Library/Photos",
    "Library/Calendars", "Library/AddressBook", "Library/Safari", "Library/Accounts",
    "Library/Application Support/AddressBook", "Library/Application Support/com.apple.sharedfilelist",
    "Library/Application Support/CallHistoryDB", "Library/Application Support/Knowledge",
    "Library/Application Support/MobileSync", "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox", "Library/Application Support/Chromium",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/BraveSoftware",
    "Library/Containers/com.apple.Notes", "Library/Containers/com.apple.mail",
    "Library/Containers/com.apple.iChat", "Library/Containers/com.apple.Safari",
    "Library/Group Containers/group.com.apple.notes", "Pictures/Photos Library.photoslibrary",
    ".mozilla", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
)


def _normalize(value: str) -> str:
    return re.sub(r"[-_\s]+", "", value).casefold()


def _valid_name(value: str) -> bool:
    normalized = _normalize(value)
    return (bool(re.fullmatch(r"[\w.]+", normalized)) and len(normalized) >= 3
            and normalized not in _GENERIC and normalized not in _DENIED_IDENTITIES)


@dataclass(frozen=True)
class Identity:
    names: tuple[str, ...]
    bundle_id: str | None = None


def find_identity(tool: ToolRecipe, env: Mapping[str, str] | None = None) -> Identity:
    lookup = os.environ if env is None else env
    home = Path(lookup.get("HOME") or str(Path.home()))
    for rule in tool.detect.any:
        if rule.cli and Path(rule.cli).name == rule.cli and _valid_name(rule.cli):
            executable = shutil.which(rule.cli, path=lookup.get("PATH", ""))
            if executable:
                resolved = Path(executable).resolve()
                if not any(resolved == root or resolved.is_relative_to(root) for root in _SYSTEM_ROOTS):
                    if chat_app_key(rule.cli) is None:
                        return Identity((rule.cli,))
        if rule.app_bundle and _valid_name(rule.app_bundle):
            for root in (Path("/Applications"), home / "Applications"):
                for app in root.glob("*.app"):
                    try:
                        if not app.resolve().is_relative_to(root.resolve()):
                            continue
                        with (app / "Contents" / "Info.plist").open("rb") as stream:
                            info = plistlib.load(stream)
                        bundle = info.get("CFBundleIdentifier")
                        name = info.get("CFBundleName")
                        if bundle != rule.app_bundle or not isinstance(name, str) or not _valid_name(name):
                            continue
                        if chat_app_key(name, bundle) is None:
                            return Identity((name, bundle), bundle)
                    except (OSError, ValueError, plistlib.InvalidFileException):
                        continue
    for rule in tool.detect.any:
        name = rule.cli
        if not isinstance(name, str) or Path(name).name != name or not _valid_name(name) or chat_app_key(name):
            continue
        prefixes = [Path(lookup.get("HOMEBREW_PREFIX", "/opt/homebrew")), Path("/usr/local")]
        for prefix in prefixes:
            cellar = prefix / "Cellar" / name
            if cellar.is_dir() and any(p.is_dir() for p in cellar.iterdir()):
                return Identity((name,))
        npm_root = Path(lookup.get("NPM_CONFIG_PREFIX", str(home / ".npm-global"))) / "lib" / "node_modules" / name
        pipx_root = Path(lookup.get("PIPX_HOME", str(home / ".local/share/pipx"))) / "venvs" / name
        for metadata in (npm_root / "package.json", pipx_root / "pipx_metadata.json"):
            try:
                with metadata.open(encoding="utf-8") as stream:
                    data = json.load(stream)
                package = data.get("name") if metadata.name == "package.json" else data.get("main_package", {}).get("package")
                if isinstance(package, str) and _normalize(package) == _normalize(name):
                    return Identity((name,))
            except (OSError, ValueError, AttributeError):
                continue
    raise ValueError("installation identity not verified or generic identity rejected")


def check_path(path: Path, identity: Identity, home: Path) -> None:
    resolved = path.resolve(strict=False)
    home = home.resolve(strict=False)
    normalized_parts = tuple(_normalize(part) for part in resolved.parts)
    for relative in _SENSITIVE:
        denied = home / relative
        denied_parts = tuple(_normalize(part) for part in denied.parts)
        if normalized_parts[:len(denied_parts)] == denied_parts:
            raise ValueError("sensitive path rejected")
    chat_names = {_normalize(value) for app in CHAT_APPS for value in (app.key, *app.names, *app.bundle_ids)}
    if any(part in chat_names for part in normalized_parts):
        raise ValueError("sensitive chat app path rejected")
    for name in identity.names:
        if not _valid_name(name):
            raise ValueError("generic identity rejected")
    try:
        relative_parts = resolved.relative_to(home).parts
    except ValueError as exc:
        raise ValueError("path outside identity-derived roots") from exc
    names = {_normalize(name) for name in identity.names}
    prefixes = ((".config",), (".local", "share"), ("Library", "Application Support"))
    if relative_parts and relative_parts[0].startswith(".") and _normalize(relative_parts[0][1:]) in names:
        return
    for prefix in prefixes:
        if len(relative_parts) > len(prefix) and tuple(map(_normalize, relative_parts[:len(prefix)])) == tuple(map(_normalize, prefix)):
            if _normalize(relative_parts[len(prefix)]) in names:
                return
    if identity.bundle_id and len(relative_parts) >= 3:
        if tuple(map(_normalize, relative_parts[:2])) == ("library", "containers"):
            if _normalize(relative_parts[2]) == _normalize(identity.bundle_id):
                return
    raise ValueError("path outside identity-derived roots")


def check_recipe_paths(tool: ToolRecipe, identity: Identity, env: Mapping[str, str] | None = None) -> None:
    lookup = os.environ if env is None else env
    home = Path(lookup.get("HOME") or str(Path.home()))
    for history in tool.history:
        expanded = expand_vars_and_tilde(history.glob, lookup)
        parts = Path(expanded).parts
        if any(any(char in part for char in "*?[") for part in parts[:-1]):
            raise ValueError("wildcards in directory components are not supported for user recipes")
        static = []
        for part in parts:
            if any(char in part for char in "*?["):
                break
            static.append(part)
        check_path(Path(*static), identity, home)


MAX_PROBE_BYTES = 10 * 1024 * 1024
MAX_PROBE_LINES = 10000


def validate_binding(tool: ToolRecipe, env: Mapping[str, str] | None = None) -> None:
    from penpal.sources.recipes.engine import resolve_history_files

    lookup = os.environ if env is None else env
    home = Path(lookup.get("HOME") or str(Path.home()))
    identity = find_identity(tool, lookup)
    check_recipe_paths(tool, identity, lookup)
    for history in tool.history:
        for path in resolve_history_files(history, lookup):
            check_path(path, identity, home)


def validate_recipe(tool: ToolRecipe, env: Mapping[str, str] | None = None) -> tuple[int, int]:
    from penpal.foundation.sqlite_rules import is_blocked_sqlite
    from penpal.sources.recipes.engine import RecipeEngine, resolve_history_files

    if tool.adapter or not tool.readable or not tool.history:
        raise ValueError("user recipe must contain readable histories and no adapter")
    validate_binding(tool, env)
    engine = RecipeEngine(env=env)
    files = 0
    turns = 0
    for history in tool.history:
        for path in resolve_history_files(history, env):
            files += 1
            extensions = {".sqlite", ".sqlite3", ".db"} if history.format == "sqlite" else {"." + history.format}
            if path.suffix.lower() not in extensions:
                raise ValueError("file type does not match recipe format")
            if path.stat().st_size > MAX_PROBE_BYTES:
                raise ValueError("probe file size limit exceeded")
            try:
                if history.format == "sqlite":
                    if is_blocked_sqlite(path)[0]:
                        raise ValueError("sensitive sqlite rejected")
                    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                        conn.execute("PRAGMA query_only=ON")
                        conn.row_factory = sqlite3.Row
                        query = history.query.strip().rstrip(";")
                        rows = conn.execute(f"SELECT * FROM ({query}) LIMIT {MAX_PROBE_LINES + 1}").fetchall()
                    if len(rows) > MAX_PROBE_LINES:
                        raise ValueError("probe row limit exceeded")
                    records = (dict(row) for row in rows)
                else:
                    with path.open("rb") as stream:
                        if sum(1 for _ in stream) > MAX_PROBE_LINES:
                            raise ValueError("probe line limit exceeded")
                    records = (record for _, _, record in engine._read_file_items(history, path, {}, True))
                for idx, record in enumerate(records, 1):
                    event, _, _, _ = engine._evaluate_record(tool, history, record, path.name, path.parent, path, idx)
                    if event and event.actor and event.intent:
                        turns += 1
            except (OSError, sqlite3.Error, UnicodeError) as exc:
                raise ValueError("probe could not read or parse file") from exc
    if not turns:
        raise ValueError("probe produced no session turn with role and content")
    return files, turns


def save_recipe(content: str, *, data_dir: Path | None = None, env: Mapping[str, str] | None = None, summary: dict | None = None) -> ToolRecipe:
    from penpal.sources.recipes.catalog import load_catalog, load_recipe_from_string
    from penpal.utils.paths import get_data_dir

    tool = load_recipe_from_string(content)
    if any(t.id == tool.id and t.adapter for t in load_catalog()):
        raise ValueError("该工具由内置插件采集，格式变化随版本修复")
    files, turns = validate_recipe(tool, env)
    directory = (data_dir if data_dir is not None else get_data_dir()) / "catalog"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / (tool.id + ".toml")
    backup = directory / (tool.id + ".previous")
    import tempfile

    def write_atomic(path: Path, text: str) -> None:
        fd, name = tempfile.mkstemp(dir=directory, prefix=".recipe-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    if target.exists():
        write_atomic(backup, target.read_text(encoding="utf-8"))
    write_atomic(target, content)
    (directory / (tool.id + ".disabled")).unlink(missing_ok=True)
    if summary is not None:
        summary.update(id=tool.id, name=tool.name, matched_files=files, parsed_turns=turns)
    return tool


def rollback_recipe(tool_id: str, *, data_dir: Path | None = None, env: Mapping[str, str] | None = None) -> ToolRecipe:
    from penpal.sources.recipes.catalog import load_recipe_from_string
    from penpal.utils.paths import get_data_dir

    if not re.fullmatch(r"[a-z0-9_-]+", tool_id):
        raise ValueError("invalid recipe id")
    directory = (data_dir if data_dir is not None else get_data_dir()) / "catalog"
    backup = directory / (tool_id + ".previous")
    content = backup.read_text(encoding="utf-8")
    if load_recipe_from_string(content).id != tool_id:
        raise ValueError("backup id mismatch")
    return save_recipe(content, data_dir=data_dir, env=env)
