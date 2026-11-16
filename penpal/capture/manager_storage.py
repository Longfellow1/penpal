"""Storage-write failure state and recovery probes for capture."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from penpal.store.db import get_conn
from penpal.utils.logging import get_logger

logger = get_logger("manager")


def _is_disk_full(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(exc, sqlite3.Error) and (
        (isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_FULL)
        or "database or disk is full" in str(exc).lower()
    )


def _storage_error_code(exc: BaseException) -> str:
    if _is_disk_full(exc):
        return "disk_full"
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(exc, sqlite3.OperationalError) and (
        (isinstance(code, int) and code & 0xFF in (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY))
        or "locked" in str(exc).lower() or "busy" in str(exc).lower()
    ):
        return "db_locked"
    return "write_failed"


def _storage_write_blocked(manager) -> bool:
    return (
        manager._storage_error is not None
        and time.monotonic() < manager._storage_next_probe_mono
    )


def _enter_storage_error(manager, reason: str) -> None:
    # Roll back the failed statement so the shared connection is not left mid-transaction.
    try:
        get_conn().rollback()
    except Exception as exc:
        logger.debug("storage rollback failed: %s", type(exc).__name__)
        pass
    if manager._storage_error is None:
        manager._storage_error_since = datetime.now(timezone.utc).isoformat()
        logger.error("capture storage error: %s", reason)
    manager._storage_error = reason
    manager._storage_next_probe_mono = time.monotonic() + manager.DISK_FULL_PROBE_SEC
    manager._persist_storage_state()


def _leave_storage_error(manager) -> None:
    if manager._storage_error is None:
        return
    logger.info(
        "capture storage writes recovered (dropped %d events)",
        manager._storage_dropped,
    )
    manager._storage_error = None
    manager._storage_error_since = None
    manager._storage_recovered_mono = time.monotonic()
    manager._persist_storage_state()


def _persist_storage_state(manager) -> None:
    try:
        manager._write_runtime_record(force=True)
    except Exception as exc:
        logger.error("capture runtime state not persisted: %s", type(exc).__name__)


def _storage_status(manager) -> dict:
    if manager._storage_error is None:
        return {"status": "ok"}
    return {
        "status": "error",
        "reason": manager._storage_error,
        "since": manager._storage_error_since,
        "dropped_events": manager._storage_dropped,
    }
