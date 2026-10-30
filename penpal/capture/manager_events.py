"""Capture event gating, sanitization, session handling, and persistence."""

from __future__ import annotations

import json
import queue
import sqlite3
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from penpal.capture.capture_control import source_writes_allowed
from penpal.foundation.chat_apps import chat_app_key
from penpal.foundation.capture_hygiene import (
    METADATA_ONLY,
    PAGE_SCOPED_STREAMS,
    capture_gate,
    is_blacklisted_app,
    is_system_overlay,
    is_terminal_app,
    host_matches_denylist as _host_matches_denylist,
    strip_url_query,
)
from penpal.capture.manager_storage import _is_disk_full
from penpal.capture.normalizer import WINDOW_FOCUS_EVENT, WINDOW_TITLE_CHANGED_EVENT
from penpal.capture.user_presence import is_user_present
from penpal.foundation.desensitizer import desensitize, desensitize_json_value, truncate
from penpal.store.models import RawEvent, SearchDoc
from penpal.store.repository import insert_raw_event, insert_search_doc, set_state
from penpal.utils.logging import get_logger

logger = get_logger("manager")
_USER_SOURCES = frozenset(
    {
        "clipboard",
        "manual",
        "browser",
        "browser_url",
        "browser_history",
        "ax_text",
        "keyboard_chunk",
    }
)
_CHAT_STYLE_APPS = frozenset(
    {"claude", "codex", "cursor", "obsidian", "notion", "visual studio code"}
)


def _derive_speaker(event: RawEvent) -> Literal["user", "system"]:
    return "user" if event.source in _USER_SOURCES else "system"


def _is_terminal_app(app_name: str | None) -> bool:
    return is_terminal_app(app_name)


def _metadata_payload(metadata_json: str | None) -> object | None:
    if not metadata_json:
        return None
    try:
        return json.loads(metadata_json)
    except Exception:
        return None


def _sanitize_metadata_json(
    metadata_json: str | None,
    *,
    extra_fields: dict[str, object] | None = None,
    redact_emails: bool,
    redact_phones: bool,
    redact_tokens: bool,
) -> str | None:
    if not metadata_json and not extra_fields:
        return metadata_json

    parsed = _metadata_payload(metadata_json)
    if parsed is None:
        if not extra_fields:
            return metadata_json
        parsed = {}

    sanitized = desensitize_json_value(
        parsed,
        redact_emails=redact_emails,
        redact_phones=redact_phones,
        redact_tokens=redact_tokens,
    )
    if extra_fields:
        if isinstance(sanitized, dict):
            sanitized = {**sanitized, **extra_fields}
        else:
            sanitized = dict(extra_fields)

    try:
        return json.dumps(sanitized, ensure_ascii=False)
    except Exception:
        return (
            metadata_json
            if not extra_fields
            else json.dumps(extra_fields, ensure_ascii=False)
        )


def _sanitize_content_text(
    text: str | None,
    *,
    redact_emails: bool,
    redact_phones: bool,
    redact_tokens: bool,
) -> str | None:
    if not text:
        return text
    return desensitize(
        text,
        redact_emails=redact_emails,
        redact_phones=redact_phones,
        redact_tokens=redact_tokens,
    )


def _browser_host_from_metadata(metadata_json: str | None) -> str | None:
    payload = _metadata_payload(metadata_json)
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    if not url:
        return None
    try:
        host = urlsplit(str(url)).hostname
    except Exception:
        return None
    if not host:
        return None
    return host.lower()


def _should_drop_browser_event(event: RawEvent, deny_hosts: list[str]) -> bool:
    if event.source not in ("browser", "browser_url"):
        return False
    host = _browser_host_from_metadata(event.metadata_json)
    if not host:
        return False
    return _host_matches_denylist(host, deny_hosts)


def _event_url(event: RawEvent) -> str | None:
    payload = _metadata_payload(event.metadata_json)
    url = payload.get("url") if isinstance(payload, dict) else None
    return str(url) if url else None


def _strip_metadata_urls(metadata_json: str | None) -> str | None:
    payload = _metadata_payload(metadata_json)
    if not isinstance(payload, dict):
        return metadata_json
    if payload.get("url"):
        payload["url"] = strip_url_query(str(payload["url"]))
    entities = payload.get("entities")
    if isinstance(entities, dict) and isinstance(entities.get("urls"), list):
        entities["urls"] = [strip_url_query(str(u)) for u in entities["urls"]]
    return json.dumps(payload, ensure_ascii=False)


def _strip_metadata_title(metadata_json: str | None) -> str | None:
    """Window session events also carry the title as metadata 'primary_title'. Drop it with window_title."""
    payload = _metadata_payload(metadata_json)
    if not isinstance(payload, dict) or "primary_title" not in payload:
        return metadata_json
    payload.pop("primary_title", None)
    return json.dumps(payload, ensure_ascii=False)


