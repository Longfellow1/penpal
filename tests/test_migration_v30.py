"""Migration v30: evidence for old markdown_vault rows, resolved via approved vault roots.
Synthetic fixtures use tmp dirs and an in-memory approval record."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from penpal.foundation.approval_store import ApprovalRecord, ApprovalStore
from penpal.store.db import close, get_conn, init_db
from penpal.store.migration_v30 import migrate_v30
from penpal.store.migrations import MIGRATIONS, SCHEMA_VERSION, run_migrations
from penpal.store.models import RawEvent
from penpal.store.repository import insert_raw_event


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    init_db(tmp_path / "t.db")
    yield tmp_path
    close()


def _discover_as(monkeypatch, roots: list[Path]) -> None:
    """Stand-in for approved vaults: one persisted approval record per existing root."""
    def fake(self):
        return [
            ApprovalRecord(
                candidate_id=str(i), status="approved", timestamp=None,
                metadata={"discoverer": "markdown_vault", "path": str(root.resolve()),
                          "shape": "document_file"},
            )
            for i, root in enumerate(roots) if root.is_dir()
        ]
    monkeypatch.setattr(ApprovalStore, "list_approved", fake)


def _old_row(vault: str, artifact: str | None, text: str = "old note") -> int:
    """A markdown_vault row as ingested before M2: vault name + vault-relative path, no absolute path."""
    meta = {"vault_name": vault, "raw_ref": f"markdown_vault:{vault}:{artifact}", "shape": "document_file"}
    if artifact is not None:
        meta["artifact"] = artifact
    n = get_conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    return insert_raw_event(RawEvent(
        source="markdown_vault", event_type="semantic_event", ts_start="2026-03-01T00:00:00+00:00",
        content_text=text, content_hash=f"v30-{n}-{artifact}", metadata_json=json.dumps(meta), bucket="Work",
    ))


def _evidence_of(event_id: int):
    rows = get_conn().execute(
        "SELECT locator, offset_or_line, available FROM evidence WHERE event_id = ?", (event_id,)
    ).fetchall()
    return tuple(rows[0]) if rows else None


def test_v30_resolves_old_row_to_the_file_under_its_vault(db, monkeypatch):
    vault = db / "Knowledge"
    note = vault / "dir" / "sub" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\nbody\n")
    _discover_as(monkeypatch, [vault])
    rid = _old_row("Knowledge", "dir/sub/note.md")
    get_conn().execute("DELETE FROM evidence")

    stats = migrate_v30(get_conn())
    get_conn().commit()

    assert _evidence_of(rid) == (str(note.resolve()), None, 1)
    assert stats == {"markdown_vault": (1, 0)}


def test_v30_missing_file_writes_no_evidence(db, monkeypatch):
    vault = db / "Knowledge"
    vault.mkdir()
    _discover_as(monkeypatch, [vault])
    rid = _old_row("Knowledge", "gone/deleted.md")
    get_conn().execute("DELETE FROM evidence")

    stats = migrate_v30(get_conn())

    assert _evidence_of(rid) is None
    assert stats == {"markdown_vault": (0, 1)}


def test_v30_rejects_dot_dot_escape_out_of_the_vault(db, monkeypatch):
    vault = db / "Knowledge"
    vault.mkdir()
    (db / "outside.md").write_text("not in the vault\n")  # reachable only by escaping the root
    _discover_as(monkeypatch, [vault])
    rid = _old_row("Knowledge", "../outside.md")
    absolute = _old_row("Knowledge", str(db / "outside.md"))
    get_conn().execute("DELETE FROM evidence")

    migrate_v30(get_conn())

    assert _evidence_of(rid) is None
    assert _evidence_of(absolute) is None


def test_v30_skips_a_row_whose_vault_is_unknown(db, monkeypatch):
    vault = db / "Knowledge"
    (vault / "a.md").parent.mkdir(parents=True)
    (vault / "a.md").write_text("x\n")
    _discover_as(monkeypatch, [vault])
    rid = _old_row("Elsewhere", "a.md")
    get_conn().execute("DELETE FROM evidence")

    stats = migrate_v30(get_conn())

    assert _evidence_of(rid) is None
    assert stats == {"markdown_vault": (0, 1)}


def test_v30_skips_a_vault_name_shared_by_two_roots(db, monkeypatch):
    first, second = db / "one" / "Knowledge", db / "two" / "Knowledge"
    for root in (first, second):
        root.mkdir(parents=True)
        (root / "a.md").write_text("x\n")
    _discover_as(monkeypatch, [first, second])
    rid = _old_row("Knowledge", "a.md")
    get_conn().execute("DELETE FROM evidence")

    migrate_v30(get_conn())

    assert _evidence_of(rid) is None


def test_v30_rerun_is_a_no_op(db, monkeypatch):
    vault = db / "Knowledge"
    vault.mkdir()
    (vault / "a.md").write_text("x\n")
    _discover_as(monkeypatch, [vault])
    rid = _old_row("Knowledge", "a.md")
    get_conn().execute("DELETE FROM evidence")
    migrate_v30(get_conn())
    first = get_conn().execute("SELECT id, event_id, locator FROM evidence ORDER BY id").fetchall()

    stats = migrate_v30(get_conn())

    assert get_conn().execute("SELECT id, event_id, locator FROM evidence ORDER BY id").fetchall() == first
    assert stats == {}  # no candidate left: discovery is not even run
    assert _evidence_of(rid) is not None


def test_v30_is_registered_as_schema_version_30(db):
    conn = get_conn()
    assert SCHEMA_VERSION == len(MIGRATIONS)
    conn.execute("DELETE FROM _schema_version WHERE version >= 30")  # re-run v30 and v31 on top
    conn.commit()
    run_migrations(conn)
    run_migrations(conn)
    assert conn.execute("SELECT COUNT(*) FROM _schema_version WHERE version = 30").fetchone()[0] == 1
    assert conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0] == SCHEMA_VERSION
