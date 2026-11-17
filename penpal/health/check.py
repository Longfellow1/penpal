from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from penpal.capabilities.registry import get_default_registry
from penpal.capabilities.store import load_states, load_states_raw
from penpal.capabilities.capture_facts import CAPTURE_FACT_CAPS
from penpal.config import Config, read_config
from penpal.health.alerts import ProductAlert
from penpal.health.product_delivery import evaluate_delivery_health
from penpal.health.report import HEALTH_JSON_PATH, write_health_report
from penpal.store.db import get_conn, init_db, read_schema_version
from penpal.runtime import RuntimeState, evaluate
from penpal.health.self_heal import run_watchdog


HEALTH_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_config_from_path(path: Path) -> Config:
    return read_config(path)


def _load_config(config_path: str | None) -> tuple[Config, bool]:
    try:
        if config_path:
            return _load_config_from_path(Path(config_path)), True
        return Config.load(), True
    except Exception:
        return Config(), False


def _watcher_names(cfg: Config) -> list[str]:
    names: list[str] = []
    for field_name in ("window", "clipboard", "idle", "manual", "browser", "browser_url", "ax_text", "ocr"):
        if bool(getattr(cfg.watchers, field_name, False)):
            names.append(field_name)
    return names


def _speaker_mislabel_ratio(checked_at: datetime) -> float:
    conn = get_conn()
    cutoff = (checked_at - timedelta(hours=1)).isoformat()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN speaker='system' THEN 1 ELSE 0 END) AS system_count
        FROM raw_events
        WHERE source IN ('clipboard', 'manual', 'browser', 'browser_url')
          AND ts_start > ?
        """,
        (cutoff,),
    ).fetchone()
    total = int(row["total"] or 0)
    if total == 0:
        return 0.0
    system_count = int(row["system_count"] or 0)
    return system_count / total


def run_healthcheck(config_path: str | None = None) -> dict[str, Any]:
    from penpal.foundation.runtime_logs import cap_launchd_output_logs

    for path, exc in cap_launchd_output_logs():
        logging.getLogger("penpal.health").warning(
            "could not cap launchd output log %s: %s", path.name, type(exc).__name__
        )

    checked_at = _utc_now()
    cfg, config_loadable = _load_config(config_path)
    db_path = cfg.db_path_expanded if config_loadable else Path("/tmp/penpal-healthcheck.db")
    runtime_status = evaluate(now=checked_at)
    schema_too_new = runtime_status.state == RuntimeState.SCHEMA_TOO_NEW
    conn = None
    if not schema_too_new:
        init_db(db_path)
        conn = get_conn()
    watchdog = run_watchdog(runtime_status, now=checked_at)
    if watchdog.get("action") == "restart":
        runtime_status = evaluate(now=checked_at)
    alerts: list[dict[str, str]] = []
    for issue in runtime_status.issues:
        alerts.append({
            "severity": issue.severity,
            "code": runtime_status.code if issue.component == "daemon" else issue.component,
            "message": issue.message_zh,
        })
    if watchdog.get("budget_exhausted"):
        alerts.append({
            "severity": "error",
            "code": "WATCHDOG_RESTART_BUDGET_EXHAUSTED",
            "message": "后台服务仍异常；过去 1 小时的自动重启额度已用完，暂不再重启。",
        })
    elif watchdog.get("action") == "restart_failed":
        alerts.append({
            "severity": "error",
            "code": "WATCHDOG_RESTART_FAILED",
            "message": watchdog.get("error_zh") or "后台服务自动重启失败。",
        })

    if conn is None:
        schema_version = read_schema_version(db_path)
    else:
        schema_row = conn.execute("SELECT MAX(version) AS version FROM _schema_version").fetchone()
        schema_version = int(schema_row["version"] or 0) if schema_row else 0

    watcher_names = _watcher_names(cfg)
    if not watcher_names:
        alerts.append(
            {
                "severity": "error",
                "code": "CRITICAL_WATCHERS_DISABLED",
                "message": "关键 watcher 全部关闭，巡检与采集都会失去覆盖面",
            }
        )

    ratio = None if schema_too_new else _speaker_mislabel_ratio(checked_at)
    if ratio is not None and ratio > 0.10:
        alerts.append(
            {
                "severity": "warn",
                "code": "SPEAKER_MISLABEL_SPIKE",
                "message": "最近 1 小时 user-source 事件里 system speaker 占比过高，疑似 backfill/打标回归",
            }
        )

    if not config_loadable:
        alerts.append(
            {
                "severity": "error",
                "code": "CONFIG_LOAD_FAILED",
                "message": "配置文件无法加载，已回退到默认配置继续巡检",
            }
        )

    capabilities_snapshot, capture_error_code = ({}, "") if schema_too_new else _capabilities_snapshot()
    delivery = None if schema_too_new else evaluate_delivery_health(runtime_status=runtime_status)
    normalized_alerts = list(alerts)
    if delivery is not None:
        normalized_alerts.extend(_legacy_alerts_from_product_alerts(delivery.alerts))
    runtime_has_error = runtime_status.state in {
        RuntimeState.NOT_INSTALLED, RuntimeState.NOT_RUNNING, RuntimeState.HUNG, RuntimeState.OUTDATED,
        RuntimeState.SCHEMA_TOO_NEW,
    }
    alive = runtime_status.pid is not None and runtime_status.state not in {
        RuntimeState.NOT_INSTALLED, RuntimeState.NOT_RUNNING,
    }

    result = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "checked_at": checked_at.isoformat(),
        "overall": "alert" if runtime_has_error or any(item["severity"] in {"warn", "error"} for item in normalized_alerts) else "ok",
        "daemon": {
            "alive": alive,
            "pid": runtime_status.pid,
            "state": runtime_status.code,
        },
        "watchdog": watchdog,
        "integrity": {
            "schema_version": schema_version,
            "config_loadable": config_loadable,
            "key_watchers_enabled": watcher_names,
            "speaker_ratio_system_in_user_source_last_1h": ratio,
        },
        "alerts": normalized_alerts,
        "product_status": {
            "watcher_emit_counts_24h": delivery.watcher_counts if delivery is not None else None,
        },
        "capabilities": capabilities_snapshot,
        "capture_error_code": capture_error_code,
        "paused": runtime_status.state == RuntimeState.PAUSED,
        "disabled_sources": None if schema_too_new else _disabled_sources_snapshot(),
        "sources_unavailable": None if schema_too_new else _unavailable_sources_snapshot(),
    }
    write_health_report(HEALTH_JSON_PATH, result)
    return result


def _legacy_alerts_from_product_alerts(alerts: list[ProductAlert]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in alerts:
        if item.level == "info":
            continue
        out.append(
            {
                "severity": "error" if item.level == "critical" else "warn",
                "code": f"PRODUCT_{item.source.upper().replace(':', '_')}",
                "message": item.message,
            }
        )
    return out


def _disabled_sources_snapshot() -> list[str]:
    """Source names switched off via `penpal sources disable` (#28). Empty on any read error."""
    try:
        from penpal.capture.capture_control import disabled_sources

        return sorted(disabled_sources())
    except Exception:
        return []


def _unavailable_sources_snapshot() -> list[dict[str, str]]:
    """Source plugins that cannot be read right now, with a reason code (issue #22). Not a failure: an
    absent app's history is simply unavailable. Empty on any probe error."""
    try:
        from penpal.sources.registry import unavailable_sources

        return unavailable_sources()
    except Exception:
        return []


def _capabilities_snapshot() -> tuple[dict[str, Any], str]:
    """Read latest capability states from the state repo and derive compat fields.

    Returns (capabilities_dict, capture_error_code).
    Falls back to ({}, "") on any error so health report generation
    never fails because of capability bugs.
    """
    try:
        capabilities_raw = load_states_raw()
        states = load_states()
    except Exception:
        return {}, ""

    registry = get_default_registry()
    capture_failure = registry.select_failure(states, names=CAPTURE_FACT_CAPS) if states else None
    capture_code = "" if capture_failure is None else capture_failure.state.code

    return capabilities_raw, capture_code
