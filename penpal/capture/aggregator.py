from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional
from penpal.capture.normalizer import (
    WINDOW_FOCUS_EVENT,
    WINDOW_FOCUS_SESSION_EVENT,
    WINDOW_SESSION_EVENT_TYPES,
    WINDOW_TITLE_CHANGED_EVENT,
)
from penpal.store.models import RawEvent, Session
from penpal.store.repository import upsert_session

import logging
logger = logging.getLogger(__name__)


class Aggregator:
    """Tracks current session, cuts sessions on window_focus, window_title_changed, or idle."""

    def __init__(self):
        self._current_session: Optional[Session] = None

    def process(self, event: RawEvent) -> Optional[Session]:
        """
        Update session state for this event.
        Returns the current session (after update), or None.
        """
        now = event.ts_start

        if event.event_type == WINDOW_FOCUS_SESSION_EVENT:
            duration_sec = 0
            if event.metadata_json:
                try:
                    metadata = json.loads(event.metadata_json)
                    duration_sec = max(int(metadata.get("duration_sec") or 0), 0)
                except Exception:
                    duration_sec = 0
            if duration_sec <= 0 and event.ts_end:
                try:
                    start = datetime.fromisoformat(event.ts_start)
                    end = datetime.fromisoformat(event.ts_end)
                    duration_sec = max(int((end - start).total_seconds()), 0)
                except Exception:
                    duration_sec = 0
            session = Session(
                id=event.session_id or Session().id,
                started_at=event.ts_start,
                ended_at=event.ts_end or event.ts_start,
                app_name=event.app_name,
                primary_window_title=event.window_title,
                duration_sec=duration_sec,
                event_count=1,
            )
            upsert_session(session)
            return session

        # Idle start: close current session
        if event.event_type == "idle_start":
            if self._current_session:
                self._close_current(now)
            return None

        # Idle end / resume: will open new session on next window event
        if event.event_type == "idle_end":
            return None

        # Window focus or title change: treat as window activity for session tracking.
        if event.event_type == WINDOW_FOCUS_EVENT:
            if self._current_session and self._current_session.app_name != event.app_name:
                self._close_current(now)
            if not self._current_session:
                self._current_session = Session(
                    started_at=now,
                    ended_at=now,
                    app_name=event.app_name,
                    primary_window_title=event.window_title,
                )
        elif event.event_type == WINDOW_TITLE_CHANGED_EVENT and not self._current_session:
            self._current_session = Session(
                started_at=now,
                ended_at=now,
                app_name=event.app_name,
                primary_window_title=event.window_title,
            )

        # Update current session if exists
        if self._current_session:
            self._current_session.ended_at = now
            self._current_session.event_count += 1
            try:
                start = datetime.fromisoformat(self._current_session.started_at)
                end = datetime.fromisoformat(now)
                self._current_session.duration_sec = int((end - start).total_seconds())
            except Exception as exc:
                logger.debug("process failed: %s", type(exc).__name__)
                pass
            # Update title if window_title changed
            if event.window_title and event.event_type in WINDOW_SESSION_EVENT_TYPES:
                self._current_session.primary_window_title = event.window_title
            upsert_session(self._current_session)

        return self._current_session

    def _close_current(self, ended_at: str):
        if self._current_session:
            self._current_session.ended_at = ended_at
            try:
                start = datetime.fromisoformat(self._current_session.started_at)
                end = datetime.fromisoformat(ended_at)
                self._current_session.duration_sec = int((end - start).total_seconds())
            except Exception as exc:
                logger.debug("close current failed: %s", type(exc).__name__)
                pass
            upsert_session(self._current_session)
        self._current_session = None

    def flush(self):
        """Force-close current session with current time."""
        now = datetime.now(timezone.utc).isoformat()
        self._close_current(now)

    def current_session(self) -> Optional[Session]:
        return self._current_session
