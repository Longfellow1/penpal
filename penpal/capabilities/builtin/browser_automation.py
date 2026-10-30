from __future__ import annotations

import subprocess

from penpal.capabilities.base import Capability, CheckResult, HealthState, Signal
from penpal.capabilities.builtin._common import now_ts
from penpal.capture.browser_automation_state import (  # noqa: F401  (re-exported for compat)
    _DENIED_BROWSERS_KEY,
    _DENIED_FAILURES_KEY,
    _DENIED_DAILY_LIMIT,
    _load_denied_browsers,
    clear_browser_automation_denied_browsers_on_startup,
    is_browser_automation_denied,
    mark_browser_automation_denied,
)

import logging
logger = logging.getLogger(__name__)


_AUTOMATION_ACTION = "open://x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"


class BrowserAutomationCapability(Capability):
    name = "browser_automation"
    level_when_failed = "warn"
    label_when_failed = "采集建议"
    error_codes = {"browser_automation_denied"}

    _HINT_PREFIX = "浏览器自动化权限未授权，请前往系统设置 → 隐私与安全性 → 自动化，勾选 Penpal 对 "
    _HINT_SUFFIX = " 的控制权限"

    def precheck(self) -> CheckResult:
        return CheckResult(ok=True, code="skipped")

    def monitor(self) -> HealthState:
        from penpal.capture.watchers.browser_discovery import discover_http_handler_apps

        try:
            relevant = tuple(discover_http_handler_apps())
        except Exception:
            relevant = ()

        if not relevant:
            return HealthState(ok=True, code="ok", last_checked=now_ts(), detail=None)

        denied = _load_denied_browsers()
        for browser in relevant:
            if browser in denied:
                continue
            script = f'tell application "{browser}" to count of windows'
            try:
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=1.0,
                )
            except Exception as exc:
                logger.debug("monitor failed: %s", type(exc).__name__)
                continue
            stderr = (result.stderr or "").strip()
            if is_browser_automation_denied(stderr, result.returncode):
                mark_browser_automation_denied(browser)
                continue
            if result.returncode == 0:
                return HealthState(ok=True, code="ok", last_checked=now_ts(), detail=None)

        relevant_denied = sorted(set(_load_denied_browsers()) & set(relevant))
        if not relevant_denied:
            return HealthState(ok=True, code="ok", last_checked=now_ts(), detail=None)
        detail = f"{self._HINT_PREFIX}{', '.join(relevant_denied)}{self._HINT_SUFFIX}"
        return HealthState(
            ok=False,
            code="browser_automation_denied",
            last_checked=now_ts(),
            detail=detail,
        )

    def diagnose(self, state: HealthState) -> Signal:
        if state.ok:
            return Signal(level="ok", label="正常", hint="", action=None)
        if state.code == "browser_automation_denied":
            return Signal(
                level="warn",
                label="采集建议",
                hint=state.detail or f"{self._HINT_PREFIX}已安装浏览器{self._HINT_SUFFIX}",
                action=_AUTOMATION_ACTION,
            )
        return Signal(level="warn", label="采集建议", hint="浏览器自动化权限异常", action=None)
