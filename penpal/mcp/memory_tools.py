"""Authenticated, audited memory write tools."""
from __future__ import annotations

from penpal.mcp import audit
from penpal.mcp.auth import check_access
from penpal.mcp.tools import TOOL_COMMIT_MEMORY, TOOL_UPDATE_MEMORY, permission_denied
from penpal.memory.writer import Denied, _target, commit_one
from penpal.store.db import get_conn


def _run(token, client, tool, action):
    decision = check_access(token, client, "write")
    if decision.grant is None:
        audit.record(client=decision.client, tool=tool, allowed=False, reason_code=decision.reason, scope=decision.scope)
        return permission_denied()
    conn = get_conn()
    try:
        with conn:
            result = action(conn, decision.grant)
    except Denied:
        audit.record(client=decision.client, tool=tool, allowed=False, reason_code="scope_denied", scope=decision.scope)
        return {"status": "not_found", "message": "The requested evidence or memory is not available to this client."}
    except (ValueError, TypeError) as exc:
        audit.record(client=decision.client, tool=tool, allowed=False, reason_code="invalid_argument", scope=decision.scope)
        return {"status": "invalid_argument", "message": str(exc)}
    audit.record(client=decision.client, tool=tool, allowed=True, reason_code="ok", scope=decision.scope, result_count=1)
    return {"status": "ok", **result}


def commit_memory(token, items, *, client=None):
    def action(conn, grant):
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list")
        return {"items": [commit_one(conn, item, grant) for item in items]}
    return _run(token, client, TOOL_COMMIT_MEMORY, action)


def update_memory(token, memory_id, action, item=None, *, client=None):
    def write(conn, grant):
        old = _target(conn, memory_id, grant)
        if old["status"] != "active" or old["superseded_by"] is not None:
            raise ValueError("memory is not active")
        if action == "correct":
            if not isinstance(item, dict):
                raise ValueError("item is required for correction")
            return commit_one(conn, {**item, "supersedes": memory_id}, grant)
        if action not in ("revoke", "invalidate"):
            raise ValueError("action must be correct, revoke or invalidate")
        conn.execute("UPDATE memories SET status = ? WHERE id = ?", ("revoked" if action == "revoke" else "invalid", old["id"]))
        return {"id": memory_id}
    return _run(token, client, TOOL_UPDATE_MEMORY, write)
