"""Storage-write failure state and recovery probes for capture."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from penpal.store.db import get_conn
from penpal.utils.logging import get_logger

logger = get_logger("manager")


def _is_disk_full(exc: BaseException) -> bool:
    return isinstance(
        exc, sqlite3.OperationalError
    ) and "database or disk is full" in str(exc)


def _storage_write_blocked(manager) -> bool:
    return (
        manager._storage_error is not None
        and time.monotonic() < manager._storage_next_probe_mono
    )


def _enter_disk_full(manager) -> None:
    # Roll back the failed statement so the shared connection is not left mid-transaction.
    try:
        get_conn().rollback()
    except Exception as exc:
        logger.debug("enter disk full failed: %s", type(exc).__name__)
        pass
    if manager._storage_error is None:
        manager._storage_error_since = datetime.now(timezone.utc).isoformat()
        logger.error(
            "capture paused: database or disk is full; will probe again in %.0fs",
            manager.DISK_FULL_PROBE_SEC,
        )
    manager._storage_error = "disk_full"
    manager._storage_next_probe_mono = time.monotonic() + manager.DISK_FULL_PROBE_SEC
    manager._persist_storage_state()


def _leave_disk_full(manager) -> None:
    if manager._storage_error is None:
        return
    logger.info(
        "capture storage writes recovered after disk_full (dropped %d events)",
        manager._storage_dropped,
    )
    manager._storage_error = None
    manager._storage_error_since = None
    manager._persist_storage_state()


def _persist_storage_state(manager) -> None:
    # The flush loop publishes this state; a full database may not accept another write.
    return


def _storage_status(manager) -> dict:
    if manager._storage_error is None:
        return {"status": "ok"}
    return {
        "status": "error",
        "reason": manager._storage_error,
        "since": manager._storage_error_since,
        "dropped_events": manager._storage_dropped,
    }
