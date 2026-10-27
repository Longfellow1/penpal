"""Shared project keys for source metadata and project-scoped bucket rules."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


def normalize_project(value: Any) -> str | None:
    """Return a project's folder name, whether given as a path or a folder name."""
    if not isinstance(value, (str, Path)):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    name = Path(raw).name.strip()
    return name if name and name not in {".", ".."} else None


def project_matches(rule_value: str, project: str | None) -> bool:
    """Whether a project rule names this project.

    Exact (case-insensitive) on the folder name. Claude Code rows recorded before the folder name was
    known carry only its encoded projects dir ("/tmp/proj/life-admin" -> "-tmp-proj-life-admin", every
    non-alphanumeric character becomes "-"), so a rule also matches that encoding's trailing segment.
    Encoding is lossy, so this can over-match a shorter name; that only moves more rows out of a shared
    bucket, never into one.
    """
    if not project or not rule_value:
        return False
    if project.casefold() == rule_value.casefold():
        return True
    encoded = re.sub(r"[^A-Za-z0-9]", "-", rule_value).casefold()
    return project.startswith("-") and project.casefold().endswith("-" + encoded)


def project_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    """Read the normalized key, falling back to legacy project_dir metadata."""
    if not isinstance(metadata, Mapping):
        return None
    return normalize_project(metadata.get("project") or metadata.get("project_dir"))


def project_from_metadata_json(metadata_json: str | None) -> str | None:
    if not metadata_json:
        return None
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError):
        return None
    return project_from_metadata(metadata)


def fold_project_rows(
    counts: Mapping[tuple[str, str, str], int],
) -> list[tuple[tuple[str, str, str], int]]:
    """Fold encoded Claude Code project keys into the folder name they stand for, for listing only.

    Rows recorded before the folder name was known carry the encoded projects dir ("-Users-alice-Go-life-admin");
    a rule on "life-admin" already covers them (project_matches), so the listing shows them as one project. The
    longest matching name of the same source wins; an encoded key that matches no name stays as it is.
    """
    names: dict[str, set[str]] = {}
    for source, project, _bucket in counts:
        if not project.startswith("-"):
            names.setdefault(source, set()).add(project)
    folded: dict[tuple[str, str, str], int] = {}
    for (source, project, bucket), count in counts.items():
        if project.startswith("-"):
            matches = [name for name in names.get(source, ()) if project_matches(name, project)]
            if matches:
                project = max(matches, key=lambda name: (len(name), name))
        key = (source, project, bucket)
        folded[key] = folded.get(key, 0) + count
    return sorted(folded.items(), key=lambda item: (item[0][0], item[0][1].casefold(), item[0][2]))
