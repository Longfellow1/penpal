from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator
from urllib.parse import urlsplit

from penpal.sources.cleaning.config import PRIVACY_TIERS, load_cleaning_config
from penpal.sources.cleaning.content_quality import is_low_signal_event
from penpal.sources.cleaning.dedup import dedup_events
from penpal.sources.availability import NOT_FOUND, PROBE_ERROR, Availability, unavailable
from penpal.sources.plugins.ai_tools import AiToolsSource
from penpal.sources.plugins.approved_sqlite import ApprovedSqliteSource
from penpal.sources.plugins.chrome_history import ChromeHistorySource
from penpal.sources.plugins.claude_code import ClaudeCodeSource
from penpal.sources.plugins.codex_cli import CodexCliSource
from penpal.sources.plugins.git_log import GitLogSource
from penpal.sources.plugins.knowledgec import KnowledgeCSource
from penpal.sources.plugins.markdown_vault import MarkdownVaultSource
from penpal.sources.plugins.leveldb_reader import LevelDbReaderSource
from penpal.sources.plugins.safari_history import SafariHistorySource
from penpal.sources.plugins.spotlight import SpotlightSource
from penpal.sources.plugins.wechat import WechatSource
from penpal.sources.plugins.zsh_history import ZshHistorySource
from penpal.capture.capture_control import source_writes_allowed
from penpal.foundation.capture_hygiene import host_matches_denylist
from penpal.config import Config
from penpal.sources.sink import persist_semantic_event
from penpal.sources.types import DataSource, DataSourceInstance, SemanticEvent
from penpal.utils.logging import get_logger


LOGGER = get_logger("sources.registry")

_TIER_RANK: dict[str, int] = {tier: rank for rank, tier in enumerate(PRIVACY_TIERS)}
_UNKNOWN_TIER_RANK: int = len(PRIVACY_TIERS)  # strictly higher than any known tier
_DEFAULT_MAX_TIER_RANK: int = _TIER_RANK["yellow"]

_PLUGINS: dict[str, DataSource] = {}


def _event_url(event: SemanticEvent) -> str | None:
    meta = event.metadata or {}
    for value in (meta.get("url"), meta.get("full_url"), event.artifact):
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def _is_excluded_domain(event: SemanticEvent, deny_hosts: list[str]) -> bool:
    """#26 (B6): browsing on a user-excluded domain is skipped before storage or output, as in capture."""
    url = _event_url(event)
    if not url:
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return bool(host) and host_matches_denylist(host, deny_hosts)


def _is_tier_allowed(event_tier: str | None, max_tier: str) -> bool:
    """Return True iff event tier rank <= max_tier rank.

    Unknown event tiers fail closed (always dropped) by mapping to a rank
    strictly above all known tiers. Unknown max_tier falls back to yellow.
    """
    event_rank = _TIER_RANK.get(str(event_tier or "").strip().lower(), _UNKNOWN_TIER_RANK)
    max_rank = _TIER_RANK.get(str(max_tier or "").strip().lower(), _DEFAULT_MAX_TIER_RANK)
    return event_rank <= max_rank


def register(source: DataSource) -> None:
    _PLUGINS[source.name] = source


def list_sources() -> list[DataSource]:
    return sorted(_PLUGINS.values(), key=lambda plugin: plugin.name)


def source_availability(source: str | None = None) -> list[tuple[DataSource, Availability]]:
    """Explicit available / unavailable(reason) per plugin (issue #22). A probe that raises is reported as
    probe_error for that plugin only; it never fails the others."""
    rows: list[tuple[DataSource, Availability]] = []
    for plugin in _select_sources(source):
        try:
            state = plugin.availability()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("availability failed for %s: %s", plugin.name, exc)
            state = unavailable(PROBE_ERROR)
        rows.append((plugin, state))
    return rows


def unavailable_sources(allowed: Iterable[str] | None = None) -> list[dict[str, str]]:
    """Plugins whose original could not be read right now, limited to `allowed` names when given (MCP coverage
    uses the caller's grant so nothing outside it is named). `not_found` (nothing configured to read, e.g. no
    approved vault or a plugin never set up) is not a failed read, so it is left out here; `penpal sources
    list` still shows it."""
    allowed_set = None if allowed is None else set(allowed)
    # Only cheap probes run here: this is called on every search_context / health / doctor. Plugins that rely
    # on discover() (e.g. Spotlight shelling out to mdfind) are reported only by `penpal sources list`.
    rows: list[dict[str, str]] = []
    for plugin in list_sources():
        if not plugin.cheap_probe or (allowed_set is not None and plugin.name not in allowed_set):
            continue
        try:
            state = plugin.availability()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("availability failed for %s: %s", plugin.name, exc)
            state = unavailable(PROBE_ERROR)
        if not state.available and state.reason != NOT_FOUND:
            rows.append({"source": plugin.name, "reason": state.reason or PROBE_ERROR})
    return rows


