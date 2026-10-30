"""Capture hygiene gate (D27): one shared write-time filter for every capture stream.

Ported from legacy-v0 (penpal/pipeline/fragments.py, event_intake.py), adapted from dict rows to
plain fields so the live path (CaptureManager._process_event) and the v24 migration share one rule set.
Pure Python, no pyobjc: the store layer may import it.

capture_gate(...) returns None (keep), or a GateVerdict:
  action "drop"           the event is not stored
  action "metadata_only"  stored with content / title nulled (app + time survive)
Content is never logged; only the reason string is counted by callers.

Rules (legacy origin):
  system_overlay    window stream, loginwindow & co            fragments L3 / evidence overlay list
  login_context     title/app has sign-in keywords             fragments._is_login_context (L3)
  terminal_app      keyboard block typed in a terminal         audit F3 option A
  privacy_keyword   password / 密码 / 验证码 / otp / 2fa        fragments._contains_privacy_keyword
  short_input       keyboard/ax < 5 chars, not whitelisted     fragments._passes_l1 (L1)
  low_entropy       keyboard/ax repeated-char junk             fragments._passes_l2 (L2), threshold changed
  ime_blob          keyboard block with no word boundary       text_filters.looks_like_ime_composition (L5)
  input_fragment    keyboard long ASCII run without spaces     event_intake.is_input_fragment
  app_chrome        ax_text equal to its own title / app       event_intake.is_app_chrome
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

from penpal.foundation.capture_rules import (
    DEFAULT_TITLE_DENY_PATTERNS,
    LOGIN_KEYWORDS as _LOGIN_KEYWORDS,
    is_placeholder_text,
    is_system_overlay,
)
from penpal.utils.text_filters import looks_like_ime_composition, strip_invisible

DROP = "drop"
METADATA_ONLY = "metadata_only"


@dataclass(frozen=True)
class GateVerdict:
    action: str
    reason: str


# ASCII keywords need word boundaries ("footprint" contains "otp"); CJK keywords are substrings.
# legacy also listed "token": dropped here, it would erase every dev conversation about LLM tokens
# and the desensitizer already redacts real tokens.
_PRIVACY_RE = re.compile(
    r"\b(?:password|passwd|verification code|otp|2fa)\b|密码|验证码",
    re.IGNORECASE,
)

_LOGIN_CONTENT_OVERRIDE_LEN = 20

# Private / incognito windows: stored as metadata-only (app + time) only.
_PRIVATE_MARKERS = ("incognito", "无痕", "inprivate", "private browsing", "隐私浏览")
_INTERNAL_SCHEMES = ("chrome://", "edge://", "about:", "chrome-extension://", "brave://", "vivaldi://", "arc://")

_title_re_cache: dict[tuple, list] = {}

_L1_MIN_LEN = 5
_L1_WHITELIST = frozenset({"y", "n", "ok", "yes", "no", "嗯", "好", "对", "是", "不"})
# Legacy L2 used entropy < 3.0, which drops "hello world" (2.85) and 7-distinct-char Chinese
# sentences. Only near-constant junk is meant to go: ("aaaaaaaaaaaa", "ababababab").
_L2_MIN_LEN = 8
_L2_MAX_ENTROPY = 2.0

_HAS_CJK_RE = re.compile(r"[一-鿿]")
_PATH_OR_URL_RE = re.compile(r"(?:https?://|www\.|[A-Za-z]:[\\/]|/[^/\s]+/|\\\\[^\\\s]+[\\/])")

TERMINAL_APP_NAMES = frozenset({"terminal", "iterm2", "iterm", "warp", "alacritty", "kitty", "ghostty"})
TERMINAL_BUNDLE_IDS = frozenset({
    "com.apple.terminal", "com.googlecode.iterm2", "dev.warp.warp-stable",
    "org.alacritty", "net.kovidgoyal.kitty", "com.mitchellh.ghostty",
})

# Input placeholders and UI prompts rendered by chat / search boxes: AX reads them as the "text" of
# the field, they are never something the user wrote. Matched against the whole stripped text,
# case-insensitively, ignoring a trailing ellipsis / full stop. Extend by adding to this tuple.
_CONTENT_STREAMS = frozenset({"keyboard_chunk", "ax_text", "clipboard", "browser_url"})


def is_terminal_app(app_name: str | None, process_name: str | None = None) -> bool:
    return (
        str(app_name or "").strip().casefold() in TERMINAL_APP_NAMES
        or str(process_name or "").strip().casefold() in TERMINAL_BUNDLE_IDS
    )


def contains_privacy_keyword(text: str | None) -> bool:
    return bool(text) and _PRIVACY_RE.search(text) is not None


def is_login_context(window_title: str | None, app_name: str | None, content_text: str | None = None) -> bool:
    title = (window_title or "").lower()
    if any(kw in title for kw in _LOGIN_KEYWORDS):
        return True
    app = (app_name or "").lower()
    if any(kw in app for kw in _LOGIN_KEYWORDS):
        # An app-name hit is trusted only when there is little content to look at.
        return len((content_text or "").strip()) < _LOGIN_CONTENT_OVERRIDE_LEN
    return False


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    total = len(text)
    return -sum((c / total) * math.log2(c / total) for c in Counter(text).values())


def passes_l1(text: str) -> bool:
    if len(text) >= _L1_MIN_LEN:
        return True
    if text.strip().lower() in _L1_WHITELIST:
        return True
    # Two or more CJK characters already carry a word ("好的", "可以"); legacy dropped them.
    return len(_HAS_CJK_RE.findall(text)) >= 2


def passes_l2(text: str) -> bool:
    return len(text) < _L2_MIN_LEN or _entropy(text) > _L2_MAX_ENTROPY


def is_code_snippet(content: str) -> bool:
    text = str(content or "")
    features = 0
    lowered = text.lower()
    if any(token in lowered for token in ("def ", "function ", "class ", "import ", "=>", "const ", "let ")):
        features += 1
    lines = text.splitlines()
    if any(line.startswith(("    ", "\t")) for line in lines) and any(
        line.rstrip().endswith((":", "{", ";")) for line in lines
    ):
        features += 1
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(", text):
        features += 1
    identifiers = re.findall(r"\b(?:[A-Za-z]+_[A-Za-z0-9_]+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b", text)
    if len(identifiers) >= 3:
        features += 1
    if len(text.strip()) > 80:
        return features >= 2
    return features >= 3


def is_input_fragment(content: str) -> bool:
    text = str(content or "").strip()
    if len(text) <= 12 or " " in text or _HAS_CJK_RE.search(text) or _PATH_OR_URL_RE.search(text):
        return False
    if re.search(r"[.?!。！？]", text) or is_code_snippet(text):
        return False
    if not all(ord(ch) < 128 for ch in text):
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    if letters / max(len(text), 1) < 0.7:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9,_:;'\-\\/+]+", text))


def is_app_chrome(content: str, window_title: str | None, app_name: str | None) -> bool:
    content = (content or "").strip()
    if not content:
        return False
    if is_placeholder_text(content):
        return True
    title = (window_title or "").strip()
    app = (app_name or "").strip()
    if title and (content == title or (len(content) >= 4 and (content in title or title.endswith(content)))):
        return True
    return bool(app and content == app)


def is_blacklisted_app(
    app_name: str | None,
    process_name: str | None,
    *,
    bundle_ids,
    patterns,
    app_names,
) -> bool:
    """Exact bundle id, fnmatch pattern, or case-insensitive app-name substring."""
    from fnmatch import fnmatch

    candidate = process_name or app_name
    if candidate:
        if candidate in bundle_ids or any(fnmatch(candidate, p) for p in patterns):
            return True
    names = [str(n).casefold() for n in app_names if str(n).strip()]
    for value in (app_name, process_name):
        folded = str(value or "").casefold()
        if folded and any(n in folded for n in names):
            return True
    return False


def _title_denied(title: str | None, patterns) -> bool:
    if not title:
        return False
    key = tuple(patterns)
    compiled = _title_re_cache.get(key)
    if compiled is None:
        compiled = []
        for p in key:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error:
                continue  # a bad user pattern must not break capture
        _title_re_cache[key] = compiled
    return any(c.search(title) for c in compiled)


def is_private_window(window_title: str | None, url: str | None = None) -> bool:
    blob = f"{window_title or ''} {url or ''}".casefold()
    return any(m in blob for m in _PRIVATE_MARKERS)


def is_internal_browser_page(source: str, window_title: str | None, url: str | None, app_name: str | None) -> bool:
    """chrome:// edge:// about: extension pages, and file:// pages with no real title."""
    if source not in ("window", "browser_url"):
        return False
    title = (window_title or "").strip()
    link = (url or "").strip().lower()
    if link.startswith(_INTERNAL_SCHEMES) or title.lower().startswith(_INTERNAL_SCHEMES):
        return True
    if link.startswith("file://"):
        bare = title in ("", link) or title.casefold() == str(app_name or "").casefold()
        return bare or title.lower().startswith(_INTERNAL_SCHEMES)
    return False


