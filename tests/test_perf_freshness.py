"""Small default E5 smoke; release timing thresholds are only asserted by the owner Mac run."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "perf_freshness.py"


def _load():
    spec = importlib.util.spec_from_file_location("perf_freshness", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fixture_is_seeded_and_reproducible():
    mod = _load()
    a, _, _ = mod.generate_events(300, 7)
    b, _, _ = mod.generate_events(300, 7)
    c, _, _ = mod.generate_events(300, 8)
    assert mod.events_digest(a) == mod.events_digest(b)
    assert mod.events_digest(a) != mod.events_digest(c)


def test_small_run_end_to_end(tmp_path):
    mod = _load()
    result = mod.run_benchmark(events=300, seed=7, query_reps=1, freshness_runs=1, dynamic_runs=1,
                               work_dir=tmp_path, scheduler_fast_poll_interval_sec=1)
    m = result["measured"]
    assert result["db"]["raw_events_rows"] == 300
    assert result["params"]["queries"] == 100
    assert m["query"]["warm"]["n"] == 100
    assert m["query"]["sanity"] == {"zero_hit_all_no_results": True, "marker_exact_all_hit": True}
    assert m["freshness_structured"]["not_found"] == 0
    assert m["freshness_structured"]["scheduler"]["fast_poll_interval_sec"] == 1
    assert m["freshness_dynamic"]["not_found"] == 0
    assert "## E5 运行指标 benchmark" in mod._markdown_report(result)
