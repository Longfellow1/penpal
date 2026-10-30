from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from penpal.sources.discoverers import CandidateSource
from penpal.foundation.sqlite_rules import is_blocked_sqlite
from penpal.sources.cleaning.path_filter import is_excluded_path
from penpal.sources.types import ContentShape, classify_fields, confidence_from_categories

import logging
logger = logging.getLogger(__name__)


_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".vscdb")
_HINT_KEYWORDS = (
    "messages",
    "chats",
    "history",
    "sessions",
    "conversations",
    "events",
    "items",
    "prompts",
    "queries",
    "commands",
)
_STRONG_FIELD_CATEGORIES = {"intent_text", "ai_dialog", "session", "nav", "comm", "artifact"}


def discover_sqlite_candidates(*, exclude_paths: set[str]) -> list[CandidateSource]:
    home = Path.home()
    deadline = time.monotonic() + 30.0
    candidates: list[CandidateSource] = []

    app_support_root = home / "Library" / "Application Support"
    for path in _iter_sqlite_paths(app_support_root, max_depth=5, deadline=deadline):
        candidate = _candidate_for(path, exclude_paths)
        if candidate is not None:
            candidates.append(candidate)

    containers_root = home / "Library" / "Containers"
    if containers_root.exists():
        for container_dir in containers_root.iterdir():
            if time.monotonic() >= deadline:
                break
            data_root = container_dir / "Data"
            for path in _iter_sqlite_paths(data_root, max_depth=8, deadline=deadline):
                candidate = _candidate_for(path, exclude_paths)
                if candidate is not None:
                    candidates.append(candidate)

    return sorted(candidates, key=lambda item: item.path)


def _iter_sqlite_paths(root: Path, *, max_depth: int, deadline: float) -> Iterator[Path]:
    if not root.exists():
        return

    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() >= deadline:
            return
        current_dir = Path(dirpath)
        try:
            depth = len(current_dir.relative_to(root).parts)
        except ValueError:
            continue
        if depth >= max_depth:
            dirnames[:] = []

        for filename in filenames:
            if time.monotonic() >= deadline:
                return
            if not filename.lower().endswith(_SQLITE_SUFFIXES):
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
    blocked, _ = is_blocked_sqlite(resolved)
    if blocked:
        return None
    if not _is_sqlite_file(resolved):
        return None

    tables = _read_table_names(resolved)
    if not tables:
        return None

    hint_tables = sorted({name for name in tables if _is_hint_table(name)})
    table_columns = _read_table_columns(resolved, tables)
    table_defs = _read_table_defs(resolved, tables)
    kv_tables = _detect_kv_blob_tables(table_defs)
    field_categories = classify_fields(table_columns)
    hint_fields = sorted(
        {
            field
            for category, matches in field_categories.items()
            if category in _STRONG_FIELD_CATEGORIES
            for field in matches
        }
    )
    shape = ContentShape.KV_JSON_BLOB.value if kv_tables else ContentShape.TABULAR_ROWS.value
    if not kv_tables and not hint_tables and not hint_fields:
        return None

    table_confidence = "high" if len(hint_tables) >= 3 else ("medium" if hint_tables else "low")
    field_confidence = confidence_from_categories(len(field_categories))
    shape_confidence = "high" if kv_tables and _infer_app_hint(resolved).lower() == "cursor" else ("medium" if kv_tables else "low")
    confidence = _max_confidence(_max_confidence(table_confidence, field_confidence), shape_confidence)
    hint_tables_result = sorted(set(hint_tables + kv_tables))
    return CandidateSource(
        discoverer="sqlite",
        path=str(resolved),
        app_hint=_infer_app_hint(resolved),
        schema_signature=",".join(sorted(tables)),
        shape=shape,
        hint_tables=hint_tables_result,
        hint_fields=hint_fields,
        confidence=confidence,
    )


def _is_excluded(path: Path, exclude_paths: set[str]) -> bool:
    for excluded in exclude_paths:
        excluded_path = Path(excluded)
        if path == excluded_path or excluded_path in path.parents:
            return True
    return False


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except Exception:
        return False
    return header == _SQLITE_MAGIC


def _read_table_names(path: Path) -> list[str]:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(path), uri=False)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()

    names: list[str] = []
    for row in rows:
        if not row:
            continue
        name = row[0]
        if isinstance(name, str) and name:
            names.append(name)
    return sorted(set(names))


def _read_table_columns(path: Path, tables: list[str]) -> set[str]:
    conn: sqlite3.Connection | None = None
    columns: set[str] = set()
    try:
        conn = sqlite3.connect(str(path), uri=False)
        conn.execute("PRAGMA query_only=ON")
        for table in tables:
            try:
                rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            except Exception as exc:
                logger.debug("read table columns failed: %s", type(exc).__name__)
                continue
            for row in rows:
                if len(row) < 2:
                    continue
                name = row[1]
                if isinstance(name, str) and name:
                    columns.add(name)
    except Exception:
        return set()
    finally:
        if conn is not None:
            conn.close()
    return columns


def _read_table_defs(path: Path, tables: list[str]) -> dict[str, list[tuple[str, str]]]:
    conn: sqlite3.Connection | None = None
    result: dict[str, list[tuple[str, str]]] = {}
    try:
        conn = sqlite3.connect(str(path), uri=False)
        conn.execute("PRAGMA query_only=ON")
        for table in tables:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            pairs: list[tuple[str, str]] = []
            for row in rows:
                if len(row) < 3:
                    continue
                name = row[1]
                col_type = row[2]
                if isinstance(name, str) and name:
                    pairs.append((name, str(col_type or "")))
            if pairs:
                result[table] = pairs
    except Exception:
        return {}
    finally:
        if conn is not None:
            conn.close()
    return result


def _detect_kv_blob_tables(table_defs: dict[str, list[tuple[str, str]]]) -> list[str]:
    kv_tables: list[str] = []
    for table, pairs in table_defs.items():
        if len(pairs) != 2:
            continue
        lower_names = {name.lower() for name, _ in pairs}
        if lower_names != {"key", "value"}:
            continue

        typed = {name.lower(): col_type.lower() for name, col_type in pairs}
        key_type = typed.get("key", "")
        value_type = typed.get("value", "")
        if "text" not in key_type and "char" not in key_type:
            continue
        if not any(token in value_type for token in ("blob", "text", "json")):
            continue
        kv_tables.append(table)
    return sorted(kv_tables)


def _is_hint_table(table_name: str) -> bool:
    lowered = table_name.lower()
    return any(keyword in lowered for keyword in _HINT_KEYWORDS)


def _infer_app_hint(path: Path) -> str:
    parts = path.parts
    if "Application Support" in parts:
        idx = parts.index("Application Support")
        if idx + 1 < len(parts):
            return _normalize_app_name(parts[idx + 1])
    if "Containers" in parts:
        idx = parts.index("Containers")
        if idx + 1 < len(parts):
            return _normalize_app_name(parts[idx + 1])
    return _normalize_app_name(path.parent.name)


def _normalize_app_name(raw: str) -> str:
    value = raw.strip()
    if not value:
        return "unknown"
    if "." in value:
        value = value.split(".")[-1]
    return value or "unknown"


def _max_confidence(a: str, b: str) -> str:
    ranking = {"low": 0, "medium": 1, "high": 2}
    return a if ranking.get(a, 0) >= ranking.get(b, 0) else b
