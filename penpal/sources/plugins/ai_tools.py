"""AI tools declarative recipe reader plugin."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

from penpal.sources.recipes.catalog import (
    is_tool_detected,
    load_combined_catalog,
)
from penpal.sources.recipes.engine import (
    RecipeEngine,
    get_base_dir_from_glob,
    resolve_history_files,
)
from penpal.sources.types import DataSource, DataSourceInstance, SemanticEvent
from penpal.utils.logging import get_logger

LOGGER = get_logger("sources.plugins.ai_tools")


class AiToolsSource(DataSource):
    name = "ai_tools"
    privacy_tier = "green"
    liveness = "always"
    fast_poll = True
    description = "Declarative AI tool session history reader"

    def __init__(
        self,
        *,
        catalog_dir: Path | None = None,
        use_cursor: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._catalog_dir = catalog_dir
        self._use_cursor = use_cursor
        self._env = env
        self._engine = RecipeEngine(env=env)
        self._pending_cursors: dict[int, dict[str, Any]] = {}

    def discover(self) -> list[DataSourceInstance]:
        recipes = load_combined_catalog(self._catalog_dir)
        instances: list[DataSourceInstance] = []

        for tool in recipes:
            if not tool.readable or tool.adapter or not tool.enabled:
                continue
            if tool.user_recipe:
                from penpal.sources.recipes.identity_gate import validate_binding

                try:
                    validate_binding(tool, self._env)
                except (ValueError, OSError) as exc:
                    LOGGER.warning("user recipe %s skipped: %s", tool.id, exc)
                    continue
            elif not is_tool_detected(tool, env=self._env):
                continue

            for h_idx, history in enumerate(tool.history):
                if history.format == "sqlite":
                    files = resolve_history_files(history, self._env)
                    for f in files:
                        locator = f"{tool.id}#{h_idx}@{f.resolve()}"
                        instances.append(
                            DataSourceInstance(
                                plugin=self.name,
                                locator=locator,
                                label=f"{tool.name} ({f.name})",
                                metadata={
                                    "tool_id": tool.id,
                                    "history_idx": h_idx,
                                    "format": history.format,
                                    "root": str(f.resolve()),
                                },
                            )
                        )
                else:
                    base_dir = get_base_dir_from_glob(history.glob, self._env)
                    if base_dir.is_dir():
                        locator = f"{tool.id}#{h_idx}@{base_dir.resolve()}"
                        instances.append(
                            DataSourceInstance(
                                plugin=self.name,
                                locator=locator,
                                label=f"{tool.name} ({history.format})",
                                metadata={
                                    "tool_id": tool.id,
                                    "history_idx": h_idx,
                                    "format": history.format,
                                    "root": str(base_dir.resolve()),
                                },
                            )
                        )

        return instances

    def read(
        self,
        instance: DataSourceInstance,
        since: datetime,
        until: datetime,
    ) -> Iterator[SemanticEvent]:
        tool_id = instance.metadata.get("tool_id")
        history_idx = instance.metadata.get("history_idx", 0)

        recipes = {t.id: t for t in load_combined_catalog(self._catalog_dir)}
        tool = recipes.get(str(tool_id))
        if tool is None or not tool.enabled or history_idx >= len(tool.history):
            LOGGER.warning("ai_tools: unknown tool %s or invalid history index %s", tool_id, history_idx)
            return iter(())

        if tool.user_recipe:
            from penpal.sources.recipes.identity_gate import check_path, find_identity, validate_binding

            try:
                validate_binding(tool, self._env)
                root = Path(str(instance.metadata.get("root", "")))
                lookup = self._env if self._env is not None else os.environ
                check_path(root, find_identity(tool, lookup), Path(lookup.get("HOME", str(Path.home()))))
            except (ValueError, OSError) as exc:
                LOGGER.warning("user recipe %s skipped: %s", tool.id, exc)
                return iter(())

        history = tool.history[history_idx]
        raw_root = instance.metadata.get("root") or (
            instance.locator.split("@", 1)[1] if "@" in instance.locator else instance.locator
        )
        root_path = Path(raw_root).expanduser()

        source_id = None
        initial_cursor: dict[str, Any] = {}
        if self._use_cursor:
            from penpal.store.sync_repo import ensure_source, get_cursor

            source_id = ensure_source(self.name, instance.locator)
            initial_cursor = get_cursor(source_id)

        stream = self._engine.stream_recipe(
            tool,
            history,
            root_path,
            source_id or 0,
            initial_cursor,
        )

        def _iter() -> Iterator[SemanticEvent]:
            for event in stream:
                if not self._use_cursor and (event.time < since or event.time > until):
                    continue
                yield event

            if self._use_cursor and source_id is not None:
                if self._engine.last_cursor is not None:
                    self._pending_cursors[source_id] = self._engine.last_cursor
                elif self._engine.last_error:
                    from penpal.store.sync_repo import record_sync_error

                    record_sync_error(source_id, self._engine.last_error)

        return _iter()

    def commit_cursors(self, *, failed: bool = False) -> None:
        """Persist resume positions once events are stored."""
        from penpal.store.sync_repo import record_sync_error, save_cursor

        pending, self._pending_cursors = self._pending_cursors, {}
        for source_id, cursor in pending.items():
            if failed:
                record_sync_error(source_id, "persist failed; cursor not advanced")
            else:
                save_cursor(source_id, cursor)
