"""Bucket assignment (V1 PRD §3.4.2, D23/D37): project/app/source rules, no content classification.

Resolution order for an event:
  1. enabled `policies` rows with mode='bucket', grouped by specificity: project, app, source.
     Within a scope type, lower priority then lower id wins. Project matches the normalized project
     folder name exactly and case-insensitively; app matches app_name / process_name by
     case-insensitive substring; source is exact. config_json = {"bucket": "<Name>"}.
  2. config `[buckets.source_defaults]` overrides, then DEFAULT_SOURCE_BUCKETS.
     chrome_history / safari_history have no default here: they are site-classified (below).
  3. Penpal's own capture streams (CAPTURE_SOURCES, D26) are classified by app: config
     `[buckets.app_defaults]` (substring on app / process name), then a chat app the user opted in
     (`[privacy] chat_apps_opt_in`) is Private, then the preset table in `store.app_categories`,
     then Personal (unknown apps are Personal).
     Browser content (D28) is classified by site first: config `[buckets.site_defaults]`, then the
     preset table in `store.site_categories`; a site with no entry falls back to the app level (so
     Personal). Browser-history plugin rows (chrome_history / safari_history) take the same site
     table from their own URL, with Personal as the fallback. browser_url rows carry their own URL;
     ax_text / clipboard / window / keyboard rows of a browser take the URL of the nearest browser_url
     row of the same browser at or before them,
     within SITE_LOOKBACK (10 minutes); with none they stay at the app level.
  4. otherwise None: an unmapped plugin source is never auto-placed in a shareable bucket (PRD:
     uncertain origin must not default to a low-sensitivity bucket), so no grant can read it.

The bucket is stamped on the row at ingest; changing rules is not retroactive until
`reapply_buckets()` (CLI: `penpal rules rebucket`) is run.
"""
from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Optional

BUCKETS = ("Work", "Personal", "Private", "Financial")  # Temporary is P1 (D23)
BUCKET_MODE = "bucket"  # policies.mode value marking a bucket rule (ignored by the privacy PolicyEngine)

DEFAULT_SOURCE_BUCKETS: dict[str, str] = {
    "claude_code": "Work",
    "codex_cli": "Work",
    "git_log": "Work",
    "zsh_history": "Work",
    "markdown_vault": "Personal",
    "wechat": "Personal",
    "knowledgec": "Personal",
    "spotlight": "Personal",
}

# raw_events.source values written by Penpal's own capture (not by source plugins).
CAPTURE_SOURCES = frozenset(
    {"keyboard_chunk", "window", "clipboard", "ax_text", "browser_url", "idle", "manual", "ocr_text", "browser"}
)

# Streams whose bucket follows the website (D28). browser_url / browser carry their own URL; the
# others inherit the nearest browser_url of the same browser when the app is a browser.
SITE_OWN_URL_SOURCES = frozenset({"browser_url", "browser"})
SITE_INHERIT_SOURCES = frozenset({"ax_text", "clipboard", "window", "keyboard_chunk"})
SITE_LOOKBACK = timedelta(minutes=10)
# Browser-history plugins (D28): every row is one visit with its own URL in metadata_json["url"]. They
# are bucketed by site like browser_url, with no app-level default (see resolve_bucket).
SITE_HISTORY_SOURCES = frozenset({"chrome_history", "safari_history"})

_overrides: Optional[dict[str, str]] = None
_app_overrides: Optional[dict[str, str]] = None
_site_overrides: Optional[dict[str, str]] = None
_chat_opt_ins: Optional[frozenset[str]] = None


def normalize_bucket(name: str) -> str:
    """Canonical bucket name (case-insensitive) or ValueError."""
    for b in BUCKETS:
        if (name or "").strip().lower() == b.lower():
            return b
    raise ValueError(f"unknown bucket {name!r}; choose from {', '.join(BUCKETS)}")


def reset_cache() -> None:
    global _overrides, _app_overrides, _site_overrides, _chat_opt_ins
    _overrides = None
    _app_overrides = None
    _site_overrides = None
    _chat_opt_ins = None


