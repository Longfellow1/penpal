"""Write-authorized submission of declarative source recipes."""
from __future__ import annotations

from typing import Any

from penpal.mcp import audit, tools
from penpal.mcp.auth import check_access
from penpal.sources.recipes.catalog import load_recipe_from_string
from penpal.sources.recipes.identity_gate import save_recipe


def propose_source_recipe(token: str | None, recipe_toml: Any, *, client: str | None = None) -> dict[str, Any]:
    decision = check_access(token, client, "write")
    if decision.grant is None:
        audit.record(client=decision.client, tool=tools.TOOL_PROPOSE_SOURCE_RECIPE, allowed=False,
                     reason_code=decision.reason, scope=decision.scope)
        return tools.permission_denied()
    try:
        if not isinstance(recipe_toml, str) or len(recipe_toml) > 100_000:
            raise ValueError("invalid recipe text or size limit exceeded")
        try:
            load_recipe_from_string(recipe_toml)
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid recipe TOML or schema") from None
        summary: dict[str, Any] = {}
        save_recipe(recipe_toml, summary=summary)
    except (ValueError, OSError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "recipe storage unavailable"
        # Only fixed gate messages may cross this boundary; never echo caller data.
        safe = ("identity", "installation identity", "path outside", "sensitive", "wildcards", "file type", "probe ", "user recipe", "该工具由内置插件", "invalid recipe")
        if not any(reason.startswith(prefix) for prefix in safe):
            reason = "invalid recipe or validation failed"
        audit.record(client=decision.client, tool=tools.TOOL_PROPOSE_SOURCE_RECIPE, allowed=False,
                     reason_code="recipe_rejected", scope=decision.scope)
        return {"status": "rejected", "reason": reason}
    audit.record(client=decision.client, tool=tools.TOOL_PROPOSE_SOURCE_RECIPE, allowed=True,
                 reason_code="ok", scope=decision.scope, result_count=summary["parsed_turns"])
    return {"status": "ok", **summary}
