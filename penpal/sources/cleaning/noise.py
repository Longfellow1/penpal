"""Self-echo helpers for the AI-transcript source plugins (claude_code, codex_cli).

``advance_echo`` / ``is_penpal_call`` implement the ``via_penpal`` mark: an assistant text
  that follows a call to a Penpal MCP tool (same session, until the next real user message) is
  mostly a paraphrase of Penpal's own output, so it is stored but flagged and hidden from search
  by default.
"""
from __future__ import annotations

from typing import Iterable

# ── via_penpal (self-echo) ─────────────────────────────────────────────────────────────────

DEFAULT_SERVER_NAMES: tuple[str, ...] = ("penpal",)


def _norm_names(names: Iterable[str] | None) -> tuple[str, ...]:
    out = tuple(n.strip().lower() for n in (names or ()) if isinstance(n, str) and n.strip())
    return out or DEFAULT_SERVER_NAMES


def resolve_server_names(override: Iterable[str] | None = None) -> tuple[str, ...]:
    """MCP server names Penpal is registered under: explicit override, else ``cleaning.toml``
    ``penpal_server_names``, else ``("penpal",)``."""
    if override is not None:
        return _norm_names(override)
    from penpal.sources.cleaning.config import load_cleaning_config

    return _norm_names(load_cleaning_config().penpal_server_names)


def _name_matches(value: object, names: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    return any(v.startswith((f"mcp__{n}__", f"{n}__", f"{n}.", f"{n}/")) for n in names)


def is_penpal_call(payload: dict, names: tuple[str, ...]) -> bool:
    """Whether a tool-call record targets a Penpal MCP server.

    Covers Claude Code ``tool_use`` (``name: mcp__keypulse__search_context``) and the Codex
    shapes: ``function_call`` / ``custom_tool_call`` (``name`` = ``mcp__keypulse__x``, or
    ``namespace`` = ``mcp__keypulse__`` / ``penpal`` with a bare ``name``) and
    ``mcp_tool_call*`` (``server`` or ``invocation.server`` = ``penpal``).
    """
    if not isinstance(payload, dict):
        return False
    names = _norm_names((*names, "keypulse"))
    for key in ("name", "namespace", "tool"):
        if _name_matches(payload.get(key), names):
            return True
    containers = [payload]
    for key in ("invocation", "item"):
        if isinstance(payload.get(key), dict):
            containers.append(payload[key])
    for c in containers:
        server = c.get("server") or c.get("server_name") or c.get("namespace")
        if isinstance(server, str):
            s = server.strip().lower()
            # Real Codex rollouts use namespace="mcp__<server>" with a bare tool name.
            if s in names or _name_matches(s, names) or any(s in (f"mcp__{n}", f"mcp__{n}__") for n in names):
                return True
    return False


def advance_echo(active: bool, role: str, has_text: bool, calls_penpal: bool) -> tuple[bool, bool]:
    """One step of the per-session echo state machine. Returns ``(mark_this_message, new_active)``.

    A message is judged by the state *before* it (text that precedes a tool call in the same
    record is not an echo of it). A user message with real text ends the window.
    """
    marked = active and role == "assistant" and has_text
    if role == "user" and has_text:
        active = False
    if calls_penpal:
        active = True
    return marked, active
