"""E3 retrieval eval runner (Eval PRD §3.3).

    python -m evals.run                         # run, print summary, write evals/report.json
    python -m evals.run --out r.json --baseline evals/baseline.json   # + regression gate (exit 1)
    python -m evals.run --write-baseline evals/baseline.json          # owner-approved baseline update

Flow: synthetic fixtures -> temp dir (fake HOME) -> real source plugins -> registry.read_all
(the same ingest path as ``penpal sources sync``) -> temp DB v20 -> ``search_context`` called
through the MCP server layer with a real grant. Nothing outside the temp dir is read or written.

Metrics
  M1  evidence recall@5   : answerable questions with >=1 gold fixture event in the top-5 results
                            (either tier: "strong" or "weak"). Also reported for the natural-language
                            (style "nl") and keyword (all other styles) subsets.
  M2  no-answer false-hit : no-answer questions that returned a "strong" result. "weak" results are
                            possible clues (PRD 3.7.3) and are not counted.

Each question carries ``keywords`` = what a compliant AI host would send (the host does the
natural-language understanding), plus the ``time_range`` / ``sources`` it would derive from the
question. ``query`` is only the user's original wording, kept for reading the report.
Questions that cannot be scored yet (conflict -> PRD says record only)
are reported as skipped / recorded and never counted as failures.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals import compare
from evals.fixture_data import BUCKETS, BY_ID, CAPTURE_EVENTS, EVENTS, PROJECT_BUCKET_RULES

QUESTIONS_PATH = Path(__file__).parent / "data" / "retrieval_questions.jsonl"
TOP_K = 5
EVAL_VERSION = "v0"


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict[str, Any]]:
    questions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "duplicate question ids"
    for q in questions:
        for fx in [*q.get("gold", []), *q.get("forbidden", []), *filter(None, [q.get("newest"), q.get("stale")])]:
            assert fx in BY_ID, f"{q['id']}: unknown fixture id {fx}"
    return questions


def question_set_digest(questions: list[dict[str, Any]]) -> str:
    """Hash of questions + fixtures: changes whenever the eval set changes (needs owner approval)."""
    payload = json.dumps(
        [questions, [e.__dict__ for e in EVENTS], [e.__dict__ for e in CAPTURE_EVENTS], PROJECT_BUCKET_RULES],
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# -- environment ---------------------------------------------------------------------------------

class _IsolatedEnv:
    """Fake HOME + temp DB + fixture plugins registered in place of the real ones."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self) -> "_IsolatedEnv":
        from penpal.sources import registry
        from penpal.sources.cleaning.config import CleaningConfig
        from penpal.store.db import init_db

        from evals.fixtures import build_plugins

        self._old_home = os.environ.get("HOME")
        home = self.root / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        self._registry = registry
        self._old_plugins = dict(registry._PLUGINS)
        self._old_cleaning = registry.load_cleaning_config
        registry._PLUGINS.clear()
        registry._PLUGINS.update(build_plugins(self.root))
        registry.load_cleaning_config = lambda: CleaningConfig()
        self.db_path = self.root / "eval.db"
        init_db(self.db_path)
        return self

    def __exit__(self, *exc: object) -> None:
        from penpal.store.db import close

        close()
        self._registry._PLUGINS.clear()
        self._registry._PLUGINS.update(self._old_plugins)
        self._registry.load_cleaning_config = self._old_cleaning
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home


def _ingest() -> int:
    from penpal.sources import registry
    from penpal.sources.sink import flush_named_entity_backfill

    now = datetime.now(timezone.utc)
    n = sum(1 for _ in registry.read_all(datetime(2000, 1, 1, tzinfo=timezone.utc), now))
    flush_named_entity_backfill()
    return n


def _ingest_capture() -> int:
    """Insert the capture fixtures through the capture repository path (normalizer -> insert_raw_event)."""
    from penpal.capture import normalizer as n
    from penpal.store.repository import insert_raw_event

    count = 0
    for ev in CAPTURE_EVENTS:
        ts = datetime.fromisoformat(ev.ts).replace(tzinfo=timezone.utc).isoformat()
        end = datetime.fromisoformat(ev.ts_end).replace(tzinfo=timezone.utc).isoformat() if ev.ts_end else None
        if ev.source == "keyboard_chunk":
            raw = n.normalize_keyboard_chunk_event(ev.text, app_name=ev.app, ts_start=ts)
        elif ev.source == "clipboard":
            raw = n.normalize_clipboard_event(ev.text, app_name=ev.app, ts_start=ts)
        elif ev.source == "ax_text":
            raw = n.normalize_ax_text_event(ev.text, app_name=ev.app, ts_start=ts)
        elif ev.source == "browser_url":
            raw = n.normalize_browser_url_event(ev.url, ev.text, browser_name=ev.app, ts_start=ts)
        elif ev.source == "window":
            raw = n.normalize_window_event("window_focus_session", ev.app, ev.title, None, ts_start=ts, ts_end=end)
        else:
            raise ValueError(f"unknown capture fixture source {ev.source}")
        count += 1 if insert_raw_event(raw) > 0 else 0
    return count


