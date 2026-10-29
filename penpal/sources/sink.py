from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from penpal.capture.capture_control import source_writes_allowed
from penpal.foundation.desensitizer import redact_secrets
from penpal.sources.entity_extractor import extract_named_entities
from penpal.sources.types import SemanticEvent
from penpal.store.context_label import context_label
from penpal.store.models import RawEvent
from penpal.store.repository import get_conn
from penpal.store.repository import insert_raw_event
from penpal.store.sync_repo import add_evidence, ensure_source


_SOURCE_WEIGHTS: dict[str, float] = {
    "git_log": 0.9,
    "claude_code": 0.8,
    "codex_cli": 0.8,
    "ai_tools": 0.8,
    "markdown_vault": 0.7,
    "chrome_history": 0.6,
    "safari_history": 0.6,
}
_COMMIT_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.IGNORECASE)
_ENTITY_BACKFILL_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="entity-backfill")
_PENDING_BACKFILL: set[Future[None]] = set()
_PENDING_LOCK = threading.Lock()


def redact_event(event: SemanticEvent) -> SemanticEvent:
    """Scrub credentials from event text before it can reach the DB / FTS index."""
    intent = redact_secrets(event.intent or "")
    if intent == (event.intent or ""):
        return event
    return replace(event, intent=intent)


def persist_semantic_event(event: SemanticEvent, *, async_named_entities: bool = True) -> int:
    if not source_writes_allowed(event.source):  # paused or switched off (#28)
        return 0
    event = redact_event(event)
    source_id = None
    if event.external_id or event.source_locator:
        source_id = ensure_source(event.source, event.source_locator or event.source)
    raw_event = semantic_event_to_raw_event(event, include_named_entities=False, source_id=source_id)
    row_id = insert_raw_event(raw_event)
    if row_id > 0 and event.evidence_locator:
        add_evidence(
            row_id,
            event.evidence_locator,
            event.evidence_line,
            hashlib.sha256((event.intent or "").encode("utf-8")).hexdigest(),
        )
    if row_id > 0:
        if async_named_entities:
            _submit_named_entity_backfill(row_id, event)
        else:
            _backfill_named_entities(row_id, event)
    return row_id


def flush_named_entity_backfill(timeout_sec: float = 5.0) -> None:
    with _PENDING_LOCK:
        futures = list(_PENDING_BACKFILL)
    for future in futures:
        future.result(timeout=timeout_sec)


def semantic_event_to_raw_event(
    event: SemanticEvent,
    *,
    include_named_entities: bool = True,
    source_id: int | None = None,
) -> RawEvent:
    event = redact_event(event)
    metadata = _metadata_payload(event, include_named_entities=include_named_entities)
    payload = json.dumps(metadata, ensure_ascii=False)
    session_id = metadata.get("entities", {}).get("session_id")
    return RawEvent(
        source=event.source,
        event_type="semantic_event",
        ts_start=event.time.isoformat(),
        app_name=_optional_string(event.metadata.get("app_name") if event.metadata else None),
        process_name=_optional_string(event.metadata.get("app_bundle_id") if event.metadata else None),
        window_title=context_label(event.source, metadata),  # place label (repo / project / site / note); never a real title
        content_text=event.intent or None,
        content_hash=_content_hash(event),
        metadata_json=payload,
        session_id=session_id if isinstance(session_id, str) else None,
        speaker=_speaker_for(event.actor),
        semantic_weight=_SOURCE_WEIGHTS.get(event.source, 0.5),
        source_id=source_id,
        external_id=event.external_id,
        bucket=None,  # assigned in insert_raw_event: source default + app rules (store.buckets)
        kind=event.kind or "semantic_event",
    )


def _metadata_payload(event: SemanticEvent, *, include_named_entities: bool) -> dict[str, Any]:
    source_metadata = dict(event.metadata or {})
    if "url" not in source_metadata:
        full_url = source_metadata.get("full_url")
        if isinstance(full_url, str) and full_url.strip():
            source_metadata["url"] = full_url

    source_metadata["raw_ref"] = event.raw_ref
    source_metadata["artifact"] = event.artifact
    source_metadata["actor"] = event.actor
    source_metadata["privacy_tier"] = event.privacy_tier
    source_metadata["entities"] = _entities_payload(
        event,
        source_metadata,
        include_named_entities=include_named_entities,
    )
    return source_metadata


