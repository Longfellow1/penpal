from __future__ import annotations

import json
import os
from pathlib import Path

from penpal.sources.cleaning.path_filter import is_excluded_path
from penpal.sources.discoverers import CandidateSource
from penpal.sources.types import ContentShape


_DENSITY_MIN_RATIO = 0.35
_DENSITY_MIN_MD_COUNT = 6
_DENSITY_MAX_TOTAL_FILES = 120
_MAX_SCAN_DEPTH = 3
_IGNORE_DIRS = {".git", ".obsidian", "node_modules", ".Trash", "Library"}


def discover_markdown_vault_candidates(*, exclude_paths: set[str]) -> list[CandidateSource]:
    candidates: dict[str, CandidateSource] = {}

    for vault in _load_obsidian_vault_paths():
        candidate = _candidate_for(vault, exclude_paths=exclude_paths, confidence="high", app_hint="Obsidian")
        if candidate is not None:
            candidates[candidate.path] = candidate

    for directory in _discover_dense_markdown_dirs():
        candidate = _candidate_for(
            directory,
            exclude_paths=exclude_paths,
            confidence="medium",
            app_hint=directory.name or "markdown",
        )
        if candidate is not None:
            candidates[candidate.path] = candidate

    return sorted(candidates.values(), key=lambda item: item.path)


def _load_obsidian_vault_paths() -> list[Path]:
    config_path = (
        Path.home()
        / "Library"
        / "Application Support"
        / "obsidian"
        / "obsidian.json"
    ).expanduser()
    if not config_path.exists():
        return []

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []

    vaults = payload.get("vaults")
    if not isinstance(vaults, dict):
        return []

    paths: list[Path] = []
    for value in vaults.values():
        if not isinstance(value, dict):
            continue
        raw_path = value.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        paths.append(Path(raw_path).expanduser())
    return paths


def _discover_dense_markdown_dirs() -> list[Path]:
    home = Path.home()
    if not home.exists():
        return []

    dirs: list[Path] = []
    for child in home.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if child.name in _IGNORE_DIRS:
            continue
        dirs.extend(_scan_for_dense_dirs(child, root=child))
    return dirs


def _scan_for_dense_dirs(base: Path, *, root: Path) -> list[Path]:
    hits: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        current = Path(dirpath)
        try:
            depth = len(current.relative_to(root).parts)
        except ValueError:
            continue
        dirnames[:] = [name for name in dirnames if name not in _IGNORE_DIRS and not name.startswith(".")]
        if depth >= _MAX_SCAN_DEPTH:
            dirnames[:] = []

        total_files = len(filenames)
        if total_files == 0:
            continue
        if total_files > _DENSITY_MAX_TOTAL_FILES:
            continue
        md_count = sum(1 for name in filenames if name.lower().endswith(".md"))
        if md_count < _DENSITY_MIN_MD_COUNT:
            continue
        ratio = md_count / total_files
        if ratio < _DENSITY_MIN_RATIO:
            continue
        hits.append(current)
    return hits


def _candidate_for(
    path: Path,
    *,
    exclude_paths: set[str],
    confidence: str,
    app_hint: str,
) -> CandidateSource | None:
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.exists() or not resolved.is_dir():
        return None
    excluded, _ = is_excluded_path(resolved)
    if excluded:
        return None
    if _is_excluded(resolved, exclude_paths):
        return None

    md_files = [item for item in resolved.rglob("*.md") if ".obsidian" not in item.parts]
    if not md_files:
        return None

    return CandidateSource(
        discoverer="markdown_vault",
        path=str(resolved),
        app_hint=app_hint,
        schema_signature=f"markdown:{len(md_files)}",
        shape=ContentShape.DOCUMENT_FILE.value,
        hint_tables=[],
        hint_fields=[],
        confidence=confidence,
    )


def _is_excluded(path: Path, exclude_paths: set[str]) -> bool:
    for excluded in exclude_paths:
        excluded_path = Path(excluded)
        if path == excluded_path or excluded_path in path.parents:
            return True
    return False