def _resolve_fixture_rows() -> tuple[dict[int, str], dict[str, int], dict[str, dict[str, Any]]]:
    """Map DB rows to fixture ids by (source, occurred_at). Returns (row->fx, fx->row, observed)."""
    from penpal.store.db import get_conn

    by_key = {(e.source, datetime.fromisoformat(e.ts).replace(tzinfo=timezone.utc).isoformat()): e for e in (*EVENTS, *CAPTURE_EVENTS)}
    row_to_fx: dict[int, str] = {}
    observed: dict[str, dict[str, Any]] = {}
    for r in get_conn().execute("SELECT id, source, ts_start, bucket FROM raw_events"):
        ev = by_key.get((r["source"], datetime.fromisoformat(r["ts_start"]).astimezone(timezone.utc).isoformat()))
        if ev is not None:
            row_to_fx[r["id"]] = ev.id
            observed[ev.id] = {"bucket": r["bucket"]}
    return row_to_fx, {v: k for k, v in row_to_fx.items()}, observed


# -- MCP calls -----------------------------------------------------------------------------------

def _call_all(tokens: dict[str, str], questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`tokens` maps question id -> the token to call with (one Host per distinct token)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    from penpal.mcp.server import build_server

    import logging

    logging.getLogger("mcp").setLevel(logging.WARNING)  # per-request INFO lines drown the report
    current = {"token": None}

    def client_of() -> str | None:
        # The eval Host presents its own grant's client id, like a real Host config would.
        from penpal.mcp.auth import hash_token
        from penpal.store.db import get_conn

        row = get_conn().execute(
            "SELECT client_id FROM access_grants WHERE token_hash = ?", (hash_token(current["token"]),)
        ).fetchone()
        return row["client_id"] if row else None

    server = build_server(token_provider=lambda: current["token"], client_provider=client_of)

    async def run() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            for q in questions:
                current["token"] = tokens[q["id"]]
                args: dict[str, Any] = {"keywords": q["keywords"], "limit": TOP_K}
                if q.get("time_range"):
                    args["time_range"] = q["time_range"]
                if q.get("sources"):
                    args["sources"] = q["sources"]
                res = await client.call_tool("search_context", args)
                out[q["id"]] = json.loads(res.content[0].text)
        return out

    return asyncio.run(run())


# -- scoring -------------------------------------------------------------------------------------

def _group(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    agg: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        agg[r[key]].append(r["hit"])
    return {k: {"n": len(v), "hits": sum(v), "rate": round(sum(v) / len(v), 4)} for k, v in sorted(agg.items())}


def run_eval() -> dict[str, Any]:
    started = time.monotonic()
    questions = load_questions()
    with tempfile.TemporaryDirectory(prefix="penpal-eval-") as tmp, _IsolatedEnv(Path(tmp)):
        from penpal.mcp import auth

        ingested = _ingest() + _ingest_capture()
        row_to_fx, fx_to_row, observed = _resolve_fixture_rows()
        missing = [e.id for e in (*EVENTS, *CAPTURE_EVENTS) if e.expect_ingest and e.id not in fx_to_row]
        if missing:
            raise RuntimeError(f"fixture events were not ingested (fixture/plugin problem, not retrieval): {missing}")
        wired = {BY_ID[fx].source for fx in fx_to_row}

        # Answerable / no-answer / conflict questions span all four buckets, so the eval Host is
        # granted all of them. Each restricted question gets its own scoped grant (default Work).
        full = auth.create_grant("eval", buckets=list(BUCKETS))
        tokens = {}
        default_questions = [q for q in questions if q["variant"] != "restricted_project"]
        project_questions = [q for q in questions if q["variant"] == "restricted_project"]
        for q in default_questions:
            if q["variant"] == "restricted":
                scope = q.get("grant", {})
                tokens[q["id"]] = auth.create_grant(f"eval-{q['id']}", buckets=scope.get("buckets"), sources=scope.get("sources"))
            else:
                tokens[q["id"]] = full
        responses = _call_all(tokens, default_questions)
        observed_default = dict(observed)

        # P0 default questions above deliberately see source defaults. Then apply the user's
        # explicit D37 project rules and exercise the same restricted queries again.
        from penpal.store.db import get_conn
        from penpal.store.models import Policy
        from penpal.store.repository import insert_policy
        from penpal.store.buckets import reapply_buckets

        for project, bucket in PROJECT_BUCKET_RULES:
            insert_policy(Policy(
                scope_type="project", scope_value=project, mode="bucket",
                config_json=json.dumps({"bucket": bucket}),
            ))
        reapply_buckets(get_conn())
        _, _, observed_project = _resolve_fixture_rows()
        project_tokens = {}
        for q in project_questions:
            scope = q.get("grant", {})
            project_tokens[q["id"]] = auth.create_grant(
                f"eval-{q['id']}", buckets=scope.get("buckets"), sources=scope.get("sources")
            )
        responses.update(_call_all(project_tokens, project_questions))

    def top_fx(q: dict[str, Any]) -> list[str | None]:
        results = responses[q["id"]].get("results", [])[:TOP_K]
        return [row_to_fx.get(int(r["id"].removeprefix("evt_"))) for r in results]

    answerable_rows: list[dict[str, Any]] = []
    no_answer_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []
    restricted: list[dict[str, Any]] = []
    restricted_project: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []

    for q in questions:
        resp = responses[q["id"]]
        top = top_fx(q)
        base = {"id": q["id"], "variant": q["variant"], "query": q["query"], "style": q["style"], "status": resp.get("status"), "top": top}
        if q["variant"] == "answerable":
            need = sorted({BY_ID[g].source for g in q["gold"]})
            if not set(need) <= wired:
                skipped.append({"id": q["id"], "variant": q["variant"], "reason": f"source not yet wired: {sorted(set(need) - wired)}", "needs": need})
                continue
            hit = any(fx in q["gold"] for fx in top)
            first = BY_ID[q["gold"][0]]
            row = {**base, "hit": hit, "scenario": q["scenario"], "source": first.source, "bucket": first.bucket, "gold": q["gold"]}
            answerable_rows.append(row)
            details.append(row)
        elif q["variant"] == "no_answer":
            strong = [r for r in resp.get("results", []) if r.get("match") == "strong"]
            row = {**base, "hit": resp.get("status") == "ok" and bool(strong),
                   "returned": len(resp.get("results", [])), "returned_strong": len(strong)}
            no_answer_rows.append(row)
            details.append(row)
        elif q["variant"] in ("restricted", "restricted_project"):
            # G1-style: no forbidden (out-of-scope) evidence may come back.
            leaked = [fx for fx in top if fx in q["forbidden"]]
            # A leak is "misbucketed" when the forbidden event's *assigned* bucket is inside the
            # grant: P0 has no content classification (D23), so e.g. a therapy chat in a Claude Code
            # session is Work by source default. Otherwise it is a scope-enforcement leak (G1).
            scope_b = set(q["grant"].get("buckets") or ["Work"])
            bucket_view = observed_default if q["variant"] == "restricted" else observed_project
            causes = {fx: ("misbucketed" if bucket_view.get(fx, {}).get("bucket") in scope_b else "scope_enforcement") for fx in leaked}
            row = {**base, "grant": q["grant"], "forbidden": q["forbidden"], "leaked": leaked,
                   "leak_causes": causes, "hit": bool(leaked)}
            (restricted if q["variant"] == "restricted" else restricted_project).append(row)
        elif q["variant"] == "conflict":
            rank = {fx: i + 1 for i, fx in enumerate(top) if fx}
            recorded.append({**base, "newest_rank": rank.get(q["newest"]), "stale_rank": rank.get(q["stale"]),
                             "newest_first": rank.get(q["newest"], 99) < rank.get(q["stale"], 99)})

    m1 = sum(r["hit"] for r in answerable_rows) / len(answerable_rows) if answerable_rows else 0.0
    nl_rows = [r for r in answerable_rows if r["style"] == "nl"]
    kw_rows = [r for r in answerable_rows if r["style"] != "nl"]
    m1_nl = sum(r["hit"] for r in nl_rows) / len(nl_rows) if nl_rows else 0.0
    m1_kw = sum(r["hit"] for r in kw_rows) / len(kw_rows) if kw_rows else 0.0
    m2 = sum(r["hit"] for r in no_answer_rows) / len(no_answer_rows) if no_answer_rows else 0.0

    coverage = Counter((e.source, e.bucket) for e in EVENTS)
    return {
        "eval_version": EVAL_VERSION,
        "question_set_digest": question_set_digest(questions),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec": round(time.monotonic() - started, 1),
        "summary": {
            "m1_evidence_recall_at_5": round(m1, 4),
            "m2_no_answer_false_hit_rate": round(m2, 4),
            "m1_nl_subset": round(m1_nl, 4),
            "m1_keyword_subset": round(m1_kw, 4),
            "nl_evaluated": len(nl_rows),
            "keyword_evaluated": len(kw_rows),
            "answerable_evaluated": len(answerable_rows),
            "no_answer_evaluated": len(no_answer_rows),
            "questions_total": len(questions),
            "questions_skipped": len(skipped),
            "questions_recorded_only": len(recorded),
            "restricted_evaluated": len(restricted),
            "restricted_leaks": sum(r["hit"] for r in restricted),
            "restricted_default_evaluated": len(restricted),
            "restricted_default_leaks": sum(r["hit"] for r in restricted),
            "restricted_project_evaluated": len(restricted_project),
            "restricted_project_leaks": sum(r["hit"] for r in restricted_project),
            "restricted_enforcement_leaks": sum("scope_enforcement" in r["leak_causes"].values() for r in restricted),
        },
        "by_scenario": _group(answerable_rows, "scenario"),
        "by_source": _group(answerable_rows, "source"),
        "by_bucket": _group(answerable_rows, "bucket"),
        "by_style": _group(answerable_rows, "style"),
        "no_answer_by_style": _group(no_answer_rows, "style"),
        "fixtures": {
            "events": len(EVENTS),
            "ingested_rows": ingested,
            "sources_wired": sorted(wired),
            "coverage_source_x_bucket": {s: {b: coverage.get((s, b), 0) for b in BUCKETS} for s in sorted({e.source for e in EVENTS})},
            "bucket_assigned_by_mainline": {
                s: sum(1 for e in EVENTS if e.source == s and observed_default.get(e.id, {}).get("bucket"))
                for s in sorted({e.source for e in EVENTS})
            },
            "project_bucket_rules": [{"project": p, "bucket": b} for p, b in PROJECT_BUCKET_RULES],
        },
        "skipped": skipped,
        "recorded_only": recorded,
        "restricted": restricted,
        "restricted_project": restricted_project,
        "questions": details,
    }


def _print(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"E3 retrieval eval {report['eval_version']} (set {report['question_set_digest']}) in {report['elapsed_sec']}s")
    print(f"  M1 evidence recall@5        : {s['m1_evidence_recall_at_5']:.1%}  ({s['answerable_evaluated']} answerable)")
    print(f"    M1 natural-language subset: {s['m1_nl_subset']:.1%}  ({s['nl_evaluated']} questions)")
    print(f"    M1 keyword subset         : {s['m1_keyword_subset']:.1%}  ({s['keyword_evaluated']} questions)")
    print(f"  M2 no-answer false-hit rate : {s['m2_no_answer_false_hit_rate']:.1%}  ({s['no_answer_evaluated']} no-answer; strong results only)")
    print(f"  skipped {s['questions_skipped']}, recorded-only {s['questions_recorded_only']}, total {s['questions_total']}")
    for title, key in (("scenario", "by_scenario"), ("source", "by_source"), ("bucket", "by_bucket"), ("style", "by_style"), ("no-answer style", "no_answer_by_style")):
        print(f"  by {title}: " + ", ".join(f"{k} {v['hits']}/{v['n']}" for k, v in report[key].items()))
    print(f"  restricted P0 default: {s['restricted_default_evaluated']} evaluated, {s['restricted_default_leaks']} forbidden returned (known D37 limitation: coding-agent default; {s['restricted_enforcement_leaks']} scope-enforcement leaks)")
    print(f"  restricted with project rules: {s['restricted_project_evaluated']} evaluated, {s['restricted_project_leaks']} forbidden returned")
    for r in report["recorded_only"]:
        print(f"  conflict {r['id']}: newest rank {r['newest_rank']}, stale rank {r['stale_rank']} (recorded only)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).parent / "report.json"), help="where to write the full JSON report")
    ap.add_argument("--baseline", help="compare against this baseline; exit 1 on regression > 2pp")
    ap.add_argument("--write-baseline", help="write summary numbers as the new baseline (owner approval required)")
    args = ap.parse_args(argv)

    report = run_eval()
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _print(report)
    print(f"  report: {args.out}")
    if args.write_baseline:
        Path(args.write_baseline).write_text(json.dumps(compare.make_baseline(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  baseline written: {args.write_baseline}")
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        problems = compare.check(report, baseline)
        for p in problems:
            print(f"  GATE FAIL: {p}")
        if problems:
            return 1
        print("  gate: ok (no regression beyond 2pp)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
