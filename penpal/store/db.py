import contextlib
import errno
import fcntl
import os
import sqlite3
import threading
import time
from pathlib import Path
from penpal.store.migrations import run_migrations
from penpal.store.migrations import SCHEMA_VERSION

_local = threading.local()

_db_path: Path | None = None
MIGRATION_LOCK_TIMEOUT_SEC = 60.0


class DatabaseSchemaTooNewError(RuntimeError):
    def __init__(self, recorded: int, supported: int = SCHEMA_VERSION):
        self.recorded = recorded
        self.supported = supported
        super().__init__(f"database schema {recorded} is newer than supported schema {supported}")


@contextlib.contextmanager
def _migration_lock(db_path: Path, timeout: float | None = None):
    """Acquire an exclusive flock on <db_path>.migrate.lock with a timeout."""
    if timeout is None:
        timeout = MIGRATION_LOCK_TIMEOUT_SEC
    lock_path = db_path.with_name(f"{db_path.name}.migrate.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    start_time = time.monotonic()
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() - start_time >= timeout:
                    raise TimeoutError(f"timed out after {timeout}s waiting for migration lock")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def read_schema_version(db_path: Path) -> int | None:
    """Read the database's recorded schema without opening it for writes."""
    target = Path(db_path)
    if not target.is_file():
        return 0
    conn = None
    try:
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_schema_version'"
        ).fetchone()
        if exists is None:
            return 0
        row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
        return int(row[0] or 0)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if conn is not None:
            conn.close()


def read_app_state(db_path: Path, key: str) -> str | None:
    """Read a single key from app_state table in read-only mode without migrations.

    Returns None if database does not exist, table does not exist, or key is not found.
    """
    target = Path(db_path)
    if not target.is_file():
        return None
    conn = None
    try:
        wal = target.with_name(f"{target.name}-wal")
        shm = target.with_name(f"{target.name}-shm")
        if wal.exists() and not shm.exists():
            return None
        immutable = not wal.exists()
        uri = f"{target.resolve().as_uri()}?mode=ro"
        if immutable:
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_state'"
        ).fetchone()
        if exists is None:
            return None
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if immutable and wal.exists():
            return None
        return str(row[0]) if row and row[0] is not None else None
    except (OSError, sqlite3.Error):
        return None
    finally:
        if conn is not None:
            conn.close()


def init_db(db_path: Path):
    global _db_path
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # One migrator at a time across processes; the version is re-read under the lock.
    with _migration_lock(target):
        recorded_schema = read_schema_version(target)
        if recorded_schema is not None and recorded_schema > SCHEMA_VERSION:
            raise DatabaseSchemaTooNewError(recorded_schema)
        if _db_path is not None and _db_path != target and hasattr(_local, "conn") and _local.conn:
            _local.conn.close()
            _local.conn = None
        _db_path = target
        run_migrations(_get_conn())


def is_initialized() -> bool:
    return _db_path is not None


def initialized_db_path() -> Path | None:
    return _db_path


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        if _db_path is None:
            raise RuntimeError("DB not initialized. Call init_db() first.")
        conn = sqlite3.connect(str(_db_path), timeout=15, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def get_conn() -> sqlite3.Connection:
    conn = _get_conn()
    row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
    recorded_schema = int(row[0] or 0)
    if recorded_schema > SCHEMA_VERSION:
        raise DatabaseSchemaTooNewError(recorded_schema)
    return conn


def close():
    if hasattr(_local, "conn") and _local.conn:
        _local.conn.close()
        _local.conn = None
