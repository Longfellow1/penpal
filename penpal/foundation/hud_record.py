"""Atomic HUD runtime evidence, separate from the daemon-owned runtime record."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from penpal.foundation.runtime_identity import RuntimeIdentity
from penpal.utils.atomic_io import atomic_write_text
from penpal.utils.paths import get_hud_record_path


@dataclass(frozen=True)
class HudRecord:
    pid: int
    process_start_time: str
    generation: str
    identity: RuntimeIdentity
    last_model_built_at: str | None
    last_main_thread_tick_at: str | None
    status_item_created: bool
    status_item_visible: bool | None
    record_format_version: int = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity_from_dict(value: Any) -> RuntimeIdentity | None:
    if not isinstance(value, dict):
        return None
    code = value.get("code_fingerprint")
    version = value.get("package_version")
    config = value.get("config_fingerprint")
    schema = value.get("database_schema_version")
    supported = value.get("supported_schema_version")
    if (
        not isinstance(code, str) or not code
        or not isinstance(version, str) or not version
        or not isinstance(config, str) or not config
        or (schema is not None and (not isinstance(schema, int) or schema < 0))
        or not isinstance(supported, int) or supported < 0
    ):
        return None
    return RuntimeIdentity(code, version, config, schema, supported)


def _optional_timestamp(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _record_from_dict(value: Any) -> HudRecord | None:
    if not isinstance(value, dict) or value.get("record_format_version") != 1:
        return None
    pid = value.get("pid")
    process_start_time = value.get("process_start_time")
    generation = value.get("generation")
    created = value.get("status_item_created")
    visible = value.get("status_item_visible")
    identity = _identity_from_dict(value.get("identity"))
    model_at = value.get("last_model_built_at")
    tick_at = value.get("last_main_thread_tick_at")
    if (
        not isinstance(pid, int) or pid <= 0
        or not isinstance(process_start_time, str) or not process_start_time
        or not isinstance(generation, str) or not generation
        or identity is None
        or not isinstance(created, bool)
        or (visible is not None and not isinstance(visible, bool))
        or not _optional_timestamp(model_at)
        or not _optional_timestamp(tick_at)
    ):
        return None
    return HudRecord(
        pid=pid,
        process_start_time=process_start_time,
        generation=generation,
        identity=identity,
        last_model_built_at=model_at,
        last_main_thread_tick_at=tick_at,
        status_item_created=created,
        status_item_visible=visible,
    )


def read_hud_record(path: Path | None = None) -> HudRecord | None:
    target = path or get_hud_record_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _record_from_dict(value)


def write_hud_record(record: HudRecord, path: Path | None = None) -> None:
    target = path or get_hud_record_path()
    payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n"
    atomic_write_text(target, payload)


def remove_hud_record(path: Path | None = None) -> None:
    target = path or get_hud_record_path()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


class HudRecordStore:
    """Serialize main-thread ticks and worker model timestamps into one atomic record."""

    def __init__(
        self,
        *,
        pid: int,
        generation: str,
        status_item_created: bool,
        path: Path | None = None,
    ) -> None:
        self.pid = pid
        self.generation = generation
        self.status_item_created = status_item_created
        self.path = path
        self._lock = threading.Lock()
        self._process_start_time: str | None = None
        self._identity: RuntimeIdentity | None = None
        self._last_model_built_at: str | None = None
        self._last_main_thread_tick_at: str | None = None
        self._status_item_visible: bool | None = None

    def initialize(self, process_start_time: str, identity: RuntimeIdentity) -> None:
        with self._lock:
            self._process_start_time = process_start_time
            self._identity = identity
            self._write_locked()

    def main_thread_tick(self, visible: bool | None) -> None:
        with self._lock:
            self._last_main_thread_tick_at = utc_now()
            self._status_item_visible = visible
            self._write_locked()

    def model_built(self, timestamp: str) -> None:
        with self._lock:
            self._last_model_built_at = timestamp
            self._write_locked()

    def _write_locked(self) -> None:
        if not self._process_start_time or self._identity is None:
            return
        write_hud_record(
            HudRecord(
                pid=self.pid,
                process_start_time=self._process_start_time,
                generation=self.generation,
                identity=self._identity,
                last_model_built_at=self._last_model_built_at,
                last_main_thread_tick_at=self._last_main_thread_tick_at,
                status_item_created=self.status_item_created,
                status_item_visible=self._status_item_visible,
            ),
            self.path,
        )
