from __future__ import annotations

import os
from pathlib import Path

from penpal.sources.discoverers import CandidateSource
from penpal.sources.cleaning.path_filter import is_excluded_path


_HIGH_VALUE_APPS = {"Cursor", "Codex", "Antigravity", "Claude", "LM Studio", "Continue", "obsidian", "Kiro"}


def discover_leveldb_candidates(*, exclude_paths: set[str]) -> list[CandidateSource]:
    root = Path.home() / "Library" / "Application Support"
    if not root.exists():
        return []

    instances: dict[str, CandidateSource] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            continue
        if depth >= 6:
            dirnames[:] = []

        if not _is_leveldb_manifest_dir(current, filenames):
            continue
        resolved_dir = current.expanduser().resolve(strict=False)
        excluded, _ = is_excluded_path(resolved_dir)
        if excluded:
            continue
        if _is_excluded(resolved_dir, exclude_paths):
            continue

        file_names = set(filenames)
        ldb_count = sum(1 for name in file_names if name.endswith(".ldb"))
        sst_count = sum(1 for name in file_names if name.endswith(".sst"))
        data_file_count = ldb_count + sst_count
        if "CURRENT" not in file_names or data_file_count < 2:
            continue

        total_size = 0
        for name in file_names:
            file_path = resolved_dir / name
            try:
                total_size += file_path.stat().st_size
            except OSError:
                continue

        total_size_mb = round(total_size / (1024 * 1024), 1)
        app_hint = _infer_app_hint(resolved_dir)
        confidence = "high" if app_hint in _HIGH_VALUE_APPS else "medium"
        instances[str(resolved_dir)] = CandidateSource(
            discoverer="leveldb",
            path=str(resolved_dir),
            app_hint=app_hint,
            schema_signature=f"leveldb:{data_file_count}files:{total_size_mb}MB",
            hint_tables=[],
            hint_fields=[],
            confidence=confidence,
        )

    return sorted(instances.values(), key=lambda item: item.path)


def _is_leveldb_manifest_dir(path: Path, filenames: list[str]) -> bool:
    if not any(name.startswith("MANIFEST-") for name in filenames):
        return False
    lower_path = str(path).lower()
    if lower_path.endswith("leveldb"):
        return True
    return lower_path.endswith("session storage") or lower_path.endswith("local storage/leveldb")


def _infer_app_hint(path: Path) -> str:
    parts = path.parts
    if "Application Support" not in parts:
        return path.parent.name or "unknown"

    idx = parts.index("Application Support")
    for value in parts[idx + 1 :]:
        if value in {"Session Storage", "Local Storage", "leveldb"}:
            continue
        if value == "Local":
            continue
        if value == "Session":
            continue
        return value
    return path.parent.name or "unknown"


def _is_excluded(path: Path, exclude_paths: set[str]) -> bool:
    for excluded in exclude_paths:
        excluded_path = Path(excluded)
        if path == excluded_path or excluded_path in path.parents:
            return True
    return False
