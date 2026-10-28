"""AppKit-free HUD identity checks and in-place process replacement or bundle relaunch."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Callable, Sequence

from penpal.foundation.runtime_identity import IdentityWatcher, RuntimeIdentity
from penpal.runtime.process import current_exec_command, exec_self

logger = logging.getLogger(__name__)

# macOS 26 hides the status item of a HUD that execv's itself (field evidence 2026-10-28), so a bundle-launched HUD
# reloads by quitting and letting LaunchServices open the bundle again once this pid has exited.
RELAUNCH_SCRIPT = 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; exec /usr/bin/open "$2"'


def bundle_path_from_launcher(launcher_path: str | None) -> str | None:
    """`X.app/Contents/MacOS/Penpal` -> `X.app`; None when the launcher is not inside an app bundle."""
    if not launcher_path:
        return None
    path = os.path.normpath(launcher_path)
    for _ in range(3):
        path = os.path.dirname(path)
    return path if path.endswith(".app") else None


def relaunch_argv(pid: int, bundle: str) -> list[str]:
    return ["/bin/sh", "-c", RELAUNCH_SCRIPT, "sh", str(pid), str(bundle)]


def _default_spawn(argv: Sequence[str]) -> object:
    if os.environ.get("PENPAL_ISOLATED") == "1" or sys.platform != "darwin":
        raise OSError("Process spawning disabled in isolated or non-macOS environment")
    return subprocess.Popen(
        list(argv),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def hud_exec_command(native_launcher: str | None = None, argv: Sequence[str] | None = None) -> tuple[str, list[str]]:
    """Preserve the native app process image across HUD reloads when it was bundle-launched."""
    if native_launcher:
        current = list(argv if argv is not None else sys.argv)
        return native_launcher, [native_launcher, *current[2:]]
    executable, arguments = current_exec_command()
    return executable, list(arguments)


class HUDReloader:
    def __init__(
        self,
        started: RuntimeIdentity,
        reader: Callable[[], RuntimeIdentity],
        *,
        execv: Callable[[str, Sequence[str]], object] = os.execv,
        executable: str | None = None,
        argv: Sequence[str] | None = None,
        interval_sec: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        launcher: str | None = None,
        spawn: Callable[[Sequence[str]], object] = _default_spawn,
        pid: int | None = None,
    ) -> None:
        self.watcher = IdentityWatcher(started, reader, interval_sec=interval_sec, clock=clock)
        default_executable, default_argv = current_exec_command()
        self.executable = executable or default_executable
        self.argv = list(argv) if argv is not None else default_argv
        self.execv = execv
        self.launcher = launcher
        self.spawn = spawn
        self.pid = os.getpid() if pid is None else pid

    def check(self) -> bool:
        return self.watcher.check()

    def exec(self) -> None:
        exec_self(execv=self.execv, executable=self.executable, argv=self.argv)

    def reload(self) -> str:
        """Relaunch through LaunchServices when bundle-launched ("relaunch": the caller quits), else execv ("exec")."""
        bundle = bundle_path_from_launcher(self.launcher)
        if bundle:
            argv = relaunch_argv(self.pid, bundle)
            try:
                self.spawn(argv)
                return "relaunch"
            except OSError as exc:
                logger.warning("HUD relaunch helper spawn failed: %s", type(exc).__name__)
        self.exec()
        return "exec"
