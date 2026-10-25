from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class RawEvent:
    source: str          # window|idle|clipboard|manual|browser
    event_type: str      # window_focus|window_title_changed|window_heartbeat|window_blur|idle_start|idle_end|clipboard_copy|manual_save|browser_tab
    ts_start: str
    ts_end: Optional[str] = None
    app_name: Optional[str] = None
    window_title: Optional[str] = None
    process_name: Optional[str] = None
    content_text: Optional[str] = None
    content_hash: Optional[str] = None
    metadata_json: Optional[str] = None
    sensitivity_level: int = 0
    skipped_reason: Optional[str] = None
    session_id: Optional[str] = None
    speaker: Literal["user", "system"] = "system"
    semantic_weight: float = 0.5
    user_present: int = 1
    id: Optional[int] = None
    created_at: str = field(default_factory=_now)
    # v20 provenance (ts_start is the event's real occurred_at)
    source_id: Optional[int] = None
    external_id: Optional[str] = None
    bucket: Optional[str] = None
    kind: Optional[str] = None


@dataclass
class Session:
    id: str = field(default_factory=_uuid)
    started_at: str = field(default_factory=_now)
    ended_at: str = field(default_factory=_now)
    app_name: Optional[str] = None
    primary_window_title: Optional[str] = None
    duration_sec: int = 0
    event_count: int = 0
    summary: Optional[str] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class SearchDoc:
    ref_type: str   # clipboard|manual|session
    ref_id: str
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[str] = None
    app_name: Optional[str] = None
    id: Optional[int] = None
    created_at: str = field(default_factory=_now)


@dataclass
class Policy:
    scope_type: str   # app|window|source|content|project (bucket rules only)
    scope_value: str
    mode: str         # allow|deny|metadata-only|redact|truncate
    enabled: bool = True
    priority: int = 100
    config_json: Optional[str] = None
    id: Optional[int] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class AppState:
    key: str
    value: Optional[str]
    updated_at: str = field(default_factory=_now)
