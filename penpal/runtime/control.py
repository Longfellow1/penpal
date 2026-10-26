"""The only LaunchAgent mutation surface for Penpal runtime operations."""
from __future__ import annotations

import plistlib
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from penpal.foundation.runtime_launchd import probe_label
from penpal.foundation.runtime_plist import launchd_jobs
from penpal.runtime.evaluate import RuntimeState, RuntimeStatus, evaluate
from penpal.utils.paths import get_launchd_plist_path

DAEMON_LABEL = "com.penpal.daemon"
# KeyPulse-era jobs unloaded before the data-dir migration (ENGINEERING §7: remove in 0.5.0).
LEGACY_LAUNCHD_LABELS = (
    "com.keypulse.daemon", "com.keypulse.healthcheck", "com.keypulse.hud",
    "com.keypulse.obsidian-sync", "com.keypulse.obsidian-sync-hourly",
)
TEARDOWN_TIMEOUT_SEC = 10.0
RESTART_SUCCESS_TIMEOUT_SEC = 40.0
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_BACKOFF_SEC = (0.25, 0.5)
POLL_INTERVAL_SEC = 0.1
Runner = Callable[..., subprocess.CompletedProcess]
Probe = Callable[..., dict]
Evaluator = Callable[[], RuntimeStatus]


def _default_runner(command: list[str], **kwargs):
    if os.environ.get("PENPAL_ISOLATED") == "1":
        raise OSError("LaunchAgent control is disabled in isolated environments")
    if sys.platform != "darwin":
        raise OSError("LaunchAgent control is only available on macOS")
    return subprocess.run(command, **kwargs)


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    action: str
    generation: str | None = None
    error_zh: str = ""
    error_en: str = ""


def _target(label: str) -> str:
    import os

    return f"gui/{os.getuid()}/{label}"


def _domain_target() -> str:
    import os

    return f"gui/{os.getuid()}"


def _run(command: list[str], runner: Runner) -> tuple[bool, str]:
    try:
        result = runner(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        return False, str(exc)
    detail = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part).strip()
    return result.returncode == 0, detail


def _loaded(label: str, runner: Runner, probe: Probe) -> bool:
    return bool(probe(label, runner=runner).get("loaded"))


def _plist_matches_install(path: Path) -> bool:
    try:
        current = plistlib.loads(path.read_bytes())
        desired = launchd_jobs().get(DAEMON_LABEL)
    except (OSError, ValueError, FileNotFoundError):
        return False
    return current == desired


def _progress_seq(status: RuntimeStatus) -> int | None:
    progress = status.progress.get("core_loop")
    seq = getattr(progress, "seq", None)
    return seq if isinstance(seq, int) else None


def _new_generation_progress(status: RuntimeStatus, old_generation: str | None) -> bool:
    return (
        status.generation is not None
        and status.generation != old_generation
        and (_progress_seq(status) or 0) > 0
        and status.state in {RuntimeState.RUNNING, RuntimeState.PAUSED}
    )


def _wait_until(
    predicate: Callable[[], bool], *, timeout: float,
    sleep: Callable[[float], None], monotonic: Callable[[], float],
) -> bool:
    deadline = monotonic() + timeout
    while True:
        if predicate():
            return True
        if monotonic() >= deadline:
            return False
        sleep(min(POLL_INTERVAL_SEC, max(0.0, deadline - monotonic())))


def _spawn_loaded(label: str, runner: Runner) -> None:
    """Start a job that bootstrap only loaded. launchd can leave a freshly bootstrapped job pending as a
    "speculative" spawn (runs = 0) instead of honoring RunAtLoad; a plain kickstart starts it now and is a
    no-op for a job already running. A failure here surfaces as the caller's generation wait timing out."""
    _run(["launchctl", "kickstart", _target(label)], runner)


def _bootstrap_with_retry(
    plist_path: Path, *, runner: Runner, sleep: Callable[[float], None],
) -> tuple[bool, str]:
    detail = ""
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        ok, detail = _run(["launchctl", "bootstrap", _domain_target(), str(plist_path)], runner)
        if ok:
            _spawn_loaded(plist_path.stem, runner)
            return True, ""
        if attempt < len(BOOTSTRAP_BACKOFF_SEC):
            sleep(BOOTSTRAP_BACKOFF_SEC[attempt])
    return False, detail


