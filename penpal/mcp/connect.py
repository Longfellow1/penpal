"""Automated MCP configuration and skill provisioning for AI tools (issue #173)."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import tomllib

from penpal.mcp.auth import CLIENT_ENV, TOKEN_ENV, active_grants, create_grant, revoke_grants
from penpal.mcp.cli import _penpal_executable
from penpal.sources.recipes.catalog import (
    expand_vars_and_tilde,
    is_tool_detected,
    load_catalog,
)
from penpal.utils.logging import get_logger
from penpal.utils.paths import get_data_dir

LOGGER = get_logger("mcp.connect")

# Only these four tools receive a skill file per specification (§3.5).
SKILL_DIR_MAP: dict[str, str] = {
    "claude_code": "~/.claude/skills/penpal",
    "codex_cli": "~/.agents/skills/penpal",
    "antigravity": "~/.gemini/config/skills/penpal",
    "cursor": "~/.cursor/skills/penpal",
}


def _get_yaml_module():
    try:
        import yaml
        return yaml
    except ImportError as exc:
        LOGGER.debug("yaml import failed: %s", type(exc).__name__)
        return None


class ConfigParseError(Exception):
    """Raised when an existing configuration file cannot be parsed."""


class ConcurrentModificationError(Exception):
    """Raised when config file was concurrently modified by another process."""


@dataclass
class ToolState:
    tool_id: str
    config_path: str
    backup_path: str | None
    original_existed: bool
    written_hash: str
    skill_path: str | None = None
    skill_created: bool = False
    connected_at: str = ""
    took_over_manual: bool = False


@dataclass
class ConnectResult:
    tool_id: str
    tool_name: str
    status: str  # connected, already_connected, not_installed, unsupported, parse_error, manual, dry_run
    config_path: str = ""
    message: str = ""
    error: str | None = None


@dataclass
class ToolStatus:
    tool_id: str
    tool_name: str
    state: str  # 已接入, 未安装, 不支持 MCP, 解析失败, 未接入
    config_path: str = ""
    detail: str = ""


@dataclass
class DisconnectResult:
    tool_id: str
    tool_name: str
    status: str  # restored_original, entry_removed_modified, not_connected, error
    message: str = ""


def _connect_dir() -> Path:
    directory = get_data_dir() / "connect"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError as exc:
        LOGGER.debug("chmod connect dir failed: %s", type(exc).__name__)
    return directory


def _backups_dir() -> Path:
    directory = _connect_dir() / "backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError as exc:
        LOGGER.debug("chmod backups dir failed: %s", type(exc).__name__)
    return directory


def _state_file_path() -> Path:
    return _connect_dir() / "state.json"


def _load_all_states() -> dict[str, ToolState]:
    path = _state_file_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        result = {}
        for k, v in data.items():
            if isinstance(v, dict):
                result[k] = ToolState(**v)
        return result
    except Exception as exc:
        LOGGER.warning("failed to load connect state: %s", exc)
        return {}


def _save_all_states(states: dict[str, ToolState]) -> None:
    path = _state_file_path()
    data = {k: asdict(v) for k, v in states.items()}
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        temp_path.chmod(0o600)
    except OSError as exc:
        LOGGER.debug("chmod state tmp failed: %s", type(exc).__name__)
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError as exc:
        LOGGER.debug("chmod state file failed: %s", type(exc).__name__)


def _file_hash(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _get_skill_template() -> str:
    template_path = Path(__file__).resolve().parent.parent / "skills" / "SKILL.md"
    return template_path.read_text(encoding="utf-8")


def _penpal_server_dict(tool_id: str, exe: str, token: str) -> dict[str, Any]:
    server = {
        "command": exe,
        "args": ["mcp", "serve"],
        "env": {
            TOKEN_ENV: token,
            CLIENT_ENV: tool_id,
        },
    }
    if tool_id == "cursor":
        server = {"type": "stdio", **server}
    return server


def _update_json_content(raw_text: str, tool_id: str, exe: str, token: str, style: str) -> str:
    if not raw_text.strip():
        data: dict[str, Any] = {}
    else:
        try:
            data = json.loads(raw_text)
        except Exception as exc:
            raise ConfigParseError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigParseError("JSON 根节点必须为对象")

    section_key = "servers" if style == "json_servers" else "mcpServers"
    if section_key not in data or not isinstance(data[section_key], dict):
        data[section_key] = {}

    data[section_key]["penpal"] = _penpal_server_dict(tool_id, exe, token)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _remove_json_penpal(raw_text: str, style: str) -> str:
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        LOGGER.debug("json parse failed in _remove_json_penpal: %s", type(exc).__name__)
        return raw_text
    if not isinstance(data, dict):
        return raw_text

    section_key = "servers" if style == "json_servers" else "mcpServers"
    if section_key in data and isinstance(data[section_key], dict):
        data[section_key].pop("penpal", None)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _check_json_has_penpal(raw_text: str, style: str) -> bool:
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            section_key = "servers" if style == "json_servers" else "mcpServers"
            return bool(data.get(section_key, {}).get("penpal"))
    except (json.JSONDecodeError, ValueError) as exc:
        LOGGER.debug("json parse failed in _check_json_has_penpal: %s", type(exc).__name__)
        return False
    return False


def _is_penpal_toml_header(line: str) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return False
    inner = stripped.lstrip("[").rstrip("]").strip()
    return inner == "mcp_servers.penpal" or inner.startswith("mcp_servers.penpal.")


def _is_any_toml_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _strip_penpal_toml_sections(raw_text: str) -> str:
    lines = raw_text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        if _is_penpal_toml_header(line):
            skipping = True
            continue
        if _is_any_toml_header(line):
            skipping = False
            out.append(line)
            continue
        if skipping:
            continue
        out.append(line)
    return "".join(out)


def _update_toml_content(raw_text: str, tool_id: str, exe: str, token: str) -> str:
    if raw_text.strip():
        try:
            tomllib.loads(raw_text)
        except Exception as exc:
            raise ConfigParseError(f"TOML 解析失败: {exc}") from exc

    cleaned_raw = _strip_penpal_toml_sections(raw_text)

    penpal_block = (
        "[mcp_servers.penpal]\n"
        f"command = {json.dumps(exe)}\n"
        'args = ["mcp", "serve"]\n'
        f"env = {{ {TOKEN_ENV} = {json.dumps(token)}, {CLIENT_ENV} = {json.dumps(tool_id)} }}\n"
    )

    prefix = cleaned_raw
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    new_text = prefix + penpal_block

    # Verify validity of updated TOML
    try:
        tomllib.loads(new_text)
    except Exception as exc:
        raise ConfigParseError(f"写入后 TOML 验证失败: {exc}") from exc
    return new_text


def _remove_toml_penpal(raw_text: str) -> str:
    return _strip_penpal_toml_sections(raw_text)


def _check_toml_has_penpal(raw_text: str) -> bool:
    try:
        data = tomllib.loads(raw_text)
        return bool(data.get("mcp_servers", {}).get("penpal"))
    except tomllib.TOMLDecodeError as exc:
        LOGGER.debug("toml parse failed in _check_toml_has_penpal: %s", type(exc).__name__)
        return False


def _update_yaml_content(raw_text: str, tool_id: str, exe: str, token: str) -> str:
    yaml_mod = _get_yaml_module()
    if yaml_mod is None:
        raise ConfigParseError("缺少 YAML 解析库，跳过")

    if raw_text.strip():
        try:
            parsed = yaml_mod.safe_load(raw_text)
            if parsed is not None and not isinstance(parsed, dict):
                raise ConfigParseError("YAML 根节点必须为字典")
        except Exception as exc:
            raise ConfigParseError(f"YAML 解析失败: {exc}") from exc
    else:
        parsed = None

    penpal_lines = [
        "  penpal:",
        f"    command: {json.dumps(exe)}",
        "    args:",
        '      - "mcp"',
        '      - "serve"',
        "    env:",
        f"      {TOKEN_ENV}: {json.dumps(token)}",
        f"      {CLIENT_ENV}: {json.dumps(tool_id)}",
    ]
    penpal_block = "\n".join(penpal_lines) + "\n"

    if not raw_text.strip() or parsed is None:
        new_text = "mcp_servers:\n" + penpal_block
    else:
        mcp_match = re.search(r"^mcp_servers\s*:\s*(\{\})?", raw_text, re.MULTILINE)
        if not mcp_match:
            prefix = raw_text
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            new_text = prefix + "mcp_servers:\n" + penpal_block
        else:
            cleaned = _remove_yaml_penpal(raw_text)
            mcp_match2 = re.search(r"^mcp_servers\s*:\s*(\{\})?[ \t]*\n?", cleaned, re.MULTILINE)
            if mcp_match2:
                insert_pos = mcp_match2.end()
                if mcp_match2.group(1):
                    start_pos = mcp_match2.start()
                    new_text = cleaned[:start_pos] + "mcp_servers:\n" + penpal_block + cleaned[insert_pos:]
                else:
                    new_text = cleaned[:insert_pos] + penpal_block + cleaned[insert_pos:]
            else:
                new_text = cleaned + "\nmcp_servers:\n" + penpal_block

    # Validate updated YAML
    try:
        yaml_mod.safe_load(new_text)
    except Exception as exc:
        raise ConfigParseError(f"写入后 YAML 验证失败: {exc}") from exc
    return new_text


def _remove_yaml_penpal(raw_text: str) -> str:
    lines = raw_text.splitlines(keepends=True)
    out: list[str] = []
    in_mcp_servers = False
    in_penpal = False

    for line in lines:
        if re.match(r"^mcp_servers\s*:", line):
            in_mcp_servers = True
            in_penpal = False
            out.append(line)
            continue
        if in_mcp_servers:
            if re.match(r"^[^\s#]", line):
                in_mcp_servers = False
                in_penpal = False
                out.append(line)
                continue
            if re.match(r"^[ \t]{2}penpal\s*:", line):
                in_penpal = True
                continue
            if in_penpal:
                if re.match(r"^[ \t]{3,}", line) or re.match(r"^\s*$", line):
                    continue
                else:
                    in_penpal = False
                    out.append(line)
                    continue
            out.append(line)
        else:
            out.append(line)

    result = "".join(out)
    return result


def _check_yaml_has_penpal(raw_text: str) -> bool:
    yaml_mod = _get_yaml_module()
    if yaml_mod is None:
        return False
    try:
        parsed = yaml_mod.safe_load(raw_text)
        if isinstance(parsed, dict):
            servers = parsed.get("mcp_servers")
            return isinstance(servers, dict) and bool(servers.get("penpal"))
    except Exception as exc:
        LOGGER.debug("yaml parse failed in _check_yaml_has_penpal: %s", type(exc).__name__)
        return False
    return False


def _update_config_file(
    path: Path,
    style: str,
    tool_id: str,
    exe: str,
    token: str,
    expected_bytes: bytes | None = None,
) -> None:
    raw_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if style in ("json_mcpServers", "json_servers"):
        new_text = _update_json_content(raw_text, tool_id, exe, token, style)
    elif style == "toml_mcp_servers":
        new_text = _update_toml_content(raw_text, tool_id, exe, token)
    elif style == "yaml_mcp_servers":
        new_text = _update_yaml_content(raw_text, tool_id, exe, token)
    else:
        raise ValueError(f"不支持的配置风格: {style}")

    # Check idempotency: if content unchanged, do not rewrite
    if path.is_file() and raw_text == new_text:
        return

    # Concurrency check right before writing
    current_bytes = path.read_bytes() if path.is_file() else None
    if current_bytes != expected_bytes:
        raise ConcurrentModificationError("配置文件正被其他程序修改，请稍后重试")

    orig_mode = None
    if path.is_file():
        try:
            orig_mode = path.stat().st_mode & 0o777
        except OSError as exc:
            LOGGER.debug("stat mode failed: %s", type(exc).__name__)
    target_mode = orig_mode if orig_mode is not None else 0o600

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(new_text, encoding="utf-8")
    try:
        temp_path.chmod(target_mode)
    except OSError as exc:
        LOGGER.debug("chmod temp_path failed: %s", type(exc).__name__)
    temp_path.replace(path)
    try:
        path.chmod(target_mode)
    except OSError as exc:
        LOGGER.debug("chmod path failed: %s", type(exc).__name__)


def _remove_penpal_from_file(path: Path, style: str) -> None:
    if not path.is_file():
        return
    raw_text = path.read_text(encoding="utf-8")
    if style in ("json_mcpServers", "json_servers"):
        new_text = _remove_json_penpal(raw_text, style)
    elif style == "toml_mcp_servers":
        new_text = _remove_toml_penpal(raw_text)
    elif style == "yaml_mcp_servers":
        new_text = _remove_yaml_penpal(raw_text)
    else:
        return

    orig_mode = None
    try:
        orig_mode = path.stat().st_mode & 0o777
    except OSError as exc:
        LOGGER.debug("stat mode failed: %s", type(exc).__name__)
    target_mode = orig_mode if orig_mode is not None else 0o600

    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(new_text, encoding="utf-8")
    try:
        temp_path.chmod(target_mode)
    except OSError as exc:
        LOGGER.debug("chmod temp_path failed: %s", type(exc).__name__)
    temp_path.replace(path)
    try:
        path.chmod(target_mode)
    except OSError as exc:
        LOGGER.debug("chmod path failed: %s", type(exc).__name__)


def _check_file_has_penpal(path: Path, style: str) -> bool:
    if not path.is_file():
        return False
    try:
        raw_text = path.read_text(encoding="utf-8")
        if style in ("json_mcpServers", "json_servers"):
            return _check_json_has_penpal(raw_text, style)
        elif style == "toml_mcp_servers":
            return _check_toml_has_penpal(raw_text)
        elif style == "yaml_mcp_servers":
            return _check_yaml_has_penpal(raw_text)
    except (OSError, UnicodeDecodeError) as exc:
        LOGGER.debug("file read failed in _check_file_has_penpal: %s", type(exc).__name__)
        return False
    return False


def _has_active_grant(tool_id: str) -> bool:
    for grant in active_grants():
        if grant.get("client_id") == tool_id:
            return True
    return False


def _write_skill_file(tool_id: str, env: Mapping[str, str] | None = None) -> tuple[Path | None, bool]:
    skill_dir_rel = SKILL_DIR_MAP.get(tool_id)
    if not skill_dir_rel:
        return None, False
    expanded = expand_vars_and_tilde(skill_dir_rel, env)
    skill_dir = Path(expanded)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"

    template = _get_skill_template()
    if skill_path.is_file() and skill_path.read_text(encoding="utf-8") == template:
        return skill_path, False

    temp_path = skill_path.with_suffix(".tmp")
    temp_path.write_text(template, encoding="utf-8")
    temp_path.replace(skill_path)
    return skill_path, True


def auto_connect(
    *,
    dry_run: bool = False,
    catalog_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ConnectResult]:
    """Auto-connect all detected AI tools to Penpal MCP."""
    recipes = load_catalog(catalog_dir)
    states = _load_all_states()
    results: list[ConnectResult] = []
    exe = _penpal_executable()

    for tool in recipes:
        if not is_tool_detected(tool, env=env):
            continue

        if not tool.mcp or tool.mcp.style == "none":
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="unsupported",
                    message="不支持 MCP",
                )
            )
            continue

        if tool.mcp.style == "manual":
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="manual",
                    config_path=tool.mcp.config,
                    message=f"请在界面中手动添加 Penpal MCP 服务（命令: {exe} mcp serve）",
                )
            )
            continue

        # YAML dependency check
        if tool.mcp.style == "yaml_mcp_servers" and _get_yaml_module() is None:
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="manual",
                    config_path=tool.mcp.config,
                    error="missing_yaml",
                    message="缺少 YAML 解析库，未自动接入，配置未改动；升级 Penpal 后重试",
                )
            )
            continue

        # Supported automated styles
        config_expanded = Path(expand_vars_and_tilde(tool.mcp.config, env))

        if dry_run:
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="dry_run",
                    config_path=str(config_expanded),
                    message=f"[演练] 将写入 MCP 配置至 {config_expanded}",
                )
            )
            continue

        # Check existing state and active grant
        existing_state = states.get(tool.id)
        active_grant_present = _has_active_grant(tool.id)
        is_already_set = _check_file_has_penpal(config_expanded, tool.mcp.style)

        # Detect manual configuration takeover
        is_manual_takeover = False
        if is_already_set and (existing_state is None or not active_grant_present):
            is_manual_takeover = True

        # Pre-check: if file exists, verify whether it can be parsed
        initial_raw_bytes: bytes | None = None
        if config_expanded.is_file():
            try:
                initial_raw_bytes = config_expanded.read_bytes()
                raw_check = initial_raw_bytes.decode("utf-8")
                if tool.mcp.style in ("json_mcpServers", "json_servers"):
                    json.loads(raw_check)
                elif tool.mcp.style == "toml_mcp_servers":
                    tomllib.loads(raw_check)
                elif tool.mcp.style == "yaml_mcp_servers":
                    yaml_mod = _get_yaml_module()
                    if yaml_mod:
                        yaml_mod.safe_load(raw_check)
            except Exception as exc:
                LOGGER.warning("config parse error for %s: %s", tool.id, exc)
                results.append(
                    ConnectResult(
                        tool_id=tool.id,
                        tool_name=tool.name,
                        status="parse_error",
                        config_path=str(config_expanded),
                        error=str(exc),
                        message=f"配置文件解析失败（未修改原文件）: {exc}",
                    )
                )
                continue

        # Backup original file before any modification
        backup_path: str | None = None
        original_existed = False
        if existing_state and existing_state.backup_path and Path(existing_state.backup_path).is_file():
            backup_path = existing_state.backup_path
            original_existed = existing_state.original_existed
        else:
            if config_expanded.is_file():
                original_existed = True
                b_dir = _backups_dir()
                b_file = b_dir / f"{tool.id}_{config_expanded.name}"
                shutil.copy2(config_expanded, b_file)
                try:
                    b_file.chmod(0o600)
                except OSError as exc:
                    LOGGER.debug("chmod backup failed: %s", type(exc).__name__)
                backup_path = str(b_file)

        # If already configured and managed by connect with active grant, check idempotency
        if existing_state and active_grant_present and is_already_set and not is_manual_takeover:
            skill_path, _ = _write_skill_file(tool.id, env=env)
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="already_connected",
                    config_path=str(config_expanded),
                    message=f"已接入，配置未变: {config_expanded}",
                )
            )
            continue

        # Create independent grant for this tool (operations: read,write)
        token = create_grant(client_id=tool.id, operations=["read", "write"])

        # Write config with concurrency guard
        try:
            _update_config_file(
                config_expanded,
                tool.mcp.style,
                tool.id,
                exe,
                token,
                expected_bytes=initial_raw_bytes,
            )
        except ConcurrentModificationError:
            revoke_grants(tool.id)
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="parse_error",
                    config_path=str(config_expanded),
                    error="concurrent_modification",
                    message="配置文件正被其他程序修改，请稍后重试",
                )
            )
            continue
        except ConfigParseError as exc:
            revoke_grants(tool.id)
            results.append(
                ConnectResult(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    status="parse_error",
                    config_path=str(config_expanded),
                    error=str(exc),
                    message=f"配置文件解析失败: {exc}",
                )
            )
            continue

        written_hash = _file_hash(config_expanded)

        # Write skill file if tool is mapped
        skill_path, skill_created = _write_skill_file(tool.id, env=env)

        # Save state
        states[tool.id] = ToolState(
            tool_id=tool.id,
            config_path=str(config_expanded),
            backup_path=backup_path,
            original_existed=original_existed,
            written_hash=written_hash,
            skill_path=str(skill_path) if skill_path else None,
            skill_created=skill_created,
            took_over_manual=is_manual_takeover,
        )
        _save_all_states(states)

        msg = f"已接管现有手动配置: {config_expanded}" if is_manual_takeover else f"成功接入: {config_expanded}"
        results.append(
            ConnectResult(
                tool_id=tool.id,
                tool_name=tool.name,
                status="connected",
                config_path=str(config_expanded),
                message=msg,
            )
        )

    return results


def list_tools(
    *,
    catalog_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ToolStatus]:
    """List connection and installation status for all catalog tools."""
    recipes = load_catalog(catalog_dir)
    states = _load_all_states()
    statuses: list[ToolStatus] = []

    for tool in recipes:
        if not is_tool_detected(tool, env=env):
            statuses.append(
                ToolStatus(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    state="未安装",
                    detail="本机未检测到该工具",
                )
            )
            continue

        if not tool.mcp or tool.mcp.style == "none":
            statuses.append(
                ToolStatus(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    state="不支持 MCP",
                    detail="该工具无 MCP 配置入口",
                )
            )
            continue

        if tool.mcp.style == "manual":
            statuses.append(
                ToolStatus(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    state="不支持 MCP",
                    config_path=tool.mcp.config,
                    detail="需在界面中手动配置",
                )
            )
            continue

        if tool.mcp.style == "yaml_mcp_servers" and _get_yaml_module() is None:
            statuses.append(
                ToolStatus(
                    tool_id=tool.id,
                    tool_name=tool.name,
                    state="未接入",
                    config_path=tool.mcp.config,
                    detail="缺少 YAML 解析库；升级 Penpal 后重试",
                )
            )
            continue

        config_expanded = Path(expand_vars_and_tilde(tool.mcp.config, env))
        if config_expanded.is_file():
            try:
                raw_text = config_expanded.read_text(encoding="utf-8")
                if tool.mcp.style in ("json_mcpServers", "json_servers"):
                    json.loads(raw_text)
                elif tool.mcp.style == "toml_mcp_servers":
                    tomllib.loads(raw_text)
                elif tool.mcp.style == "yaml_mcp_servers":
                    yaml_mod = _get_yaml_module()
                    if yaml_mod:
                        yaml_mod.safe_load(raw_text)
            except Exception as exc:
                statuses.append(
                    ToolStatus(
                        tool_id=tool.id,
                        tool_name=tool.name,
                        state="解析失败",
                        config_path=str(config_expanded),
                        detail=str(exc),
                    )
                )
                continue

            has_penpal = _check_file_has_penpal(config_expanded, tool.mcp.style)
            has_grant = _has_active_grant(tool.id)
            state_rec = states.get(tool.id)

            if has_penpal and has_grant:
                detail = "接管了已有的手动配置" if (state_rec and state_rec.took_over_manual) else "MCP 与授权正常"
                statuses.append(
                    ToolStatus(
                        tool_id=tool.id,
                        tool_name=tool.name,
                        state="已接入",
                        config_path=str(config_expanded),
                        detail=detail,
                    )
                )
            else:
                statuses.append(
                    ToolStatus(
                        tool_id=tool.id,
                        tool_name=tool.name,
                        state="未接入",
                        config_path=str(config_expanded),
                        detail="已安装，尚未接入 Penpal",
                    )
                )
        else:
            if tool.id in states and _has_active_grant(tool.id):
                statuses.append(
                    ToolStatus(
                        tool_id=tool.id,
                        tool_name=tool.name,
                        state="已接入",
                        config_path=str(config_expanded),
                        detail="配置文件已被删除",
                    )
                )
            else:
                statuses.append(
                    ToolStatus(
                        tool_id=tool.id,
                        tool_name=tool.name,
                        state="未接入",
                        config_path=str(config_expanded),
                        detail="配置文件不存在",
                    )
                )

    return statuses


def disconnect_tool(
    tool_id: str,
    *,
    catalog_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> DisconnectResult:
    """Disconnect a single tool and restore its configuration."""
    recipes = {t.id: t for t in load_catalog(catalog_dir)}
    states = _load_all_states()

    tool = recipes.get(tool_id)
    tool_name = tool.name if tool else tool_id

    state = states.get(tool_id)
    config_path: Path | None = None
    style = tool.mcp.style if tool and tool.mcp else "json_mcpServers"

    if state:
        config_path = Path(state.config_path)
    elif tool and tool.mcp:
        config_path = Path(expand_vars_and_tilde(tool.mcp.config, env))

    # Revoke tokens
    revoke_grants(tool_id)

    status = "not_connected"
    message = f"{tool_name} ({tool_id}) 未接入"

    if config_path and config_path.is_file():
        current_hash = _file_hash(config_path)
        if state and current_hash == state.written_hash:
            # File has not been touched since Penpal wrote it
            if state.original_existed and state.backup_path and Path(state.backup_path).is_file():
                shutil.copy2(Path(state.backup_path), config_path)
                try:
                    orig_mode = Path(state.backup_path).stat().st_mode & 0o777
                    config_path.chmod(orig_mode)
                except OSError as exc:
                    LOGGER.debug("chmod restored config failed: %s", type(exc).__name__)
                status = "restored_original"
                message = f"已断开 {tool_name}，恢复原文件字节一致"
            else:
                config_path.unlink(missing_ok=True)
                status = "restored_original"
                message = f"已断开 {tool_name}，移除由 Penpal 创建的配置文件"
        else:
            _remove_penpal_from_file(config_path, style)
            status = "entry_removed_modified"
            message = f"已断开 {tool_name}；检测到配置文件在接入后被修改，已仅删除 Penpal 条目"

    # Remove backup file
    if state and state.backup_path and Path(state.backup_path).is_file():
        Path(state.backup_path).unlink(missing_ok=True)

    # Remove skill file if created by Penpal
    if state and state.skill_path:
        skill_file = Path(state.skill_path)
        if skill_file.is_file():
            skill_file.unlink(missing_ok=True)
            if skill_file.parent.is_dir() and not any(skill_file.parent.iterdir()):
                try:
                    skill_file.parent.rmdir()
                except OSError as exc:
                    LOGGER.debug("rmdir failed: %s", type(exc).__name__)

    # Clean up state
    if tool_id in states:
        del states[tool_id]
        _save_all_states(states)

    return DisconnectResult(tool_id=tool_id, tool_name=tool_name, status=status, message=message)


def disconnect_all(
    *,
    catalog_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[DisconnectResult]:
    """Disconnect all tools currently connected in state or active grants."""
    states = _load_all_states()
    recipes = {t.id: t for t in load_catalog(catalog_dir)}

    target_ids = set(states.keys())
    for grant in active_grants():
        cid = grant.get("client_id")
        if cid and (cid in recipes or cid in states):
            target_ids.add(cid)

    results: list[DisconnectResult] = []
    for tid in sorted(target_ids):
        res = disconnect_tool(tid, catalog_dir=catalog_dir, env=env)
        results.append(res)
    return results