def _drain_queue(manager):
    events = []
    while True:
        try:
            events.append(manager._queue.get_nowait())
        except queue.Empty:
            break
    for event in events:
        try:
            manager._process_event(event)
        except Exception as e:
            logger.error(f"Error processing event: {e}")


def _is_blacklisted(manager, event: RawEvent) -> bool:
    privacy = manager.config.privacy
    chat = chat_app_key(event.app_name, event.process_name)
    if chat is not None:
        # Chat apps are excluded by default (D19). An explicit opt-in overrides the default blacklist
        # entries for that app (bundle ids, com.tencent.* pattern, app names).
        return chat not in privacy.chat_apps_opt_in
    return is_blacklisted_app(
        event.app_name,
        event.process_name,
        bundle_ids=privacy.blacklist_bundle_ids,
        patterns=privacy.blacklist_patterns,
        app_names=privacy.blacklist_app_names,
    )


def _note_browser_page(manager, event: RawEvent) -> None:
    """Remember the page a browser app is showing. Called before any drop, so the URL of an excluded
    domain is recorded too (its own event is dropped, but the text typed after it must be checked).

    The URL watcher emits only when the frontmost browser's URL changes, and it polls every few seconds,
    so a value can lag a tab switch by about one poll interval. A browser with no URL seen yet is
    unknown and is not blocked.
    """
    if event.source not in ("browser", "browser_url"):
        return
    url = _event_url(event)
    app = (event.app_name or "").strip().casefold()
    if url and app:
        manager._browser_pages[app] = url


def _current_page_url(manager, event: RawEvent) -> str | None:
    app = (event.app_name or "").strip().casefold()
    return manager._browser_pages.get(app) if app else None


def _count_gate_hit(manager, event: RawEvent, reason: str) -> None:
    key = f"{event.source}:{reason}"
    manager._gate_hits[key] = manager._gate_hits.get(key, 0) + 1
    logger.debug("capture gate: %s (%s)", reason, event.source)  # never log content


