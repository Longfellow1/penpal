from __future__ import annotations

import json
import os
from pathlib import Path

from penpal.sources.discoverers import CandidateSource
from penpal.sources.cleaning.path_filter import is_excluded_path
from penpal.sources.types import classify_fields, confidence_from_categories


_EXCLUDED_DIRS = {"node_modules", "__pycache__"}
_EXCLUDED_SUBSTRINGS = ("backup", "cache")
_EXCLUDED_NAME_TOKENS = ("lock", "manifest")
_MAX_FILE_SIZE = 10 * 1024 * 1024
_SIGNAL_CATEGORIES = {"intent_text", "ai_dialog", "nav", "comm", "artifact"}


def discover_json_files_candidates(*, exclude_paths: set[str]) -> list[CandidateSource]:
    root = Path.home() / "Library" / "Application Support"
    if not root.exists():
        return []

    candidates: list[CandidateSource] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            continue
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not _contains_excluded_token(d)]
        if depth >= 5:
            dirnames[:] = []

        for filename in filenames:
            if not filename.lower().endswith(".json"):
                continue
            if _contains_excluded_token(filename):
                continue
            path = current / filename
            try:
                file_depth = len(path.relative_to(root).parts) - 1
            except ValueError:
                continue
            if file_depth > 5:
                continue

            candidate = _candidate_for(path, exclude_paths)
            if candidate is not None:
                candidates.append(candidate)

    return sorted(candidates, key=lambda item: item.path)


def _candidate_for(path: Path, exclude_paths: set[str]) -> CandidateSource | None:
    resolved = path.expanduser().resolve(strict=False)
    excluded, _ = is_excluded_path(resolved)
    if excluded:
        return None
    if _is_excluded(resolved, exclude_paths):
        return None

    try:
        size = resolved.stat().st_size
    except Exception:
        return None
    if size > _MAX_FILE_SIZE:
        return None

    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    keys = _collect_keys(parsed, max_depth=3)
    if not keys:
        return None

    categories = classify_fields(keys)
    signal_categories = sorted(category for category in categories if category in _SIGNAL_CATEGORIES)
    if len(signal_categories) < 2:
        return None

    matched_fields = sorted({field for category in signal_categories for field in categories[category]})
    confidence = confidence_from_categories(len(signal_categories))

    return CandidateSource(
        discoverer="json_files",
        path=str(resolved),
        app_hint=_infer_app_hint(resolved),
        schema_signature=",".join(sorted(keys)),
        hint_tables=[],
        hint_fields=matched_fields,
        confidence=confidence,
    )


def _collect_keys(value: object, *, max_depth: int, depth: int = 0) -> set[str]:
    if depth > max_depth:
        return set()
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key:
                keys.add(key)
            keys.update(_collect_keys(nested, max_depth=max_depth, depth=depth + 1))
    elif isinstance(value, list):
        for item in value[:20]:
            keys.update(_collect_keys(item, max_depth=max_depth, depth=depth + 1))
    return keys


def _contains_excluded_token(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in _EXCLUDED_SUBSTRINGS + _EXCLUDED_NAME_TOKENS)


def _infer_app_hint(path: Path) -> str:
    parts = path.parts
    if "Application Support" in parts:
        idx = parts.index("Application Support")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return path.parent.name or "unknown"


def _is_excluded(path: Path, exclude_paths: set[str]) -> bool:
    for excluded in exclude_paths:
        excluded_path = Path(excluded)
        if path == excluded_path or excluded_path in path.parents:
            return True
    return False