def get_source(name: str) -> DataSource | None:
    return _PLUGINS.get(name)


def discover_all(source: str | None = None) -> dict[str, list[DataSourceInstance]]:
    selected = _select_sources(source)
    discovered: dict[str, list[DataSourceInstance]] = {}
    for plugin in selected:
        try:
            discovered[plugin.name] = plugin.discover()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("discover failed for %s: %s", plugin.name, exc)
            discovered[plugin.name] = []
    return discovered


def read_all(
    since: datetime,
    until: datetime,
    *,
    source: str | None = None,
    persist_raw_events: bool = True,
) -> Iterator[SemanticEvent]:
    since_utc = _as_utc(since)
    until_utc = _as_utc(until)

    events: list[SemanticEvent] = []
    persist_failed: set[str] = set()
    discovered = discover_all(source)
    selected = _select_sources(source)
    if persist_raw_events:
        # Paused or switched-off plugins are not read at all, so their cursors do not advance (#28).
        selected = [plugin for plugin in selected if source_writes_allowed(plugin.name)]
    cleaning_config = load_cleaning_config()
    deny_hosts = Config.load().privacy.url_deny_hosts

    for plugin in selected:
        instances = discovered.get(plugin.name, [])
        for instance in instances:
            try:
                stream = plugin.read(instance, since_utc, until_utc)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("read setup failed for %s (%s): %s", plugin.name, instance.locator, exc)
                continue
            try:
                for event in stream:
                    if accept_event(plugin.name, event, cleaning_config, deny_hosts):
                        events.append(event)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("read stream failed for %s (%s): %s", plugin.name, instance.locator, exc)

    try:
        deduped = dedup_events(events, time_window_minutes=cleaning_config.dedup_time_window_minutes)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("L4 dedup failed: %s", exc)
        deduped = events
    deduped.sort(key=lambda event: event.time)
    for event in deduped:
        if persist_raw_events:
            try:
                persist_semantic_event(event)
            except Exception as exc:  # pragma: no cover - defensive
                persist_failed.add(event.source)
                LOGGER.warning("persist semantic event failed source=%s ref=%s: %s", event.source, event.raw_ref, exc)
        yield event

    # Incremental plugins advance their resume cursor only after storage.
    if persist_raw_events:
        for plugin in selected:
            commit = getattr(plugin, "commit_cursors", None)
            if commit is None:
                continue
            try:
                commit(failed=plugin.name in persist_failed)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("cursor commit failed for %s: %s", plugin.name, exc)


def accept_event(source_name: str, event: SemanticEvent, cleaning_config, deny_hosts: list[str]) -> bool:
    """Privacy tier, excluded-domain (#26) and low-signal filters shared by read_all and the batched importer (#32)."""
    if _is_excluded_domain(event, deny_hosts):
        return False
    if not _is_tier_allowed(event.privacy_tier, cleaning_config.privacy_max_tier):
        LOGGER.info(
            "privacy_tier dropped: %s actor=%s tier=%s max=%s",
            source_name, event.actor, event.privacy_tier, cleaning_config.privacy_max_tier,
        )
        return False
    try:
        is_noise, _ = is_low_signal_event(event)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("L3 filter failed for %s: %s", source_name, exc)
        is_noise = False
    return not is_noise


def _select_sources(source: str | None) -> list[DataSource]:
    if source:
        plugin = _PLUGINS.get(source)
        return [plugin] if plugin is not None else []
    return list_sources()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


register(GitLogSource(use_cursor=True))
register(ClaudeCodeSource(use_cursor=True))
register(CodexCliSource(use_cursor=True))
register(AiToolsSource(use_cursor=True))
register(ApprovedSqliteSource())
register(ChromeHistorySource())
register(KnowledgeCSource())
register(SafariHistorySource())
register(ZshHistorySource(use_cursor=True))
register(MarkdownVaultSource())
register(LevelDbReaderSource())
register(SpotlightSource())
register(WechatSource())
