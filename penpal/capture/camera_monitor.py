from __future__ import annotations

import re
import subprocess
import threading
from typing import Callable

from penpal.utils.logging import get_logger


logger = get_logger("camera_monitor")

_CMIO_PATTERN = re.compile(r"kCMIODevicePropertyDeviceIsRunningSomewhere.*?(\d)")
_CMIO_COMMAND = [
    "log",
    "stream",
    "--predicate",
    'subsystem == "com.apple.cmio"',
    "--style",
    "compact",
]


class CameraMonitor:
    def __init__(
        self,
        on_change: Callable[[bool], None],
        popen_factory: Callable[..., subprocess.Popen] | None = None,
    ):
        self._on_change = on_change
        self._popen_factory = popen_factory
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._state_lock = threading.Lock()
        self._active_count = 0
        self._in_use = False

    @property
    def in_use(self) -> bool:
        with self._state_lock:
            return self._in_use

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        process = self._process
        if process is not None:
            try:
                process.kill()
            except Exception:
                logger.warning("Camera monitor subprocess kill failed")
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    stdout.close()
                except Exception as exc:
                    logger.debug("stop failed: %s", type(exc).__name__)
                    pass
        thread = self._thread
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=5)
        self._thread = None
        self._process = None

    def _run(self) -> None:
        try:
            popen_factory = self._popen_factory or subprocess.Popen
            process = popen_factory(
                _CMIO_COMMAND,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            logger.warning("Camera monitor failed to start: %s", exc)
            return

        self._process = process
        try:
            stdout = getattr(process, "stdout", None)
            if stdout is None:
                logger.warning("Camera monitor started without stdout")
                return
            for line in stdout:
                if not self._running.is_set():
                    break
                self._handle_line(line)
            if self._running.is_set():
                logger.warning("Camera monitor subprocess exited unexpectedly")
        except Exception as exc:
            logger.warning("Camera monitor crashed: %s", exc)
        finally:
            self._process = None
            self._running.clear()

    def _handle_line(self, line: str) -> None:
        changed = False
        next_in_use = False
        with self._state_lock:
            previous = self._in_use
            active_count = self._active_count
            for match in _CMIO_PATTERN.finditer(line):
                value = match.group(1)
                if value == "1":
                    active_count += 1
                elif value == "0" and active_count > 0:
                    active_count -= 1
            next_in_use = active_count > 0
            self._active_count = active_count
            self._in_use = next_in_use
            changed = next_in_use != previous
        if changed:
            try:
                self._on_change(next_in_use)
            except Exception as exc:
                logger.warning("Camera monitor callback failed: %s", exc)
