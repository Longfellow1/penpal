"""HUD AppKit shell: it only routes page messages to the controller and draws the model it gets back.

No window, status item or WebView is created: the ObjC objects are built bare and given fakes.
"""
from __future__ import annotations

import json
import types

import pytest

pytest.importorskip("AppKit")
pytest.importorskip("WebKit")

from Foundation import NSDictionary  # noqa: E402

from penpal.hud import app as hud_app  # noqa: E402

MODEL = {"title": "概览", "tabs": [{"id": "overview", "badge": "amber"}], "status": {}, "sections": []}


class _Controller:
    def __init__(self):
        self.calls = []
        self.closed = 0

    def dispatch(self, action, args=None):
        self.calls.append((action, args))
        return {**MODEL, "title": f"after {action}"}

    def model(self, fresh=False):
        self.calls.append(("model", fresh))
        return MODEL

    def popover_closed(self):
        self.closed += 1


class _Worker:
    def __init__(self):
        self.refreshes = []
        self.actions = []
        self.tasks = []

    def refresh(self, *, fresh=False):
        self.refreshes.append(fresh)

    def dispatch(self, action, args=None):
        self.actions.append((action, args))

    def submit(self, task, callback=None, *, rebuild=False):
        self.tasks.append((task, callback, rebuild))


class _WebView:
    def __init__(self):
        self.scripts = []

    def evaluateJavaScript_completionHandler_(self, script, handler):
        self.scripts.append(script)


@pytest.fixture()
def shell():
    app = hud_app.PenpalHUDApp.alloc().init()
    app.controller, app.webview, app.status_item, app._worker = _Controller(), _WebView(), None, _Worker()
    return app


def _rendered(shell):
    script = shell.webview.scripts[-1]
    assert script.startswith("window.render(") and script.endswith(");")
    return json.loads(script[len("window.render("):-2])


def test_page_message_is_queued_for_the_worker(shell):
    shell.handle_message({"action": "capture.pause", "args": {}})
    assert shell._worker.actions == [("capture.pause", {})]


def test_ready_message_pushes_the_current_model_without_an_action(shell):
    shell.handle_message({"action": "ui.ready"})
    assert shell._worker.refreshes == [False]


def test_junk_messages_are_ignored(shell):
    shell.handle_message("nope")
    shell.handle_message(None)
    assert shell.controller.calls == [] and shell.webview.scripts == []


def test_script_is_safe_to_embed(shell):
    shell._apply_model_script('window.render({"title":"<\\/script><b>"});', None)
    assert "</script><b>" not in shell.webview.scripts[-1]


def test_message_body_from_javascript_becomes_plain_python():
    body = NSDictionary.dictionaryWithDictionary_({"action": "source.toggle", "args": {"name": "clipboard", "enable": False}})
    plain = hud_app._plain(body)
    assert plain == {"action": "source.toggle", "args": {"name": "clipboard", "enable": False}}
    assert type(plain) is dict and type(plain["args"]) is dict and plain["args"]["enable"] is False


def test_closing_the_popover_tells_the_controller_to_forget_transient_state(shell):
    shell.popoverDidClose_(None)
    task, callback, rebuild = shell._worker.tasks[-1]
    assert task == shell.controller.popover_closed and callback is None and rebuild


def test_message_handler_forwards_to_the_shell():
    seen = []
    handler = hud_app.MessageHandler.alloc().initWithShell_(types.SimpleNamespace(handle_message=seen.append))
    handler.userContentController_didReceiveScriptMessage_(None, types.SimpleNamespace(body=lambda: {"action": "nav.back"}))
    assert seen == [{"action": "nav.back"}]


def test_navigation_is_limited_to_the_loaded_page(shell):
    decisions = []

    def action(url):
        return types.SimpleNamespace(request=lambda: types.SimpleNamespace(URL=lambda: url))

    for scheme, expected in [("about", hud_app.WKNavigationActionPolicyAllow), ("https", hud_app.WKNavigationActionPolicyCancel),
                             ("file", hud_app.WKNavigationActionPolicyCancel)]:
        shell.webView_decidePolicyForNavigationAction_decisionHandler_(None, action(types.SimpleNamespace(scheme=lambda s=scheme: s)), decisions.append)
        assert decisions[-1] == expected


def test_reload_after_timer_relaunch_removes_status_item_and_terminates(shell, monkeypatch):
    removed = []
    terminated = []

    fake_item = object()
    shell.status_item = fake_item
    shell._hud_reloader = types.SimpleNamespace(reload=lambda: "relaunch")

    monkeypatch.setattr(hud_app, "_remove_status_item", lambda item: removed.append(item))
    monkeypatch.setattr(hud_app, "_terminate_app", lambda: terminated.append(True))

    shell.reloadAfterTimer_(None)

    assert removed == [fake_item]
    assert shell.status_item is None
    assert terminated == [True]


def test_reload_after_timer_exec_does_not_remove_status_item_or_terminate(shell, monkeypatch):
    removed = []
    terminated = []

    fake_item = object()
    shell.status_item = fake_item
    shell._hud_reloader = types.SimpleNamespace(reload=lambda: "exec")

    monkeypatch.setattr(hud_app, "_remove_status_item", lambda item: removed.append(item))
    monkeypatch.setattr(hud_app, "_terminate_app", lambda: terminated.append(True))

    shell.reloadAfterTimer_(None)

    assert removed == []
    assert shell.status_item is fake_item
    assert terminated == []


