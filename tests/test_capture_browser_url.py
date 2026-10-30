from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import patch


from penpal.capture.normalizer import normalize_browser_url_event
from penpal.capture.watchers.browser_url import (
    BrowserUrlWatcher,
    _parse_browser_output,
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _watcher() -> BrowserUrlWatcher:
    return BrowserUrlWatcher(
        queue.Queue(),
        poll_interval_sec=0.1,
        supported_browsers=["Safari", "Google Chrome", "Arc", "Microsoft Edge", "Brave Browser"],
        emit_on_url_change_only=True,
    )


def test_parse_browser_output_supports_pipe_delimiter():
    parsed = _parse_browser_output("https://example.com/a|||Page A")
    assert parsed == ("https://example.com/a", "Page A")


def test_browser_url_watcher_dedupes_same_url():
    watcher = _watcher()
    with (
        patch("penpal.capture.watchers.browser_url._get_frontmost_app_name", return_value="Google Chrome"),
        patch("penpal.capture.watchers.browser_url.subprocess.run") as run,
    ):
        run.side_effect = [
            _completed(stdout="https://example.com/a|||Page A"),
            _completed(stdout="https://example.com/a|||Page B"),
        ]
        first = watcher.capture_once()
        second = watcher.capture_once()

    assert first is not None
    assert first.source == "browser_url"
    assert first.event_type == "browser_url_capture"
    assert second is None


def test_browser_url_watcher_marks_denied_and_disables_browser():
    watcher = _watcher()
    with (
        patch("penpal.capture.watchers.browser_url._get_frontmost_app_name", return_value="Google Chrome"),
        patch("penpal.capture.watchers.browser_url.subprocess.run") as run,
        patch("penpal.capture.watchers.browser_url.mark_browser_automation_denied") as mark_denied,
    ):
        run.return_value = _completed(stderr="not allowed to send Apple events", returncode=1)
        first = watcher.capture_once()
        second = watcher.capture_once()

    assert first is None
    assert second is None
    assert run.call_count == 1
    mark_denied.assert_called_once_with("Google Chrome")


def test_browser_url_watcher_auto_discovers_supported_browsers():
    with (
        patch(
            "penpal.capture.watchers.browser_url.discover_http_handler_apps",
            return_value=("ChatGPT Atlas", "Safari"),
        ) as discover,
    ):
        watcher = BrowserUrlWatcher(
            queue.Queue(),
            poll_interval_sec=0.1,
            supported_browsers="auto",
            emit_on_url_change_only=True,
        )

    with (
        patch("penpal.capture.watchers.browser_url._get_frontmost_app_name", return_value="ChatGPT Atlas"),
        patch("penpal.capture.watchers.browser_url.subprocess.run") as run,
    ):
        run.return_value = _completed(stdout="https://example.com/a|||Page A")
        event = watcher.capture_once()

    discover.assert_called_once()
    assert watcher._supported_browsers == ("ChatGPT Atlas", "Safari")
    assert event is not None
    assert event.app_name == "ChatGPT Atlas"
    assert event.content_text == "Page A"
    assert event.window_title == "Page A - ChatGPT Atlas"
    run.assert_called_once()


def test_browser_url_watcher_disables_unsupported_dialect_without_marking_denied():
    watcher = BrowserUrlWatcher(
        queue.Queue(),
        poll_interval_sec=0.1,
        supported_browsers=["ChatGPT Atlas"],
        emit_on_url_change_only=True,
    )
    with (
        patch("penpal.capture.watchers.browser_url._get_frontmost_app_name", return_value="ChatGPT Atlas"),
        patch("penpal.capture.watchers.browser_url.subprocess.run") as run,
        patch("penpal.capture.watchers.browser_url.mark_browser_automation_denied") as mark_denied,
    ):
        run.return_value = _completed(
            stderr="ChatGPT Atlas got an error: Can't get active tab of front window. (-1728)",
            returncode=1,
        )
        first = watcher.capture_once()
        second = watcher.capture_once()

    assert first is None
    assert second is None
    assert run.call_count == 1
    mark_denied.assert_not_called()


def test_browser_url_watcher_skips_unsupported_frontmost_app():
    watcher = _watcher()
    with (
        patch("penpal.capture.watchers.browser_url._get_frontmost_app_name", return_value="Terminal"),
        patch("penpal.capture.watchers.browser_url.subprocess.run") as run,
    ):
        event = watcher.capture_once()

    assert event is None
    run.assert_not_called()


def test_normalize_browser_url_event_hash_uses_url_not_title():
    first = normalize_browser_url_event(
        url="https://example.com/a",
        title="Title One",
        browser_name="Safari",
    )
    second = normalize_browser_url_event(
        url="https://example.com/a",
        title="Title Two",
        browser_name="Safari",
    )
    assert first.content_hash == second.content_hash
