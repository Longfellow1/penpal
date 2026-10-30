from __future__ import annotations

from types import SimpleNamespace

from penpal.hud.approval_window import (
    ApprovalDataController,
    PendingCandidateRow,
    list_pending_candidates,
    safe_pending_candidate_count,
)
from penpal.foundation.approval_store import ApprovalStore
from penpal.sources.discoverers import CandidateSource


def _candidate(path: str, *, app_hint: str = "App", discoverer: str = "sqlite") -> CandidateSource:
    return CandidateSource(
        discoverer=discoverer,
        path=path,
        app_hint=app_hint,
        schema_signature="sig",
        hint_tables=["events", "sessions"],
        hint_fields=["title", "body"],
    )


def test_list_pending_candidates_filters_approved_rejected_and_excluded(tmp_path):
    store = ApprovalStore(path=tmp_path / "sources-approval.json")

    pending = _candidate("/tmp/pending.db", app_hint="Pending")
    approved = _candidate("/tmp/approved.db", app_hint="Approved")
    rejected = _candidate("/tmp/rejected.db", app_hint="Rejected")
    excluded = _candidate("/tmp/excluded.db", app_hint="Excluded")

    store.approve(approved, note="ok")
    store.reject(rejected, reason="skip")

    discovered_instances = {
        "approved_sqlite": [SimpleNamespace(locator=excluded.path)],
    }

    discovered_candidates = {
        "sqlite": [pending, approved, excluded],
        "jsonl": [rejected],
    }

    rows = list_pending_candidates(
        approval_store=store,
        discover_all_fn=lambda: discovered_instances,
        discover_candidates_fn=lambda *, exclude_paths: {
            key: [candidate for candidate in values if candidate.path not in exclude_paths]
            for key, values in discovered_candidates.items()
        },
    )

    assert len(rows) == 1
    assert rows[0].app_name == "Pending"
    assert rows[0].path == "/tmp/pending.db"


def test_safe_pending_candidate_count_returns_error_message(tmp_path):
    store = ApprovalStore(path=tmp_path / "sources-approval.json")

    count, error = safe_pending_candidate_count(
        approval_store=store,
        discover_all_fn=lambda: {},
        discover_candidates_fn=lambda *, exclude_paths: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert count is None
    assert "boom" in (error or "")


def test_approval_data_controller_approve_and_reject_flow(tmp_path):
    store = ApprovalStore(path=tmp_path / "sources-approval.json")

    first = _candidate("/tmp/first.db", app_hint="First")
    second = _candidate("/tmp/second.db", app_hint="Second")

    rows = [
        PendingCandidateRow(candidate_id=store.candidate_id(first), candidate=first),
        PendingCandidateRow(candidate_id=store.candidate_id(second), candidate=second),
    ]

    controller = ApprovalDataController(
        approval_store=store,
        list_pending_fn=lambda *, approval_store: list(rows),
    )

    controller.reload()
    assert controller.count() == 2

    assert controller.approve_at(0, note="go") is True
    assert controller.count() == 1
    assert [record.candidate_id for record in store.list_approved()] == [store.candidate_id(first)]

    assert controller.reject_at(0, reason="no") is True
    assert controller.count() == 0
    assert [record.candidate_id for record in store.list_rejected()] == [store.candidate_id(second)]


def test_approval_data_load_leaves_rows_alone_until_applied(tmp_path):
    # load() runs on the scan thread; only apply(), on the main thread, changes what the table shows.
    rows = [SimpleNamespace(app_name="Example", path="/tmp/example.db")]
    data = ApprovalDataController(
        approval_store=ApprovalStore(path=tmp_path / "sources-approval.json"),
        list_pending_fn=lambda **_kwargs: rows,
    )
    loaded = data.load()
    assert data.rows == [] and data.count() == 0
    assert data.apply(loaded) == rows and data.count() == 1 and data.last_error is None
