from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from penpal.sources.plugins.codex_cli import CodexCliSource


def test_codex_cli_discover_and_read(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    history = home / ".codex" / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    long_prompt = "first prompt " + ("x" * 2300)
    history.write_text(
        "\n".join(
            [
                json.dumps({"session_id": "s-1", "ts": 1774855914, "text": long_prompt}),
                "{bad json}",
                json.dumps({"session_id": "s-2", "ts": 1774856914, "text": "second prompt"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    source = CodexCliSource()
    instances = source.discover()

    assert len(instances) == 1
    instance = instances[0]
    assert instance.plugin == "codex_cli"
    assert instance.locator == str(history.resolve())

    events = list(
        source.read(
            instance,
            datetime.fromtimestamp(1774855800, tz=timezone.utc),
            datetime.fromtimestamp(1774856000, tz=timezone.utc),
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event.source == "codex_cli"
    assert event.actor == "user"
    assert event.intent == long_prompt
    assert len(event.intent) == len(long_prompt)
    assert event.artifact == "codex:session:s-1"
    assert event.raw_ref == "codex:history:1"
    assert event.metadata == {"session_id": "s-1"}


def test_codex_cli_discover_returns_empty_when_missing(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    source = CodexCliSource()

    assert source.discover() == []


def test_codex_session_records_normalized_project_folder(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    rollout = sessions / "2026" / "04" / "23" / "rollout.jsonl"
    rows = [
        {"timestamp": "2026-04-23T09:00:00Z", "ordinal": 1, "type": "session_meta",
         "payload": {"session_id": "s-4", "cwd": "/tmp/proj/cx-s4"}},
        {"timestamp": "2026-04-23T09:00:00Z", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "private note"}]}},
    ]
    rollout.parent.mkdir(parents=True)
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    source = CodexCliSource(sessions_root=sessions)
    instance = source.discover()[0]
    events = list(source.read(
        instance, datetime(2026, 4, 23, 0, tzinfo=timezone.utc), datetime(2026, 4, 24, 0, tzinfo=timezone.utc)
    ))
    assert events[0].metadata["project"] == "cx-s4"
    assert events[0].metadata["project_dir"] == "/tmp/proj/cx-s4"
