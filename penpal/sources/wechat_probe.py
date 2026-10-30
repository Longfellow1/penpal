from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from penpal.utils.paths import get_wechat_authorization_path

import logging
logger = logging.getLogger(__name__)


_WECHAT_APP_CANDIDATES = (
    Path("/Applications/WeChat.app"),
    Path("/Applications/微信.app"),
)


@dataclass
class WechatProbeResult:
    chatlog_installed: bool
    chatlog_path: str | None
    chatlog_version: str | None
    wechat_running: bool
    wechat_app_path: str | None
    user_authorized: bool


def probe() -> WechatProbeResult:
    """全部探测，任何步骤失败都 graceful（不抛）"""
    chatlog_path = _probe_chatlog_path()
    chatlog_version = _probe_chatlog_version()
    wechat_running = _probe_wechat_running()
    wechat_app_path = _probe_wechat_app_path()
    user_authorized = is_authorized()
    return WechatProbeResult(
        chatlog_installed=chatlog_path is not None,
        chatlog_path=chatlog_path,
        chatlog_version=chatlog_version,
        wechat_running=wechat_running,
        wechat_app_path=wechat_app_path,
        user_authorized=user_authorized,
    )


def mark_authorized(path: Path | None = None) -> None:
    """写 ~/.penpal/wechat-authorized 文件（含时间戳）。这是用户显式同意标志"""
    target = _resolve_authorization_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    target.write_text(f"authorized_at={timestamp}\n", encoding="utf-8")


def revoke_authorization(path: Path | None = None) -> None:
    """删 wechat-authorized 文件"""
    target = _resolve_authorization_path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def is_authorized(path: Path | None = None) -> bool:
    """检查 wechat-authorized 是否存在"""
    target = _resolve_authorization_path(path)
    try:
        return target.exists()
    except Exception:
        return False


def _resolve_authorization_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return get_wechat_authorization_path()


def _probe_chatlog_path() -> str | None:
    result = _run_command(["which", "chatlog"])
    if result is None or result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _probe_chatlog_version() -> str | None:
    result = _run_command(["chatlog", "--version"], timeout=5)
    if result is None or result.returncode != 0:
        return None
    stdout = (result.stdout or "").strip()
    if stdout:
        return stdout.splitlines()[0].strip() or None
    stderr = (result.stderr or "").strip()
    if stderr:
        return stderr.splitlines()[0].strip() or None
    return None


def _probe_wechat_running() -> bool:
    for command in (["pgrep", "-i", "wechat"], ["pgrep", "-f", "WeChat"]):
        result = _run_command(command)
        if result is None or result.returncode != 0:
            continue
        if (result.stdout or "").strip():
            return True
    return False


def _probe_wechat_app_path() -> str | None:
    for app_path in _WECHAT_APP_CANDIDATES:
        try:
            if app_path.exists():
                return str(app_path)
        except Exception as exc:
            logger.debug("probe wechat app path failed: %s", type(exc).__name__)
            continue
    return None


def _run_command(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
