from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import replace

from penpal.capture.merge import (
    NEAR_DUPLICATE_WINDOW_SEC,
    jaccard_similarity,
    jaccard_threshold,
    parse_ts,
    similarity_ratio,
    source_priority,
)
from penpal.store.models import RawEvent

import logging
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FusionResult:
    persist: bool
    event: RawEvent | None
    reason: str | None = None


@dataclass
class _CanonicalRecord:
    event: RawEvent


class CaptureFusionEngine:
    def __init__(self, similarity_threshold: float = 0.92, window_sec: float = 15.0):
        self._threshold = similarity_threshold
        self._window_sec = window_sec
        self._canonicals: list[_CanonicalRecord] = []

    _MAX_CANONICALS = 500

    def _prune(self, now_ts) -> None:
        keep_sec = max(self._window_sec, NEAR_DUPLICATE_WINDOW_SEC)
        self._canonicals = [
            r for r in self._canonicals
            if abs((now_ts - parse_ts(r.event.ts_start)).total_seconds()) <= keep_sec
        ][-(self._MAX_CANONICALS - 1):]

    def fuse(self, event: RawEvent) -> FusionResult:
        if not event.content_text or source_priority(event.source) == 0:
            return FusionResult(persist=True, event=event)

        self._prune(parse_ts(event.ts_start))
        near = self._find_near_duplicate(event)
        if near is not None:
            reason = (
                "lower_priority_duplicate"
                if source_priority(event.source) <= source_priority(near.event.source)
                else "near_duplicate"
            )
            return FusionResult(persist=False, event=None, reason=reason)
        matched = self._find_match(event)
        if matched is None:
            self._canonicals.append(_CanonicalRecord(event=event))
            return FusionResult(persist=True, event=event)

        current_priority = source_priority(event.source)
        matched_priority = source_priority(matched.event.source)
        if current_priority <= matched_priority:
            return FusionResult(
                persist=False,
                event=None,
                reason="lower_priority_duplicate",
            )

        merged_event = self._merge_metadata(matched.event, event)
        matched.event = merged_event
        return FusionResult(
            persist=True,
            event=merged_event,
            reason="replaced_lower_priority_duplicate",
        )

    def _find_match(self, event: RawEvent) -> _CanonicalRecord | None:
        event_ts = parse_ts(event.ts_start)
        for record in reversed(self._canonicals):
            delta = abs((event_ts - parse_ts(record.event.ts_start)).total_seconds())
            if delta > self._window_sec:
                continue
            if not self._same_context(event, record.event):
                continue
            if similarity_ratio(event.content_text, record.event.content_text) >= self._threshold:
                return record
        return None

    @staticmethod
    def _same_context(a: RawEvent, b: RawEvent) -> bool:
        if a.app_name != b.app_name:
            return False
        # Clipboard events carry no window title: if either side has none, compare the app only.
        if a.window_title and b.window_title:
            return a.window_title == b.window_title
        return True

    def _find_near_duplicate(self, event: RawEvent) -> _CanonicalRecord | None:
        """Same app, near-identical token set within 300s (legacy collapse_self_repeat thresholds)."""
        event_ts = parse_ts(event.ts_start)
        for record in reversed(self._canonicals):
            gap = abs((event_ts - parse_ts(record.event.ts_start)).total_seconds())
            if gap > NEAR_DUPLICATE_WINDOW_SEC:
                continue
            # keyboard chunks are deduplicated only against keyboard chunks
            if (event.source == "keyboard_chunk") != (record.event.source == "keyboard_chunk"):
                continue
            if not self._same_context(event, record.event):
                continue
            if jaccard_similarity(event.content_text, record.event.content_text) >= jaccard_threshold(gap):
                return record
        return None

    def _merge_metadata(self, previous: RawEvent, current: RawEvent) -> RawEvent:
        payload: dict[str, object] = {}
        for candidate in (previous.metadata_json, current.metadata_json):
            if not candidate:
                continue
            try:
                payload.update(json.loads(candidate))
            except Exception as exc:
                logger.debug("merge metadata failed: %s", type(exc).__name__)
                continue

        source_chain: list[str] = []
        for candidate in (payload.get("sources"), [previous.source], [current.source]):
            if isinstance(candidate, list):
                for source in candidate:
                    if isinstance(source, str) and source not in source_chain:
                        source_chain.append(source)

        payload["sources"] = source_chain
        payload["merge_reason"] = "replaced_lower_priority_duplicate"
        return replace(current, metadata_json=json.dumps(payload))
