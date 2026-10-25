from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from penpal.sources.plugins.claude_code import ClaudeCodeSource
from penpal.sources.types import DataSourceInstance


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_discover_claude_projects(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    project_dir = home / ".claude" / "projects" / "-Users-someone-Work-penpal"
    _write_jsonl(project_dir / "session-a.jsonl", [{"type": "user", "timestamp": "2026-04-28T01:00:00Z"}])
    _write_jsonl(project_dir / "session-b.jsonl", [{"type": "assistant", "timestamp": "2026-04-28T02:00:00Z"}])

    source = ClaudeCodeSource()
    instances = source.discover()

    assert len(instances) == 1
    instance = instances[0]
    assert instance.plugin == "claude_code"
    assert instance.locator == str(project_dir.resolve())
    assert instance.label == "penpal"
    assert instance.metadata["project_path"] == "/Users/someone/Work/penpal"
    assert instance.metadata["project"] == "-Users-someone-Work-penpal"
    assert instance.metadata["session_count"] == 2


def test_read_claude_jsonl_handles_dict_and_stringified_message(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    project_dir = home / ".claude" / "projects" / "-Users-someone-Work-penpal"
    session_file = project_dir / "abc-session.jsonl"
    rows = [
        {
            "type": "user",
            "uuid": "u-1",
            "parentUuid": None,
            "timestamp": "2026-04-28T01:00:00Z",
            "message": {"role": "user", "content": "run tests now"},
        },
        {
            "type": "assistant",
            "uuid": "a-1",
            "parentUuid": "u-1",
            "timestamp": "2026-04-28T01:10:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "tests passed with fixes"},
                    {
                        "type": "tool_result",
                        "content": [
                            {
                                "type": "text",
                                "text": "1\t- [大道至简](feedback_dadao_zhijian.md) — should be ignored",
                            }
                        ],
                    },
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u-2",
            "parentUuid": "a-1",
            "timestamp": "2026-04-28T01:20:00Z",
            "message": "{'role': 'user', 'content': 'string payload'}",
        },
        {
            "type": "assistant",
            "uuid": "a-2",
            "parentUuid": "u-2",
            "timestamp": "2026-04-28T01:25:00Z",
            "message": {"role": "assistant", "content": "a" * 2300},
        },
        {
            "type": "assistant",
            "uuid": "a-old",
            "parentUuid": "u-0",
            "timestamp": "2026-04-27T23:59:59Z",
            "message": {"role": "assistant", "content": "out of range"},
        },
        {
            "type": "tool_result",
            "timestamp": "2026-04-28T01:30:00Z",
            "message": {"content": "skip"},
        },
    ]
    _write_jsonl(session_file, rows)

    instance = DataSourceInstance(plugin="claude_code", locator=str(project_dir.resolve()), label="penpal")
    source = ClaudeCodeSource()

    events = list(
        source.read(
            instance,
            datetime(2026, 4, 28, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 1, 30, tzinfo=timezone.utc),
        )
    )

    assert len(events) == 4
    assert [event.actor for event in events] == ["user", "assistant", "user", "assistant"]
    assert events[0].intent == "run tests now"
    assert events[1].intent == "tests passed with fixes"
    assert events[2].intent == "string payload"
    assert events[1].intent != "1\t- [大道至简](feedback_dadao_zhijian.md) — should be ignored"
    assert len(events[3].intent) == 2300
    assert events[0].artifact == "claude:session:abc-session:msg:u-1"
    assert events[1].metadata["parent_uuid"] == "u-1"
    assert events[0].metadata["project"] == "-Users-someone-Work-penpal"
    assert events[0].metadata["project_dir"] == str(project_dir.resolve())
    assert events[2].raw_ref.endswith(":3")


def test_discover_claude_projects_returns_empty_when_missing(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    source = ClaudeCodeSource()

    assert source.discover() == []


def test_self_exclude_skips_keypulse_own_project_dir(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    skip_dir = home / ".claude" / "projects" / "-Users-Harland-Go-penpal"
    keep_dir = home / ".claude" / "projects" / "-Users-someone-Work-other-app"
    _write_jsonl(skip_dir / "s1.jsonl", [{"type": "user", "timestamp": "2026-04-28T01:00:00Z"}])
    _write_jsonl(keep_dir / "s1.jsonl", [{"type": "user", "timestamp": "2026-04-28T01:00:00Z"}])

    source = ClaudeCodeSource(self_exclude_dirs={"-Users-Harland-Go-penpal"})

    instances = source.discover()
    assert len(instances) == 1
    assert instances[0].locator == str(keep_dir.resolve())

    # read() also returns empty when called on an excluded instance directly
    excluded_instance = DataSourceInstance(
        plugin="claude_code", locator=str(skip_dir.resolve()), label="penpal"
    )
    events = list(
        source.read(
            excluded_instance,
            datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 23, 59, tzinfo=timezone.utc),
        )
    )
    assert events == []


def test_default_does_not_exclude_keypulse_named_project_dir(monkeypatch, tmp_path: Path) -> None:
    # Regression: an editable install used to exclude the user's main Claude project (0 events).
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    main_dir = home / ".claude" / "projects" / "-Users-Harland-Go-penpal"
    _write_jsonl(main_dir / "s1.jsonl", [{"type": "user", "timestamp": "2026-04-28T01:00:00Z"}])

    source = ClaudeCodeSource()

    assert [i.locator for i in source.discover()] == [str(main_dir.resolve())]


def test_claude_rows_record_the_real_folder_name_from_cwd(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    project_dir = home / ".claude" / "projects" / "-Users-someone-life-admin"
    project_dir.mkdir(parents=True)
    row = {"type": "user", "uuid": "u-1", "parentUuid": None, "timestamp": "2026-04-28T01:00:00Z",
           "cwd": "/Users/someone/life-admin", "message": {"role": "user", "content": "compare card fees"}}
    (project_dir / "s.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    source = ClaudeCodeSource()
    since = datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    events = [event for instance in source.discover() for event in source.read(instance, since, until)]
    assert [event.metadata["project"] for event in events] == ["life-admin"]
