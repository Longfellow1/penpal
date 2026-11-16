"""D36 runtime-record construction, progress, component snapshots, and writes."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from penpal import __version__
from penpal.config import config_fingerprint
from penpal.capture.capture_control import disabled_sources, is_capture_paused
from penpal.foundation.runtime_record import RECORD_WRITE_INTERVAL_SEC
from penpal.foundation.runtime_identity import code_fingerprint, make_identity
from penpal.foundation.runtime_launchd import process_start_time
from penpal.foundation.runtime_record import (
    Component,
    Progress,
    RuntimeRecord,
    utc_now,
    write_record,
)
from penpal.store.models import RawEvent
from penpal.store.repository import get_state
from penpal.store.db import DatabaseSchemaTooNewError, read_schema_version
from penpal.store.migrations import SCHEMA_VERSION


def _record_runtime_event(manager, event: RawEvent) -> None:
    manager._source_counts[event.source] = (
        manager._source_counts.get(event.source, 0) + 1
    )
    manager._source_last_event_at[event.source] = (
        event.ts_start or datetime.now(timezone.utc).isoformat()
    )


def report_progress(manager, name: str) -> None:
    """Let a daemon loop report completion without writing the shared record."""
    with manager._runtime_record_lock:
        previous = manager._runtime_progress.get(name)
        manager._runtime_progress[name] = Progress(
            (previous.seq if previous else 0) + 1, utc_now()
        )


def set_runtime_component(manager, name: str, state: str, detail: str = "") -> None:
    with manager._runtime_record_lock:
        manager._runtime_components[name] = Component(state, detail)


def _component_snapshot(manager) -> dict[str, Component]:
    components = dict(manager._runtime_components)
    if manager._storage_error:
        paused = manager._paused.is_set()
        disabled = manager._disabled_applied
    else:
        try:
            paused = is_capture_paused()
            disabled = disabled_sources()
        except sqlite3.Error:
            paused = manager._paused.is_set()
            disabled = manager._disabled_applied
    for name, watcher in manager._watchers.items():
        health = watcher.health()
        beat_at = health.get("last_beat_at")
        if isinstance(beat_at, str):
            manager._runtime_progress[f"watcher.{name}"] = Progress(
                int(health.get("beat_seq") or 0), beat_at
            )
        emit_at = health.get("last_emit_at")
        if isinstance(emit_at, str):
            manager._runtime_progress[f"emit.{name}"] = Progress(
                int(health.get("emit_seq") or 0), emit_at
            )
        if name in disabled:
            state = "disabled"
        elif paused or health.get("paused"):
            state = "idle"
        elif health.get("gave_up") or health.get("heartbeat_gave_up"):
            state = "gave_up"
        elif not health.get("running"):
            state = "idle"
        else:
            state = "running"
        detail = ""
        if state == "gave_up":
            detail = (
                "heartbeat_retries_exhausted"
                if health.get("heartbeat_gave_up")
                else "crash_retries_exhausted"
            )
        components[name] = Component(state, detail)
    components["storage"] = (
        Component("storage_error", manager._storage_error)
        if manager._storage_error
        else Component("running")
    )
    components["storage_dropped_events"] = Component(
        "storage_error" if manager._storage_dropped else "idle", str(manager._storage_dropped)
    )
    components["core_loop"] = Component(
        "running" if manager._running.is_set() else "idle"
    )
    try:
        if manager._storage_error:
            previous = manager._runtime_record
            if previous is not None:
                components.update({
                    name: component for name, component in previous.components.items()
                    if name.startswith("permission.")
                })
            return components
        raw = get_state("daemon_permissions") or ""
        permission_snapshot = json.loads(raw) if raw else {}
        permission_sources = {
            "input_monitoring": ("keyboard_chunk",),
            "accessibility": ("ax_text", "keyboard_chunk"),
        }
        for name, value in (permission_snapshot.get("permissions") or {}).items():
            affected = permission_sources.get(name, ())
            if value == "denied" and any(
                source in manager._watchers and source not in disabled
                for source in affected
            ):
                components[f"permission.{name}"] = Component("missing_permission")
    except (TypeError, ValueError):
        pass
    except DatabaseSchemaTooNewError:
        logging.getLogger(__name__).warning("runtime permission snapshot skipped for a newer database schema")
    except sqlite3.Error as exc:
        logging.getLogger(__name__).warning("runtime permission snapshot unavailable: %s", type(exc).__name__)
    return components


def _write_runtime_record(manager, force: bool = False) -> None:
    now_mono = time.monotonic()
    if (
        not force
        and now_mono - manager._runtime_last_written_mono < RECORD_WRITE_INTERVAL_SEC
    ):
        return
    with manager._runtime_record_lock:
        previous = manager._runtime_record
        if previous is None:
            return
        record = RuntimeRecord(
            generation=previous.generation,
            pid=previous.pid,
            process_start_time=previous.process_start_time,
            version=previous.version,
            code_fingerprint=previous.code_fingerprint,
            config_fingerprint=previous.config_fingerprint,
            database_schema_version=previous.database_schema_version,
            started_at=previous.started_at,
            written_at=utc_now(),
            progress=dict(manager._runtime_progress),
            components=manager._component_snapshot(),
            record_format_version=previous.record_format_version,
        )
        write_record(record)
        manager._runtime_record = record
        manager._runtime_last_written_mono = now_mono


def initialize_runtime_record(manager, started_at: str) -> None:
    manager._runtime_progress["core_loop"] = Progress(0, started_at)
    identity = manager._runtime_identity or make_identity(
        code_fingerprint=code_fingerprint(),
        package_version=__version__,
        config_fingerprint=config_fingerprint(),
        database_schema_version=read_schema_version(manager.config.db_path_expanded),
        supported_schema_version=SCHEMA_VERSION,
    )
    manager._runtime_identity = identity
    manager._runtime_record = RuntimeRecord(
        generation=str(uuid.uuid4()),
        pid=os.getpid(),
        process_start_time=process_start_time(os.getpid()) or "unknown",
        version=__version__,
        code_fingerprint=identity.code_fingerprint,
        config_fingerprint=identity.config_fingerprint,
        database_schema_version=identity.database_schema_version or 0,
        started_at=started_at,
        written_at=started_at,
        progress=dict(manager._runtime_progress),
    )
    manager._write_runtime_record(force=True)
