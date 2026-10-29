"""Test codex_cli reading .jsonl.zst and archived_sessions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import zstandard

from penpal.sources.plugins.codex_cli import CodexCliSource
from penpal.sources.sink import persist_semantic_event
from penpal.store.db import get_conn, init_db

SID_1 = "0199aaaa-1111-2222-3333-444455556666"
SID_2 = "0199bbbb-2222-3333-4444-555566667777"


def _rec(ordinal: int, ts: str, rtype: str, payload: dict) -> str:
    return json.dumps({"timestamp": ts, "ordinal": ordinal, "type": rtype, "payload": payload})


def _msg(role: str, text: str) -> dict:
    return {"type": "message", "role": role, "content": [{"type": "text", "text": text}]}


def _make_rollout_lines(sid: str) -> list[str]:
    return [
        _rec(1, "2026-05-01T08:00:00.000Z", "session_meta", {"session_id": sid, "id": sid, "cwd": "/work/test"}),
        _rec(2, "2026-05-01T08:00:01.000Z", "response_item", _msg("user", "Hello Codex")),
        _rec(3, "2026-05-01T08:00:02.000Z", "response_item", _msg("assistant", "Hello human")),
    ]


def _write_zst(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cctx = zstandard.ZstdCompressor()
    compressed = cctx.compress(("\n".join(lines) + "\n").encode("utf-8"))
    path.write_bytes(compressed)


def test_codex_cli_archive_does_not_duplicate_rows(tmp_path: Path):
    """Defect 1: Moving a session from sessions/ to archived_sessions/ must not duplicate rows."""
    init_db(tmp_path / "penpal.db")
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "05" / "01"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    archived_dir = codex_home / "archived_sessions" / "2026" / "05" / "01"
    archived_dir.mkdir(parents=True, exist_ok=True)

    rollout_file = sessions_dir / f"rollout-2026-05-01T08-00-00-{SID_1}.jsonl"
    rollout_file.write_text("\n".join(_make_rollout_lines(SID_1)) + "\n", encoding="utf-8")

    source = CodexCliSource(
        sessions_root=codex_home / "sessions",
        archived_sessions_root=codex_home / "archived_sessions",
        use_cursor=True,
    )
    # 1. Exactly one instance discovered (with the sessions locator)
    instances = source.discover()
    assert len(instances) == 1
    assert instances[0].locator == str((codex_home / "sessions").resolve())

    # First sync: ingest from sessions/
    now = datetime.now(timezone.utc)
    for event in source.read(instances[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now):
        persist_semantic_event(event)
    source.commit_cursors()

    conn = get_conn()
    count_1 = conn.execute("SELECT count(*) FROM raw_events WHERE source = 'codex_cli'").fetchone()[0]
    assert count_1 == 2

    # Move rollout file to archived_sessions/ and compress to .zst
    archived_zst = archived_dir / f"rollout-2026-05-01T08-00-00-{SID_1}.jsonl.zst"
    _write_zst(archived_zst, _make_rollout_lines(SID_1))
    rollout_file.unlink()

    # Second sync: re-read using a new source instance (upgrade / restart)
    source2 = CodexCliSource(
        sessions_root=codex_home / "sessions",
        archived_sessions_root=codex_home / "archived_sessions",
        use_cursor=True,
    )
    insts2 = source2.discover()
    assert len(insts2) == 1
    for event in source2.read(insts2[0], datetime(2020, 1, 1, tzinfo=timezone.utc), now):
        persist_semantic_event(event)
    source2.commit_cursors()

    count_2 = conn.execute("SELECT count(*) FROM raw_events WHERE source = 'codex_cli'").fetchone()[0]
    # Row count must remain unchanged (no duplicate rows)
    assert count_2 == count_1


def test_codex_cli_stable_ids_plain_and_zst(tmp_path: Path):
    """Defect 3: rollout-X.jsonl and rollout-X.jsonl.zst yield identical external_ids."""
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "05" / "01"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    plain_file = sessions_dir / f"rollout-2026-05-01T08-00-00-{SID_1}.jsonl"
    plain_file.write_text("\n".join(_make_rollout_lines(SID_1)) + "\n", encoding="utf-8")

    source = CodexCliSource(sessions_root=codex_home / "sessions")
    inst = source.discover()[0]
    plain_events = list(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)))

    # Now replace with zst
    plain_file.unlink()
    zst_file = sessions_dir / f"rollout-2026-05-01T08-00-00-{SID_1}.jsonl.zst"
    _write_zst(zst_file, _make_rollout_lines(SID_1))

    zst_events = list(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)))

    assert len(plain_events) == len(zst_events)
    assert [e.external_id for e in plain_events] == [e.external_id for e in zst_events]
    assert [e.artifact for e in plain_events] == [e.artifact for e in zst_events]


def test_codex_cli_zst_reread_starts_variables_fresh(tmp_path: Path):
    """Defect 2: When a .zst file is re-read from line 1, scan variables start fresh."""
    init_db(tmp_path / "penpal.db")
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "05" / "01"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    zst_file = sessions_dir / f"rollout-2026-05-01T08-00-00-{SID_1}.jsonl.zst"

    lines1 = [
        _rec(1, "2026-05-01T08:00:00.000Z", "session_meta", {"session_id": SID_1, "id": SID_1, "cwd": "/work/project1"}),
        _rec(2, "2026-05-01T08:00:01.000Z", "response_item", _msg("user", "Hello 1")),
    ]
    _write_zst(zst_file, lines1)

    source = CodexCliSource(sessions_root=codex_home / "sessions", use_cursor=True)
    inst = source.discover()[0]
    now = datetime.now(timezone.utc)
    evts1 = list(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(evts1) == 1
    assert evts1[0].metadata.get("cwd") == "/work/project1"
    source.commit_cursors()

    # Rewrite file with smaller size (re-read from line 1) without cwd in session_meta
    lines2 = [
        _rec(1, "2026-05-01T09:00:00.000Z", "session_meta", {"session_id": SID_1, "id": SID_1}),
        _rec(2, "2026-05-01T09:00:01.000Z", "response_item", _msg("user", "Hello fresh")),
    ]
    _write_zst(zst_file, lines2)

    evts2 = list(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), now))
    assert len(evts2) == 1
    # cwd must be None, not inherited from run 1


def test_codex_subagent_messages_survive_store_roundtrip(tmp_path: Path):
    """Item 1: Two sub-agent files sharing session_id with parent survive store round trip."""
    init_db(tmp_path / "penpal.db")
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "05" / "01"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    parent_id = "0199aaaa-0000-0000-0000-000000000001"
    sub1_id = "0199aaaa-0000-0000-0000-000000000002"
    sub2_id = "0199aaaa-0000-0000-0000-000000000003"

    parent_file = sessions_dir / f"rollout-2026-05-01T08-00-00-{parent_id}.jsonl"
    sub1_file = sessions_dir / f"rollout-2026-05-01T08-01-00-{sub1_id}.jsonl"
    sub2_file = sessions_dir / f"rollout-2026-05-01T08-02-00-{sub2_id}.jsonl"

    def _sub_lines(tid: str, role_suffix: str) -> list[str]:
        return [
            _rec(1, "2026-05-01T08:00:00.000Z", "session_meta", {"session_id": parent_id, "id": tid, "cwd": "/work/test"}),
            _rec(1, "2026-05-01T08:00:01.000Z", "response_item", _msg("user", f"User {role_suffix}")),
            _rec(2, "2026-05-01T08:00:02.000Z", "response_item", _msg("assistant", f"Assistant {role_suffix}")),
        ]

    parent_file.write_text("\n".join(_sub_lines(parent_id, "parent")) + "\n", encoding="utf-8")
    sub1_file.write_text("\n".join(_sub_lines(sub1_id, "sub1")) + "\n", encoding="utf-8")
    sub2_file.write_text("\n".join(_sub_lines(sub2_id, "sub2")) + "\n", encoding="utf-8")

    source = CodexCliSource(sessions_root=codex_home / "sessions", use_cursor=True)
    inst = source.discover()[0]
    now = datetime.now(timezone.utc)
    for event in source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), now):
        persist_semantic_event(event)
    source.commit_cursors()

    conn = get_conn()
    total_stored = conn.execute("SELECT count(*) FROM raw_events WHERE source = 'codex_cli'").fetchone()[0]
    # All 6 messages (2 from parent, 2 from sub1, 2 from sub2) must survive
    assert total_stored == 6


def test_codex_main_threads_external_id_and_artifact_unchanged(tmp_path: Path):
    """Item 1: Main threads (session_id == id == filename id) produce byte-identical external_id and artifact."""
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "05" / "01"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    mid = "0199aaaa-0000-0000-0000-000000000001"
    main_file = sessions_dir / f"rollout-2026-05-01T08-00-00-{mid}.jsonl"
    lines = [
        _rec(1, "2026-05-01T08:00:00.000Z", "session_meta", {"session_id": mid, "id": mid, "cwd": "/work/test"}),
        _rec(1, "2026-05-01T08:00:01.000Z", "response_item", _msg("user", "Hello main")),
    ]
    main_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    source = CodexCliSource(sessions_root=codex_home / "sessions")
    inst = source.discover()[0]
    events = list(source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)))
    assert len(events) == 1
    ev = events[0]
    assert ev.external_id == f"{mid}:1"
    assert ev.artifact == f"codex:session:{mid}:msg:1"
    assert ev.metadata["session_id"] == mid
    assert ev.metadata["thread_id"] == mid


def test_codex_coverage_counts_threads_not_only_sessions(tmp_path: Path):
    """Item 2: Coverage for Codex counts distinct thread_id, matching sessions_on_disk."""
    from penpal.sources.coverage import compute_tool_coverage

    init_db(tmp_path / "penpal.db")
    codex_home = tmp_path / "codex"
    sessions_dir = codex_home / "sessions" / "2026" / "05" / "01"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    parent_id = "0199aaaa-0000-0000-0000-000000000001"
    sub1_id = "0199aaaa-0000-0000-0000-000000000002"

    def _make_f(tid: str):
        f = sessions_dir / f"rollout-2026-05-01T08-00-00-{tid}.jsonl"
        lines = [
            _rec(1, "2026-05-01T08:00:00.000Z", "session_meta", {"session_id": parent_id, "id": tid}),
            _rec(1, "2026-05-01T08:00:01.000Z", "response_item", _msg("user", f"Hi from {tid}")),
        ]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _make_f(parent_id)
    _make_f(sub1_id)

    source = CodexCliSource(sessions_root=codex_home / "sessions")
    inst = source.discover()[0]
    for ev in source.read(inst, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc)):
        persist_semantic_event(ev)

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "codex_cli.toml").write_text(
        'id = "codex_cli"\nname = "Codex CLI"\nkind = "agent"\nreadable = true\nadapter = "codex_cli"\n[detect]\nany = []\n',
        encoding="utf-8",
    )

    rows, totals = compute_tool_coverage(catalog_dir=catalog_dir, env={"CODEX_HOME": str(codex_home)})
    by_id = {r["id"]: r for r in rows}
    assert by_id["codex_cli"]["ingested_sessions"] == 2