def _process_event(manager, event: RawEvent):
    # Write gate (#28): global pause or a switched-off source writes nothing new.
    if not source_writes_allowed(event.source):
        manager._count_gate_hit(event, "paused_or_disabled")
        return

    if manager._storage_write_blocked():
        manager._storage_dropped += 1
        return
    event.speaker = _derive_speaker(event)

    manager._note_browser_page(event)
    if manager._is_blacklisted(event):
        logger.debug(
            f"Event blacklisted: {event.process_name or event.app_name} ({event.source}/{event.event_type})"
        )
        return

    if _should_drop_browser_event(event, manager.config.privacy.url_deny_hosts):
        logger.debug(
            "Event dropped by browser URL denylist: %s (%s/%s)",
            event.process_name or event.app_name,
            event.source,
            event.event_type,
        )
        return

    verdict = capture_gate(
        source=event.source,
        app_name=event.app_name,
        process_name=event.process_name,
        window_title=event.window_title,
        content_text=event.content_text,
        url=_event_url(event),
        title_deny_patterns=manager.config.privacy.title_deny_patterns,
        page_url=manager._current_page_url(event)
        if event.source in PAGE_SCOPED_STREAMS
        else None,
        url_deny_hosts=manager.config.privacy.url_deny_hosts,
    )
    if verdict is not None:
        manager._count_gate_hit(event, verdict.reason)
        if verdict.action != METADATA_ONLY:
            return

    if event.source != "window" and is_system_overlay(
        event.app_name, event.process_name
    ):
        # Setup Assistant & co: keep what was typed/copied/read, but it belongs to no app (D27).
        event.app_name = None
        event.process_name = None

    manager._sync_window_idle_state(event)
    if event.source == "window" and event.event_type in {
        WINDOW_FOCUS_EVENT,
        WINDOW_TITLE_CHANGED_EVENT,
    }:
        return

    # 1. Policy
    result = manager._policy.apply(event)
    if result is None:
        logger.debug(f"Event denied by policy: {event.source}/{event.event_type}")
        return

    # 2. Desensitize content
    text_dropped_reason: str | None = None
    if verdict is not None:  # metadata-only: keep app + time, null the content
        text_dropped_reason = verdict.reason
        result.content_text = None
        result.content_hash = None
        if (
            verdict.reason != "terminal_app"
        ):  # title-based reasons: the title itself is the sensitive part
            result.window_title = None
        if result.source == "browser_url":
            result.metadata_json = _strip_metadata_urls(result.metadata_json)
        if result.source == "window":
            result.metadata_json = _strip_metadata_title(result.metadata_json)
    if result.source == "ax_text" and _is_terminal_app(result.app_name):
        if manager.config.privacy.drop_terminal_text:
            result.content_text = None
            text_dropped_reason = "terminal_app"
        elif result.content_text:
            result.content_text = desensitize(
                result.content_text,
                redact_emails=manager.config.privacy.redact_emails,
                redact_phones=manager.config.privacy.redact_phones,
                redact_tokens=manager.config.privacy.redact_tokens,
            )
            result.content_text = truncate(
                result.content_text, manager.config.clipboard.max_text_length
            )
            fusion = manager._fusion.fuse(result)
            if not fusion.persist:
                return
            if fusion.event is not None:
                result = fusion.event
    elif result.content_text:
        result.content_text = _sanitize_content_text(
            result.content_text,
            redact_emails=manager.config.privacy.redact_emails,
            redact_phones=manager.config.privacy.redact_phones,
            redact_tokens=manager.config.privacy.redact_tokens,
        )
        result.content_text = truncate(
            result.content_text, manager.config.clipboard.max_text_length
        )
        fusion = manager._fusion.fuse(result)
        if not fusion.persist:
            return
        if fusion.event is not None:
            result = fusion.event

    if result.window_title:
        result.window_title = desensitize(
            result.window_title,
            redact_emails=manager.config.privacy.redact_emails,
            redact_phones=manager.config.privacy.redact_phones,
            redact_tokens=manager.config.privacy.redact_tokens,
        )
    if result.source == "keyboard_chunk":
        result.content_text = _sanitize_content_text(
            result.content_text,
            redact_emails=manager.config.privacy.redact_emails,
            redact_phones=manager.config.privacy.redact_phones,
            redact_tokens=manager.config.privacy.redact_tokens,
        )
        if result.app_name and result.app_name.strip().lower() not in _CHAT_STYLE_APPS:
            result.semantic_weight = 0.5
        else:
            result.semantic_weight = 1.0

    preserve_raw_browser_url = result.source == "browser_url"
    result.metadata_json = _sanitize_metadata_json(
        result.metadata_json,
        extra_fields={"text_dropped": text_dropped_reason}
        if text_dropped_reason
        else None,
        redact_emails=False
        if preserve_raw_browser_url
        else manager.config.privacy.redact_emails,
        redact_phones=False
        if preserve_raw_browser_url
        else manager.config.privacy.redact_phones,
        redact_tokens=False
        if preserve_raw_browser_url
        else manager.config.privacy.redact_tokens,
    )

    if result.session_id is None and result.event_type not in {
        "idle_start",
        "idle_end",
    }:
        session_id = manager._current_window_session_id()
        if session_id:
            result.session_id = session_id

    # 3. Session tracking
    session = manager._aggregator.process(result)
    if session and result.session_id is None:
        result.session_id = session.id

    # 4. User presence
    result.user_present = 1 if is_user_present() else 0

    # 5. Persist raw event
    try:
        row_id = insert_raw_event(result)
    except sqlite3.OperationalError as exc:
        if not _is_disk_full(exc):
            raise
        manager._enter_disk_full()
        return
    manager._leave_disk_full()
    result.id = row_id
    manager._record_runtime_event(result)

    # 5. Index searchable content
    if result.source in ("clipboard", "manual") and result.content_text:
        tags = None
        if result.metadata_json:
            try:
                tags = json.loads(result.metadata_json).get("tags")
            except Exception as exc:
                logger.debug("process event failed: %s", type(exc).__name__)
                pass
        doc = SearchDoc(
            ref_type=result.source,
            ref_id=str(row_id),
            title=result.window_title or result.app_name,
            body=result.content_text,
            tags=tags,
            app_name=result.app_name,
        )
        insert_search_doc(doc)

    set_state("last_flush", datetime.now(timezone.utc).isoformat())


def _current_window_session_id(manager) -> str | None:
    watcher = manager._watchers.get("window")
    if watcher is None or not hasattr(watcher, "current_session_id"):
        return None
    session_id = watcher.current_session_id()
    return str(session_id) if session_id else None


def _flush_window_session(manager, reason: str) -> None:
    watcher = manager._watchers.get("window")
    if watcher is None or not hasattr(watcher, "flush_current_session"):
        return
    event = watcher.flush_current_session(
        ended_at=datetime.now(timezone.utc).isoformat(),
        ended_at_mono=time.monotonic(),
        reason=reason,
    )
    if event is not None:
        manager._process_event(event)


def _sync_window_idle_state(manager, event: RawEvent) -> None:
    watcher = manager._watchers.get("window")
    if watcher is None:
        return
    if event.event_type == "idle_start":
        manager._flush_window_session(reason="idle_timeout")
        if hasattr(watcher, "set_idle"):
            watcher.set_idle(True)
    elif event.event_type == "idle_end" and hasattr(watcher, "set_idle"):
        watcher.set_idle(False)
