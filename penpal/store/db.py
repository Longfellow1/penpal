import sqlite3
import threading
from pathlib import Path
from penpal.store.migrations import run_migrations
from penpal.store.migrations import SCHEMA_VERSION

_local = threading.local()

_db_path: Path | None = None


class DatabaseSchemaTooNewError(RuntimeError):
    def __init__(self, recorded: int, supported: int = SCHEMA_VERSION):
        self.recorded = recorded
        self.supported = supported
        super().__init__(f"database schema {recorded} is newer than supported schema {supported}")


def read_schema_version(db_path: Path) -> int | None:
    """Read the database's recorded schema without opening it for writes."""
    target = Path(db_path)
    if not target.is_file():
        return 0
    conn = None
    try:
        conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
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


def init_db(db_path: Path):
    global _db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    recorded_schema = read_schema_version(db_path)
    if recorded_schema is not None and recorded_schema > SCHEMA_VERSION:
        raise DatabaseSchemaTooNewError(recorded_schema)
    if _db_path is not None and _db_path != db_path and hasattr(_local, "conn") and _local.conn:
        _local.conn.close()
        _local.conn = None
    _db_path = db_path
    conn = _get_conn()
    run_migrations(conn)


def is_initialized() -> bool:
    return _db_path is not None


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        if _db_path is None:
            raise RuntimeError("DB not initialized. Call init_db() first.")
        conn = sqlite3.connect(str(_db_path), check_same_thread=False)
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
