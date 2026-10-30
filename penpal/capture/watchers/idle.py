from __future__ import annotations
import queue
import time
from datetime import datetime, timezone

from penpal.capture.base import BaseWatcher
from penpal.capture.normalizer import normalize_idle_event
from penpal.utils.logging import get_logger

logger = get_logger("watcher.idle")

POLL_INTERVAL = 10  # seconds


def _get_idle_seconds() -> float:
    """Return seconds since last user input event."""
    try:
        import Quartz
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGAnyInputEventType,
        )
    except Exception as e:
        logger.debug(f"idle_seconds error: {e}")
        return 0.0


class IdleWatcher(BaseWatcher):
    name = "idle"

    def __init__(self, event_queue: queue.Queue, threshold_sec: int = 180):
        super().__init__(event_queue)
        self._threshold = threshold_sec
        self._is_idle = False

    def _run(self):
        while self._running.is_set():
            if self._paused.is_set():
                time.sleep(POLL_INTERVAL)
                continue
            try:
                idle_secs = _get_idle_seconds()
                now = datetime.now(timezone.utc).isoformat()

                if idle_secs >= self._threshold and not self._is_idle:
                    self._is_idle = True
                    self.emit(normalize_idle_event("idle_start", idle_secs, now))
                    logger.debug(f"Idle started ({idle_secs:.0f}s)")

                elif idle_secs < self._threshold and self._is_idle:
                    self._is_idle = False
                    self.emit(normalize_idle_event("idle_end", idle_secs, now))
                    logger.debug("Idle ended")

            except Exception as e:
                logger.error(f"IdleWatcher error: {e}")

            time.sleep(POLL_INTERVAL)