def _result_error(action: str, zh: str, en: str, generation: str | None = None) -> ControlResult:
    return ControlResult(False, action, generation, zh, en)


def _wait_for_started_generation(
    *, old_generation: str | None, evaluator: Evaluator, sleep: Callable[[float], None],
    monotonic: Callable[[], float], timeout: float = RESTART_SUCCESS_TIMEOUT_SEC,
) -> RuntimeStatus | None:
    latest: RuntimeStatus | None = None

    def ready() -> bool:
        nonlocal latest
        latest = evaluator()
        return _new_generation_progress(latest, old_generation)

    return latest if _wait_until(ready, timeout=timeout, sleep=sleep, monotonic=monotonic) else None


def start(
    *, runner: Runner = _default_runner, probe: Probe = probe_label, evaluator: Evaluator = evaluate,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
) -> ControlResult:
    path = get_launchd_plist_path(DAEMON_LABEL)
    if not path.is_file():
        return _result_error("start", "尚未安装后台服务，请运行 penpal install init。", "The daemon is not installed; run penpal install init.")
    if not _plist_matches_install(path):
        return restart("start", runner=runner, probe=probe, evaluator=evaluator, sleep=sleep, monotonic=monotonic)

    before = evaluator()
    if _loaded(DAEMON_LABEL, runner, probe):
        if before.state in {RuntimeState.RUNNING, RuntimeState.PAUSED}:
            return ControlResult(True, "already_running", before.generation)
        if before.state in {RuntimeState.HUNG, RuntimeState.OUTDATED}:
            return restart("start", runner=runner, probe=probe, evaluator=evaluator, sleep=sleep, monotonic=monotonic)
        ok, detail = _run(["launchctl", "kickstart", _target(DAEMON_LABEL)], runner)
        action = "kickstart"
    else:
        if before.pid is not None and before.state not in {RuntimeState.NOT_RUNNING, RuntimeState.NOT_INSTALLED}:
            return _result_error(
                "not_managed",
                "后台服务正在运行，但未由 launchd 管理；请通过它的进程管理器停止。",
                "The daemon is running outside launchd; stop it through its process supervisor.",
                before.generation,
            )
        ok, detail = _bootstrap_with_retry(path, runner=runner, sleep=sleep)
        action = "bootstrap"
    if not ok:
        return _result_error(action, f"启动后台服务失败：{detail or 'launchctl 返回错误'}", f"Could not start the daemon: {detail or 'launchctl returned an error'}")
    started = _wait_for_started_generation(
        old_generation=before.generation, evaluator=evaluator, sleep=sleep, monotonic=monotonic,
    )
    if started is None:
        latest = evaluator()
        new_generation = latest.generation if latest.generation != before.generation else None
        return _result_error(action, "后台服务未在时限内报告新代次及核心循环进度。", "The daemon did not report a new generation with core-loop progress before the timeout.", new_generation)
    return ControlResult(True, action, started.generation)


def stop(
    *, runner: Runner = _default_runner, probe: Probe = probe_label,
    evaluator: Evaluator = evaluate,
    sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
) -> ControlResult:
    if not _loaded(DAEMON_LABEL, runner, probe):
        status = evaluator()
        if status.state not in {RuntimeState.NOT_RUNNING, RuntimeState.NOT_INSTALLED}:
            return _result_error(
                "not_managed",
                "后台服务正在运行，但未由 launchd 管理；请通过它的进程管理器停止。",
                "The daemon is running outside launchd; stop it through its process supervisor.",
                status.generation,
            )
        return ControlResult(True, "already_stopped")
    ok, detail = _run(["launchctl", "bootout", _target(DAEMON_LABEL)], runner)
    if not ok and _loaded(DAEMON_LABEL, runner, probe):
        return _result_error("bootout", f"停止后台服务失败：{detail or 'launchctl 返回错误'}", f"Could not stop the daemon: {detail or 'launchctl returned an error'}")
    gone = _wait_until(lambda: not _loaded(DAEMON_LABEL, runner, probe), timeout=TEARDOWN_TIMEOUT_SEC, sleep=sleep, monotonic=monotonic)
    if not gone:
        return _result_error("bootout", "等待后台服务退出超时。", "Timed out waiting for the daemon to stop.")
    return ControlResult(True, "bootout")


