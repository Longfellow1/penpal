"""Read and transform Memmy's distilled memories without modifying its database."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from penpal.foundation.desensitizer import desensitize, redact_secrets
from penpal.memory.writer import source_default_bucket
from penpal.mcp import audit


# Memmy writes this summary while distillation is still queued; it says nothing about the memory.
_PLACEHOLDERS = frozenset({"摘要排队中"})
_HEADLINE_CHARS = 200
_TRANSCRIPT_LABELS = ("Summary:", "User:", "Agent:")


def _headline(detail: str) -> str:
    """Pick a readable headline: a skill's front matter name and description, else the first spoken line that is not a bare tag."""
    lines = detail.splitlines()
    if lines and lines[0].strip() == "---" and "---" in (line.strip() for line in lines[1:]):
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        meta = {key.strip(): value.strip() for key, sep, value in (line.partition(":") for line in lines[1:end]) if sep}
        headline = ": ".join(value for value in (meta.get("name"), meta.get("description")) if value)
        if headline:
            return headline
        lines = lines[end + 1:]
    for line in lines:
        text = line.strip()
        if text.startswith("Summary:"):
            continue
        for label in _TRANSCRIPT_LABELS:
            if text.startswith(label):
                text = text[len(label):].strip()
                break
        if text and text != "---" and not (text.startswith("<") and text.endswith(">")):
            return text
    return ""


def import_memmy(path: Path, target: sqlite3.Connection | None, *, dry_run: bool = False) -> dict:
    try:
        with sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.2) as source:
            source.row_factory = sqlite3.Row
            rows = source.execute("SELECT id, memory_layer, memory_value, info_json, agent_id, created_at FROM memories WHERE status = 'activated' AND memory_layer IN ('L1', 'Skill')").fetchall()
    except sqlite3.Error as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise ValueError("Memmy database is locked; close the other writer and retry") from None
        raise ValueError("Cannot read Memmy database or its memories table") from None

    existing = set()
    if target is not None:
        try:
            existing = {row[0] for row in target.execute("SELECT external_id FROM memories WHERE source = 'memmy'")}
        except sqlite3.OperationalError as exc:
            if not dry_run or "no such table" not in str(exc):
                raise
    counts = Counter({"eligible": len(rows), "imported": 0, "existing": 0, "invalid": 0, "redacted": 0})
    reasons = Counter()
    pending = []
    buckets: dict[str, str] = {}
    for row in rows:
        external_id, detail, created = row["id"], row["memory_value"], row["created_at"]
        if not isinstance(external_id, str) or not external_id.strip():
            reasons["missing_id"] += 1
            continue
        if external_id in existing:
            counts["existing"] += 1
            continue
        if not isinstance(detail, str) or not detail.strip() or not isinstance(created, str) or not created.strip():
            reasons["missing_content_or_date"] += 1
            continue
        try:
            info = json.loads(row["info_json"] or "{}")
            if not isinstance(info, dict):
                info = {}
        except (ValueError, TypeError):
            info = {}
        candidates = (info.get("summary"), info.get("title"), _headline(detail))
        content = next((" ".join(value.split())[:_HEADLINE_CHARS] for value in candidates if isinstance(value, str) and value.strip() and value.strip() not in _PLACEHOLDERS), "")
        if not content:
            reasons["missing_summary"] += 1
            continue
        clean_content = desensitize(redact_secrets(content))
        clean_detail = desensitize(redact_secrets(detail))
        if (clean_content, clean_detail) != (content, detail):
            counts["redacted"] += 1
        tool = row["agent_id"] if isinstance(row["agent_id"], str) and row["agent_id"].strip() else "memmy"
        tool = {"codex": "codex_cli", "memmy-onboarding": "memmy", "memmy-agent": "memmy"}.get(tool, tool)
        kind = "fact" if row["memory_layer"] == "L1" else "procedure"
        digest = hashlib.sha256(json.dumps([kind, "global", None, " ".join(clean_content.casefold().split())], ensure_ascii=False).encode()).hexdigest()
        if tool not in buckets:
            buckets[tool] = source_default_bucket(tool) or "Work"
        bucket = buckets[tool]
        pending.append((kind, clean_content, clean_detail, tool, json.dumps([bucket]), digest, created, created, external_id))
        existing.add(external_id)
        counts["imported"] += 1
    counts["invalid"] = sum(reasons.values())
    if not dry_run and target is not None:
        with target:
            target.executemany("INSERT INTO memories(type,content,detail,scope,source_tool,evidence_ids,buckets,content_hash,valid_from,created_at,status,confidence,source,external_id) VALUES (?,?,?,'global',?,'[]',?,?,?,?,'active',1.0,'memmy',?)", pending)
        summary = f"eligible={counts['eligible']},existing={counts['existing']},invalid={counts['invalid']},redacted={counts['redacted']}"
        audit.record(client="cli", tool="import_memmy", allowed=True, reason_code="ok", scope=summary, result_count=counts["imported"])
    return {**counts, "invalid_reasons": dict(reasons)}
