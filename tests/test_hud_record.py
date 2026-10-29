from __future__ import annotations

from datetime import datetime, timedelta, timezone

from penpal.foundation.hud_record import HudRecord, HudRecordStore, read_hud_record, write_hud_record
from penpal.foundation.runtime_identity import RuntimeIdentity


IDENTITY = RuntimeIdentity("code", "0.3.0", "config", 34, 34)
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def test_hud_record_round_trip_and_store_updates(tmp_path):
    path = tmp_path / "run" / "hud.json"
    record = HudRecord(42, "start-42", "generation-1", IDENTITY, None, None, True, True)
    write_hud_record(record, path)
    assert read_hud_record(path) == record

    store = HudRecordStore(pid=42, generation="generation-2", status_item_created=True, path=path)
    store.main_thread_tick(True)
    store.initialize("start-42", IDENTITY)
    store.model_built(NOW.isoformat())
    updated = read_hud_record(path)
    assert updated is not None
    assert updated.generation == "generation-2" and updated.identity == IDENTITY
    assert updated.last_model_built_at == NOW.isoformat()
    assert updated.last_main_thread_tick_at is not None and updated.status_item_visible is True
    assert list(path.parent.iterdir()) == [path]


def test_hud_record_rejects_invalid_timestamp_and_visibility_type(tmp_path):
    path = tmp_path / "hud.json"
    path.write_text(
        '{"pid":42,"process_start_time":"start","generation":"g","identity":{},'
        '"last_model_built_at":"yesterday","last_main_thread_tick_at":null,'
        '"status_item_created":true,"status_item_visible":"yes","record_format_version":1}',
        encoding="utf-8",
    )
    assert read_hud_record(path) is None


def test_hud_record_staleness_uses_model_tick_not_model_build_time():
    from penpal.runtime.evaluate import _hud_status

    record = HudRecord(
        42, "start-42", "generation-1", IDENTITY, NOW.isoformat(),
        (NOW - timedelta(seconds=46)).isoformat(), True, True,
    )
    status, issues = _hud_status(record, now=NOW, process_start_reader=lambda _pid: "start-42")
    assert status is not None and status.alive
    assert status.last_model_built_at == NOW.isoformat()
    assert [issue.message_zh for issue in issues] == ["HUD 无响应：主线程超过 45 秒没有响应。后台服务会自动重启它；也可运行 `penpal doctor --fix` 立即重启。"]
