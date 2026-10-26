"""The single mapping from daemon evidence to user-visible runtime health."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import ctypes
import os
import sys
from typing import Callable

from penpal.config import Config, config_fingerprint
from penpal.foundation.runtime_identity import (
    RuntimeIdentity,
    code_fingerprint as _code_fingerprint,
    installed_fingerprint as _installed_fingerprint,
    installed_package_version,
    loaded_package_version,
    make_identity,
)
from penpal.foundation.hud_record import HudRecord, read_hud_record
from penpal.foundation.runtime_launchd import is_installed, probe_daemon, probe_label, process_start_time
from penpal.foundation.runtime_record import RECORD_WRITE_INTERVAL_SEC, Component, RuntimeRecord, read_record
from penpal.i18n import T
from penpal.store.db import read_schema_version
from penpal.store.migrations import SCHEMA_VERSION

HUNG_AFTER_SEC = RECORD_WRITE_INTERVAL_SEC * 3
STARTING_GRACE_SEC = RECORD_WRITE_INTERVAL_SEC * 3
HUD_MAIN_THREAD_STALE_SEC = 45.0
WATCHER_PERMISSION_EVIDENCE_SEC = 30 * 60
CORE_LOOP_NAME = "core_loop"


class RuntimeState(str, Enum):
    NOT_INSTALLED = "not_installed"
    NOT_RUNNING = "not_running"
    STARTING = "starting"
    RUNNING = "running"
    HUNG = "hung"
    OUTDATED = "outdated"
    PAUSED = "paused"


STATE_COPY: dict[RuntimeState, tuple[str, str, str, str, str]] = {
    RuntimeState.NOT_INSTALLED: ("尚未安装", "not installed", "后台服务尚未安装。", "The background service is not installed.", "penpal install init"),
    RuntimeState.NOT_RUNNING: ("未运行", "not running", "后台服务未运行。", "The background service is not running.", "penpal start"),
    RuntimeState.STARTING: ("正在启动", "starting", "后台服务正在启动。", "The background service is starting.", ""),
    RuntimeState.RUNNING: ("正在记录", "recording", "后台服务运行正常。", "The background service is running.", ""),
    RuntimeState.HUNG: ("后台无响应", "not responding", "后台服务核心循环没有推进。", "The daemon core loop has stopped making progress.", "penpal restart"),
    RuntimeState.OUTDATED: ("后台代码已过期", "daemon code is outdated", "后台服务仍在运行旧版本代码。", "The daemon is running outdated code.", "penpal restart"),
    RuntimeState.PAUSED: ("已暂停", "paused", "采集已暂停。", "Capture is paused.", "penpal resume"),
}
_FAILED_STATES = frozenset({
    RuntimeState.NOT_INSTALLED, RuntimeState.NOT_RUNNING, RuntimeState.HUNG, RuntimeState.OUTDATED,
})


def _identity_config(config: Config | None, config_path: Path | str | None) -> Config:
    try:
        if config_path is not None:
            from penpal.config import read_config

            return read_config(config_path)
        return Config.load()
    except (OSError, ValueError):
        # The raw config fingerprint still detects a broken edit. Keep the running process
        # on its last valid settings long enough to exec the replacement process.
        if config is not None:
            return config
        raise


def current_identity(
    config: Config | None = None,
    *,
    config_path: Path | str | None = None,
) -> RuntimeIdentity:
    """Resolve the one installed identity shared by the evaluator and runtime consumers."""
    current_config = _identity_config(config, config_path)
    return make_identity(
        code_fingerprint=_installed_fingerprint(),
        package_version=installed_package_version(),
        config_fingerprint=config_fingerprint(Path(config_path) if config_path is not None else None),
        database_schema_version=read_schema_version(current_config.db_path_expanded),
        supported_schema_version=SCHEMA_VERSION,
    )


def loaded_identity(
    config: Config | None = None,
    *,
    config_path: Path | str | None = None,
) -> RuntimeIdentity:
    """Resolve the identity captured from this process's loaded package at import time."""
    current_config = _identity_config(config, config_path)
    return make_identity(
        code_fingerprint=_code_fingerprint(),
        package_version=loaded_package_version(),
        config_fingerprint=config_fingerprint(Path(config_path) if config_path is not None else None),
        database_schema_version=read_schema_version(current_config.db_path_expanded),
        supported_schema_version=SCHEMA_VERSION,
    )


