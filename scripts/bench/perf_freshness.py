#!/usr/bin/env python3
"""Synthetic performance / freshness benchmark (Eval PRD E5, metrics M4 and M5).

    python scripts/bench/perf_freshness.py --report-md docs/eval-reports/E5-YYYYMMDD.md
    python scripts/bench/perf_freshness.py --events 5000 --report-md /tmp/e5-smoke.md

Everything runs in a temp dir with HOME and the working directory redirected there. The fixture is
generated from a seed and written through the real storage path:
  * semantic sources (claude / codex / git / zsh / chrome / safari) -> persist_semantic_event (the sink
    used by `penpal sources sync`), with the named-entity backfill run synchronously;
  * clipboard -> normalize_clipboard_event -> insert_raw_event.

Measured (all through the MCP server layer, in-process, with a real grant):
  * search_context latency, 100 deterministic fixed queries: cold (fresh SQLite connection per call; the
    OS page cache is NOT dropped) and warm (after a warm-up pass, one measured pass by default);
  * get_timeline and get_evidence latency (warm);
  * structured-source freshness: append a known record to a zsh history file, let the public
    SourcesScheduler configured through the isolated Config path sync it, and time until search_context
    returns it, including scheduler wait;
  * dynamic-activity freshness: enqueue a clipboard capture event on CaptureManager's queue while its
    real flush loop runs, time until search_context returns it;
  * DB size after the fixture is loaded.

Verdicts use the Eval PRD section 5 thresholds (M4: 5 min / 30 s P95, M5: 2 s P95 at 100k events).
The script prints a pass/fail table, writes an E5 markdown section, and exits nonzero if a gate fails.
Timings are wall-clock on the machine that runs the script; they are only meaningful on the PRD
reference machine. The script does not tune anything; it reports.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import hashlib
import itertools
import json
import logging
import math
import os
import platform
import random
import sqlite3
import string
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from penpal.capture.manager import CaptureManager  # noqa: E402
from penpal.capture.normalizer import normalize_clipboard_event  # noqa: E402
from penpal.config import Config, SourcesSchedulerConfig  # noqa: E402
from penpal.mcp import auth  # noqa: E402
from penpal.mcp.server import build_server  # noqa: E402
from penpal.sources import registry  # noqa: E402
from penpal.sources.plugins.zsh_history import ZshHistorySource  # noqa: E402
from penpal.sources.history_gate import mark_onboarded  # noqa: E402
from penpal.sources.scheduler import SourcesScheduler  # noqa: E402
from penpal.sources.sink import flush_named_entity_backfill, persist_semantic_event  # noqa: E402
from penpal.sources.types import SemanticEvent  # noqa: E402
from penpal.store import buckets  # noqa: E402
from penpal.store.buckets import BUCKETS  # noqa: E402
from penpal.store.db import close as close_db  # noqa: E402
from penpal.store.db import get_conn, init_db  # noqa: E402
from penpal.store.repository import insert_raw_event, seed_policies_from_config  # noqa: E402

logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("penpal").setLevel(logging.WARNING)

DEFAULT_EVENTS = 100_000
DEFAULT_SEED = 20261024
ANCHOR = datetime(2026, 10, 24, tzinfo=timezone.utc)  # fixed, so the fixture does not depend on today's date
WINDOW_DAYS = 365
CLIENT_ID = "perf-host"
POLL_SEC = 0.05
DYNAMIC_TIMEOUT_SEC = 60.0
STRUCTURED_TIMEOUT_SEC = 600.0

# PRD section 5 thresholds (V1.0 initial values; the owner confirms them after the first real run).
THRESHOLDS = {
    "M4_structured_p95_sec": 300.0,
    "M4_dynamic_p95_sec": 30.0,
    "M5_query_p95_sec": 2.0,
}

# Synthetic source mix (weights are relative). Semantic sources and clipboard both go to raw_events.
SOURCE_WEIGHTS = {
    "claude_code": 0.30,
    "codex_cli": 0.10,
    "git_log": 0.08,
    "zsh_history": 0.15,
    "chrome_history": 0.12,
    "safari_history": 0.10,
    "clipboard": 0.15,
}
SEMANTIC_TIER = {"claude_code": "yellow", "codex_cli": "yellow", "git_log": "green",
                 "zsh_history": "green", "chrome_history": "yellow", "safari_history": "yellow"}
ACTOR_SOURCES = {"claude_code", "codex_cli"}  # these have both user and assistant turns
N_MARKERS = 40
MARKER_EVENTS_PER = 50  # planted per marker -> exact-term queries have ~50 hits each
ZERO_HIT_WORDS = [f"qjzxv{i:02d}" for i in range(20)]  # q/x/j are absent from the generated vocabulary
TIMELINE_CALLS = 20
EVIDENCE_CALLS = 20


# -- fixture -------------------------------------------------------------------------------------

class _Vocab:
    """Synthetic Latin syllable words (Zipf-distributed) and CJK tokens."""

    def __init__(self, rng: random.Random) -> None:
        cons, vow = "bdfgklmnprstvz", "aeiou"
        seen: set[str] = set()
        words: list[str] = []
        while len(words) < 4000:
            n = rng.choice((2, 2, 3))
            w = "".join(rng.choice(cons) + rng.choice(vow) for _ in range(n))
            if w not in seen:
                seen.add(w)
                words.append(w)
        self.latin = words
        self._cum = list(itertools.accumulate(1.0 / (i + 1) ** 1.07 for i in range(len(words))))
        chars = [chr(c) for c in rng.sample(range(0x4E00, 0x9FFF), 800)]
        self.cjk = ["".join(rng.choice(chars) for _ in range(rng.choice((3, 4)))) for _ in range(600)]

    def latin_word(self, rng: random.Random) -> str:
        idx = bisect.bisect(self._cum, rng.random() * self._cum[-1])
        return self.latin[min(idx, len(self.latin) - 1)]


def _markers(rng: random.Random) -> list[str]:
    out: list[str] = []
    while len(out) < N_MARKERS:
        m = "kpb" + "".join(rng.choice(string.ascii_lowercase) for _ in range(5))
        if m not in out:
            out.append(m)
    return out


def _text(vocab: _Vocab, rng: random.Random, source: str) -> str:
    if source == "zsh_history":
        n = rng.randint(2, 7)
    elif source == "git_log":
        n = rng.randint(4, 20)
    elif source in ("chrome_history", "safari_history"):
        n = rng.randint(3, 12)
    elif source == "clipboard":
        n = rng.randint(3, 60)
    else:  # chat turns: mostly medium, ~2% long documents
        n = rng.randint(300, 600) if rng.random() < 0.02 else rng.randint(15, 120)
    toks = [rng.choice(vocab.cjk) if rng.random() < 0.15 else vocab.latin_word(rng) for _ in range(n)]
    return " ".join(toks)


def generate_events(n: int, seed: int) -> tuple[list[dict[str, Any]], _Vocab, list[str]]:
    """Seeded synthetic fixture. Same (n, seed) -> identical events, vocabulary and markers."""
    rng = random.Random(seed)
    vocab = _Vocab(rng)
    markers = _markers(rng)
    names = list(SOURCE_WEIGHTS)
    weights = [SOURCE_WEIGHTS[s] for s in names]
    planted = {}
    per_marker = max(1, min(MARKER_EVENTS_PER, n // (N_MARKERS * 10)))  # 50 at 100k, fewer in small fixtures
    picks = rng.sample(range(n), N_MARKERS * per_marker)
    for k, idx in enumerate(picks):
        planted[idx] = markers[k % N_MARKERS]
    span_sec = int(timedelta(days=WINDOW_DAYS).total_seconds())
    start = ANCHOR - timedelta(days=WINDOW_DAYS)
    events: list[dict[str, Any]] = []
    for i in range(n):
        source = rng.choices(names, weights)[0]
        ts = start + timedelta(seconds=rng.randrange(span_sec), microseconds=rng.randrange(1_000_000))
        text = _text(vocab, rng, source)
        if i in planted:
            text = f"{text} {planted[i]}"
        events.append({
            "i": i,
            "source": source,
            "ts": ts,
            "text": text,
            "actor": ("assistant" if rng.random() < 0.5 else "user") if source in ACTOR_SOURCES else "user",
        })
    return events, vocab, markers


def events_digest(events: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for ev in events:
        h.update(f"{ev['i']}|{ev['source']}|{ev['ts'].isoformat()}|{ev['text']}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def load_events(events: list[dict[str, Any]]) -> dict[str, float]:
    """Write the fixture through the real storage path. Returns timing info (informational)."""
    t0 = time.perf_counter()
    for n, ev in enumerate(events, start=1):
        if ev["source"] == "clipboard":
            insert_raw_event(normalize_clipboard_event(ev["text"], app_name="Notes",
                                                       ts_start=ev["ts"].isoformat(),
                                                       process_name="com.apple.Notes"))
        else:
            persist_semantic_event(SemanticEvent(
                time=ev["ts"], source=ev["source"], actor=ev["actor"], intent=ev["text"], artifact="",
                raw_ref=f"{ev['source']}:{ev['i']}", privacy_tier=SEMANTIC_TIER[ev["source"]],
                metadata={"app_name": "Terminal" if ev["source"] == "zsh_history" else "Bench"},
                external_id=f"bench-{ev['i']}", kind="message",
            ), async_named_entities=False)
        if n % 10_000 == 0:
            print(f"  ingested {n}/{len(events)}", file=sys.stderr)
    flush_named_entity_backfill()
    return {"seconds": round(time.perf_counter() - t0, 2)}


def _db_size(db_path: Path) -> dict[str, Any]:
    conn = get_conn()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rows = conn.execute("SELECT COUNT(*) AS n FROM raw_events").fetchone()["n"]
    size = db_path.stat().st_size
    out: dict[str, Any] = {"raw_events_rows": rows, "db_bytes": size,
                           "db_mb": round(size / 1e6, 1), "bytes_per_event": round(size / max(rows, 1), 1)}
    try:  # per-table/index footprint, when the sqlite build has dbstat
        parts = conn.execute("SELECT name, SUM(pgsize) AS b FROM dbstat GROUP BY name ORDER BY b DESC LIMIT 6").fetchall()
        out["largest_objects_bytes"] = {r["name"]: int(r["b"]) for r in parts}
    except sqlite3.Error:
        out["largest_objects_bytes"] = None
    return out


# -- queries -------------------------------------------------------------------------------------

def build_queries(vocab: _Vocab, markers: list[str], rng: random.Random) -> list[dict[str, Any]]:
    common = vocab.latin[:30]
    mid = vocab.latin[30:200]
    q: list[dict[str, Any]] = []

    def add(cat: str, keywords: list[str], time_range=None, sources=None) -> None:
        q.append({"id": f"q{len(q) + 1:02d}", "category": cat, "keywords": keywords,
                  "time_range": time_range, "sources": sources})

    for i in range(20):
        add("exact_term", [markers[i]])
    for _ in range(10):
        add("multi_term", [rng.choice(common), rng.choice(mid)])
    for i in range(5):
        add("multi_term", [markers[20 + i], common[i]])
    for i in range(10):
        add("partial_clue", [markers[25 + i][:5]])  # marker prefix, as a host would guess it
    for _ in range(5):
        add("partial_clue", [rng.choice(vocab.latin[:300])[1:4]])  # middle trigram of a common word
    for i, days in enumerate((30, 90, 180, 30, 90, 14, 45, 120, 240, 7, 60, 150, 300, 21, 75)):
        add("time_filtered", [common[i]], time_range={
            "start": (ANCHOR - timedelta(days=days)).isoformat(), "end": ANCHOR.isoformat()})
    for i, src in enumerate([
        "claude_code", "zsh_history", "git_log", "chrome_history", "codex_cli",
        "safari_history", "clipboard", "claude_code", "zsh_history", "git_log",
        "chrome_history", "codex_cli", "safari_history", "clipboard", "claude_code",
    ]):
        add("source_filtered", [common[i + 3]], sources=[src])
    for w in ZERO_HIT_WORDS:
        add("zero_hit", [w])
    assert len(q) == 100
    return q


def _search_args(q: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"keywords": q["keywords"]}
    if q["time_range"]:
        args["time_range"] = q["time_range"]
    if q["sources"]:
        args["sources"] = q["sources"]
    return args


# -- stats ---------------------------------------------------------------------------------------

def _pct(vals: list[float], p: float) -> float:
    """Nearest-rank percentile. Missing (None) samples are passed in as inf and count as worst case."""
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def _finite(x: float) -> float | None:
    return None if (x != x or x == float("inf")) else round(x, 4)


def _stats(vals: list[float]) -> dict[str, Any]:
    s = sorted(vals)
    return {"n": len(s), "p50": _finite(_pct(s, 50)), "p95": _finite(_pct(s, 95)),
            "max": _finite(s[-1]) if s else None, "mean": _finite(sum(s) / len(s)) if s else None}


def _verdict(value: float | None, limit: float) -> str:
    return "FAIL" if value is None or value > limit else "PASS"


# -- measurement ---------------------------------------------------------------------------------

async def _timed(client: Any, name: str, args: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    t = time.perf_counter()
    res = await client.call_tool(name, args)
    dt = time.perf_counter() - t
    if res.isError:
        raise RuntimeError(f"{name} returned an MCP error")  # fixed text only; never echo payloads
    return dt, json.loads(res.content[0].text)


async def _query_phase(client: Any, queries: list[dict[str, Any]], reps: int, evidence_ids: list[str]) -> dict[str, Any]:
    cold: list[dict[str, Any]] = []
    for q in queries:
        close_db()  # fresh SQLite connection: empty page cache, re-prepared statements
        dt, payload = await _timed(client, "search_context", _search_args(q))
        cold.append({"id": q["id"], "category": q["category"], "sec": dt, "status": payload.get("status"),
                     "n": len(payload.get("results", []))})
        evidence_ids.extend(r["evidence_id"] for r in payload.get("results", []) if r.get("evidence_id"))

    for q in queries:  # warm-up pass, not recorded
        await _timed(client, "search_context", _search_args(q))
    warm: list[dict[str, Any]] = []
    for _ in range(reps):
        for q in queries:
            dt, payload = await _timed(client, "search_context", _search_args(q))
            warm.append({"id": q["id"], "category": q["category"], "sec": dt, "status": payload.get("status"),
                         "n": len(payload.get("results", []))})

    def by_cat(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {c: _stats([r["sec"] for r in rows if r["category"] == c])
                for c in dict.fromkeys(q["category"] for q in queries)}

    # Sanity: the fixture must answer the planted/common queries and must not answer the zero-hit ones.
    sanity = {"zero_hit_all_no_results": all(r["status"] == "no_results" and r["n"] == 0
                                              for r in warm if r["category"] == "zero_hit"),
              "marker_exact_all_hit": all(r["n"] > 0 for r in warm if r["category"] == "exact_term")}
    return {"cold": _stats([r["sec"] for r in cold]), "warm": _stats([r["sec"] for r in warm]),
            "cold_by_category": by_cat(cold), "warm_by_category": by_cat(warm),
            "cold_rows": cold, "warm_samples": len(warm), "sanity": sanity}


async def _timeline_phase(client: Any, rng: random.Random) -> dict[str, Any]:
    secs: list[float] = []
    for k in range(TIMELINE_CALLS):
        span = 1 if k % 2 == 0 else 7
        start = ANCHOR - timedelta(days=rng.randint(0, WINDOW_DAYS - span - 1))
        dt, payload = await _timed(client, "get_timeline", {"start": start.isoformat(),
                                                             "end": (start + timedelta(days=span)).isoformat()})
        secs.append(dt)
    return {"span_days": "1 (half the calls) / 7 (half)", "limit": "default 50", **_stats(secs)}


async def _evidence_phase(client: Any, evidence_ids: list[str], rng: random.Random) -> dict[str, Any]:
    pool = sorted(set(evidence_ids))
    picks = rng.sample(pool, min(EVIDENCE_CALLS, len(pool)))
    secs: list[float] = []
    statuses: dict[str, int] = {}
    for eid in picks:
        dt, payload = await _timed(client, "get_evidence", {"evidence_id": eid})
        secs.append(dt)
        statuses[str(payload.get("status"))] = statuses.get(str(payload.get("status")), 0) + 1
    return {"statuses": statuses, **_stats(secs)}


async def _wait_for(client: Any, marker: str, t0: float, timeout: float) -> float | None:
    """Poll search_context until the marker shows up in a snippet. Returns seconds since t0 or None."""
    while True:
        _, payload = await _timed(client, "search_context", {"keywords": [marker], "limit": 5})
        if any(marker in str(r.get("snippet", "")) for r in payload.get("results", [])):
            return time.perf_counter() - t0
        if time.perf_counter() - t0 > timeout:
            return None
        await asyncio.sleep(POLL_SEC)


async def _structured_freshness(
    client: Any,
    history: Path,
    runs: int,
    rng: random.Random,
    scheduler_config: SourcesSchedulerConfig,
) -> dict[str, Any]:
    """Measure append-to-search through the public configured scheduler path, including its wait."""
    samples: list[float | None] = []

    def append_marker(marker: str, *, after_scan: bool = False) -> None:
        now = time.time()
        timestamp = int(now) + (1 if after_scan else 0)
        with history.open("a", encoding="utf-8") as fh:
            # zsh timestamps have whole-second precision; measured records use the next second
            # so appends just after a scan are newer than the scheduler's since cursor.
            fh.write(f": {timestamp}:0;cargo test {marker}\n")

    # The first marker waits for the scheduler's immediate initial scan. Later samples are appended
    # only after a scan completes, so each includes the actual configured scheduler wait.
    mark_onboarded("synthetic-e5-benchmark")
    initial_marker = f"kpf-init-{rng.getrandbits(24):06x}"
    append_marker(initial_marker)
    scheduler = SourcesScheduler(
        enabled=scheduler_config.enabled,
        poll_interval_sec=scheduler_config.poll_interval_sec,
        debounce_sec=scheduler_config.debounce_sec,
        fast_poll_interval_sec=scheduler_config.fast_poll_interval_sec,
        event_source_factory=lambda: None,
    )
    scheduler.start()
    try:
        if await _wait_for(client, initial_marker, time.perf_counter(), timeout=STRUCTURED_TIMEOUT_SEC) is None:
            samples.extend([None] * runs)
        else:
            for i in range(runs):
                marker = f"kpf{i:02d}{rng.getrandbits(24):06x}"
                t0 = time.perf_counter()
                append_marker(marker, after_scan=True)
                samples.append(await _wait_for(client, marker, t0, timeout=STRUCTURED_TIMEOUT_SEC))
    finally:
        scheduler.stop()
    vals = [s if s is not None else float("inf") for s in samples]
    return {"runs": runs, "not_found": sum(1 for s in samples if s is None),
            "scheduler": {"enabled": scheduler_config.enabled,
                          "poll_interval_sec": scheduler_config.poll_interval_sec,
                          "fast_poll_interval_sec": scheduler_config.fast_poll_interval_sec,
                          "debounce_sec": scheduler_config.debounce_sec},
            "p95": _finite(_pct(vals, 95)), "p50": _finite(_pct(vals, 50)), "max": _finite(max(vals)),
            "samples": [_finite(v) for v in vals]}


async def _dynamic_freshness(client: Any, cfg: Config, runs: int, rng: random.Random) -> dict[str, Any]:
    """Enqueue capture events on CaptureManager's queue while its real flush loop drains it."""
    mgr = CaptureManager(cfg)
    seed_policies_from_config(cfg.policies)
    mgr._policy.reload()
    mgr._running.set()
    flusher = threading.Thread(target=mgr._flush_loop, daemon=True, name="bench-flush-loop")
    flusher.start()
    samples: list[float | None] = []
    try:
        for i in range(runs):
            await asyncio.sleep(rng.uniform(0.0, 2.0))  # vary the phase against the flush interval
            marker = f"kpd{i:02d}{rng.getrandbits(24):06x}"
            text = f"{marker} " + " ".join(rng.choice(["note", "draft", "meeting", "review", "todo"]) for _ in range(4))
            t0 = time.perf_counter()
            mgr._queue.put(normalize_clipboard_event(text, app_name="Notes",
                                                     ts_start=datetime.now(timezone.utc).isoformat(),
                                                     process_name="com.apple.Notes"))
            samples.append(await _wait_for(client, marker, t0, timeout=DYNAMIC_TIMEOUT_SEC))
    finally:
        mgr._running.clear()
        flusher.join(timeout=cfg.app.flush_interval_sec + 10)
    vals = [s if s is not None else float("inf") for s in samples]
    return {"runs": runs, "not_found": sum(1 for s in samples if s is None),
            "flush_interval_sec": cfg.app.flush_interval_sec,
            "p95": _finite(_pct(vals, 95)), "p50": _finite(_pct(vals, 50)), "max": _finite(max(vals)),
            "samples": [_finite(v) for v in vals]}


