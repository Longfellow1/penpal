from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from penpal.sources.types import SemanticEvent

import logging
logger = logging.getLogger(__name__)


_COMMIT_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
# The lookbehind only lets a match start at a token boundary; without it a long run of word
# characters is rescanned from every offset (quadratic: ~4s on a 32k-char message).
_FILE_RE = re.compile(r"(?<![\w\-./])[\w\-./]+\.(py|ts|tsx|js|jsx|md|json|yaml|toml|sh|go|rs|java|cpp|h|c)\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://([\w\-.]+)(/[\w\-./]*)?(?:\?[^\s]*)?", re.IGNORECASE)
_ISSUE_RE = re.compile(r"#\d+|(?:GH|JIRA|LIN)[-_]\d+", re.IGNORECASE)
_INTENT_ENTITY_RE = re.compile(r"(?:修复|实现)\s+([^\n,，。;；]+)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{3,}")
_KEYWORD_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}")
_KEYWORD_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "have",
    "done",
    "doing",
    "用户",
    "事件",
    "活动",
    "相关",
    "进行",
    "处理",
    "一个",
    "一些",
    "以及",
}


@dataclass(frozen=True)
class Entity:
    kind: str
    value: str
    raw: str
    confidence: float


def extract(event: SemanticEvent) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, raw: str, confidence: float) -> None:
        normalized_value = value.strip()
        if not normalized_value:
            return
        key = (kind, normalized_value)
        if key in seen:
            return
        seen.add(key)
        entities.append(Entity(kind=kind, value=normalized_value, raw=raw, confidence=max(0.0, min(1.0, confidence))))

    artifact = str(event.artifact or "")
    intent = str(event.intent or "")
    raw_ref = str(event.raw_ref or "")
    metadata = event.metadata or {}
    text = "\n".join([intent, artifact, raw_ref, " ".join(f"{k}:{v}" for k, v in metadata.items())])

    for marker in re.findall(r"commit:((?=[0-9a-f]*[a-f])[0-9a-f]{7,40})", artifact, flags=re.IGNORECASE):
        add("commit", marker[:7], f"commit:{marker}", 1.0)
    for marker in _COMMIT_RE.findall(text):
        add("commit", marker[:7], marker, 0.9)

    for raw in _FILE_RE.findall(artifact + "\n" + intent + "\n" + raw_ref):
        # regex returns suffix group when using findall with capture; re-find full matches
        pass
    for match in _FILE_RE.finditer(artifact + "\n" + intent + "\n" + raw_ref):
        raw = match.group(0)
        add("file", raw, raw, 0.9)

    for match in _URL_RE.finditer(text):
        host = match.group(1) or ""
        path = match.group(2) or ""
        value = f"{host}{path}"
        add("url", value, match.group(0), 0.9)

    for marker in _ISSUE_RE.findall(text):
        add("issue_pr", marker.upper() if marker.upper().startswith(("JIRA", "GH", "LIN")) else marker, marker, 0.8)

    for key in ("repo_path", "project_dir"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            add("project", Path(raw).name, raw, 0.7)

    for candidate in re.findall(r"(/[\w\-./]+)", raw_ref):
        try:
            add("project", Path(candidate).name, candidate, 0.6)
        except Exception as exc:
            logger.debug("extract failed: %s", type(exc).__name__)
            continue

    session_id = metadata.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        add("session", session_id.strip(), session_id, 1.0)

    for match in _INTENT_ENTITY_RE.finditer(intent):
        payload = match.group(1).strip().lower()
        for token in _TOKEN_RE.findall(payload):
            add("project", token, token, 0.5)

    return entities


def extract_keywords(event: SemanticEvent) -> set[str]:
    text = " ".join(
        [
            str(event.intent or ""),
            str(event.artifact or ""),
            str(event.raw_ref or ""),
        ]
    )
    keywords = extract_text_keywords(text)
    for entity in extract(event):
        value = entity.value.strip().lower()
        if len(value) >= 2:
            keywords.add(value)
        keywords |= extract_text_keywords(value)
    return keywords


def extract_text_keywords(text: str) -> set[str]:
    keywords: set[str] = set()
    for raw in _KEYWORD_TOKEN_RE.findall(text.lower()):
        if len(raw) < 2 or raw in _KEYWORD_STOPWORDS:
            continue
        keywords.add(raw)
    return keywords


def extract_named_entities(event: SemanticEvent) -> list[str]:
    """Extract de-duplicated named entities for raw_events metadata storage."""
    named_entities: list[str] = []
    seen: set[str] = set()
    for entity in extract(event):
        value = str(entity.value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        named_entities.append(value)
    return named_entities
