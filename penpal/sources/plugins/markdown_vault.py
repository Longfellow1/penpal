from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from penpal.foundation.source_roots import approved_markdown_vault_roots
from penpal.foundation.approval_store import ApprovalStore
from penpal.sources.availability import (
    AVAILABLE, FILE_MISSING, NOT_FOUND, PERMISSION_DENIED, Availability, unavailable,
)
from penpal.sources.types import ContentShape, DataSource, DataSourceInstance, SemanticEvent

_VAULT_EXCLUDE_DIRS = frozenset({
    ".obsidian",
    "principles",
    "Daily",
    "Weekly",
    "anchors",
    ".claude",
    ".trash",
    ".obsidian-sync",
})


class MarkdownVaultSource(DataSource):
    name = "markdown_vault"
    privacy_tier = "yellow"
    liveness = "always"
    cheap_probe = True
    description = "读取用户已批准的 markdown vault/document 文件目录"

    def __init__(
        self,
        *,
        roots: list[Path] | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> None:
        self._explicit_roots = roots
        self._approval_store = approval_store or ApprovalStore()

    def availability(self) -> Availability:
        roots = self._approved_roots()
        if self._explicit_roots is not None:
            roots = [*roots, *self._explicit_roots]
        if not roots:
            return unavailable(NOT_FOUND)
        existing = [root for root in roots if root.is_dir()]
        if not existing:
            return unavailable(FILE_MISSING)
        if not all(os.access(root, os.R_OK | os.X_OK) for root in existing):
            return unavailable(PERMISSION_DENIED)
        return AVAILABLE

    def discover(self) -> list[DataSourceInstance]:
        instances: dict[str, DataSourceInstance] = {}

        for root in self._approved_roots():
            if not root.exists() or not root.is_dir():
                continue
            key = str(root.resolve(strict=False))
            if key in instances:
                continue
            note_count = self._count_notes(root)
            label = root.name or "vault"
            instances[key] = DataSourceInstance(
                plugin=self.name,
                locator=key,
                label=label,
                metadata={
                    "vault_name": label,
                    "note_count": note_count,
                    "shape": ContentShape.DOCUMENT_FILE.value,
                },
            )

        if self._explicit_roots is not None:
            for root in self._explicit_roots:
                if not root.exists() or not root.is_dir():
                    continue
                key = str(root.resolve(strict=False))
                if key in instances:
                    continue
                note_count = self._count_notes(root)
                label = root.name or "vault"
                instances[key] = DataSourceInstance(
                    plugin=self.name,
                    locator=key,
                    label=label,
                    metadata={
                        "vault_name": label,
                        "note_count": note_count,
                        "shape": ContentShape.DOCUMENT_FILE.value,
                    },
                )

        return sorted(instances.values(), key=lambda item: item.locator)

    def read(
        self,
        instance: DataSourceInstance,
        since: datetime,
        until: datetime,
    ) -> Iterator[SemanticEvent]:
        vault_root = Path(instance.locator).expanduser()
        if not vault_root.exists() or not vault_root.is_dir():
            return iter(())

        vault_name = str(instance.metadata.get("vault_name") or vault_root.name or "vault")
        since_utc = since.astimezone(timezone.utc)
        until_utc = until.astimezone(timezone.utc)

        def _iter_events() -> Iterator[SemanticEvent]:
            patterns = ("*.md", "*.txt")
            for pattern in patterns:
                for path in sorted(vault_root.rglob(pattern)):
                    if any(part in _VAULT_EXCLUDE_DIRS for part in path.parts):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue

                    event_time = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    if event_time < since_utc or event_time > until_utc:
                        continue

                    try:
                        rel_path = str(path.relative_to(vault_root))
                    except Exception:
                        rel_path = path.name

                    title, title_line = _extract_title_and_line(path)
                    event = ContentShape.DOCUMENT_FILE.to_semantic_event(
                        {
                            "mtime": event_time,
                            "actor": "user",
                            "intent": title or path.stem,
                            "artifact": rel_path,
                            "path": str(path),
                            "raw_ref": f"markdown_vault:{vault_name}:{rel_path}",
                            "metadata": {
                                "vault_name": vault_name,
                                "file_size": stat.st_size,
                                "shape": ContentShape.DOCUMENT_FILE.value,
                            },
                        },
                        source=self.name,
                        privacy_tier=self.privacy_tier,
                    )
                    if event is not None:
                        event.evidence_locator = str(path.resolve())
                        event.evidence_line = title_line
                        yield event

        return _iter_events()

    def _count_notes(self, vault_root: Path) -> int:
        count = 0
        for pattern in ("*.md", "*.txt"):
            for path in vault_root.rglob(pattern):
                if any(part in _VAULT_EXCLUDE_DIRS for part in path.parts):
                    continue
                count += 1
        return count

    def _approved_roots(self) -> list[Path]:
        try:
            approved = self._approval_store.list_approved()
        except Exception:
            return []
        return approved_markdown_vault_roots(
            approved, document_shape=ContentShape.DOCUMENT_FILE.value
        )


def vault_roots_by_name(source: MarkdownVaultSource | None = None) -> dict[str, Path]:
    """vault_name (the label discover() stores, as read() uses it) -> vault root dir, via discover().

    Names shared by several discovered vaults are left out: an old row cannot be matched to one root.
    """
    found: dict[str, list[Path]] = {}
    for instance in (source or MarkdownVaultSource()).discover():
        name = str(instance.metadata.get("vault_name") or "")
        if name:
            found.setdefault(name, []).append(Path(instance.locator))
    return {name: roots[0] for name, roots in found.items() if len(roots) == 1}


def _extract_title(path: Path) -> str:
    return _extract_title_and_line(path)[0]


def _extract_title_and_line(path: Path) -> tuple[str, int]:
    """(title, 1-based line the title was read from); line 1 when the note has no heading."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            in_frontmatter = False
            seen_first = False
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                if not seen_first and line == "---":
                    in_frontmatter = True
                    seen_first = True
                    continue
                seen_first = True
                if in_frontmatter:
                    if line == "---":
                        in_frontmatter = False
                    continue
                if line.startswith("#"):
                    return line.lstrip("#").strip()[:200], line_no
                return path.stem, line_no
    except Exception:
        return path.stem, 1
    return path.stem, 1
