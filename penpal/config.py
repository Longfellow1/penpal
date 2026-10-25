from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from penpal.foundation.chat_apps import normalize_opt_ins
from penpal.foundation.capture_rules import DEFAULT_TITLE_DENY_PATTERNS
from penpal.utils.atomic_io import atomic_write_text
from penpal.utils.paths import get_data_dir, get_config_path


_DEFAULT_USER_CONFIG = """# Penpal user config.
# Homebrew installs the CLI. This file controls the local runtime.

[app]
db_path = "~/.penpal/penpal.db"
log_path = "~/.penpal/penpal.log"
flush_interval_sec = 5

[watchers]
window = true
idle = true
clipboard = true
manual = true
keyboard_chunk = true
browser = false
browser_url = true
ax_text = false
ocr = false
"""
_CONFIG_STRING_LINE_RE = re.compile(r'^(\s*[A-Za-z_][\w.-]*\s*=\s*)"([^"\\]*)"(.*)$')

class AppConfig(BaseModel):
    db_path: str = "~/.penpal/penpal.db"
    log_path: str = "~/.penpal/penpal.log"
    flush_interval_sec: int = 5
    # D32: deprecated and ignored. Kept only so old config files still parse; nothing is deleted automatically.
    retention_days: int = 30
    timezone: str | None = None  # IANA name (e.g. "Asia/Shanghai"); None = follow system tz


class WatchersConfig(BaseModel):
    window: bool = True
    idle: bool = True
    clipboard: bool = True
    manual: bool = True
    keyboard_chunk: bool = True
    browser: bool = False
    browser_url: bool = False
    ax_text: bool = False
    ocr: bool = False


class IdleConfig(BaseModel):
    threshold_sec: int = 180


class ClipboardConfig(BaseModel):
    max_text_length: int = 2000
    dedup_window_sec: int = 600


class AXTextConfig(BaseModel):
    poll_interval_sec: float = 1.0


class KeyboardChunkConfig(BaseModel):
    silence_sec: float = 2.0
    # D27: a flush after ~20s of continuous typing keeps chunks near sentence length
    # (2s chunks were too short for any block-level filter to work on).
    force_flush_sec: float = 20.0
    store_text: bool = True


class BrowserConfig(BaseModel):
    poll_interval_sec: float = 1.0
    supported_browsers: list[str] = Field(
        default_factory=lambda: [
            "Safari",
            "Google Chrome",
            "Arc",
            "Brave Browser",
            "Microsoft Edge",
        ]
    )


class BrowserURLConfig(BaseModel):
    poll_interval_sec: float = 3.0
    supported_browsers: Literal["auto"] | list[str] | tuple[str, ...] = Field(
        default="auto"
    )
    emit_on_url_change_only: bool = True


class BrowserHistoryConfig(BaseModel):
    enabled: bool = False
    poll_interval_sec: float = 300.0
    copy_to_cache: bool = True
    browsers: list[str] = Field(
        default_factory=lambda: [
            "Safari",
            "Google Chrome",
            "Arc",
            "Microsoft Edge",
            "Brave Browser",
            "Firefox",
        ]
    )


def _default_title_deny():
    return DEFAULT_TITLE_DENY_PATTERNS