# -- environment and driver ----------------------------------------------------------------------

def _cmd(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True, cwd=REPO_ROOT).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def environment() -> dict[str, Any]:
    dirty = _cmd(["git", "status", "--porcelain"])
    return {
        "machine_model": _cmd(["sysctl", "-n", "hw.model"]),
        "macos_version": _cmd(["sw_vers", "-productVersion"]),
        "macos_build": _cmd(["sw_vers", "-buildVersion"]),
        "cpu": _cmd(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "memory_bytes": _cmd(["sysctl", "-n", "hw.memsize"]),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "commit": _cmd(["git", "rev-parse", "HEAD"]),
        "worktree_dirty": bool(dirty and dirty != "unavailable"),
    }


@contextmanager
def _isolated(work_dir: Path) -> Iterator[Path]:
    """HOME and cwd point into work_dir so no real user data (or repo config) is read or written."""
    home = work_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
    old_plugins = dict(registry._PLUGINS)
    os.environ["HOME"] = str(home)
    os.chdir(work_dir)
    try:
        yield home
    finally:
        os.chdir(old_cwd)
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        registry._PLUGINS.clear()
        registry._PLUGINS.update(old_plugins)


def run_benchmark(*, events: int = DEFAULT_EVENTS, seed: int = DEFAULT_SEED, query_reps: int = 1,
                  freshness_runs: int = 20, dynamic_runs: int = 20, work_dir: Path | None = None,
                  scheduler_fast_poll_interval_sec: int | None = None) -> dict[str, Any]:
    owned = None
    if work_dir is None:
        owned = tempfile.TemporaryDirectory(prefix="penpal-perf-")
        work_dir = Path(owned.name)
    try:
        with _isolated(work_dir):
            return _run(events, seed, query_reps, freshness_runs, dynamic_runs, work_dir,
                        scheduler_fast_poll_interval_sec)
    finally:
        close_db()
        buckets.reset_cache()
        if owned is not None:
            owned.cleanup()


def _run(events: int, seed: int, query_reps: int, freshness_runs: int, dynamic_runs: int, work_dir: Path,
         scheduler_fast_poll_interval_sec: int | None) -> dict[str, Any]:
    env = environment()
    db_path = work_dir / "perf.db"
    buckets.reset_cache()
    init_db(db_path)
    cfg = Config.load()
    scheduler_config = cfg.sources.scheduler
    if scheduler_fast_poll_interval_sec is not None:
        scheduler_config = scheduler_config.model_copy(update={
            "fast_poll_interval_sec": scheduler_fast_poll_interval_sec,
            "debounce_sec": 0,
        })
    seed_policies_from_config(cfg.policies)

    # Structured source: a zsh history file the real plugin reads. Only this plugin is registered.
    history = work_dir / "zsh_history"
    history.write_text("", encoding="utf-8")
    registry._PLUGINS.clear()
    registry._PLUGINS["zsh_history"] = ZshHistorySource(history_path=history)

    fixture, vocab, markers = generate_events(events, seed)
    digest = events_digest(fixture)
    ingest = load_events(fixture)
    del fixture
    size = _db_size(db_path)
    print(f"fixture: {size['raw_events_rows']} rows, {size['db_mb']} MB, ingest {ingest['seconds']} s", file=sys.stderr)
    queries = build_queries(vocab, markers, random.Random(seed + 1))

    token = auth.create_grant(CLIENT_ID, buckets=list(BUCKETS), sources=None, operations=["read"],
                              purpose="perf benchmark", expires_at=None)
    server = build_server(token_provider=lambda: token, client_provider=lambda: CLIENT_ID)

    async def phases() -> dict[str, Any]:
        out: dict[str, Any] = {}
        evidence_ids: list[str] = []
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            out["query"] = await _query_phase(client, queries, query_reps, evidence_ids)
            out["timeline"] = await _timeline_phase(client, random.Random(seed + 2))
            out["evidence"] = await _evidence_phase(client, evidence_ids, random.Random(seed + 3))
            out["freshness_structured"] = await _structured_freshness(
                client, history, freshness_runs, random.Random(seed + 4), scheduler_config)
            out["freshness_dynamic"] = await _dynamic_freshness(client, cfg, dynamic_runs, random.Random(seed + 5))
        return out

    measured = asyncio.run(phases())
    cold_p95 = measured["query"]["cold"]["p95"]
    warm_p95 = measured["query"]["warm"]["p95"]
    fs = measured["freshness_structured"]
    fd = measured["freshness_dynamic"]
    verdicts = {
        "M4_structured_freshness_p95": {
            "value_sec": fs["p95"], "limit_sec": THRESHOLDS["M4_structured_p95_sec"],
            "verdict": _verdict(fs["p95"], THRESHOLDS["M4_structured_p95_sec"])},
        "M4_dynamic_freshness_p95": {
            "value_sec": fd["p95"], "limit_sec": THRESHOLDS["M4_dynamic_p95_sec"],
            "verdict": _verdict(fd["p95"], THRESHOLDS["M4_dynamic_p95_sec"])},
        "M5_query_latency_p95_warm": {
            "value_sec": warm_p95, "limit_sec": THRESHOLDS["M5_query_p95_sec"],
            "verdict": _verdict(warm_p95, THRESHOLDS["M5_query_p95_sec"])},
        "M5_query_latency_p95_cold": {
            "value_sec": cold_p95, "limit_sec": THRESHOLDS["M5_query_p95_sec"],
            "verdict": _verdict(cold_p95, THRESHOLDS["M5_query_p95_sec"])},
    }
    return {
        "params": {"events": events, "seed": seed, "fixture_digest": digest, "window_days": WINDOW_DAYS,
                   "anchor": ANCHOR.isoformat(), "queries": len(queries), "query_reps": query_reps,
                   "freshness_runs": freshness_runs, "dynamic_runs": dynamic_runs,
                   "source_weights": SOURCE_WEIGHTS, "thresholds": THRESHOLDS},
        "environment": env,
        "ingest": {**ingest, "rows_per_sec": round(events / max(ingest["seconds"], 1e-9), 1)},
        "db": size,
        "query_fixture": [{k: q[k] for k in ("id", "category", "keywords", "time_range", "sources")} for q in queries],
        "measured": measured,
        "verdicts": verdicts,
    }


def _print_summary(result: dict[str, Any]) -> None:
    m = result["measured"]
    print(json.dumps(result["environment"], ensure_ascii=False, indent=2))
    print(f"events={result['params']['events']} rows={result['db']['raw_events_rows']} "
          f"db={result['db']['db_mb']} MB ({result['db']['bytes_per_event']} B/event)")
    print("| 指标 | 样本 | P95（秒） | 通过线 | 判定 |")
    print("|---|---:|---:|---:|---|")
    labels = {
        "M4_structured_freshness_p95": "M4 结构化源新鲜度",
        "M4_dynamic_freshness_p95": "M4 动态活动新鲜度",
        "M5_query_latency_p95_warm": "M5 查询时延（warm）",
        "M5_query_latency_p95_cold": "M5 查询时延（cold）",
    }
    for name, verdict in result["verdicts"].items():
        count = {
            "M4_structured_freshness_p95": m["freshness_structured"]["runs"],
            "M4_dynamic_freshness_p95": m["freshness_dynamic"]["runs"],
            "M5_query_latency_p95_warm": m["query"]["warm"]["n"],
            "M5_query_latency_p95_cold": m["query"]["cold"]["n"],
        }[name]
        print(f"| {labels[name]} | {count} | {verdict['value_sec']} | ≤ {verdict['limit_sec']} | **{verdict['verdict']}** |")
    sanity_passed = all(m["query"]["sanity"].values())
    print(f"| 合成查询夹具 sanity | {result['params']['queries']} | — | 全部检查通过 | **{'PASS' if sanity_passed else 'FAIL'}** |")
    print(f"\n性能夹具：{result['params']['events']} 个合成事件；查询集：{result['params']['queries']} 条固定查询。")
    print(f"结构化源 scheduler 配置：{m['freshness_structured']['scheduler']}")
    print(f"结构化源未找到：{m['freshness_structured']['not_found']}；动态活动未找到：{m['freshness_dynamic']['not_found']}。")
    print(f"get_timeline P95={m['timeline']['p95']} s；get_evidence P95={m['evidence']['p95']} s（仅报告）。")


def _markdown_report(result: dict[str, Any]) -> str:
    m = result["measured"]
    labels = {
        "M4_structured_freshness_p95": "M4 结构化源新鲜度 P95",
        "M4_dynamic_freshness_p95": "M4 动态活动新鲜度 P95",
        "M5_query_latency_p95_warm": "M5 查询时延 P95（warm）",
        "M5_query_latency_p95_cold": "M5 查询时延 P95（cold）",
    }
    rows = []
    for name, verdict in result["verdicts"].items():
        count = {
            "M4_structured_freshness_p95": m["freshness_structured"]["runs"],
            "M4_dynamic_freshness_p95": m["freshness_dynamic"]["runs"],
            "M5_query_latency_p95_warm": m["query"]["warm"]["n"],
            "M5_query_latency_p95_cold": m["query"]["cold"]["n"],
        }[name]
        rows.append(f"| {labels[name]} | {count} | {verdict['value_sec']} s | ≤ {verdict['limit_sec']} s | **{verdict['verdict']}** |")
    sanity_passed = all(m["query"]["sanity"].values())
    rows.append(f"| 合成查询夹具 sanity | {result['params']['queries']} | — | 全部检查通过 | **{'PASS' if sanity_passed else 'FAIL'}** |")
    env = result["environment"]
    return "\n".join([
        "## E5 运行指标 benchmark",
        "",
        f"- 时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 环境：{env['machine_model']} / macOS {env['macos_version']} / Python {env['python']} / SQLite {env['sqlite']}",
        f"- 合成事件：{result['params']['events']}；查询：{result['params']['queries']} 条固定查询；seed：{result['params']['seed']}；fixture digest：`{result['params']['fixture_digest']}`",
        f"- 数据库：{result['db']['raw_events_rows']} 行，{result['db']['db_mb']} MB；导入 {result['ingest']['seconds']} 秒。",
        f"- 结构化源 scheduler 配置（来自隔离 HOME 的 Config）：`{m['freshness_structured']['scheduler']}`。",
        "",
        "| 指标 | 样本 | P95 | 通过线 | 判定 |",
        "|---|---:|---:|---:|---|",
        *rows,
        "",
        f"未找到样本：结构化源 {m['freshness_structured']['not_found']}；动态活动 {m['freshness_dynamic']['not_found']}。",
        f"查询 sanity：`{m['query']['sanity']}`。",
        f"get_timeline P95：{m['timeline']['p95']} s；get_evidence P95：{m['evidence']['p95']} s（仅报告）。",
        "",
        "本报告仅使用脚本生成的合成事件；结构化源新鲜度经过 `SourcesScheduler` 公共调度入口并包含 scheduler 等待。",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=int, default=DEFAULT_EVENTS, help="fixture size (default 100000)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--query-reps", type=int, default=1, help="measured warm repetitions of the 100 fixed queries")
    ap.add_argument("--freshness-runs", type=int, default=20)
    ap.add_argument("--dynamic-runs", type=int, default=20)
    ap.add_argument("--out", type=Path, help="write the full result as JSON")
    ap.add_argument("--report-md", type=Path,
                    help="write an E5 markdown section (default: docs/eval-reports/E5-<timestamp>.md)")
    args = ap.parse_args(argv)
    if args.events < 200:
        ap.error("--events must be at least 200 (the planted markers need room)")
    result = run_benchmark(events=args.events, seed=args.seed, query_reps=args.query_reps,
                           freshness_runs=args.freshness_runs, dynamic_runs=args.dynamic_runs)
    _print_summary(result)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    report_path = args.report_md or Path("docs/eval-reports") / f"E5-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    if not report_path.is_absolute():
        report_path = REPO_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_markdown_report(result), encoding="utf-8")
    print(f"wrote {report_path}")
    failed = any(v["verdict"] != "PASS" for v in result["verdicts"].values())
    failed = failed or not all(result["measured"]["query"]["sanity"].values())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