def restart(
    reason: str = "manual", *, runner: Runner = _default_runner, probe: Probe = probe_label,
    evaluator: Evaluator = evaluate, sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic, force_reload: bool = False,
) -> ControlResult:
    logging.getLogger(__name__).info("daemon restart requested: %s", reason)
    path = get_launchd_plist_path(DAEMON_LABEL)
    if not path.is_file():
        return _result_error("restart", "尚未安装后台服务，请运行 penpal install init。", "The daemon is not installed; run penpal install init.")

    before = evaluator()
    old_generation = before.generation
    loaded = _loaded(DAEMON_LABEL, runner, probe)
    if loaded and _plist_matches_install(path) and not force_reload:
        ok, detail = _run(["launchctl", "kickstart", "-k", _target(DAEMON_LABEL)], runner)
        action = "kickstart"
        if not ok:
            return _result_error(action, f"重启后台服务失败：{detail or 'launchctl 返回错误'}", f"Could not restart the daemon: {detail or 'launchctl returned an error'}")
    else:
        if loaded:
            bootout_ok, detail = _run(["launchctl", "bootout", _target(DAEMON_LABEL)], runner)
            if not bootout_ok and _loaded(DAEMON_LABEL, runner, probe):
                return _result_error("bootout", f"卸载旧后台服务失败：{detail or 'launchctl 返回错误'}", f"Could not boot out the old daemon: {detail or 'launchctl returned an error'}")
            gone = _wait_until(lambda: not _loaded(DAEMON_LABEL, runner, probe), timeout=TEARDOWN_TIMEOUT_SEC, sleep=sleep, monotonic=monotonic)
            if not gone:
                return _result_error("bootout", "等待旧后台服务退出超时，未尝试重新加载。", "Timed out waiting for the old daemon to stop; bootstrap was not attempted.")
        bootstrapped, detail = _bootstrap_with_retry(path, runner=runner, sleep=sleep)
        action = "bootout_bootstrap" if loaded else "bootstrap"
        if not bootstrapped:
            return _result_error(action, f"重新加载后台服务失败：{detail or 'launchctl 返回错误'}", f"Could not bootstrap the daemon: {detail or 'launchctl returned an error'}")

    started = _wait_for_started_generation(
        old_generation=old_generation, evaluator=evaluator, sleep=sleep, monotonic=monotonic,
    )
    if started is None:
        latest = evaluator()
        new_generation = latest.generation if latest.generation != old_generation else None
        return _result_error(action, "重启后未观察到新代次的核心循环进度。", "Restart completed without observing core-loop progress in a new generation.", new_generation)
    return ControlResult(True, action, started.generation)


def load_job(
    label: str, plist_path: Path, *, runner: Runner = _default_runner, probe: Probe = probe_label,
    spawn: bool = True,
) -> bool:
    """Load an auxiliary Penpal LaunchAgent, such as the periodic healthcheck. ``spawn=False`` only registers
    it (a login item that should start at the next login, not now)."""
    if _loaded(label, runner, probe):
        return True
    ok, _ = _run(["launchctl", "bootstrap", _domain_target(), str(plist_path)], runner)
    if ok and spawn:
        _spawn_loaded(label, runner)
    return ok


def unload_job(label: str, plist_path: Path | None = None, *, runner: Runner = _default_runner, probe: Probe = probe_label) -> bool:
    """Unload a LaunchAgent. Plist deletion stays with the installer."""
    target = _target(label) if plist_path is None else _target(label)
    ok, _ = _run(["launchctl", "bootout", target], runner)
    return ok or not _loaded(label, runner, probe)


def unload_legacy_jobs(*, runner: Runner = _default_runner, probe: Probe = probe_label) -> list[str]:
    """Stop historical KeyPulse jobs before the data-directory migration runs."""
    failures: list[str] = []
    for label in LEGACY_LAUNCHD_LABELS:
        loaded = _loaded(label, runner, probe)
        if not loaded:
            continue
        ok, detail = _run(["launchctl", "bootout", _target(label)], runner)
        if not ok and _loaded(label, runner, probe):
            failures.append(f"{label}: {detail or 'still loaded'}")
    return failures