@dataclass(frozen=True)
class Issue:
    component: str
    severity: str
    message_zh: str
    message_en: str
    fix_command: str = ""
    label_zh: str = ""
    label_en: str = ""


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    issues: tuple[Issue, ...] = ()
    components: dict[str, Component] = field(default_factory=dict)
    progress: dict[str, object] = field(default_factory=dict)
    pid: int | None = None
    generation: str | None = None
    managed: bool = False
    hud: "HudStatus | None" = None

    @property
    def code(self) -> str:
        return self.state.value

    @property
    def fix_command(self) -> str:
        return STATE_COPY[self.state][4]

    @property
    def failed(self) -> bool:
        return self.state in _FAILED_STATES


@dataclass(frozen=True)
class HudStatus:
    pid: int
    generation: str
    alive: bool
    status_item_created: bool
    status_item_visible: bool | None
    last_model_built_at: str | None
    last_main_thread_tick_at: str | None
    main_thread_tick_age_sec: float | None


def state_text(state: RuntimeState | str, *, language: str | None = None) -> str:
    value = state if isinstance(state, RuntimeState) else RuntimeState(state)
    if language is None:
        return T(STATE_COPY[value][0], STATE_COPY[value][1])
    return STATE_COPY[value][0 if language == "zh" else 1]


def state_message(status: RuntimeStatus, *, language: str | None = None) -> str:
    issue = next((item for item in status.issues if item.component != "daemon"), None) if status.state == RuntimeState.RUNNING else None
    if issue is not None:
        message = issue.message_zh if language == "zh" else issue.message_en if language == "en" else ""
        if not message:
            from penpal.i18n import current_lang

            message = issue.message_zh if current_lang() == "zh" else issue.message_en
        fix = issue.fix_command
    else:
        message = state_text(status.state, language=language)
        fix = status.fix_command
    if fix:
        if language == "zh":
            return f"Penpal：{message} 请运行 `{fix}`。"
        if language == "en":
            return f"Penpal: {message}. Run `{fix}`."
        from penpal.i18n import T

        return T(f"Penpal：{message} 请运行 `{fix}`。", f"Penpal: {message}. Run `{fix}`.")
    if language == "zh":
        return f"Penpal：{message}。"
    if language == "en":
        return f"Penpal: {message}."
    from penpal.i18n import T

    return T(f"Penpal：{message}。", f"Penpal: {message}.")


def launchd_label_loaded(label: str) -> bool:
    """Expose read-only launchd state to install diagnostics without duplicating parsing."""
    return bool(probe_label(label).get("loaded"))


def watcher_recently_emitted(status: RuntimeStatus, name: str, now: datetime | None = None) -> bool:
    progress = status.progress.get(f"emit.{name}")
    last_done_at = getattr(progress, "last_done_at", None)
    age = _seconds_since(last_done_at, now or datetime.now(timezone.utc))
    return age is not None and age <= WATCHER_PERMISSION_EVIDENCE_SEC


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _seconds_since(value: str | None, now: datetime) -> float | None:
    then = _parse_time(value)
    return None if then is None else max(0.0, (now - then).total_seconds())


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int)]


def _read_last_system_wake_time() -> datetime | None:
    """Read macOS's last wake timestamp; unavailable platforms degrade to no wake evidence."""
    if sys.platform != "darwin":
        return None
    try:
        sysctlbyname = ctypes.CDLL(None, use_errno=True).sysctlbyname
        sysctlbyname.argtypes = [
            ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p, ctypes.c_size_t,
        ]
        sysctlbyname.restype = ctypes.c_int
        value = _Timeval()
        size = ctypes.c_size_t(ctypes.sizeof(value))
        result = sysctlbyname(b"kern.waketime", ctypes.byref(value), ctypes.byref(size), None, 0)
        if result != 0 or size.value < ctypes.sizeof(value) or not 0 <= value.tv_usec < 1_000_000:
            return None
        return datetime.fromtimestamp(value.tv_sec + value.tv_usec / 1_000_000, timezone.utc)
    except (AttributeError, OSError, OverflowError, ValueError):
        return None


def _normalise_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _last_progress_age(last_done_at: str | None, last_wake: datetime | None, now: datetime) -> float | None:
    anchors = [value for value in (_parse_time(last_done_at), _normalise_datetime(last_wake)) if value is not None]
    return None if not anchors else max(0.0, (now - max(anchors)).total_seconds())


def _process_started_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _unrecorded_process_status(pid: int, started_at: str | None, now: datetime) -> RuntimeStatus:
    started = _process_started_at(started_at)
    age = None if started is None else max(0.0, (now - started).total_seconds())
    state = RuntimeState.STARTING if age is None or age < STARTING_GRACE_SEC else RuntimeState.HUNG
    issue = _state_issue(state)
    return RuntimeStatus(state, (issue,) if issue else (), pid=pid, managed=True)


