from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from penpal.sources.discoverers import CandidateSource
from penpal.sources.cleaning.path_filter import is_excluded_path
from penpal.sources.types import classify_fields, confidence_from_categories


_EXCLUDED_DIRS = {".Trash", "node_modules", ".git", "__pycache__"}
_MAX_FILE_SIZE = 50 * 1024 * 1024


def discover_jsonl_candidates(*, exclude_paths: set[str]) -> list[CandidateSource]:
    home = Path.home()
    candidates: list[CandidateSource] = []

    roots: list[tuple[Path, int]] = [
        (home / "Library" / "Application Support", 4),
        (home / ".local", 4),
    ]

    for child in home.iterdir() if home.exists() else []:
        if not child.is_dir() or not child.name.startswith("."):
            continue
        if child.name in _EXCLUDED_DIRS:
            continue
        if child == home / ".local":
            continue
        roots.append((child, 4))

    seen: set[str] = set()
    for root, max_depth in roots:
        for path in _iter_jsonl_paths(root, max_depth=max_depth):
            resolved = str(path.expanduser().resolve(strict=False))
            if resolved in seen:
                continue
            seen.add(resolved)
            candidate = _candidate_for(path, exclude_paths)
            if candidate is not None:
                candidates.append(candidate)

    return sorted(candidates, key=lambda item: item.path)


def _iter_jsonl_paths(root: Path, *, max_depth: int) -> Iterator[Path]:
    if not root.exists():
        return

    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        try:
            depth = len(current_dir.relative_to(root).parts)
        except ValueError:
            continue

        dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIRS]
        if depth >= max_depth:
            dirnames[:] = []

        for filename in filenames:
            if not filename.lower().endswith(".jsonl"):
                continue
            file_path = current_dir / filename
            try:
                file_depth = len(file_path.relative_to(root).parts) - 1
            except ValueError:
                continue
            if file_depth > max_depth:
                continue
            yield file_path


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
        return CandidateSource(
            discoverer="jsonl",
            path=str(resolved),
            app_hint=_infer_app_hint(resolved),
            schema_signature="",
            hint_tables=[],
            hint_fields=[],
            confidence="low",
        )

    fields = _sample_fields(resolved)
    categories = classify_fields(fields)
    hint_fields = sorted({field for matched in categories.values() for field in matched})
    if not hint_fields:
        return None

    confidence = confidence_from_categories(len(categories))
    return CandidateSource(
        discoverer="jsonl",
        path=str(resolved),
        app_hint=_infer_app_hint(resolved),
        schema_signature=",".join(sorted(fields)),
        hint_tables=hint_fields,
        hint_fields=hint_fields,
        confidence=confidence,
    )


def _sample_fields(path: Path) -> set[str]:
    fields: set[str] = set()
    non_empty_lines: list[str] = []

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                non_empty_lines.append(stripped)
                if len(non_empty_lines) >= 2:
                    break
    except Exception:
        return fields

    for line in non_empty_lines:
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        for key in parsed.keys():
            if isinstance(key, str) and key:
                fields.add(key)

    return fields


def _is_excluded(path: Path, exclude_paths: set[str]) -> bool:
    for excluded in exclude_paths:
        excluded_path = Path(excluded)
        if path == excluded_path or excluded_path in path.parents:
            return True
    return False


def _infer_app_hint(path: Path) -> str:
    parts = path.parts
    if "Application Support" in parts:
        idx = parts.index("Application Support")
        if idx + 1 < len(parts):
            return parts[idx + 1]

    home = Path.home()
    try:
        rel = path.relative_to(home)
    except Exception:
        return path.parent.name or "unknown"

    if rel.parts and rel.parts[0].startswith("."):
        return rel.parts[0].lstrip(".") or "unknown"
    return path.parent.name or "unknown"
