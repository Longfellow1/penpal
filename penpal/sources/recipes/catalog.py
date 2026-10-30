"""Declarative tool recipe catalog loader and validator."""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import logging
logger = logging.getLogger(__name__)

_VALID_KINDS = {"agent", "ide", "assistant"}
_VALID_FORMATS = {"jsonl", "json", "sqlite"}
_VALID_COMPRESSIONS = {"auto", "none"}
_VALID_TIMESTAMPS = {"auto", "iso", "unix_s", "unix_ms", "file_mtime"}
_VALID_MCP_STYLES = {
    "json_mcpServers",
    "json_servers",
    "toml_mcp_servers",
    "yaml_mcp_servers",
    "manual",
    "none",
}
_ALLOWED_TOP_KEYS = {
    "id",
    "name",
    "kind",
    "readable",
    "unreadable_reason",
    "adapter",
    "detect",
    "history",
    "mcp",
}
_ALLOWED_DETECT_KEYS = {"any"}
_ALLOWED_DETECT_RULE_KEYS = {"app_bundle", "cli", "dir"}
_ALLOWED_HISTORY_KEYS = {
    "format",
    "glob",
    "compression",
    "records_path",
    "query",
    "fields",
    "role_map",
    "timestamp",
    "cursor",
    "keep_roles",
    "min_parse_rate",
}
_ALLOWED_FIELDS_KEYS = {
    "message_id",
    "conversation_id",
    "role",
    "content",
    "created_at",
    "workspace",
}
_REQUIRED_FIELDS = {"role", "content", "created_at"}
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class DetectRule:
    app_bundle: str | None = None
    cli: str | None = None
    dir: str | None = None


@dataclass
class DetectConfig:
    any: list[DetectRule] = field(default_factory=list)


@dataclass
class HistoryRecipe:
    format: str
    glob: str
    fields: dict[str, str]
    compression: str = "auto"
    records_path: str = ""
    query: str = ""
    role_map: dict[str, str] = field(default_factory=dict)
    timestamp: str = "auto"
    cursor: str = "file_offset"
    keep_roles: list[str] = field(default_factory=lambda: ["user", "assistant"])
    min_parse_rate: float = 0.9


@dataclass
class McpConfig:
    config: str
    style: str


@dataclass
class ToolRecipe:
    id: str
    name: str
    kind: str
    readable: bool = True
    unreadable_reason: str = ""
    adapter: str = ""
    detect: DetectConfig = field(default_factory=DetectConfig)
    history: list[HistoryRecipe] = field(default_factory=list)
    mcp: McpConfig | None = None


