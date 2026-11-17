from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from penpal.utils.paths import get_daemon_lock_path, get_pid_path


class FileLockTimeout(TimeoutError):
    """Raised when an exclusive file lock cannot be acquired before the deadline."""


@contextmanager
def exclusive_file_lock(lock_path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    """Hold an exclusive flock on lock_path, waiting at most timeout seconds."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileLockTimeout(f"等待文件锁超时: {lock_path.name}") from exc
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # The kernel releases the lock when fd is closed below.
                pass
    finally:
        os.close(fd)


class SingleInstanceLock:
    """Advisory process lock; the recorded PID is diagnostic only."""

    def __init__(self, lock_path: Path | None = None, pid_path: Path | None = None):
        self.lock_path = lock_path or get_daemon_lock_path()
        self.pid_path = pid_path or (get_pid_path() if lock_path is None else lock_path.with_suffix(".pid"))
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise

        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:
            # The kernel lock is authoritative; a diagnostic PID file is optional.
            pass
        return True

    def release(self):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def get_pid(self) -> int | None:
        """Return the locked process's diagnostic PID, if the flock is held."""
        try:
            fd = os.open(str(self.lock_path), os.O_RDONLY)
        except OSError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    return None
                try:
                    return int(os.read(fd, 64).decode().strip())
                except (ValueError, UnicodeError, OSError):
                    try:
                        return int(self.pid_path.read_text(encoding="utf-8").strip())
                    except (ValueError, UnicodeError, OSError):
                        return None
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
                return None
        finally:
            os.close(fd)

    def is_running(self) -> bool:
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                return exc.errno in (errno.EACCES, errno.EAGAIN)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)
