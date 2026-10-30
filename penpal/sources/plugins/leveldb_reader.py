from __future__ import annotations

import ast
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from penpal.foundation.desensitizer import desensitize, desensitize_json_value
from penpal.foundation.approval_store import ApprovalStore
from penpal.sources.cleaning.path_filter import is_excluded_path
from penpal.sources.types import ContentShape, DataSource, DataSourceInstance, SemanticEvent

import logging
logger = logging.getLogger(__name__)


_PER_FILE_LIMIT = 5000
_SUPPORTED_SUFFIXES = (".ldb", ".log")


class LevelDbReaderSource(DataSource):
    name = "leveldb_reader"
    privacy_tier = "yellow"
    liveness = "after_app_quit"
    app_hints = (
        "com.todesktop.230313mzl4w4u92",
        "Cursor",
        "com.microsoft.VSCode",
        "Visual Studio Code",
        "com.figma.Desktop",
        "Figma",
        "com.openai.chat",
        "ChatGPT",
    )
    description = "读取用户已批准的 LevelDB 候选目录（strings + JSON fallback）"

    def __init__(self, *, approval_store: ApprovalStore | None = None) -> None:
        self._approval_store = approval_store or ApprovalStore()

    def discover(self) -> list[DataSourceInstance]:
        try:
            approved_records = self._approval_store.list_approved()
        except Exception:
            return []

        instances: list[DataSourceInstance] = []
        seen: set[str] = set()
        for record in approved_records:
            if str(record.metadata.get("discoverer") or "") != "leveldb":
                continue
            raw_path = str(record.metadata.get("path") or "").strip()
            if not raw_path:
                continue

            leveldb_dir = Path(raw_path).expanduser().resolve(strict=False)
            key = str(leveldb_dir)
            if key in seen:
                continue
            if not leveldb_dir.exists() or not leveldb_dir.is_dir():
                continue
            excluded, _ = is_excluded_path(leveldb_dir)
            if excluded:
                continue

            candidate_id = str(record.candidate_id or "")
            app_hint = str(record.metadata.get("app_hint") or leveldb_dir.parent.name or "leveldb")
            instances.append(
                DataSourceInstance(
                    plugin=self.name,
                    locator=key,
                    label=app_hint,
                    metadata={
                        "candidate_id": candidate_id,
                        "approved_candidate_id": candidate_id,
                        "app_hint": app_hint,
                        "shape": ContentShape.KV_JSON_BLOB.value,
                    },
                )
            )
            seen.add(key)

        return sorted(instances, key=lambda item: item.locator)

    def read(
        self,
        instance: DataSourceInstance,
        since: datetime,
        until: datetime,
    ) -> Iterator[SemanticEvent]:
        leveldb_dir = Path(instance.locator).expanduser()
        if not leveldb_dir.exists() or not leveldb_dir.is_dir():
            return iter(())

        since_utc = since.astimezone(timezone.utc)
        until_utc = until.astimezone(timezone.utc)
        candidate_id = str(
            instance.metadata.get("candidate_id")
            or instance.metadata.get("approved_candidate_id")
            or "unknown"
        )
        app_hint = str(instance.metadata.get("app_hint") or instance.label or "leveldb")

        def _iter_events() -> Iterator[SemanticEvent]:
            for db_file in _iter_leveldb_files(leveldb_dir, since_utc, until_utc):
                file_rows = _extract_rows(db_file, limit=_PER_FILE_LIMIT)
                for row_index, (key, payload) in enumerate(file_rows, start=1):
                    key_text = desensitize(key) or f"row:{row_index}"
                    safe_payload = desensitize_json_value(payload)
                    raw_ref = desensitize(
                        f"{self.name}:{candidate_id}:{db_file.name}:{row_index}:{key_text}"
                    )
                    event = ContentShape.KV_JSON_BLOB.to_semantic_event(
                        {
                            "key": key_text,
                            "value": safe_payload,
                            "artifact": desensitize(f"{app_hint}:{key_text}"),
                            "raw_ref": raw_ref,
                            "metadata": {
                                "candidate_id": candidate_id,
                                "approved_candidate_id": candidate_id,
                                "app_hint": app_hint,
                                "shape": ContentShape.KV_JSON_BLOB.value,
                                "file": str(db_file),
                            },
                        },
                        source=self.name,
                        privacy_tier=self.privacy_tier,
                    )
                    if event is None:
                        continue
                    if event.time < since_utc or event.time > until_utc:
                        continue
                    yield event

        return _iter_events()


def _iter_leveldb_files(leveldb_dir: Path, since: datetime, until: datetime) -> list[Path]:
    files: list[Path] = []
    for path in sorted(leveldb_dir.iterdir()):
        if not path.is_file():
            continue
        if not (_is_manifest(path) or path.suffix.lower() in _SUPPORTED_SUFFIXES):
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except Exception as exc:
            logger.debug("iter leveldb files failed: %s", type(exc).__name__)
            continue
        if modified < since or modified > until:
            continue
        files.append(path)
    return files


def _is_manifest(path: Path) -> bool:
    return path.name.startswith("MANIFEST-")


def _extract_rows(path: Path, *, limit: int) -> list[tuple[str, dict[str, Any]]]:
    lines = _read_strings(path)
    rows: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        if len(rows) >= limit:
            break
        for key, payload in _line_to_kv_rows(line):
            rows.append((key, payload))
            if len(rows) >= limit:
                break
    return rows


def _read_strings(path: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["strings", "-a", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout:
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception as exc:
        logger.debug("read strings failed: %s", type(exc).__name__)
        pass

    try:
        data = path.read_bytes()
    except Exception:
        return []

    decoded = data.decode("utf-8", errors="ignore")
    return [line.strip() for line in decoded.splitlines() if line.strip()]


def _line_to_kv_rows(line: str) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for match in re.finditer(r"\{[^{}]+\}", line):
        raw_obj = match.group(0)
        payload = _parse_obj(raw_obj)
        if payload is None:
            continue
        prefix = line[: match.start()].strip()
        key = _extract_key(prefix)
        rows.append((key, payload))
    return rows


def _parse_obj(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except Exception:
        try:
            parsed = ast.literal_eval(value)
        except Exception:
            return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _extract_key(prefix: str) -> str:
    if not prefix:
        return "unknown-key"
    scoped = re.findall(r"[A-Za-z0-9._/-]+:[A-Za-z0-9._/-]+", prefix)
    if scoped:
        return scoped[-1]
    token = prefix.split()[-1].strip("'\"` ")
    token = re.sub(r"[^\w:./-]", "", token)
    return token or "unknown-key"


__all__ = ["LevelDbReaderSource"]