PAGE_SCOPED_STREAMS = frozenset({"keyboard_chunk", "ax_text", "clipboard", "window"})


def page_in_denied_domain(page_url: str | None, deny_hosts) -> bool:
    """True when the page's host is on the user's excluded-domain list.

    An unknown page (None) is not blocked: the browser URL watcher is off by default, and until it reports
    a URL for the frontmost browser there is nothing to check. A known page is the last URL that browser
    showed. It has no age limit, because the watcher emits only on a change and a long read emits nothing.
    """
    if not page_url or not deny_hosts:
        return False
    try:
        host = urlsplit(page_url).hostname
    except ValueError:
        return False
    return bool(host) and host_matches_denylist(host, list(deny_hosts))


def capture_gate(
    *,
    source: str,
    app_name: str | None,
    process_name: str | None,
    window_title: str | None,
    content_text: str | None,
    url: str | None = None,
    title_deny_patterns=None,
    page_url: str | None = None,
    url_deny_hosts=None,
) -> GateVerdict | None:
    """Decide the fate of one capture event. Never logs or returns content.

    page_url: the URL the frontmost browser showed when this event was captured, resolved by the capture
    manager (None when no browser URL is known for the frontmost app). With url_deny_hosts it applies the
    user's excluded-domain list to typed, read and copied text and to window titles, the same way private
    windows are handled.
    """
    # Zero-width characters must not hide a keyword from the rules below.
    window_title = strip_invisible(window_title)
    content_text = strip_invisible(content_text)
    if is_internal_browser_page(source, window_title, url, app_name):
        return GateVerdict(DROP, "browser_internal_page")
    if source in _CONTENT_STREAMS or source == "window":
        if is_private_window(window_title, url):
            return GateVerdict(METADATA_ONLY, "private_window")
        if source in PAGE_SCOPED_STREAMS and page_in_denied_domain(page_url, url_deny_hosts):
            return GateVerdict(METADATA_ONLY, "denied_domain")
        patterns = DEFAULT_TITLE_DENY_PATTERNS if title_deny_patterns is None else title_deny_patterns
        if _title_denied(window_title, patterns):
            return GateVerdict(METADATA_ONLY, "title_deny")
    if source == "window":
        if is_system_overlay(app_name, process_name):
            return GateVerdict(DROP, "system_overlay")
        if is_login_context(window_title, app_name):
            return GateVerdict(METADATA_ONLY, "login_context")
        return None

    if source not in _CONTENT_STREAMS:
        return None

    text = content_text or ""
    if source != "browser_url" and not text.strip():
        return GateVerdict(DROP, "empty")
    if contains_privacy_keyword(text):
        return GateVerdict(DROP, "privacy_keyword")
    if is_login_context(window_title, app_name, text):
        return GateVerdict(METADATA_ONLY, "login_context")

    if source == "keyboard_chunk":
        if is_terminal_app(app_name, process_name):
            return GateVerdict(METADATA_ONLY, "terminal_app")
        text = text.strip()
        if not passes_l1(text):
            return GateVerdict(DROP, "short_input")
        if not passes_l2(text):
            return GateVerdict(DROP, "low_entropy")
        if looks_like_ime_composition(text):
            return GateVerdict(DROP, "ime_blob")
        if is_input_fragment(text):
            return GateVerdict(DROP, "input_fragment")
    elif source == "ax_text":
        text = text.strip()
        if not passes_l1(text):
            return GateVerdict(DROP, "short_input")
        if not passes_l2(text):
            return GateVerdict(DROP, "low_entropy")
        if is_app_chrome(text, window_title, app_name):
            return GateVerdict(DROP, "app_chrome")
    # clipboard: L1 stays lenient (a short copied word can matter); only privacy/login apply.
    return None


def host_matches_denylist(host: str, deny_hosts: list[str]) -> bool:
    """Exact host or any subdomain of a user deny-list entry (case-insensitive)."""
    normalized_host = host.lower().strip()
    if not normalized_host:
        return False
    for deny_host in deny_hosts:
        candidate = str(deny_host or "").strip().lower()
        if not candidate:
            continue
        if normalized_host == candidate or normalized_host.endswith(f".{candidate}"):
            return True
    return False


def strip_url_query(url: str | None) -> str | None:
    """scheme://host/path without query or fragment (login pages carry tokens there)."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