class PrivacyConfig(BaseModel):
    redact_emails: bool = True
    redact_phones: bool = True
    redact_tokens: bool = True
    drop_terminal_text: bool = True
    url_deny_hosts: list[str] = Field(
        default_factory=lambda: [
            "apple.com",
            "appleid.apple.com",
            "xiaohongshu.com",
            "weibo.com",
            "bilibili.com",
            "zhihu.com",
            "douyin.com",
            "xhs.com",
        ]
    )
    camera_scene_pause: bool = True
    blacklist_bundle_ids: list[str] = Field(
        default_factory=lambda: [
            "com.agilebits.onepassword7",
            "com.agilebits.onepassword-ios",
            "com.bitwarden.desktop",
            "com.dashlane.mac",
            "com.lastpass.lp",
            "com.apple.keychainaccess",
            "com.microsoft.authenticator",
            "com.tencent.wechat",
            "com.tencent.qq",
            "org.telegram.desktop",
            "org.signal.signal-desktop",
            "com.whatsapp.messenger",
            "com.apple.Messages",
            # Slack's macOS bundle id (was "com.tinyspk.slack", a typo).
            "com.tinyspeck.slackmacgap",
            "com.hnc.Discord",
            "com.tencent.wxwork",
            "com.alibaba.dingtalk",
            # Verified from locally installed Lark.app / Telegram.app Info.plist (CFBundleIdentifier);
            # the old feishu id was a guess. DingTalk's mac id is from public listings, not verified here.
            "com.alibaba.DingTalkMac",
            "com.electron.lark",
            "com.bytedance.feishu",
            "ru.keepcoder.Telegram",
            "com.tencent.xinWeChat",
            "jp.naver.line",
            "com.kakao.talk",
            "com.icbc.mobile",
            "com.bankofchina.mobilebank",
            "com.wf.wellsfargomobile",
            "com.chase.mobile",
            "com.bankofamerica.mobile",
            "com.dbs.dbsmbanking",
            "com.robinhoodinc.robinhood",
            "com.schwab.mobile",
            "com.etrade.mobile",
            "com.crypto.app",
            "io.metamask.mobile",
            "com.ledger.live",
            "com.epic.mychart",
            "com.headspace.meditation",
            "com.calm.ios",
        ]
    )
    blacklist_patterns: list[str] = Field(default_factory=lambda: ["com.tencent.*"])
    # D27: events without a bundle id (clipboard) are matched by app name, case-insensitive substring.
    # D27: regexes (case-insensitive) matched against window titles; a hit makes the event
    # metadata-only. Seeded from the login-context keywords (sign in, password, 登录, ...).
    title_deny_patterns: list[str] = Field(default_factory=lambda: list(_default_title_deny()))
    blacklist_app_names: list[str] = Field(
        default_factory=lambda: [
            "微信", "WeChat", "企业微信", "钉钉", "DingTalk", "飞书", "Lark", "Feishu",
            "1Password", "Keychain Access", "钥匙串", "密码",
        ]
    )
    # Chat apps (penpal.foundation.chat_apps) are excluded from capture by default. Listing one here
    # opts it in: its rows are stored and bucketed as Private. Keys or listed names, e.g. ["WeChat", "Slack"].
    chat_apps_opt_in: list[str] = Field(default_factory=list)

    @field_validator("chat_apps_opt_in")
    @classmethod
    def _chat_apps_opt_in_known(cls, value: list[str]) -> list[str]:
        return normalize_opt_ins(value)


class MarkdownVaultSourceConfig(BaseModel):
    extra_roots: list[str] = Field(default_factory=list)


class SourcesSchedulerConfig(BaseModel):
    enabled: bool = True
    poll_interval_sec: int = 600
    debounce_sec: int = 300
    fast_poll_interval_sec: int = 120


class SourcesConfig(BaseModel):
    markdown_vault: MarkdownVaultSourceConfig = Field(default_factory=MarkdownVaultSourceConfig)
    scheduler: SourcesSchedulerConfig = Field(default_factory=SourcesSchedulerConfig)


class BucketsConfig(BaseModel):
    # Overrides of the built-in per-source default bucket, e.g. {"markdown_vault": "Work"}.
    source_defaults: dict[str, str] = Field(default_factory=dict)
    # Capture-stream app overrides (D26), e.g. {"Obsidian": "Personal"}; case-insensitive substring
    # of the app / process name. Beats the preset app table, loses to user bucket rules.
    app_defaults: dict[str, str] = Field(default_factory=dict)
    # Website overrides for browser content (D28), e.g. {"notion.so": "Personal", "corp.example.com": "Work"};
    # host-suffix match. Beats the preset site table (store.site_categories), loses to user bucket rules.
    site_defaults: dict[str, str] = Field(default_factory=dict)