def _paused() -> bool:
    from penpal.capture.capture_control import is_capture_paused
    from penpal.store import db

    # evaluate() is the first call of every status entry point (doctor, hud status, the HUD, the watchdog),
    # so it opens the configured DB itself instead of relying on the caller having done so.
    if not db.is_initialized():
        db.init_db(Config.load().db_path_expanded)
    return is_capture_paused()


def _state_issue(state: RuntimeState) -> Issue | None:
    if state in (RuntimeState.RUNNING,):
        return None
    severity = "error" if state in _FAILED_STATES else "info"
    text = STATE_COPY[state]
    return Issue("daemon", severity, text[2], text[3], text[4], "后台服务", "Background service")


def _hud_status(
    record: HudRecord | None,
    *,
    now: datetime,
    last_wake: datetime | None = None,
    process_start_reader: Callable[[int], str | None],
) -> tuple[HudStatus | None, tuple[Issue, ...]]:
    if record is None:
        return None, ()
    current_start = process_start_reader(record.pid)
    alive = current_start is not None and current_start == record.process_start_time
    # Sleep suspends the HUD too; count staleness from the later of the last tick and the last wake.
    age = _last_progress_age(record.last_main_thread_tick_at, last_wake, now)
    status = HudStatus(
        pid=record.pid,
        generation=record.generation,
        alive=alive,
        status_item_created=record.status_item_created,
        status_item_visible=record.status_item_visible,
        last_model_built_at=record.last_model_built_at,
        last_main_thread_tick_at=record.last_main_thread_tick_at,
        main_thread_tick_age_sec=age,
    )
    issues: list[Issue] = []
    if alive and record.status_item_created and record.status_item_visible is False:
        issues.append(Issue(
            "hud", "error",
            "菜单栏图标被系统隐藏：系统设置 → 菜单栏 → 打开 Penpal。",
            "The menu bar item is hidden. Open System Settings → Menu Bar and enable Penpal.",
            label_zh="菜单栏 HUD", label_en="Menu bar HUD",
        ))
    # Unknown visibility is not reported: macOS 26 exposes no API for the System Settings toggle, so a
    # standing "could not confirm" warning would fire for nearly every user. R7 covers it by hand.
    if alive and age is not None and age > HUD_MAIN_THREAD_STALE_SEC:
        issues.append(Issue(
            "hud", "error",
            "HUD 无响应：主线程超过 45 秒没有响应。可运行 `penpal hud stop` 后从 Finder 重新打开 Penpal.app。",
            "HUD is not responding: its main thread has not responded for over 45 seconds. Run `penpal hud stop`, then reopen Penpal.app from Finder.",
            "penpal hud stop", "菜单栏 HUD", "Menu bar HUD",
        ))
    return status, tuple(issues)


def _attach_hud(
    status: RuntimeStatus,
    hud_status: HudStatus | None,
    hud_issues: tuple[Issue, ...],
) -> RuntimeStatus:
    return RuntimeStatus(
        state=status.state,
        issues=status.issues + hud_issues,
        components=status.components,
        progress=status.progress,
        pid=status.pid,
        generation=status.generation,
        managed=status.managed,
        hud=hud_status,
    )


_COMPONENT_LABELS: dict[str, tuple[str, str]] = {
    "storage": ("采集存储", "Capture storage"),
    "permission.accessibility": ("辅助功能权限", "Accessibility permission"),
    "permission.input_monitoring": ("输入监控权限", "Input Monitoring permission"),
    "core_loop": ("后台核心循环", "Daemon core loop"),
}
_WATCHER_LABELS: dict[str, tuple[str, str]] = {
    "window": ("前台窗口", "Window watcher"),
    "clipboard": ("剪贴板", "Clipboard watcher"),
    "keyboard_chunk": ("键盘片段", "Keyboard watcher"),
    "ax_text": ("正文采集", "Accessibility text watcher"),
    "browser": ("浏览器", "Browser watcher"),
    "browser_url": ("浏览器地址", "Browser URL watcher"),
    "idle": ("空闲检测", "Idle watcher"),
}


def _component_issues(components: dict[str, Component]) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    for name, component in sorted(components.items()):
        label_zh, label_en = _COMPONENT_LABELS.get(name, _WATCHER_LABELS.get(name, (name, name)))
        if component.state == "missing_permission":
            issues.append(Issue(name, "warning", f"{label_zh}缺少系统权限。", f"{label_en} is missing a system permission.", "penpal doctor", label_zh, label_en))
        elif component.state == "storage_error":
            if component.detail == "disk_full":
                zh, en = "磁盘已满，采集暂停写入；释放空间后会自动恢复。", "Disk is full; capture will resume after space is freed."
            else:
                zh, en = "采集存储发生错误。", "Capture storage has an error."
            issues.append(Issue(name, "error", zh, en, "penpal doctor", label_zh, label_en))
        elif component.state == "gave_up":
            issues.append(Issue(name, "error", f"{label_zh}已停止重试。", f"{label_en} has exhausted its restart attempts.", "penpal doctor", label_zh, label_en))
    return tuple(issues)


