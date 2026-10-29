"""Thin AppKit shell for the HUD: a menu bar icon, a 360x600 popover hosting the WebView, and message routing.

All state and actions live in ``HudController`` (pure Python, same core functions as the CLI); all drawing
lives in ``render.py``. This file only connects them to AppKit, so it holds no business logic.
The daemon keeps every macOS permission; this process needs none.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import uuid

import AppKit
import objc
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper
from WebKit import (
    WKNavigationActionPolicyAllow,
    WKNavigationActionPolicyCancel,
    WKUserContentController,
    WKWebView,
    WKWebViewConfiguration,
)

from penpal.capabilities.store import load_states as load_capability_states
from penpal.config import Config
from penpal.hud.approval_window import ApprovalWindowController, safe_pending_candidate_count
from penpal.hud.controller import HudController, HudModelWorker
from penpal.hud.render import HUD_HEIGHT, HUD_WIDTH, build_html
from penpal.hud.reload import HUDReloader, hud_exec_command
from penpal.foundation.hud_record import HudRecordStore
from penpal.foundation.native_launcher import native_launcher
from penpal.foundation.runtime_launchd import process_start_time
from penpal.runtime.evaluate import current_identity, loaded_identity
from penpal.store.repository import get_state, set_state

REFRESH_SEC = 15.0
SIGNAL_WAKE_SEC = 0.5
MESSAGE_HANDLER_NAME = "penpal"
BADGE_COLORS = {"red": (0.77, 0.28, 0.23), "amber": (0.78, 0.54, 0.16)}


def _copy_to_pasteboard(text: str) -> None:
    board = AppKit.NSPasteboard.generalPasteboard()
    board.clearContents()
    board.setString_forType_(text, AppKit.NSPasteboardTypeString)


def _remove_status_item(item) -> None:
    if item is not None:
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(item)


def _terminate_app() -> None:
    AppKit.NSApp.terminate_(None)


def _confirm_quit_alert() -> bool:
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_("退出 Penpal HUD？")
    alert.setInformativeText_("HUD 退出后状态栏图标会消失，后台采集 daemon 继续运行。")
    alert.addButtonWithTitle_("退出")
    alert.addButtonWithTitle_("取消")
    return alert.runModal() == AppKit.NSAlertFirstButtonReturn


def _system_hosts_status_items() -> bool:
    """macOS 26 draws every status item in Control Center's windows; the app's own button window never reaches the
    screen, so its occlusion state says nothing about what the user sees (owner Mac, 2026-10-28)."""
    # Binaries linked against an older SDK see macOS 26 as 16, so anything from 16 up counts.
    return AppKit.NSProcessInfo.processInfo().operatingSystemVersion()[0] >= 16


def _plain(value):
    """NSDictionary / NSArray / NSString coming from JS -> plain Python containers."""
    if isinstance(value, dict) or hasattr(value, "keys"):
        return {str(k): _plain(value[k]) for k in value.keys()}
    if isinstance(value, (list, tuple)) or hasattr(value, "objectAtIndex_"):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)


class MessageHandler(NSObject):
    """WKScriptMessageHandler: forwards every tap from the page to the shell."""

    def initWithShell_(self, shell):
        self = objc.super(MessageHandler, self).init()
        if self is None:
            return None
        self.shell = shell
        return self

    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        self.shell.handle_message(_plain(message.body()))


class PenpalHUDApp(NSObject):
    def initWithConfig_(self, cfg: Config):
        self = objc.super(PenpalHUDApp, self).init()
        if self is None:
            return None
        self.cfg = cfg
        self._reload_scheduled = False
        self._hud_reloader = None
        self._controller_prepared = False
        self.controller = HudController(
            Config.load,
            copy_to_clipboard=lambda text: AppHelper.callAfter(_copy_to_pasteboard, text),
            quit_hud=self._quit_hud,
        )
        self.pending_count = 0
        self.pending_count_error = None
        self.approval_window = None
        self._pending_count_last_sync = 0.0
        self._message_handler = MessageHandler.alloc().initWithShell_(self)

        self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self._record_store = HudRecordStore(
            pid=os.getpid(), generation=uuid.uuid4().hex, status_item_created=self.status_item is not None,
        )
        self._install_status_icon()
        button = self.status_item.button()
        button.setTarget_(self)
        button.setAction_(objc.selector(self.togglePopover_, signature=b"v@:@"))
        if hasattr(button, "sendActionOn_"):
            events = int(getattr(AppKit, "NSEventMaskLeftMouseUp", 0)) | int(getattr(AppKit, "NSEventMaskRightMouseUp", 0))
            if events:
                button.sendActionOn_(events)
        self._setup_status_menu()

        self.popover = AppKit.NSPopover.alloc().init()
        self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        self.popover.setDelegate_(self)
        self.popover_vc = AppKit.NSViewController.alloc().init()
        self.popover.setContentViewController_(self.popover_vc)
        self.popover.setContentSize_(AppKit.NSMakeSize(HUD_WIDTH, HUD_HEIGHT))
        self.popover_vc.setView_(self._build_webview())
        self._worker = HudModelWorker(
            self.controller,
            main_dispatcher=AppHelper.callAfter,
            apply_script=self._apply_model_script,
            before_model=self._prepare_runtime_and_check_reload,
            model_built=self._record_store.model_built,
        )
        self.controller.set_push_model(self._worker.push_current_model)
        self._record_main_thread_tick()
        self._worker.refresh(fresh=True)
        self._worker.submit(self._read_browser_automation_guide, self._show_browser_automation_guide)
        self._worker.submit(self._read_pending_count, self._apply_pending_count)
        return self

    # ── WebView ─────────────────────────────────────────────────────────────

    @objc.python_method
    def _build_webview(self):
        config = WKWebViewConfiguration.alloc().init()
        content = WKUserContentController.alloc().init()
        content.addScriptMessageHandler_name_(self._message_handler, MESSAGE_HANDLER_NAME)
        config.setUserContentController_(content)
        frame = AppKit.NSMakeRect(0.0, 0.0, HUD_WIDTH, HUD_HEIGHT)
        webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.setNavigationDelegate_(self)
        if hasattr(webview, "setValue_forKey_"):
            webview.setValue_forKey_(False, "drawsBackground")
        webview.loadHTMLString_baseURL_(build_html(), None)
        self.webview = webview
        return webview

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, _webview, action, decision_handler):
        # The page is loaded once from a string; it never needs to navigate anywhere.
        url = action.request().URL()
        scheme = str(url.scheme() or "").lower() if url is not None else ""
        decision_handler(WKNavigationActionPolicyAllow if scheme in ("", "about") else WKNavigationActionPolicyCancel)

    @objc.python_method
    def handle_message(self, body) -> None:
        """Queue page actions for the model worker; message delivery stays on AppKit's main thread."""
        if not isinstance(body, dict):
            return
        action = body.get("action")
        if action == "ui.ready":
            self._push_model()
            return
        self._worker.dispatch(action, body.get("args"))

    @objc.python_method
    def _push_model(self, fresh: bool = False) -> None:
        self._worker.refresh(fresh=fresh)

    @objc.python_method
    def _apply_model_script(self, script: str, badge) -> None:
        """Apply a finished model only on the main thread."""
        self._set_badge(badge)
        webview = getattr(self, "webview", None)
        if webview is None:
            return
        try:
            webview.evaluateJavaScript_completionHandler_(script, None)
        except Exception:
            pass  # the first full page load renders the initial model via ui.ready

    @objc.python_method
    def _status_item_visibility(self):
        """Return the best public AppKit visibility signal available for this status item."""
        item = getattr(self, "status_item", None)
        if item is None:
            return False
        try:
            visible = item.isVisible()
        except (AttributeError, TypeError, ValueError):
            return None
        if not visible:
            return False
        if _system_hosts_status_items():
            return True
        try:
            button = item.button()
            window = button.window() if button is not None else None
            if window is not None and hasattr(window, "occlusionState"):
                state = int(window.occlusionState())
                visible_mask = int(getattr(AppKit, "NSWindowOcclusionStateVisible", 1))
                return bool(state & visible_mask)
        except (AttributeError, TypeError, ValueError):
            pass
        return bool(visible)

    @objc.python_method
    def _record_main_thread_tick(self) -> None:
        try:
            self._record_store.main_thread_tick(self._status_item_visibility())
        except OSError as exc:
            logging.getLogger(__name__).warning("HUD runtime record write failed: %s", type(exc).__name__)

    @objc.python_method
    def _prepare_runtime_and_check_reload(self) -> bool:
        """Called by the worker, including identity hashing and the process-start probe."""
        if not self._controller_prepared:
            identity = loaded_identity(self.cfg)
            started = process_start_time(os.getpid())
            if started:
                try:
                    self._record_store.initialize(started, identity)
                except OSError as exc:
                    logging.getLogger(__name__).warning("HUD runtime record write failed: %s", type(exc).__name__)
            launcher = native_launcher()
            executable, arguments = hud_exec_command(launcher, sys.argv)
            self._hud_reloader = HUDReloader(
                identity,
                lambda: current_identity(self.cfg),
                executable=executable,
                argv=arguments,
                launcher=launcher,
            )
            self._controller_prepared = True
        if self._hud_reloader is not None and self._hud_reloader.check():
            AppHelper.callAfter(self._schedule_reload)
            return True
        return False

    @objc.python_method
    def _schedule_reload(self) -> None:
        if self._reload_scheduled:
            return
        self._reload_scheduled = True
        self.performSelector_withObject_afterDelay_(
            objc.selector(self.reloadAfterTimer_, signature=b"v@:@"), None, 0.0
        )

    # ── status item ─────────────────────────────────────────────────────────

    @objc.python_method
    def _install_status_icon(self) -> None:
        button = self.status_item.button()
        button.setTitle_("")
        image = None
        if hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("square.and.pencil", "Penpal")
        if image is None:
            image = AppKit.NSImage.imageNamed_("NSActionTemplate")
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
            button.setImagePosition_(AppKit.NSImageLeft)

    @objc.python_method
    def _set_badge(self, tone) -> None:
        """A small coloured dot next to the icon when something needs attention (same tone as the Overview tab)."""
        button = self.status_item.button() if getattr(self, "status_item", None) is not None else None
        if button is None:
            return
        try:
            if tone in BADGE_COLORS:
                r, g, b = BADGE_COLORS[tone]
                color = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)
                attrs = {AppKit.NSForegroundColorAttributeName: color}
                button.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(" ●", attrs))
            else:
                button.setTitle_("")
        except Exception:
            pass

    @objc.python_method
    def _read_browser_automation_guide(self) -> bool:
        if str(get_state("browser_automation_guide_shown") or "").strip() == "1":
            return False
        state = load_capability_states().get("browser_automation")
        if state is None or state.ok or state.code != "browser_automation_denied":
            return False
        return True

    @objc.python_method
    def _show_browser_automation_guide(self, should_show: bool) -> None:
        if not should_show:
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("请开启浏览器自动化权限")
        alert.setInformativeText_(
            "请前往系统设置 → 隐私与安全性 → 自动化，勾选 Penpal 对 Safari / Google Chrome / Arc / Microsoft Edge / Brave Browser 的控制权限。"
        )
        alert.addButtonWithTitle_("知道了")
        alert.runModal()
        self._worker.submit(lambda: set_state("browser_automation_guide_shown", "1"))

    @objc.IBAction
    def togglePopover_(self, sender):
        if self._is_secondary_click():
            self._refresh_approval_menu_item()
            if getattr(self, "status_menu", None) is not None and hasattr(self.status_item, "popUpStatusItemMenu_"):
                self.status_item.popUpStatusItemMenu_(self.status_menu)
            return
        if self.popover.isShown():
            self.popover.performClose_(None)
            return
        self._push_model(fresh=True)
        self.popover.showRelativeToRect_ofView_preferredEdge_(sender.bounds(), sender, 1)

    def popoverDidClose_(self, _notification):
        self._worker.submit(self.controller.popover_closed, rebuild=True)

    def wakeForSignals_(self, _timer):
        pass

    def pollSignals_(self, _timer):
        self._record_main_thread_tick()
        self._sync_pending_count()
        self._worker.refresh(fresh=True)

    def reloadAfterTimer_(self, _sender):
        """Reload on the next run-loop turn, after the timer callback has returned: relaunch (quit here) or exec in place."""
        if self._hud_reloader is None:
            return
        action = self._hud_reloader.reload()
        if action == "relaunch":
            item = getattr(self, "status_item", None)
            if item is not None:
                _remove_status_item(item)
                self.status_item = None
            _terminate_app()

    # ── right-click menu (candidate approval, daemon restart, quit) ─────────

    @objc.python_method
    def _setup_status_menu(self) -> None:
        self.status_menu = AppKit.NSMenu.alloc().init()
        self.approval_menu_item = self._menu_item("候选审批", self.openApprovalWindow_)
        self.status_menu.addItem_(self.approval_menu_item)
        self.status_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self.status_menu.addItem_(self._menu_item("重新检测", self.recheckHealth_))
        self.status_menu.addItem_(self._menu_item("重启后台服务", self.restartDaemon_))
        self.status_menu.addItem_(self._menu_item("退出 HUD", self.confirmQuit_))

    @objc.python_method
    def _menu_item(self, title, handler):
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, objc.selector(handler, signature=b"v@:@"), "")
        item.setTarget_(self)
        return item

    @objc.python_method
    def _sync_pending_count(self) -> None:
        now = time.monotonic()
        if now - self._pending_count_last_sync < 60.0:
            return
        self._pending_count_last_sync = now
        self._worker.submit(self._read_pending_count, self._apply_pending_count)

    @objc.python_method
    def _read_pending_count(self):
        return safe_pending_candidate_count()

    @objc.python_method
    def _apply_pending_count(self, result) -> None:
        count, error = result
        self.pending_count_error = error
        if count is not None:
            self.pending_count = int(count)
        self._refresh_approval_menu_item()

    @objc.python_method
    def _refresh_approval_menu_item(self) -> None:
        item = getattr(self, "approval_menu_item", None)
        if item is None:
            return
        if self.pending_count_error:
            item.setTitle_("候选审批 (?)")
            item.setEnabled_(True)
            return
        count = max(int(self.pending_count), 0)
        item.setTitle_(f"候选审批 ({count})" if count else "候选审批")
        item.setEnabled_(count > 0)

    @objc.python_method
    def _is_secondary_click(self) -> bool:
        if not hasattr(AppKit.NSApp, "currentEvent"):
            return False
        event = AppKit.NSApp.currentEvent()
        if event is None or not hasattr(event, "type"):
            return False
        return int(event.type()) in {int(getattr(AppKit, "NSEventTypeRightMouseUp", -1)), int(getattr(AppKit, "NSEventTypeRightMouseDown", -2))}

    @objc.IBAction
    def openApprovalWindow_(self, _sender):
        if self.pending_count_error:
            self._queue_pending_count_for_approval()
            return
        self._show_approval_window()

    @objc.python_method
    def _queue_pending_count_for_approval(self) -> None:
        self._pending_count_last_sync = time.monotonic()
        self._worker.submit(self._read_pending_count, self._after_pending_count_for_approval)

    @objc.python_method
    def _after_pending_count_for_approval(self, result) -> None:
        self._apply_pending_count(result)
        if self.pending_count_error:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("候选审批读取失败")
            alert.setInformativeText_(self.pending_count_error)
            alert.addButtonWithTitle_("OK")
            alert.runModal()
            return
        self._show_approval_window()

    @objc.python_method
    def _show_approval_window(self) -> None:
        if self.approval_window is None:
            self.approval_window = ApprovalWindowController.alloc().initWithStore_(None)
        self.approval_window.showWindow_(None)
        self._pending_count_last_sync = 0.0

    @objc.IBAction
    def recheckHealth_(self, _sender):
        self._worker.dispatch("health.recheck", {})

    @objc.IBAction
    def restartDaemon_(self, _sender):
        self.popover.performClose_(None)
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("重启后台服务？")
        alert.setInformativeText_("Penpal 会通过 launchd 重启后台服务并确认新代次开始推进。")
        alert.addButtonWithTitle_("重启")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != AppKit.NSAlertFirstButtonReturn:
            return

        def _run_restart() -> None:
            from penpal.runtime.control import restart

            restart("hud")

        threading.Thread(target=_run_restart, daemon=True, name="hud-daemon-restart").start()

    @objc.python_method
    def _quit_hud(self) -> None:
        AppHelper.callAfter(self.confirmQuit_, None)

    @objc.IBAction
    def confirmQuit_(self, _sender):
        self.popover.performClose_(None)
        if _confirm_quit_alert():
            from penpal.foundation.hud_record import remove_hud_record

            remove_hud_record()
            _terminate_app()


def run_hud(cfg: Config | None = None) -> None:
    config = cfg or Config.load()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    delegate = PenpalHUDApp.alloc().initWithConfig_(config)
    app.setDelegate_(delegate)

    def _force_shutdown(sig, frame):
        print("\n[Penpal] 接收到退出信号，清理并关闭...")
        from penpal.foundation.hud_record import remove_hud_record

        remove_hud_record()
        AppKit.NSApp.terminate_(None)
        os._exit(0)  # make sure the terminal gets control back after Ctrl+C

    signal.signal(signal.SIGINT, _force_shutdown)
    signal.signal(signal.SIGTERM, _force_shutdown)

    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        SIGNAL_WAKE_SEC, delegate, objc.selector(delegate.wakeForSignals_, signature=b"v@:@"), None, True
    )
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        REFRESH_SEC, delegate, objc.selector(delegate.pollSignals_, signature=b"v@:@"), None, True
    )
    print("Penpal HUD 已启动。")
    app.run()