class PolicyConfig(BaseModel):
    scope_type: str
    scope_value: str
    mode: str = "allow"  # allow|deny|metadata-only|redact|truncate (ignored when `bucket` is set)
    bucket: str | None = None  # bucket rule: Work|Personal|Private|Financial (scope_type app|source|project)
    enabled: bool = True
    priority: int = 100


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    watchers: WatchersConfig = Field(default_factory=WatchersConfig)
    idle: IdleConfig = Field(default_factory=IdleConfig)
    clipboard: ClipboardConfig = Field(default_factory=ClipboardConfig)
    ax_text: AXTextConfig = Field(default_factory=AXTextConfig)
    keyboard_chunk: KeyboardChunkConfig = Field(default_factory=KeyboardChunkConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    browser_url: BrowserURLConfig = Field(default_factory=BrowserURLConfig)
    browser_history: BrowserHistoryConfig = Field(default_factory=BrowserHistoryConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    buckets: BucketsConfig = Field(default_factory=BucketsConfig)
    policies: list[PolicyConfig] = Field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        """Load config from ~/.penpal/config.toml or ./config.toml, falling back to defaults."""
        from penpal.utils.env import load_dotenv_quiet
        from penpal.utils.paths import get_config_path

        load_dotenv_quiet()
        paths = [get_config_path(), Path("config.toml")]
        for p in paths:
            if p.exists():
                config = read_config(p)
                # A path still under the legacy ~/.keypulse (the migration marker dir) resolves to ~/.penpal.
                from penpal.utils.paths import resolve_legacy_data_path

                for field in ("db_path", "log_path"):
                    setattr(config.app, field, resolve_legacy_data_path(getattr(config.app, field)))
                return config
        return cls()

    @property
    def db_path_expanded(self) -> Path:
        path = Path(self.app.db_path).expanduser()
        legacy = path.with_name("keypulse.db") if path.name == "penpal.db" else None
        return legacy if legacy is not None and not path.exists() and legacy.exists() else path

    @property
    def log_path_expanded(self) -> Path:
        from penpal.utils.paths import get_log_path

        return get_log_path(self.app.log_path)


def config_fingerprint(path: Path | None = None) -> str:
    """Hash the exact config.toml bytes selected by Config.load()."""
    if path is None:
        candidates = (get_config_path(), Path("config.toml"))
        path = next((candidate for candidate in candidates if candidate.exists()), None)
    digest = hashlib.sha256()
    if path is None:
        digest.update(b"<missing-config>")
    else:
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"<missing-config>")
    return digest.hexdigest()


def read_toml(path: Path | str) -> dict:
    """Read and parse TOML from an explicit path."""
    config_path = Path(path)
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def read_config(path: Path | str) -> Config:
    """Read and validate a Penpal config file from an explicit path."""
    return Config.model_validate(read_toml(path))


def read_cleaning_config(path: Path | None = None) -> dict:
    """Read cleaning.toml, returning an empty mapping when the optional file is absent."""
    config_path = path or (get_data_dir() / "cleaning.toml")
    try:
        return read_toml(config_path)
    except FileNotFoundError:
        return {}


def write_default_config(path: Path | None = None, *, force: bool = False) -> str:
    """Write the default user config if absent (or forced), then read it back for validation."""
    config_path = path or get_config_path()
    existed = config_path.exists()
    if existed and not force:
        return "exists"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config_path, _DEFAULT_USER_CONFIG)
    read_config(config_path)
    return "updated" if existed else "created"


def rewrite_legacy_config_paths(data_dir: Path, home: Path | None = None) -> bool:
    """Rewrite legacy data paths in config.toml while preserving its formatting and a one-time backup."""
    from penpal.utils.paths import (
        CONFIG_BACKUP_NAME,
        LEGACY_DATA_DIRNAME,
        _guard_penpal_path,
        resolve_legacy_data_path,
    )

    data_dir = _guard_penpal_path(data_dir)
    config_path = _guard_penpal_path(data_dir / "config.toml")
    try:
        if config_path.is_symlink() or not config_path.is_file():
            return False
        original = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if LEGACY_DATA_DIRNAME not in original:
        return False

    changed = False
    lines: list[str] = []
    for line in original.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        match = _CONFIG_STRING_LINE_RE.match(body)
        if match:
            new_value = resolve_legacy_data_path(match.group(2), home)
            if new_value != match.group(2):
                body = f'{match.group(1)}"{new_value}"{match.group(3)}'
                changed = True
        lines.append(body + eol)
    if not changed:
        return False

    rewritten = "".join(lines)
    tomllib.loads(rewritten)
    backup = _guard_penpal_path(data_dir / CONFIG_BACKUP_NAME)
    if not backup.exists():
        atomic_write_text(backup, original)
    atomic_write_text(config_path, rewritten)
    read_toml(config_path)
    return True