def _entities_payload(
    event: SemanticEvent,
    metadata: dict[str, Any],
    *,
    include_named_entities: bool,
) -> dict[str, Any]:
    entities: dict[str, Any] = {}

    commit_hash = _extract_commit_hash(event, metadata)
    if commit_hash:
        entities["commit_hash"] = commit_hash

    file_paths = _extract_file_paths(event, metadata)
    if file_paths:
        entities["file_paths"] = file_paths

    urls = _extract_urls(event, metadata)
    if urls:
        entities["urls"] = urls

    session_id = _optional_string(metadata.get("session_id"))
    if session_id:
        entities["session_id"] = session_id

    app_bundle_id = _optional_string(metadata.get("app_bundle_id") or metadata.get("bundle_id"))
    if app_bundle_id:
        entities["app_bundle_id"] = app_bundle_id

    if include_named_entities:
        named_entities = extract_named_entities(event)
        if named_entities:
            entities["named_entities"] = _dedupe_keep_order(named_entities)

    return entities


def _submit_named_entity_backfill(row_id: int, event: SemanticEvent) -> None:
    future = _ENTITY_BACKFILL_EXECUTOR.submit(_backfill_named_entities, row_id, event)
    with _PENDING_LOCK:
        _PENDING_BACKFILL.add(future)

    def _remove(done: Future[None]) -> None:
        with _PENDING_LOCK:
            _PENDING_BACKFILL.discard(done)

    future.add_done_callback(_remove)


def _backfill_named_entities(row_id: int, event: SemanticEvent) -> None:
    named_entities = _dedupe_keep_order(extract_named_entities(event))
    if not named_entities:
        return

    conn = get_conn()
    row = conn.execute("SELECT metadata_json FROM raw_events WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return

    raw_metadata = row["metadata_json"]
    payload: dict[str, Any] = {}
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = dict(parsed)

    entities_raw = payload.get("entities")
    if isinstance(entities_raw, dict):
        entities = dict(entities_raw)
    else:
        entities = {}
    current_named_entities = entities.get("named_entities")
    combined = list(named_entities)
    if isinstance(current_named_entities, list):
        for value in current_named_entities:
            if isinstance(value, str):
                combined.append(value)
    entities["named_entities"] = _dedupe_keep_order(combined)
    payload["entities"] = entities
    conn.execute(
        "UPDATE raw_events SET metadata_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), row_id),
    )
    conn.commit()


def _extract_commit_hash(event: SemanticEvent, metadata: dict[str, Any]) -> str | None:
    full_hash = _optional_string(metadata.get("full_hash") or metadata.get("commit_hash"))
    if full_hash:
        return full_hash
    artifact = str(event.artifact or "")
    match = _COMMIT_RE.search(artifact)
    if match:
        return match.group(0)
    return None


def _extract_file_paths(event: SemanticEvent, metadata: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("repo_path", "project_dir", "file_path", "path"):
        value = _optional_string(metadata.get(key))
        if value:
            paths.append(value)
    if event.source == "markdown_vault":
        artifact = _optional_string(event.artifact)
        if artifact:
            paths.append(artifact)
    return _dedupe_keep_order(paths)


def _extract_urls(event: SemanticEvent, metadata: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("full_url", "url"):
        value = _optional_string(metadata.get(key))
        if value:
            candidates.append(value)
    artifact = _optional_string(event.artifact)
    if artifact and (artifact.startswith("http://") or artifact.startswith("https://")):
        candidates.append(artifact)

    normalized = [_normalize_url_without_query(value) for value in candidates]
    cleaned = [value for value in normalized if value]
    return _dedupe_keep_order(cleaned)


def _normalize_url_without_query(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    if not parsed.scheme or not parsed.netloc:
        return url.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _content_hash(event: SemanticEvent) -> str:
    payload = json.dumps(
        {
            "time": event.time.isoformat(),
            "source": event.source,
            "actor": event.actor,
            "intent": event.intent,
            "artifact": event.artifact,
            "raw_ref": event.raw_ref,
            "metadata": event.metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _speaker_for(actor: str) -> str:
    normalized = str(actor or "").strip().lower()
    return "user" if normalized in {"user", "human"} else "system"


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized if normalized else None
    return None


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped
