from __future__ import annotations
from penpal.capture.base import BaseWatcher


class ManualWatcher(BaseWatcher):
    """
    ManualWatcher does not poll anything.
    Events are injected via CaptureManager.save_manual() → queue.put().
    The _run loop is a no-op keep-alive.
    """
    name = "manual"

    def _run(self):
        import time
        while self._running.is_set():
            time.sleep(1)