_TABLE_HEADER_RE = re.compile(r"^[ \t]*\[[^\]\n]+\][ \t]*(?:#.*)?$", re.M)


def _write_privacy_list(path: Path, key: str, values: list[str]) -> None:
    """Update one `[privacy]` string array without reformatting the rest of config.toml."""
    key_re = re.compile(r"^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*\[.*?\]", re.S | re.M)
    line = key + " = [" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    privacy = re.search(r"^[ \t]*\[privacy\][ \t]*(?:#.*)?$", text, re.M)
    if privacy is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new = f"{text}{sep}[privacy]\n{line}\n"
    else:
        nxt = _TABLE_HEADER_RE.search(text, privacy.end())
        end = nxt.start() if nxt else len(text)
        body = text[privacy.end():end]
        if key_re.search(body):
            body = key_re.sub(lambda _m: line, body, count=1)
        else:
            body = "\n" + line + body
        new = text[: privacy.end()] + body + text[end:]
    parsed = Config.model_validate(tomllib.loads(new))  # raises on a broken edit; nothing written yet
    if getattr(parsed.privacy, key) != values:
        raise ValueError("could not update privacy." + key + " in " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, new)
    persisted = read_config(path)
    if getattr(persisted.privacy, key) != values:
        raise ValueError("could not verify privacy." + key + " in " + str(path))


def write_chat_apps_opt_in(path: Path, keys: list[str]) -> None:
    """Persist `[privacy] chat_apps_opt_in` in a user config.toml, leaving the rest of the file as is.

    Text edit (no TOML writer dependency): replaces the existing array, else adds the key under
    [privacy], else appends a [privacy] table. The result is re-parsed and validated before it is written.
    """
    _write_privacy_list(path, "chat_apps_opt_in", keys)


def normalize_url_deny_host(value: str) -> str:
    """Extract a canonical host from a URL or host, dropping scheme, path, port and leading www."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("website host cannot be empty")
    source = raw if "://" in raw else "//" + raw
    try:
        parts = urlsplit(source)
        host = parts.hostname
        parts.port  # validate the optional port instead of silently accepting malformed values
    except ValueError as exc:
        raise ValueError("website address must contain a valid host and port") from exc
    if not host:
        raise ValueError("website address must contain a host")
    host = host.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError("website address must contain a host")
    return host


def write_url_deny_hosts(path: Path, hosts: list[str]) -> None:
    """Persist `[privacy] url_deny_hosts`, leaving the rest of config.toml as is."""
    _write_privacy_list(path, "url_deny_hosts", hosts)


def set_chat_app_opt_in(name: str, allowed: bool, *, path: Path | None = None) -> tuple[str, list[str]]:
    """Allow / deny one chat app (``penpal privacy chat-apps allow|deny``, and the HUD switch).

    Returns the canonical key and the new opt-in list. Raises ValueError for an unknown app name.
    The daemon notices the updated config and reloads itself within about one minute.
    """
    key = normalize_opt_ins([name])[0]
    current = list(Config.load().privacy.chat_apps_opt_in)
    if allowed and key not in current:
        current.append(key)
    elif not allowed:
        current = [k for k in current if k != key]
    write_chat_apps_opt_in(path or get_config_path(), current)
    return key, current


def set_url_deny_host(host: str, blocked: bool, *, path: Path | None = None) -> tuple[str, list[str]]:
    """Block or unblock a website host and persist the effective deny list."""
    from penpal.utils.paths import get_config_path

    key = normalize_url_deny_host(host)
    current: list[str] = []
    for existing in Config.load().privacy.url_deny_hosts:
        try:
            value = normalize_url_deny_host(existing)
        except ValueError:
            value = existing.strip().lower()
        if value and value not in current:
            current.append(value)
    if blocked and key not in current:
        current.append(key)
    elif not blocked:
        current = [value for value in current if value != key]
    write_url_deny_hosts(path or get_config_path(), current)
    return key, current