def _config_app_overrides() -> dict[str, str]:
    global _app_overrides
    if _app_overrides is None:
        found: dict[str, str] = {}
        try:
            from penpal.config import Config

            for app, bucket in Config.load().buckets.app_defaults.items():
                try:
                    found[app.strip().lower()] = normalize_bucket(bucket)
                except ValueError:
                    continue
        except Exception:
            found = {}
        _app_overrides = found
    return _app_overrides


def _config_site_overrides() -> dict[str, str]:
    global _site_overrides
    if _site_overrides is None:
        from penpal.store.site_categories import normalize_site_key

        found: dict[str, str] = {}
        try:
            from penpal.config import Config

            for site, bucket in Config.load().buckets.site_defaults.items():
                try:
                    key = normalize_site_key(site)
                    if key:
                        found[key] = normalize_bucket(bucket)
                except ValueError:
                    continue
        except Exception:
            found = {}
        _site_overrides = found
    return _site_overrides


def url_from_metadata(metadata_json: Optional[str]) -> Optional[str]:
    if not metadata_json or "url" not in metadata_json:
        return None
    try:
        payload = json.loads(metadata_json)
    except (TypeError, ValueError):
        return None
    url = payload.get("url") if isinstance(payload, dict) else None
    return url if isinstance(url, str) and url.strip() else None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def wants_site_url(source: Optional[str], app_name: Optional[str], process_name: Optional[str] = None) -> bool:
    """Whether a row's bucket depends on a website (so a URL lookup is worth doing)."""
    from penpal.store.site_categories import browser_family

    if source in SITE_OWN_URL_SOURCES or source in SITE_HISTORY_SOURCES:
        return True
    return source in SITE_INHERIT_SOURCES and browser_family(app_name, process_name) is not None


def _norm_title(title: Optional[str]) -> str:
    from penpal.utils.text_filters import strip_invisible

    return (strip_invisible(title) or "").strip()


def page_title_of(content_text: Optional[str], window_title: Optional[str], app_name: Optional[str]) -> str:
    """Page title of a browser_url row: its content_text (the tab title), else its window_title minus
    the " - <Browser>" suffix. A bare URL is not a title."""
    text = _norm_title(content_text)
    if text and "://" not in text:
        return text
    title = _norm_title(window_title)
    suffix = f" - {(app_name or '').strip()}"
    if app_name and title.endswith(suffix):
        title = title[: -len(suffix)].rstrip()
    return title


def title_matches(event_title: Optional[str], page_title: str) -> bool:
    """The event's window title is the page title, optionally followed by " - ..." decorations
    ("... - 已固定 - 内存用量高 - 1.8 GB - Google Chrome")."""
    title = _norm_title(event_title)
    page = _norm_title(page_title)
    if not title or not page:
        return False
    return title == page or title.startswith(page + " - ")


def nearest_browser_url(
    conn: sqlite3.Connection, app_name, process_name, ts_start: Optional[str], window_title: Optional[str] = None
) -> Optional[str]:
    """URL of the browser_url row of the same browser at or before ts_start, within SITE_LOOKBACK.

    With a window title (tab switching makes "nearest in time" unreliable) only a row whose page title
    matches the title counts, latest first; no match means no URL. Without a title it is the latest row."""
    from penpal.store.site_categories import browser_family

    family = browser_family(app_name, process_name)
    ts = _parse_ts(ts_start)
    if family is None or ts is None:
        return None
    wants_title = bool(_norm_title(window_title))
    lower = (ts - SITE_LOOKBACK).astimezone(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT ts_start, app_name, process_name, metadata_json, content_text, window_title FROM raw_events "
        "WHERE source = 'browser_url' AND ts_start <= ? AND ts_start >= ? ORDER BY ts_start DESC LIMIT 200",
        (ts_start, lower),
    ).fetchall()
    for row in rows:
        if browser_family(row[1], row[2]) != family:
            continue
        row_ts = _parse_ts(row[0])
        if row_ts is None or not (ts - SITE_LOOKBACK <= row_ts <= ts):
            continue
        url = url_from_metadata(row[3])
        if not url:
            continue
        if wants_title and not title_matches(window_title, page_title_of(row[4], row[5], row[1])):
            continue
        return url
    return None


