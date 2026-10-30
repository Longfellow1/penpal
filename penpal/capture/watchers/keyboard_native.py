from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from penpal.capture.watchers.window import _get_frontmost_app

import logging
logger = logging.getLogger(__name__)


class KeyboardInputSource(Protocol):
    def start(self, callback: Callable[..., None]) -> bool:
        ...

    def stop(self) -> None:
        ...

    def health(self) -> dict:
        ...


def build_keyboard_input_source():
    try:
        import Quartz  # noqa: F401
    except Exception:
        return None
    return MacOSKeyboardInputSource()


def _is_secure_input_enabled(application_services_module=None) -> bool:
    try:
        application_services = application_services_module
        if application_services is None:
            import ApplicationServices as application_services
        checker = getattr(application_services, "IsSecureEventInputEnabled", None)
        if callable(checker):
            return bool(checker())
    except Exception:
        return False
    return False


# CGEventFlags masks (Quartz kCGEventFlagMask*), used when the Quartz module lacks the constants.
_FLAG_COMMAND = 0x100000
_FLAG_CONTROL = 0x40000
_FLAG_OPTION = 0x80000


def clean_key_text(text: str) -> str:
    """Keep spaces, Enter and Tab (the old .strip() ate them, so 'hello world' became 'helloworld')."""
    return text.replace("\r", "\n")


class MacOSKeyboardInputSource:
    def __init__(self, quartz_module=None):
        self._quartz_module = quartz_module
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._run_loop = None
        self._status = "idle"

    def _load_quartz(self):
        if self._quartz_module is not None:
            return self._quartz_module
        try:
            import Quartz

            self._quartz_module = Quartz
        except Exception:
            self._quartz_module = None
        return self._quartz_module

    def start(self, callback: Callable[..., None]) -> bool:
        quartz = self._load_quartz()
        if quartz is None:
            self._status = "unavailable"
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._running.set()
        self._thread = threading.Thread(
            target=self._run_loop_forever,
            args=(callback,),
            daemon=True,
            name="keyboard-input-source",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running.clear()
        quartz = self._load_quartz()
        if quartz is not None and self._run_loop is not None:
            try:
                quartz.CFRunLoopStop(self._run_loop)
            except Exception as exc:
                logger.debug("stop failed: %s", type(exc).__name__)
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._status = "stopped"

    def health(self) -> dict:
        return {"status": self._status, "running": self._thread is not None and self._thread.is_alive()}

    @staticmethod
    def _has_command_modifier(quartz, event) -> bool:
        """Cmd/Ctrl/Option chords are shortcuts (Cmd+C yields 'c'), not typing. Shift is kept."""
        try:
            flags = int(quartz.CGEventGetFlags(event))
        except Exception:
            return False
        mask = (
            getattr(quartz, "kCGEventFlagMaskCommand", _FLAG_COMMAND)
            | getattr(quartz, "kCGEventFlagMaskControl", _FLAG_CONTROL)
            | getattr(quartz, "kCGEventFlagMaskAlternate", _FLAG_OPTION)
        )
        return bool(flags & mask)

    def _extract_text(self, event) -> str:
        quartz = self._load_quartz()
        if quartz is None:
            return ""
        try:
            extracted = quartz.CGEventKeyboardGetUnicodeString(event, 255, None, None)
        except Exception:
            return ""
        if isinstance(extracted, tuple):
            for item in reversed(extracted):
                if isinstance(item, str):
                    return item
        if isinstance(extracted, str):
            return extracted
        return ""

    def _run_loop_forever(self, callback: Callable[..., None]) -> None:
        quartz = self._load_quartz()
        if quartz is None:
            self._status = "unavailable"
            return

        self._status = "starting"

        def handle_event(_proxy, _event_type, event, _refcon):
            if not self._running.is_set():
                return event
            secure_input = _is_secure_input_enabled()
            if secure_input:
                return event
            if self._has_command_modifier(quartz, event):
                return event
            text = clean_key_text(self._extract_text(event))
            if not text:
                return event
            app_name, window_title, process_name = _get_frontmost_app()
            try:
                callback(
                    text,
                    app_name=app_name,
                    window_title=window_title,
                    process_name=process_name,
                    now=time.monotonic(),
                    secure_input=False,
                )
            except Exception as exc:
                logger.debug("run loop forever failed: %s", type(exc).__name__)
                pass
            return event

        try:
            event_mask = 1 << quartz.kCGEventKeyDown
            tap = quartz.CGEventTapCreate(
                quartz.kCGSessionEventTap,
                quartz.kCGHeadInsertEventTap,
                quartz.kCGEventTapOptionListenOnly,
                event_mask,
                handle_event,
                None,
            )
            if tap is None:
                self._status = "permission_denied"
                return

            source = quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            self._run_loop = quartz.CFRunLoopGetCurrent()
            quartz.CFRunLoopAddSource(self._run_loop, source, quartz.kCFRunLoopCommonModes)
            quartz.CGEventTapEnable(tap, True)
            self._status = "running"
            quartz.CFRunLoopRun()
        except Exception:
            self._status = "error"
        finally:
            self._run_loop = None
            if self._status == "running":
                self._status = "stopped"
