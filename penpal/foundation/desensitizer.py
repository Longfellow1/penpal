from __future__ import annotations

import re
from typing import Any

from penpal.foundation.detectors import PATTERNS, detect

# Email masking keeps the domain (useful for "which company / service") and drops the local part:
# alice.w@gmail.com -> ***@gmail.com. Already-masked "***@x.com" does not match the pattern (the
# local part class has no "*"), so masking is idempotent across the write-time and query-time passes.
# Matches without a TLD ("user@host" shell prompts) never match the email pattern, so they are untouched.
EMAIL_MASK_PREFIX = "***@"
# Service accounts, not people: "git@github.com:org/repo.git" SSH remotes must stay usable.
_SERVICE_LOCAL_PARTS = frozenset({"git"})
_EMAIL = re.compile(r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")


def _mask_email_match(m: re.Match) -> str:
    if m.group(1).lower() in _SERVICE_LOCAL_PARTS:
        return m.group(0)
    return EMAIL_MASK_PREFIX + m.group(2)


def mask_emails(text: str) -> str:
    """Mask the local part of every email, keeping the domain: alice@example.com -> ***@example.com."""
    if not text or "@" not in text:
        return text
    return _EMAIL.sub(_mask_email_match, text)


_SHELL_LINE_PATTERNS = (
    re.compile(r"(?m)^\s*sudo\s+\S.*$"),
    re.compile(r"(?m)^\s*export\s+[A-Z_][A-Z0-9_]*=.*$"),
    re.compile(r"(?m)^\s*(brew|npm|pip|pipx|uv|cargo|gem|apt|yum|dnf|port)\s+(install|tap|update|upgrade|remove|uninstall)\b.*$"),
    re.compile(r"(?m)^\s*cat\s*>>?\s*~?/[\w./-]+\s*<<['\"]?\w+['\"]?$"),
    re.compile(r"(?m)^\s*\S+\s*>>?\s*~?/[\w./-]+$"),
    re.compile(r"(?m)^Last login:\s+\w+\s+\w+\s+\d+.*$"),
    re.compile(r"(?m)^==>\s+(Installing|Pouring|Caveats|Downloading)\b.*$"),
    re.compile(r"(?m)^\s*(which|type)\s+\S+$"),
)


def _redact_shell_lines(text: str) -> str:
    redacted = text
    for pattern in _SHELL_LINE_PATTERNS:
        redacted = pattern.sub("[REDACTED_SHELL]", redacted)
    return redacted


def desensitize(text: str, redact_emails: bool = True, redact_phones: bool = True, redact_tokens: bool = True) -> str:
    """Replace sensitive patterns with [REDACTED] / labelled placeholders; emails become ***@domain."""
    if not text:
        return text
    text = _redact_shell_lines(text)
    if redact_emails:
        text = mask_emails(text)
    enabled = {
        "email": False,  # handled above (domain-preserving mask)
        "phone_cn": redact_phones,
        "id_card": True,
        "bank_card": True,
        "api_key": redact_tokens,
        "bearer_token": redact_tokens,
        "jwt": redact_tokens,
        "aws_key": redact_tokens,
        "url_token": redact_tokens,
        "private_key": redact_tokens,
        "uuid_v4": redact_tokens,
        "openai_key": redact_tokens,
        "github_pat": redact_tokens,
        "slack_token": redact_tokens,
    }
    detections = detect(text, enabled)
    if not detections:
        return text
    result = []
    prev = 0
    for d in detections:
        result.append(text[prev:d.start])
        # labelled placeholders for card numbers; everything else stays [REDACTED]
        result.append(d.replacement if d.replacement.startswith("[") else "[REDACTED]")
        prev = d.end
    result.append(text[prev:])
    return "".join(result)


def desensitize_json_value(
    value: Any,
    *,
    redact_emails: bool = True,
    redact_phones: bool = True,
    redact_tokens: bool = True,
) -> Any:
    if isinstance(value, str):
        return desensitize(
            value,
            redact_emails=redact_emails,
            redact_phones=redact_phones,
            redact_tokens=redact_tokens,
        )
    if isinstance(value, dict):
        return {
            key: desensitize_json_value(
                item,
                redact_emails=redact_emails,
                redact_phones=redact_phones,
                redact_tokens=redact_tokens,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            desensitize_json_value(
                item,
                redact_emails=redact_emails,
                redact_phones=redact_phones,
                redact_tokens=redact_tokens,
            )
            for item in value
        ]
    return value


def truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length] + "...[truncated]"


# Secret-class detectors only (no email / phone / uuid / card numbers): used
# for ingestion where content must stay searchable but credentials must never
# reach the DB or the FTS index.
_SECRET_PATTERNS = frozenset({
    "api_key", "bearer_token", "jwt", "aws_key", "url_token",
    "private_key", "openai_key", "github_pat", "slack_token",
})
_ENABLED_SECRETS = {name: name in _SECRET_PATTERNS for name in PATTERNS}
# The detector only matches the PEM header line; swallow the whole block.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)


# Penpal's own MCP tokens ("kp_" + token_urlsafe(32)). They end up in host transcripts
# (e.g. a `claude mcp add -e PENPAL_TOKEN=...` command) that Penpal itself ingests (B6).
_PENPAL_TOKEN = re.compile(r"\bkp_[A-Za-z0-9_-]{32,}")


def redact_secrets(text: str) -> str:
    if not text:
        return text
    text = _PEM_BLOCK.sub("[REDACTED]", text)
    text = _PENPAL_TOKEN.sub("[REDACTED]", text)
    detections = detect(text, _ENABLED_SECRETS)
    if not detections:
        return text
    parts: list[str] = []
    prev = 0
    for d in detections:
        parts.append(text[prev:d.start])
        parts.append("[REDACTED]")
        prev = d.end
    parts.append(text[prev:])
    return "".join(parts)