class BrowserUrlTimeline:
    """In-memory index of browser_url rows (per browser family) for bulk re-bucketing: one pass over
    the table instead of one lookup per row. Same matching rules as nearest_browser_url."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        from penpal.store.site_categories import browser_family

        by_family: dict[str, list[tuple[datetime, str, str]]] = {}
        for row in conn.execute(
            "SELECT ts_start, app_name, process_name, metadata_json, content_text, window_title FROM raw_events "
            "WHERE source = 'browser_url' ORDER BY ts_start"
        ):
            family = browser_family(row[1], row[2])
            ts = _parse_ts(row[0])
            url = url_from_metadata(row[3])
            if family and ts and url:
                by_family.setdefault(family, []).append((ts, url, page_title_of(row[4], row[5], row[1])))
        for entries in by_family.values():
            entries.sort(key=lambda e: e[0])
        self._entries = by_family
        self._times = {f: [e[0] for e in entries] for f, entries in by_family.items()}

    def lookup(self, app_name, process_name, ts_start: Optional[str], window_title: Optional[str] = None) -> Optional[str]:
        from penpal.store.site_categories import browser_family

        family = browser_family(app_name, process_name)
        ts = _parse_ts(ts_start)
        if family is None or ts is None or family not in self._entries:
            return None
        wants_title = bool(_norm_title(window_title))
        entries = self._entries[family]
        i = bisect_right(self._times[family], ts) - 1
        while i >= 0:
            entry_ts, url, page = entries[i]
            if ts - entry_ts > SITE_LOOKBACK:
                return None
            if not wants_title or title_matches(window_title, page):
                return url
            i -= 1
        return None


def _config_chat_opt_ins() -> frozenset[str]:
    global _chat_opt_ins
    if _chat_opt_ins is None:
        try:
            from penpal.config import Config

            found = frozenset(Config.load().privacy.chat_apps_opt_in)
        except Exception:
            found = frozenset()  # fail closed: no opt-in when the config cannot be read
        _chat_opt_ins = found
    return _chat_opt_ins


def classify_capture_app(
    app_name: Optional[str],
    process_name: Optional[str] = None,
    app_overrides: Optional[dict[str, str]] = None,
    chat_opt_ins: Optional[frozenset[str]] = None,
) -> str:
    """Capture-stream bucket from the app alone: config overrides > opted-in chat app (Private) > preset
    table > Personal."""
    from penpal.foundation.chat_apps import PRIVATE_BUCKET, chat_app_key
    from penpal.store.app_categories import DEFAULT_APP_BUCKET, classify_app

    merged = _config_app_overrides() if app_overrides is None else app_overrides
    names = [(n or "").lower() for n in (app_name, process_name)]
    for key, bucket in merged.items():
        if key and any(key in n for n in names):
            return bucket
    opted = _config_chat_opt_ins() if chat_opt_ins is None else chat_opt_ins
    chat = chat_app_key(app_name, process_name)
    if chat is not None and chat in opted:
        return PRIVATE_BUCKET
    return classify_app(app_name, process_name) or DEFAULT_APP_BUCKET


def _site_bucket(url: Optional[str], site_overrides: Optional[dict[str, str]] = None) -> Optional[str]:
    """Site table only (config site_defaults > preset); None when no URL or no entry matches."""
    if not url:
        return None
    from penpal.store.site_categories import classify_url

    merged = _config_site_overrides() if site_overrides is None else site_overrides
    return classify_url(url, merged)


def _config_overrides() -> dict[str, str]:
    global _overrides
    if _overrides is None:
        found: dict[str, str] = {}
        try:
            from penpal.config import Config

            for src, bucket in Config.load().buckets.source_defaults.items():
                try:
                    found[src] = normalize_bucket(bucket)
                except ValueError:
                    continue  # ignore invalid config entries rather than failing ingest
        except Exception:
            found = {}
        _overrides = found
    return _overrides


def default_bucket_for_source(source: Optional[str], overrides: Optional[dict[str, str]] = None) -> Optional[str]:
    if not source:
        return None
    merged = _config_overrides() if overrides is None else overrides
    return merged.get(source) or DEFAULT_SOURCE_BUCKETS.get(source)


def load_bucket_rules(conn: sqlite3.Connection) -> list[dict]:
    from penpal.foundation.project import normalize_project

    rows = conn.execute(
        "SELECT scope_type, scope_value, config_json FROM policies "
        "WHERE enabled = 1 AND mode = ? AND scope_type IN ('project', 'app', 'source') "
        "ORDER BY priority ASC, id ASC",
        (BUCKET_MODE,),
    ).fetchall()
    rules = []
    for r in rows:
        try:
            bucket = normalize_bucket(json.loads(r["config_json"] or "{}").get("bucket", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        value = r["scope_value"]
        if r["scope_type"] == "project":
            value = normalize_project(value)
        rules.append({"scope_type": r["scope_type"], "scope_value": value, "bucket": bucket})
    scope_order = {"project": 0, "app": 1, "source": 2}
    return sorted(rules, key=lambda rule: scope_order[rule["scope_type"]])


def resolve_bucket(
    source: Optional[str],
    app_name: Optional[str],
    process_name: Optional[str] = None,
    *,
    rules: list[dict],
    project: Optional[str] = None,
    overrides: Optional[dict[str, str]] = None,
    app_overrides: Optional[dict[str, str]] = None,
    url: Optional[str] = None,
    site_overrides: Optional[dict[str, str]] = None,
) -> Optional[str]:
    from penpal.foundation.project import normalize_project, project_matches

    project = normalize_project(project)
    for rule in rules:
        value = (rule["scope_value"] or "").lower()
        if not value:
            continue
        if rule["scope_type"] == "project":
            if project_matches(value, project):
                return rule["bucket"]
        elif rule["scope_type"] == "app":
            if any(value in (name or "").lower() for name in (app_name, process_name)):
                return rule["bucket"]
        elif rule["scope_type"] == "source" and source == rule["scope_value"]:
            return rule["bucket"]
    default = default_bucket_for_source(source, overrides)
    if default is None and source in SITE_HISTORY_SOURCES:
        from penpal.store.site_categories import DEFAULT_SITE_BUCKET

        return _site_bucket(url, site_overrides) or DEFAULT_SITE_BUCKET
    if default is None and source in CAPTURE_SOURCES:
        if url and wants_site_url(source, app_name, process_name):
            site = _site_bucket(url, site_overrides)
            if site:
                return site
        return classify_capture_app(app_name, process_name, app_overrides)
    return default


def resolve_for_event(
    conn: sqlite3.Connection,
    source,
    app_name,
    process_name=None,
    *,
    metadata_json: Optional[str] = None,
    ts_start: Optional[str] = None,
    window_title: Optional[str] = None,
) -> Optional[str]:
    from penpal.foundation.project import project_from_metadata_json

    url = None
    if wants_site_url(source, app_name, process_name):
        url = url_from_metadata(metadata_json)
        if url is None and source in SITE_INHERIT_SOURCES:
            url = nearest_browser_url(conn, app_name, process_name, ts_start, window_title)
    return resolve_bucket(
        source, app_name, process_name, rules=load_bucket_rules(conn),
        project=project_from_metadata_json(metadata_json), url=url,
    )


def reapply_buckets(conn: sqlite3.Connection, *, only_missing: bool = False) -> int:
    """Re-evaluate buckets for stored rows with current rules; returns rows changed."""
    from penpal.foundation.project import project_from_metadata_json

    rules = load_bucket_rules(conn)
    overrides = _config_overrides()
    app_overrides = _config_app_overrides()
    site_overrides = _config_site_overrides()
    timeline = BrowserUrlTimeline(conn)
    where = " WHERE bucket IS NULL" if only_missing else ""
    changed = 0
    for row in conn.execute(
        f"SELECT id, source, app_name, process_name, bucket, ts_start, metadata_json, window_title FROM raw_events{where}"
    ).fetchall():
        url = None
        if wants_site_url(row["source"], row["app_name"], row["process_name"]):
            url = url_from_metadata(row["metadata_json"])
            if url is None and row["source"] in SITE_INHERIT_SOURCES:
                url = timeline.lookup(row["app_name"], row["process_name"], row["ts_start"], row["window_title"])
        new = resolve_bucket(
            row["source"], row["app_name"], row["process_name"], rules=rules, overrides=overrides,
            project=project_from_metadata_json(row["metadata_json"]),
            app_overrides=app_overrides, url=url, site_overrides=site_overrides,
        )
        if new != row["bucket"]:
            conn.execute("UPDATE raw_events SET bucket = ? WHERE id = ?", (new, row["id"]))
            changed += 1
    conn.commit()
    return changed