def expand_vars_and_tilde(text: str, env: Mapping[str, str] | None = None) -> str:
    """Expand ${VAR:-default} and ~ in paths using provided env or os.environ."""
    lookup = os.environ if env is None else env

    def _replace_var(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_val = match.group(2)
        val = lookup.get(var_name)
        if val is not None and val != "":
            return val
        if default_val is not None:
            return default_val
        return ""

    expanded = _VAR_PATTERN.sub(_replace_var, text)
    if expanded == "~" or expanded.startswith("~/"):
        home = lookup.get("HOME") or str(Path.home())
        if expanded == "~":
            return home
        return home + expanded[1:]
    return expanded


def _validate_keys(d: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(d.keys()) - allowed
    if unknown:
        raise ValueError(f"Unknown keys in {context}: {sorted(unknown)}")


def parse_recipe_dict(data: dict[str, Any], origin: str = "<memory>") -> ToolRecipe:
    _validate_keys(data, _ALLOWED_TOP_KEYS, f"{origin} top-level")

    tool_id = data.get("id")
    if not isinstance(tool_id, str) or not re.match(r"^[a-z0-9_-]+$", tool_id):
        raise ValueError(f"{origin}: 'id' must be a non-empty kebab/snake string, got {tool_id!r}")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{origin}: 'name' must be a non-empty string")

    kind = data.get("kind")
    if kind not in _VALID_KINDS:
        raise ValueError(f"{origin}: 'kind' must be one of {_VALID_KINDS}, got {kind!r}")

    readable = data.get("readable", True)
    if not isinstance(readable, bool):
        raise ValueError(f"{origin}: 'readable' must be a boolean")

    unreadable_reason = str(data.get("unreadable_reason", ""))
    if not readable and not unreadable_reason.strip():
        raise ValueError(f"{origin}: 'unreadable_reason' is required when readable=false")

    adapter = str(data.get("adapter", ""))

    # detect
    detect_raw = data.get("detect")
    detect_rules: list[DetectRule] = []
    if detect_raw is not None:
        if not isinstance(detect_raw, dict):
            raise ValueError(f"{origin}: 'detect' must be a table")
        _validate_keys(detect_raw, _ALLOWED_DETECT_KEYS, f"{origin} detect")
        any_raw = detect_raw.get("any")
        if not isinstance(any_raw, list):
            raise ValueError(f"{origin}: detect.any must be a list")
        for idx, rule_dict in enumerate(any_raw):
            if not isinstance(rule_dict, dict):
                raise ValueError(f"{origin}: detect.any[{idx}] must be a table")
            _validate_keys(rule_dict, _ALLOWED_DETECT_RULE_KEYS, f"{origin} detect.any[{idx}]")
            if not rule_dict:
                raise ValueError(f"{origin}: detect.any[{idx}] cannot be empty")
            detect_rules.append(
                DetectRule(
                    app_bundle=rule_dict.get("app_bundle"),
                    cli=rule_dict.get("cli"),
                    dir=rule_dict.get("dir"),
                )
            )

    # history
    history_raw = data.get("history")
    history_entries: list[HistoryRecipe] = []
    if history_raw is not None:
        if not isinstance(history_raw, list):
            raise ValueError(f"{origin}: 'history' must be an array of tables")
        for idx, h in enumerate(history_raw):
            if not isinstance(h, dict):
                raise ValueError(f"{origin}: history[{idx}] must be a table")
            _validate_keys(h, _ALLOWED_HISTORY_KEYS, f"{origin} history[{idx}]")

            fmt = h.get("format")
            if fmt not in _VALID_FORMATS:
                raise ValueError(f"{origin}: history[{idx}].format must be one of {_VALID_FORMATS}, got {fmt!r}")

            glob_val = h.get("glob")
            if not isinstance(glob_val, str) or not glob_val.strip():
                raise ValueError(f"{origin}: history[{idx}].glob must be a non-empty string")

            compression = h.get("compression", "auto")
            if compression not in _VALID_COMPRESSIONS:
                raise ValueError(f"{origin}: history[{idx}].compression must be one of {_VALID_COMPRESSIONS}")

            records_path = str(h.get("records_path", ""))
            if fmt != "json" and records_path != "":
                raise ValueError(f"{origin}: history[{idx}].records_path is only allowed for format='json'")

            query = str(h.get("query", ""))
            if fmt == "sqlite":
                if not query.strip():
                    raise ValueError(f"{origin}: history[{idx}].query must be non-empty for sqlite format")
                clean_q = query.strip()
                if not re.match(r"^SELECT\b", clean_q, re.IGNORECASE):
                    raise ValueError(f"{origin}: sqlite query must start with SELECT, got: {query!r}")
                # Strip single-quoted literals before checking for ';' (with '' escape)
                stripped_semicolon = re.sub(r"'(''|[^'])*'", "''", clean_q).rstrip().rstrip(";")
                if ";" in stripped_semicolon:
                    raise ValueError(f"{origin}: sqlite query must not contain multiple statements: {query!r}")
            elif query != "":
                raise ValueError(f"{origin}: history[{idx}].query is only allowed for format='sqlite'")

            ts = h.get("timestamp", "auto")
            if ts not in _VALID_TIMESTAMPS:
                raise ValueError(f"{origin}: history[{idx}].timestamp must be one of {_VALID_TIMESTAMPS}")

            fields = h.get("fields")
            if not isinstance(fields, dict):
                raise ValueError(f"{origin}: history[{idx}].fields must be a table")
            _validate_keys(fields, _ALLOWED_FIELDS_KEYS, f"{origin} history[{idx}].fields")
            req_fields = {"role", "content"} if ts == "file_mtime" else _REQUIRED_FIELDS
            missing_req = req_fields - set(fields.keys())
            if missing_req:
                raise ValueError(f"{origin}: history[{idx}].fields missing required keys: {sorted(missing_req)}")
            for rf in req_fields:
                rf_val = fields.get(rf)
                if not isinstance(rf_val, str) or not rf_val.strip():
                    raise ValueError(f"{origin}: history[{idx}].fields.{rf} must be a non-empty string")
            if fmt == "sqlite":
                if "message_id" not in fields:
                    raise ValueError(f"{origin}: history[{idx}].fields must include 'message_id' for sqlite format")
                if not str(fields.get("message_id", "")).strip():
                    raise ValueError(f"{origin}: history[{idx}].fields.message_id must be a non-empty string for sqlite format")

            # Drop empty string optional field values at load time
            cleaned_fields = {k: str(v) for k, v in fields.items() if str(v).strip() != ""}

            role_map = h.get("role_map", {})
            if not isinstance(role_map, dict):
                raise ValueError(f"{origin}: history[{idx}].role_map must be a table")

            cursor = str(h.get("cursor", "file_offset"))
            if cursor not in {"file_offset", "record_index"} and not cursor.startswith("sqlite_watermark:"):
                raise ValueError(f"{origin}: invalid cursor {cursor!r}")
            if cursor.startswith("sqlite_watermark:"):
                col_name = cursor.split(":", 1)[1].strip()
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col_name):
                    raise ValueError(f"{origin}: invalid sqlite watermark column identifier: {col_name!r}")

            keep_roles = h.get("keep_roles", ["user", "assistant"])
            if not isinstance(keep_roles, list) or not all(isinstance(r, str) for r in keep_roles):
                raise ValueError(f"{origin}: history[{idx}].keep_roles must be a list of strings")

            min_parse_rate = float(h.get("min_parse_rate", 0.9))
            if not (0.0 <= min_parse_rate <= 1.0):
                raise ValueError(f"{origin}: history[{idx}].min_parse_rate must be between 0.0 and 1.0")

            history_entries.append(
                HistoryRecipe(
                    format=fmt,
                    glob=glob_val,
                    fields=cleaned_fields,
                    compression=compression,
                    records_path=records_path,
                    query=query,
                    role_map={str(k): str(v) for k, v in role_map.items()},
                    timestamp=ts,
                    cursor=cursor,
                    keep_roles=[str(r) for r in keep_roles],
                    min_parse_rate=min_parse_rate,
                )
            )

    # mcp
    mcp_raw = data.get("mcp")
    mcp_config: McpConfig | None = None
    if mcp_raw is not None:
        if not isinstance(mcp_raw, dict):
            raise ValueError(f"{origin}: 'mcp' must be a table")
        _validate_keys(mcp_raw, {"config", "style"}, f"{origin} mcp")
        cfg_path = mcp_raw.get("config")
        style = mcp_raw.get("style")
        if not isinstance(cfg_path, str) or not cfg_path.strip():
            raise ValueError(f"{origin}: mcp.config must be a non-empty string")
        if style not in _VALID_MCP_STYLES:
            raise ValueError(f"{origin}: mcp.style must be one of {_VALID_MCP_STYLES}, got {style!r}")
        mcp_config = McpConfig(config=cfg_path, style=style)

    return ToolRecipe(
        id=tool_id,
        name=name,
        kind=kind,
        readable=readable,
        unreadable_reason=unreadable_reason,
        adapter=adapter,
        detect=DetectConfig(any=detect_rules),
        history=history_entries,
        mcp=mcp_config,
    )


def load_recipe_from_string(content: str, origin: str = "<string>") -> ToolRecipe:
    data = tomllib.loads(content)
    return parse_recipe_dict(data, origin=origin)


def load_recipe(path: Path) -> ToolRecipe:
    content = path.read_text(encoding="utf-8")
    return load_recipe_from_string(content, origin=str(path))


def get_default_catalog_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "catalog"


def load_catalog(catalog_dir: Path | None = None) -> list[ToolRecipe]:
    target_dir = catalog_dir or get_default_catalog_dir()
    if not target_dir.is_dir():
        return []
    recipes: list[ToolRecipe] = []
    seen_ids: dict[str, Path] = {}
    for toml_file in sorted(target_dir.glob("*.toml")):
        recipe = load_recipe(toml_file)
        if recipe.id in seen_ids:
            raise ValueError(f"Duplicate tool id {recipe.id!r} in {toml_file} and {seen_ids[recipe.id]}")
        seen_ids[recipe.id] = toml_file
        recipes.append(recipe)
    return recipes


def _check_app_bundle(bundle_id: str, app_roots: list[Path] | None = None) -> bool:
    """Check if app bundle exists in /Applications or ~/Applications."""
    if app_roots is None:
        home = Path(os.environ.get("HOME", str(Path.home())))
        app_roots = [Path("/Applications"), home / "Applications"]
    for root in app_roots:
        if not root.is_dir():
            continue
        for app in root.glob("*.app"):
            plist_path = app / "Contents" / "Info.plist"
            if plist_path.is_file():
                try:
                    with plist_path.open("rb") as f:
                        info = plistlib.load(f)
                    if info.get("CFBundleIdentifier") == bundle_id:
                        return True
                except Exception as exc:
                    logger.debug("check app bundle failed: %s", type(exc).__name__)
                    continue
    return False


def is_tool_detected(
    tool: ToolRecipe,
    env: Mapping[str, str] | None = None,
    *,
    app_roots: list[Path] | None = None,
) -> bool:
    """Check if any detection rule in tool.detect.any matches."""
    if not tool.detect.any:
        return False
    lookup = os.environ if env is None else env
    for rule in tool.detect.any:
        if rule.dir:
            expanded = expand_vars_and_tilde(rule.dir, lookup)
            if Path(expanded).is_dir():
                return True
        if rule.cli:
            # Check shutil.which
            path_val = lookup.get("PATH")
            if shutil.which(rule.cli, path=path_val):
                return True
        if rule.app_bundle:
            if _check_app_bundle(rule.app_bundle, app_roots=app_roots):
                return True
    return False
