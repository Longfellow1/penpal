"""Explain the breadth of Work grants that can read coding-agent conversations."""
from __future__ import annotations

from collections.abc import Iterable

from penpal.i18n import T

CODING_AGENT_SOURCES = frozenset({"claude_code", "codex_cli", "ai_tools"})


def enabled_coding_agent_sources() -> frozenset[str]:
    from penpal.capture.capture_control import disabled_sources

    return CODING_AGENT_SOURCES - disabled_sources()


def work_project_notice(
    buckets: Iterable[str],
    sources: Iterable[str] | None,
    enabled_sources: Iterable[str],
) -> str | None:
    """Return one notice when this Work grant can read an enabled agent source."""
    try:
        includes_work = any(isinstance(bucket, str) and bucket.casefold() == "work" for bucket in buckets)
    except TypeError:
        return None
    if not includes_work:
        return None
    enabled = CODING_AGENT_SOURCES & set(enabled_sources)
    if sources is not None:
        try:
            enabled &= {source for source in sources if isinstance(source, str)}
        except TypeError:
            return None
    if not enabled:
        return None
    return T(
        "Work 包含 Claude Code、Codex 等 AI 工具里的全部对话；私人项目可用 `penpal rules add --scope project …` 归到 Personal。",
        "Work includes all conversations in Claude Code, Codex and other AI tools; move a private project to Personal with `penpal rules add --scope project …`.",
    )
