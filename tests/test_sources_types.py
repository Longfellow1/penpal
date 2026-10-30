from __future__ import annotations

from datetime import datetime, timezone

import pytest

from penpal.sources.types import (
    ContentShape,
    DataSource,
    DataSourceInstance,
    SemanticEvent,
    classify_fields,
    confidence_from_categories,
)


class _DummySource(DataSource):
    name = "dummy"
    privacy_tier = "green"
    liveness = "always"

    def discover(self) -> list[DataSourceInstance]:
        return [
            DataSourceInstance(
                plugin=self.name,
                locator="/tmp/dummy",
                label="dummy",
            )
        ]

    def read(self, instance: DataSourceInstance, since: datetime, until: datetime):
        yield SemanticEvent(
            time=since,
            source=self.name,
            actor="tester",
            intent="test",
            artifact="artifact",
            raw_ref="raw",
            privacy_tier=self.privacy_tier,
            metadata={"locator": instance.locator},
        )


def test_semantic_event_normalizes_to_utc() -> None:
    event = SemanticEvent(
        time=datetime.fromisoformat("2026-04-28T10:20:00+08:00"),
        source="git_log",
        actor="alice",
        intent="Add source registry",
        artifact="commit:abcdef1",
        raw_ref="git:penpal:abcdef123456",
        privacy_tier="green",
        metadata={"repo_path": "/tmp/repo"},
    )

    assert event.time.tzinfo is not None
    assert event.time.utcoffset() == timezone.utc.utcoffset(event.time)
    assert event.time.isoformat() == "2026-04-28T02:20:00+00:00"


def test_semantic_event_rejects_naive_time() -> None:
    with pytest.raises(ValueError):
        SemanticEvent(
            time=datetime(2026, 4, 28, 10, 20, 0),
            source="git_log",
            actor="alice",
            intent="Add source registry",
            artifact="commit:abcdef1",
            raw_ref="git:penpal:abcdef123456",
            privacy_tier="green",
            metadata={},
        )


def test_data_source_instance_defaults_metadata() -> None:
    instance = DataSourceInstance(plugin="git_log", locator="/tmp/repo", label="repo (main)")

    assert instance.metadata == {}


def test_data_source_contract_shape() -> None:
    source = _DummySource()
    instances = source.discover()

    assert len(instances) == 1
    events = list(source.read(instances[0], datetime.now(timezone.utc), datetime.now(timezone.utc)))
    assert len(events) == 1
    assert events[0].source == "dummy"


def test_classify_fields_groups_keywords() -> None:
    classified = classify_fields(["role", "message", "session_id", "foo", "path"])
    assert set(classified.keys()) >= {"ai_dialog", "intent_text", "session", "artifact"}
    assert classified["ai_dialog"] == ["role"]


def test_classify_fields_accepts_camel_case_variants() -> None:
    classified = classify_fields(["createdAt", "updated-at", "sessionId"])
    assert "time" in classified
    assert set(classified["time"]) == {"createdAt", "updated-at"}
    assert "session" in classified
    assert classified["session"] == ["sessionId"]


def test_confidence_from_categories_mapping() -> None:
    assert confidence_from_categories(0) == "low"
    assert confidence_from_categories(1) == "medium"
    assert confidence_from_categories(3) == "high"


def test_content_shape_kv_json_blob_to_event() -> None:
    event = ContentShape.KV_JSON_BLOB.to_semantic_event(
        {
            "key": "chat:1",
            "value": {
                "timestamp": int(datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc).timestamp()),
                "role": "assistant",
                "text": "hello",
            },
            "artifact": "Cursor:cursorDiskKV:chat:1",
            "raw_ref": "approved_sqlite:abc:cursorDiskKV:chat:1",
        },
        source="approved_sqlite",
        privacy_tier="yellow",
    )

    assert event is not None
    assert event.actor == "assistant"
    assert event.intent == "hello"
    assert event.metadata["shape"] == "kv_json_blob"


def test_content_shape_document_file_to_event() -> None:
    event = ContentShape.DOCUMENT_FILE.to_semantic_event(
        {
            "mtime": datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc),
            "intent": "Daily",
            "artifact": "Daily/2026-04-28.md",
            "path": "/tmp/vault/Daily/2026-04-28.md",
            "raw_ref": "markdown_vault:Knowledge:Daily/2026-04-28.md",
        },
        source="markdown_vault",
        privacy_tier="yellow",
    )

    assert event is not None
    assert event.actor == "user"
    assert event.intent == "Daily"
    assert event.metadata["shape"] == "document_file"
