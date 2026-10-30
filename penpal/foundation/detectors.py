import re
from dataclasses import dataclass


@dataclass
class Detection:
    pattern_name: str
    start: int
    end: int
    replacement: str = "***"


def valid_id_card(number: str) -> bool:
    """GB 11643-1999 check digit (weights 7 9 10 5 8 4 2 1 6 3 7 9 10 5 8 4 2)."""
    if len(number) != 18 or not number[:17].isdigit():
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    total = sum(int(c) * w for c, w in zip(number[:17], weights))
    return "10X98765432"[total % 11] == number[17].upper()


def luhn_valid(number: str) -> bool:
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


# A match is redacted only if its checksum holds: keeps timestamps / order ids out of the net.
VALIDATORS = {"id_card": valid_id_card, "bank_card": luhn_valid}
REPLACEMENTS = {"id_card": "[ID_CARD]", "bank_card": "[BANK_CARD]"}

# Compiled patterns
PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "phone_cn": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # \b never matches next to CJK (CJK counts as \w), so use digit lookarounds like phone_cn.
    "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "api_key": re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*\S+"),
    "bearer_token": re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "url_token": re.compile(r"[?&](token|key|secret|password|access_token)=[^&\s]+"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Enhanced patterns for high-severity secrets
    "uuid_v4": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    "openai_key": re.compile(r"(?i)sk-[A-Za-z0-9_-]{15,}"),
    "github_pat": re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{35,})\b"),
    "slack_token": re.compile(r"\bxox[abpr]-[A-Za-z0-9_-]{10,}\b"),
}


def detect(text: str, enabled: dict[str, bool] | None = None) -> list[Detection]:
    """Return list of detections in text."""
    results = []
    for name, pattern in PATTERNS.items():
        if enabled and not enabled.get(name, True):
            continue
        check = VALIDATORS.get(name)
        for m in pattern.finditer(text):
            if check is not None and not check(m.group()):
                continue
            results.append(
                Detection(pattern_name=name, start=m.start(), end=m.end(), replacement=REPLACEMENTS.get(name, "***"))
            )
    # Sort by start position, remove overlaps
    results.sort(key=lambda d: d.start)
    merged = []
    for d in results:
        if merged and d.start < merged[-1].end:
            if d.end > merged[-1].end:
                merged[-1] = Detection(merged[-1].pattern_name, merged[-1].start, d.end, merged[-1].replacement)
        else:
            merged.append(d)
    return merged


def has_sensitive_content(text: str) -> bool:
    return bool(detect(text))