def evaluate(
    now: datetime | None = None,
    *,
    record_reader: Callable[[], RuntimeRecord | None] = read_record,
    launchd_probe: Callable[[], dict] = probe_daemon,
    installed_fingerprint: Callable[[], str] | None = None,
    identity_reader: Callable[[], RuntimeIdentity] = current_identity,
    paused_reader: Callable[[], bool] = _paused,
    wake_time_reader: Callable[[], datetime | None] = _read_last_system_wake_time,
    hud_record_reader: Callable[[], HudRecord | None] = read_hud_record,
) -> RuntimeStatus:
    """Evaluate installed code and daemon progress; idle event streams are healthy."""
    now = now or datetime.now(timezone.utc)
    identity = identity_reader()
    if installed_fingerprint is not None:
        identity = RuntimeIdentity(
            code_fingerprint=installed_fingerprint(),
            package_version=identity.package_version,
            config_fingerprint=identity.config_fingerprint,
            database_schema_version=identity.database_schema_version,
            supported_schema_version=identity.supported_schema_version,
        )
    last_wake = _normalise_datetime(wake_time_reader())
    record = record_reader()
    hud_status, hud_issues = _hud_status(
        hud_record_reader(), now=now, last_wake=last_wake, process_start_reader=process_start_time,
    )
    if identity.schema_is_newer_than_code:
        state = RuntimeState.OUTDATED
        issue = _state_issue(state)
        return _attach_hud(RuntimeStatus(
            state, (issue,) if issue else (),
            pid=record.pid if record else None,
            generation=record.generation if record else None,
        ), hud_status, hud_issues)
    launchd = launchd_probe()
    loaded = bool(launchd.get("loaded"))
    launchd_pid = launchd.get("pid")

    if loaded:
        if not isinstance(launchd_pid, int):
            state = RuntimeState.NOT_RUNNING
            issue = _state_issue(state)
            return _attach_hud(RuntimeStatus(state, (issue,) if issue else (), managed=loaded), hud_status, hud_issues)
        launchd_start = process_start_time(launchd_pid)
        if not launchd_start:
            state = RuntimeState.NOT_RUNNING
            issue = _state_issue(state)
            return _attach_hud(RuntimeStatus(state, (issue,) if issue else (), pid=launchd_pid, managed=loaded), hud_status, hud_issues)
        if record is None or launchd_pid != record.pid or launchd_start != record.process_start_time:
            return _attach_hud(_unrecorded_process_status(launchd_pid, launchd_start, now), hud_status, hud_issues)
    elif record is None:
        state = RuntimeState.NOT_INSTALLED if not is_installed() else RuntimeState.NOT_RUNNING
        issue = _state_issue(state)
        return _attach_hud(RuntimeStatus(state, (issue,) if issue else (), managed=loaded), hud_status, hud_issues)

    if not loaded:
        current_start = process_start_time(record.pid)
        if not current_start or current_start != record.process_start_time:
            state = RuntimeState.NOT_RUNNING
            issue = _state_issue(state)
            return _attach_hud(RuntimeStatus(state, (issue,) if issue else (), pid=record.pid, generation=record.generation, managed=loaded), hud_status, hud_issues)

    if record.code_fingerprint != identity.code_fingerprint or record.version != identity.package_version:
        state = RuntimeState.OUTDATED
    elif paused_reader():
        state = RuntimeState.PAUSED
    else:
        age = _last_progress_age(
            record.progress.get(CORE_LOOP_NAME).last_done_at if CORE_LOOP_NAME in record.progress else None,
            last_wake,
            now,
        )
        startup_age = _seconds_since(record.started_at, now)
        if age is None or age > HUNG_AFTER_SEC:
            state = RuntimeState.STARTING if startup_age is not None and startup_age < STARTING_GRACE_SEC else RuntimeState.HUNG
        else:
            state = RuntimeState.RUNNING

    issues = tuple(filter(None, (_state_issue(state),))) + _component_issues(record.components)
    return _attach_hud(RuntimeStatus(
        state, issues, record.components, record.progress, record.pid, record.generation, loaded,
    ), hud_status, hud_issues)
