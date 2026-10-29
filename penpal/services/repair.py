"""Single repair function mapping issues to corrective actions."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from penpal.capture.capture_control import set_capture_paused
from penpal.permissions import (
    CONTROL_CENTER_PANE,
    PERMISSION_SPECS,
    SCREEN_RECORDING_SPEC,
)
from penpal.runtime.control import (
    ControlResult,
    restart as default_restart,
    restart_hud as default_restart_hud,
)
from penpal.runtime.evaluate import Issue


@dataclass(frozen=True)
class RepairResult:
    issue_code: str
    attempted: bool
    fixed: bool
    message_zh: str
    message_en: str
    guidance: str = ""


def _default_opener(url: str) -> None:
    subprocess.run(["/usr/bin/open", url], check=False)


def _resolve_issue_code(issue: Issue | str | Any) -> str:
    if isinstance(issue, str):
        return issue
    if isinstance(issue, Issue):
        return issue.code
    return getattr(issue, "code", "")


def _resolve_permission_url(code: str) -> str | None:
    if not (code.startswith("permission.") and code.endswith(".missing_permission")):
        return None
    key = code.removeprefix("permission.").removesuffix(".missing_permission")
    for spec in (*PERMISSION_SPECS, SCREEN_RECORDING_SPEC):
        if spec.key == key:
            return spec.settings_pane
    return None


def _repair_daemon_restart(code: str, issue: Any, control: Any, opener: Any) -> RepairResult:
    restart_fn = getattr(control, "restart", None) if control is not None else default_restart
    if not callable(restart_fn):
        restart_fn = default_restart
    res: ControlResult = restart_fn("repair")
    if res.ok:
        return RepairResult(
            issue_code=code,
            attempted=True,
            fixed=True,
            message_zh="后台服务已成功重启。",
            message_en="Background service restarted successfully.",
        )
    return RepairResult(
        issue_code=code,
        attempted=True,
        fixed=False,
        message_zh=f"重启后台服务失败：{res.error_zh or '未知错误'}",
        message_en=f"Failed to restart background service: {res.error_en or 'unknown error'}",
    )


def _repair_paused(code: str, issue: Any, control: Any, opener: Any) -> RepairResult:
    resume_fn = getattr(control, "resume", None) if control is not None else None
    if callable(resume_fn):
        resume_fn()
    else:
        set_capture_paused(False)
    return RepairResult(
        issue_code=code,
        attempted=True,
        fixed=True,
        message_zh="活动采集已成功恢复。",
        message_en="Activity capture resumed successfully.",
    )


def _repair_hud_restart(code: str, issue: Any, control: Any, opener: Any) -> RepairResult:
    restart_hud_fn = getattr(control, "restart_hud", None) if control is not None else default_restart_hud
    if not callable(restart_hud_fn):
        restart_hud_fn = default_restart_hud
    res: ControlResult = restart_hud_fn()
    if res.ok:
        return RepairResult(
            issue_code=code,
            attempted=True,
            fixed=True,
            message_zh="HUD 已重新启动。",
            message_en="HUD relaunched successfully.",
        )
    return RepairResult(
        issue_code=code,
        attempted=True,
        fixed=False,
        message_zh=f"重新启动 HUD 失败：{res.error_zh or '未知错误'}",
        message_en=f"Failed to relaunch HUD: {res.error_en or 'unknown error'}",
    )


def _repair_hud_icon_hidden(code: str, issue: Any, control: Any, opener: Any) -> RepairResult:
    open_cmd = opener or _default_opener
    open_cmd(CONTROL_CENTER_PANE)
    guidance_zh = "已打开系统设置「控制中心」。请在「菜单栏」中将 Penpal 的开关关掉再打开。"
    guidance_en = "Opened Control Center in System Settings. In Menu Bar, turn Penpal off and on again."
    return RepairResult(
        issue_code=code,
        attempted=True,
        fixed=True,
        message_zh=guidance_zh,
        message_en=guidance_en,
        guidance=guidance_zh,
    )


def _repair_permission(code: str, issue: Any, control: Any, opener: Any) -> RepairResult:
    perm_url = _resolve_permission_url(code)
    if perm_url is not None:
        open_cmd = opener or _default_opener
        open_cmd(perm_url)
    guidance_zh = "已打开系统设置对应权限面板。请授予 Penpal 相应权限。"
    guidance_en = "Opened System Settings permission pane. Please grant the permission to Penpal."
    return RepairResult(
        issue_code=code,
        attempted=True,
        fixed=True,
        message_zh=guidance_zh,
        message_en=guidance_en,
        guidance=guidance_zh,
    )


_CANONICAL_REPAIR_DISPATCH: dict[str, Callable[[str, Any, Any, Any], RepairResult]] = {
    "daemon.not_running": _repair_daemon_restart,
    "daemon.hung": _repair_daemon_restart,
    "daemon.outdated": _repair_daemon_restart,
    "daemon.paused": _repair_paused,
    "hud.dead": _repair_hud_restart,
    "hud.hung": _repair_hud_restart,
    "hud.icon_hidden": _repair_hud_icon_hidden,
}


def _lookup_repair_handler(code: str) -> Callable[[str, Any, Any, Any], RepairResult] | None:
    if code in _CANONICAL_REPAIR_DISPATCH:
        return _CANONICAL_REPAIR_DISPATCH[code]
    if _resolve_permission_url(code) is not None:
        return _repair_permission
    return None


def is_fixable_issue(issue: Issue | str | Any) -> bool:
    """Return whether the repair function can act on this issue."""
    code = _resolve_issue_code(issue)
    return _lookup_repair_handler(code) is not None


def is_fixable_in_hud(issue: Issue | str | Any) -> bool:
    """Return whether the issue can be repaired from inside the HUD panel."""
    code = _resolve_issue_code(issue)
    if code in ("hud.dead", "hud.hung"):
        return False
    return is_fixable_issue(code)


def repair(
    issue: Issue | str | Any,
    *,
    control: Any = None,
    opener: Callable[[str], None] | None = None,
) -> RepairResult:
    """Map each issue to one repair action and return the result."""
    code = _resolve_issue_code(issue)
    handler = _lookup_repair_handler(code)
    if handler is not None:
        return handler(code, issue, control, opener)
    existing_message = issue.message_zh if isinstance(issue, Issue) else str(issue)
    return RepairResult(
        issue_code=code,
        attempted=False,
        fixed=False,
        message_zh=f"此问题需要手动处理：{existing_message}",
        message_en=f"Manual action required: {existing_message}",
        guidance=existing_message,
    )