def _status_item(visible, occlusion):
    window = types.SimpleNamespace(occlusionState=lambda: occlusion)
    button = types.SimpleNamespace(window=lambda: window)
    return types.SimpleNamespace(isVisible=lambda: visible, button=lambda: button)


@pytest.mark.parametrize("system_hosts_items,visible,occlusion,expected", [
    # macOS 26: Control Center draws the item; the app's own button window is never on screen.
    (True, True, 0, True),
    (True, False, 0, False),
    # Earlier macOS: the button window is the item, so its occlusion state is meaningful.
    (False, True, 0, False),
    (False, True, 2, True),
])
def test_status_item_visibility_ignores_window_occlusion_when_the_system_hosts_status_items(
    shell, monkeypatch, system_hosts_items, visible, occlusion, expected,
):
    monkeypatch.setattr(hud_app, "_system_hosts_status_items", lambda: system_hosts_items)
    shell.status_item = _status_item(visible, occlusion)
    assert shell._status_item_visibility() is expected


@pytest.mark.parametrize("version,expected", [((15, 6, 0), False), ((16, 0, 0), True), ((26, 7, 0), True)])
def test_system_hosts_status_items_from_macos_26(monkeypatch, version, expected):
    info = types.SimpleNamespace(operatingSystemVersion=lambda: version)
    monkeypatch.setattr(hud_app.AppKit, "NSProcessInfo", types.SimpleNamespace(processInfo=lambda: info))
    assert hud_app._system_hosts_status_items() is expected


def test_recheck_health_menu_item_dispatches_to_worker(shell):
    shell.recheckHealth_(None)
    assert shell._worker.actions == [("health.recheck", {})]


def test_page_recheck_and_repair_actions_dispatch_to_worker(shell):
    shell.handle_message({"action": "health.recheck", "args": {}})
    shell.handle_message({"action": "issue.repair", "args": {"issue_code": "daemon.hung"}})
    assert shell._worker.actions == [
        ("health.recheck", {}),
        ("issue.repair", {"issue_code": "daemon.hung"}),
    ]


def test_rendered_status_includes_rechecked_subtext(shell):
    shell._apply_model_script(
        'window.render({"status":{"text":"正在记录","sub":"已重新检测 20:45:00 · 正常","tone":"ok"},"tabs":[{"id":"overview"}],"pause":{"label":"暂停","action":"capture.pause"},"view":{"tab":"overview","page":""},"title":"概览","sections":[]});',
        None,
    )
    rendered = _rendered(shell)
    assert rendered["status"]["sub"] == "已重新检测 20:45:00 · 正常"


def test_confirm_quit_removes_hud_record(shell, monkeypatch):
    removed = []
    terminated = []
    monkeypatch.setattr("penpal.foundation.hud_record.remove_hud_record", lambda: removed.append(True))
    monkeypatch.setattr(hud_app, "_confirm_quit_alert", lambda: True)
    monkeypatch.setattr(hud_app, "_terminate_app", lambda: terminated.append(True))
    fake_popover = types.SimpleNamespace(performClose_=lambda _s: None)
    shell.popover = fake_popover

    shell.confirmQuit_(None)
    assert removed == [True]
    assert terminated == [True]


def test_wake_for_signals_is_noop(shell):
    assert shell.wakeForSignals_(None) is None


def test_run_hud_registers_signal_wake_timer(monkeypatch):
    timers = []
    fake_app = types.SimpleNamespace(
        setActivationPolicy_=lambda policy: None,
        setDelegate_=lambda delegate: None,
        run=lambda: None,
    )
    fake_appkit = types.SimpleNamespace(
        NSApplication=types.SimpleNamespace(sharedApplication=lambda: fake_app),
        NSApplicationActivationPolicyAccessory=0,
    )
    fake_delegate = hud_app.PenpalHUDApp.alloc().init()
    monkeypatch.setattr(hud_app, "AppKit", fake_appkit)
    monkeypatch.setattr(
        hud_app,
        "PenpalHUDApp",
        types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(initWithConfig_=lambda cfg: fake_delegate)),
    )
    monkeypatch.setattr(hud_app.signal, "signal", lambda sig, handler: None)

    def fake_timer(interval, target, selector, user_info, repeats):
        timers.append({
            "interval": interval,
            "target": target,
            "selector": selector,
            "user_info": user_info,
            "repeats": repeats,
        })

    monkeypatch.setattr(
        hud_app,
        "NSTimer",
        types.SimpleNamespace(scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=fake_timer),
    )

    fake_cfg = types.SimpleNamespace()
    hud_app.run_hud(fake_cfg)

    fast_timers = [t for t in timers if t["interval"] <= 1.0]
    assert len(fast_timers) == 1
    timer = fast_timers[0]
    assert timer["repeats"] is True
    assert timer["target"] is fake_delegate
    assert "wakeForSignals" in str(timer["selector"].selector)
    assert fake_delegate.wakeForSignals_(None) is None


def test_quit_hud_schedules_confirm_quit_via_call_after(shell, monkeypatch):
    calls = []
    monkeypatch.setattr(hud_app.AppHelper, "callAfter", lambda fn, *args: calls.append((fn, args)))

    shell._quit_hud()

    assert calls == [(shell.confirmQuit_, (None,))]


