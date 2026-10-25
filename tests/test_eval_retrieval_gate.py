"""E3 retrieval eval harness: fixtures ingest cleanly and the 2pp regression gate really blocks."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from evals import compare
from evals.fixture_data import CAPTURE_EVENTS, EVENTS, FAKE_KEY
from evals.run import load_questions, run_eval

EVALS = Path(__file__).resolve().parents[1] / "evals"
BASELINE = EVALS / "baseline.json"


@pytest.fixture(scope="module")
def report() -> dict:
    return run_eval()


@pytest.fixture
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_every_p0_source_and_bucket_is_covered_by_synthetic_fixtures(report) -> None:
    cov = report["fixtures"]["coverage_source_x_bucket"]
    assert set(cov) == {"claude_code", "codex_cli", "git_log", "zsh_history", "chrome_history", "safari_history", "markdown_vault"}
    for bucket in ("Work", "Personal", "Private", "Financial"):
        assert sum(row[bucket] for row in cov.values()) >= 3
    assert report["fixtures"]["ingested_rows"] == len(EVENTS) + len(CAPTURE_EVENTS)
    capture = [r for r in report["questions"] if r.get("scenario") == "C01"]
    assert len(capture) >= 10 and all(r["hit"] for r in capture)  # D26: answerable only from capture streams
    assert set(report["fixtures"]["sources_wired"]) == set(cov) | {e.source for e in CAPTURE_EVENTS}


def test_question_set_has_required_shape() -> None:
    qs = load_questions()
    assert len(qs) >= 30
    scenarios = {s: sum(1 for q in qs if q.get("scenario") == s) for s in ("W01", "W02", "L01", "L02", "cross")}
    assert all(n >= 8 for n in scenarios.values()), scenarios
    variants = {variant: sum(q["variant"] == variant for q in qs) for variant in
                ("answerable", "no_answer", "restricted", "conflict")}
    assert variants["answerable"] >= 50, variants
    assert variants["no_answer"] >= 15, variants
    assert variants["restricted"] >= 10, variants
    assert variants["conflict"] >= 5, variants


def test_run_is_isolated_from_real_home(report) -> None:
    # the run must restore HOME and leave no fixture plugin registered
    from penpal.sources import registry

    assert "penpal-eval-" not in os.environ.get("HOME", "")
    assert not any("penpal-eval-" in str(getattr(p, "_history_path", "")) for p in registry._PLUGINS.values())


def test_project_rule_variant_eliminates_known_coding_agent_default_leaks(report) -> None:
    summary = report["summary"]
    assert summary["restricted_default_leaks"] == 3
    assert summary["restricted_project_evaluated"] == 3
    assert summary["restricted_project_leaks"] == 0


def test_fake_secret_never_searchable_in_eval(report) -> None:
    row = next(q for q in report["questions"] if q["id"] == "n08")
    assert row["status"] == "no_results" and not row["hit"]
    assert FAKE_KEY not in json.dumps(report)


def test_current_results_match_the_approved_baseline(report, baseline) -> None:
    assert report["question_set_digest"] == baseline["question_set_digest"], "eval set changed: baseline needs owner approval"
    assert compare.check(report, baseline) == []


def test_gate_tolerates_up_to_2pp_and_blocks_beyond(report, baseline) -> None:
    ok = copy.deepcopy(report)
    ok["question_set_digest"] = baseline["question_set_digest"]
    ok["summary"]["m1_evidence_recall_at_5"] = baseline["m1_evidence_recall_at_5"] - 0.02
    ok["summary"]["m2_no_answer_false_hit_rate"] = baseline["m2_no_answer_false_hit_rate"] + 0.02
    assert compare.check(ok, baseline) == []

    bad_m1 = copy.deepcopy(report)
    bad_m1["question_set_digest"] = baseline["question_set_digest"]
    bad_m1["summary"]["m1_evidence_recall_at_5"] = baseline["m1_evidence_recall_at_5"] - 0.021
    assert any("M1" in p for p in compare.check(bad_m1, baseline))

    bad_m2 = copy.deepcopy(report)
    bad_m2["question_set_digest"] = baseline["question_set_digest"]
    bad_m2["summary"]["m2_no_answer_false_hit_rate"] = baseline["m2_no_answer_false_hit_rate"] + 0.021
    assert any("M2" in p for p in compare.check(bad_m2, baseline))


def test_gate_fails_when_the_eval_set_changes_without_rebaseline(report, baseline) -> None:
    changed = copy.deepcopy(report)
    changed["question_set_digest"] = "deadbeef"
    assert any("owner approval" in p for p in compare.check(changed, baseline))


def test_gate_fails_when_a_project_ruled_forbidden_item_is_returned(report, baseline) -> None:
    leaked = copy.deepcopy(report)
    leaked["summary"]["restricted_project_leaks"] = 1
    problems = compare.check(leaked, baseline)
    assert any("project-rule restricted set returned 1 forbidden item" in p for p in problems)

    missing = copy.deepcopy(report)
    del missing["summary"]["restricted_project_leaks"]
    assert any("project-rule restricted leak metric is missing" in p for p in compare.check(missing, baseline))

    empty = copy.deepcopy(report)
    empty["summary"]["restricted_project_evaluated"] = 0
    assert any("must evaluate at least 3 questions" in p for p in compare.check(empty, baseline))


def test_gate_fails_on_a_really_degraded_retriever(baseline, monkeypatch) -> None:
    """Break search for real (returns nothing); the end-to-end run must trip the gate."""
    from penpal.mcp import tools

    monkeypatch.setattr(tools, "search_events", lambda *a, **k: [])
    degraded = run_eval()
    assert degraded["summary"]["m1_evidence_recall_at_5"] == 0.0
    assert any("M1" in p for p in compare.check(degraded, baseline))

    # and a retriever that answers everything trips M2
    def always_hit(query, **kwargs):
        return [{"id": 1, "source": "claude_code", "occurred_at": "2026-04-20T02:00:00+00:00", "bucket": "Work",
                 "kind": "message", "evidence_id": 1, "snippet": "x", "matched": 2, "keyword_count": 2}]

    monkeypatch.setattr(tools, "search_events", always_hit)
    noisy = run_eval()
    assert noisy["summary"]["m2_no_answer_false_hit_rate"] == 1.0
    assert any("M2" in p for p in compare.check(noisy, baseline))
