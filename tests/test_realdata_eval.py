"""evals/realdata harness: synthetic DBs and a fake `claude` only. No real data, no real model calls."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from evals.realdata import __main__ as cli
from evals.realdata import judge, report
from evals.realdata import validate as validate_mod
from evals.realdata.__main__ import main
from evals.realdata.paths import REPO_ROOT, resolve_data_dir
from evals.realdata.prepare import contamination_counts, strip_transcripts
from evals.realdata.questions import QuestionError, load_questions, parse_question
from evals.realdata.replay import check_buckets, forbidden_hits, scan_leaks
from evals.realdata.sandbox import build_profile
from penpal.store.db import close, init_db


def _q(**kw):
    base = {"id": "q1", "group": "A", "grant": ["Work"], "query": "what?", "gold_answer": "gold"}
    return parse_question({**base, **kw})


# ---- question parsing ----

def test_question_parsing_accepts_full_and_minimal(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text(json.dumps({"id": "a", "group": "A", "grant": ["Work", "Personal"], "query": "q", "gold_answer": "g"}) + "\n\n"
                 + json.dumps({"id": "b", "group": "D", "grant": ["Work"], "query": "q", "gold_answer": "-",
                               "expect": "must_not_see", "gold_event_ids": [12, "evt_13"], "gold_match": "zzz",
                               "human_verdicts": {"kp": "correct"}}) + "\n")
    a, b = load_questions(p)
    assert a.expect == "answer" and a.grant == ("Personal", "Work")
    assert b.event_ids == {"evt_12", "evt_13"} and b.gold_match == ("zzz",)


@pytest.mark.parametrize("patch", [{"expect": "maybe"}, {"grant": []}, {"grant": "Work"}, {"query": ""},
                                   {"expect": "must_not_see"}, {"human_verdicts": {"kp": "great"}}])
def test_question_parsing_rejects_bad_input(patch):
    with pytest.raises(QuestionError):
        _q(**patch)


def test_question_file_rejects_bad_json_and_duplicates(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text("{not json}\n")
    with pytest.raises(QuestionError, match="line 1"):
        load_questions(p)
    row = json.dumps({"id": "a", "group": "A", "grant": ["Work"], "query": "q", "gold_answer": "g"})
    p.write_text(row + "\n" + row + "\n")
    with pytest.raises(QuestionError, match="duplicate"):
        load_questions(p)


def test_data_dir_never_inside_repo():
    with pytest.raises(SystemExit):
        resolve_data_dir(REPO_ROOT / "evals" / "data")
    with pytest.raises(SystemExit):
        resolve_data_dir(REPO_ROOT)


# ---- sandbox profile ----

def test_profile_denies_paths_and_regexes():
    prof = build_profile([Path("/Users/x/.penpal"), "/tmp/evals"], ["abc-123", r"/\.claude/projects/[^/]*foo"])
    assert prof.startswith("(version 1)\n(allow default)\n(deny file-read* file-write*")
    assert '(subpath "/Users/x/.penpal")' in prof and '(subpath "/tmp/evals")' in prof
    assert '(regex #"abc-123")' in prof and r'(regex #"/\.claude/projects/[^/]*foo")' in prof
    assert prof.rstrip().endswith(")")


def test_profile_rejects_unsafe_input():
    with pytest.raises(ValueError):
        build_profile([], [])
    with pytest.raises(ValueError):
        build_profile(["/"])
    with pytest.raises(ValueError):
        build_profile(["/a"], ['x") (allow default'])
    assert 'a\\"b' in build_profile(['/a"b'])  # path quotes are escaped, not injected


# ---- synthetic DB ----

def _make_db(path: Path) -> Path:
    init_db(path)
    conn = sqlite3.connect(path)
    n = 0

    def add(source, text, ts, bucket="Work", session=None, locator=None):
        nonlocal n
        n += 1
        cur = conn.execute(
            "INSERT INTO raw_events(source,event_type,ts_start,content_text,content_hash,created_at,bucket,session_id) "
            "VALUES (?, 'semantic_event', ?, ?, ?, ?, ?, ?)", (source, ts, text, f"h{n}", ts, bucket, session))
        if locator:
            conn.execute("INSERT INTO evidence(event_id,locator,created_at) VALUES (?,?,?)", (cur.lastrowid, locator, ts))
        return cur.lastrowid

    add("claude_code", "alpha-gold discussed in old session", "2026-01-01T10:00:00", session="old-sess")
    add("claude_code", "building the eval harness, gold-in-build-session", "2026-09-01T10:00:00", session="build-sess")
    add("codex_cli", "after cutoff gold-after", "2026-10-05T10:00:00", locator="/x/rollout-late-sess.jsonl")
    add("claude_code", "gold-in-locator-only", "2026-02-01T10:00:00", locator="/x/projects/drop-me-123/s.jsonl")
    add("chrome_history", "visited gold-in-browser", "2026-10-06T10:00:00")
    add("zsh_history", "secretword in private shell", "2026-03-01T10:00:00", bucket="Personal")
    add("git_log", "commit about secretword plumbing", "2026-03-02T10:00:00", bucket="Work")
    conn.commit()
    conn.close()
    close()
    return path


def test_strip_transcripts_removes_sessions_cutoff_and_keeps_fts_consistent(tmp_path):
    db = _make_db(tmp_path / "s.db")
    deleted = strip_transcripts(db, ["build-sess", "drop-me-123"], "2026-10-01T00:00:00")
    assert deleted == {"sessions": 2, "after": 1}
    conn = sqlite3.connect(db)
    left = {r[0] for r in conn.execute("SELECT content_text FROM raw_events")}
    assert not any("build-session" in t or "gold-after" in t or "locator-only" in t for t in left)
    assert any("alpha-gold" in t for t in left) and any("gold-in-browser" in t for t in left)  # non-transcript kept
    assert conn.execute("SELECT COUNT(*) FROM raw_events_fts WHERE raw_events_fts MATCH '\"build-session\"'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0  # cascade
    conn.close()


def test_contamination_counts_are_counts_over_transcript_rows_only(tmp_path):
    db = _make_db(tmp_path / "s.db")
    qs = [_q(id="c1", gold_match=["alpha-gold"]), _q(id="c2", gold_match=["gold-in-browser"]),
          _q(id="c3", gold_answer="nothing like this")]
    assert contamination_counts(db, qs) == {"c1": 1, "c2": 0, "c3": 0}  # browser row is not a transcript


# ---- judge parsing ----

@pytest.mark.parametrize("text", ["", "   ", "not json", "[1]", '{"verdict": "correct"}', '{"verdict": "great", "reason": "x"}',
                                  '{"verdict": "correct", "reason": ""}', '{"verdict": null, "reason": "x"}',
                                  'Sure! {"verdict": "correct", "reason": "ok"}', None])
def test_malformed_judge_output_is_error_never_correct(text):
    assert judge.parse_judge_output(text)["verdict"] == "error"


def test_valid_judge_output_parses_plain_and_fenced():
    assert judge.parse_judge_output('{"verdict": "partial", "reason": "missing source"}') == {"verdict": "partial", "reason": "missing source"}
    assert judge.parse_judge_output('```json\n{"verdict": "correct", "reason": "same"}\n```')["verdict"] == "correct"


def test_judge_prompt_states_not_found_rule():
    assert "expect=not_found" in judge.JUDGE_PROMPT and "presents nothing" in judge.JUDGE_PROMPT


# ---- must_not_see (mechanical) ----

def test_forbidden_hits_by_event_id_and_text_without_echoing_gold():
    q = _q(expect="must_not_see", gold_event_ids=[7], gold_match=["TopSecret"])
    assert forbidden_hits(q, json.dumps({"results": [{"id": "evt_70"}]})) == []  # evt_70 is not evt_7
    hits = forbidden_hits(q, json.dumps({"results": [{"id": "evt_7", "snippet": "a topsecret b"}]}))
    assert hits == ["event_id#0", "match#0"] and "TopSecret" not in "".join(hits)


def test_mechanical_verdict_from_replay():
    q = _q(expect="must_not_see", gold_match=["x"])
    assert judge.mechanical_must_not_see(q, {"forbidden_hits": []})["verdict"] == "correct"
    assert judge.mechanical_must_not_see(q, {"forbidden_hits": ["match#0"]})["verdict"] == "wrong"
    assert judge.mechanical_must_not_see(q, None)["verdict"] == "error"


def test_leak_and_bucket_checks():
    assert scan_leaks("nothing here") == []
    assert "id_number" in scan_leaks("id 11010519491231002X ok")
    assert check_buckets({"results": [{"bucket": "Work"}, {"bucket": "Personal"}], "events": [{"bucket": "Private"}]}, {"Work"}) == ["Personal", "Private"]


# ---- report aggregation ----

def _res(id_, secs, tools, cost):
    return {"id": id_, "seconds": secs, "tool_calls": tools, "cost_usd": cost}


def _ver(id_, v):
    return {"id": id_, "verdict": v, "reason": "r"}


def test_report_aggregation_exclusive_and_agreement():
    qs = {i: _q(id=i, human_verdicts=h) for i, h in
          {"a": {"kp": "correct", "bash": "wrong"}, "b": {"kp": "correct"}, "c": {}, "d": {}}.items()}
    results = {"kp": [_res("a", 10, 2, 0.1), _res("b", 20, 4, 0.2), _res("c", 30, 6, 0.3), _res("d", 5, 1, 0.0)],
               "bash": [_res("a", 40, 9, 0.5), _res("b", 50, 11, 0.5), _res("c", 60, 3, 0.5)]}
    judged = {"kp": [_ver("a", "correct"), _ver("b", "wrong"), _ver("c", "correct"), {"id": "d", "verdict": "n/a", "reason": ""}],
              "bash": [_ver("a", "wrong"), _ver("b", "correct"), _ver("c", "correct")]}
    replay = {"calls": 3, "skipped": 0, "latency_ms": {"p50": 1.0, "p95": 2.0, "max": 3.0}, "leaks": [{"id": "a", "tool": "t", "patterns": ["api_key"]}],
              "bucket_violations": [], "per_question": {}}
    agg = report.aggregate(qs, results, judged, replay)
    kp = agg["arms"]["kp"]
    assert (kp["n"], kp["correct"], kp["wrong"]) == (3, 2, 1) and kp["accuracy"] == pytest.approx(2 / 3)
    assert kp["median_seconds"] == 15 and kp["max_seconds"] == 30 and kp["median_tool_calls"] == 3
    assert kp["cost_usd"] == pytest.approx(0.6)
    assert kp["judge_disagreements"] == ["b"] and kp["judge_agreement"] == 0.5
    assert agg["exclusive"] == {"compared": 3, "kp_only": ["a"], "bash_only": ["b"]}
    md = report.render(agg, "run1")
    assert "p95 2.0" in md and "leak hits: 1 (a:api_key)" in md and "Penpal correct, bash not: 1" in md


# ---- end to end with a fake claude on PATH ----

FAKE_CLAUDE = """#!{py}
import json, os, sys
a = sys.argv
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(a[1:]) + "\\n")
def emit(*evs):
    for e in evs: print(json.dumps(e))
if "--tools" in a:
    emit({{"type": "result", "result": os.environ.get("FAKE_JUDGE_REPLY", "garbage"), "total_cost_usd": 0.01}})
    sys.exit(0)
if "--mcp-config" in a:
    cfg = json.load(open(a[a.index("--mcp-config") + 1]))
    assert cfg["mcpServers"]["penpal"]["env"]["PENPAL_TOKEN"].startswith("kp_")
    use = {{"type": "tool_use", "name": "mcp__penpal__search_context", "input": {{"keywords": ["secretword"]}}}}
else:
    use = {{"type": "tool_use", "name": "Bash", "input": {{"command": "grep -r secretword ~"}}}}
emit({{"type": "assistant", "message": {{"content": [use]}}}},
     {{"type": "result", "result": os.environ.get("FAKE_ANSWER", "found it"), "total_cost_usd": 0.05, "is_error": False}})
"""
FAKE_SANDBOX = "#!/bin/sh\nshift 2\nexec \"$@\"\n"


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in (("claude", FAKE_CLAUDE.format(py=sys.executable)), ("sandbox-exec", FAKE_SANDBOX)):
        (b / name).write_text(body)
        (b / name).chmod(b.joinpath(name).stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_JUDGE_REPLY", '{"verdict": "correct", "reason": "matches gold"}')
    yield log
    close()


def test_end_to_end_prepare_run_replay_judge_report(tmp_path, fake_bin, capsys):
    src = _make_db(tmp_path / "src.db")
    data = tmp_path / "evals"
    data.mkdir()
    rows = [
        {"id": "A1", "group": "A", "grant": ["Work", "Personal"], "query": "q1", "gold_answer": "g1"},
        {"id": "A2", "group": "A", "grant": ["Work"], "query": "q2", "gold_answer": "nothing exists", "expect": "not_found"},
        {"id": "D1", "group": "D", "grant": ["Work"], "query": "q3", "gold_answer": "-", "expect": "must_not_see",
         "gold_match": ["private shell"]},
        {"id": "D2", "group": "D", "grant": ["Work", "Personal"], "query": "q4", "gold_answer": "-", "expect": "must_not_see",
         "gold_match": ["private shell"], "human_verdicts": {"kp": "wrong"}},
    ]
    (data / "questions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    base = ["--data-dir", str(data)]

    assert main(["prepare", *base, "--db", str(src), "--exclude-session", "build-sess", "--authored-after", "2026-10-01T00:00:00"]) == 0
    out = capsys.readouterr().out
    # The run directory is a random temp path that can itself contain "g1"; check the rest only.
    report = "\n".join(line for line in out.splitlines() if not line.startswith("run dir:"))
    assert "excluded sessions=1, after cutoff=1" in out and "g1" not in report
    assert "gold validation: 3 checked" in out  # A1, D1, D2 (not_found skipped); no row text
    run_dir = next((data / "runs").iterdir())

    assert main(["run", *base, "--arm", "kp", "--penpal-bin", "penpal", "--jobs", "1"]) == 0
    assert main(["run", *base, "--arm", "bash", "--deny-regex", "build-sess", "--jobs", "1"]) == 0
    kp = [json.loads(l) for l in (run_dir / "results-kp.jsonl").read_text().splitlines()]
    bash = [json.loads(l) for l in (run_dir / "results-bash.jsonl").read_text().splitlines()]
    assert [r["id"] for r in kp] == ["A1", "A2", "D1", "D2"]
    assert [r["id"] for r in bash] == ["A1", "A2"]  # must_not_see is Penpal-only
    assert all(r["exit"] == 0 and r["tool_calls"] == 1 and r["cost_usd"] == 0.05 for r in kp + bash)
    assert kp[0]["tools"][0]["args"] == {"keywords": ["secretword"]} and "args" not in bash[0]["tools"][0]
    assert 'regex #"build-sess"' in (run_dir / "bash_arm.sb").read_text()
    assert "kp_" not in "".join(p.read_text() for p in run_dir.glob("*.jsonl"))  # no token in results
    assert not list(run_dir.rglob("mcp-*.json"))

    n_calls = len(fake_bin.read_text().splitlines())
    assert main(["run", *base, "--arm", "kp"]) == 0  # resumable: nothing re-run
    assert len(fake_bin.read_text().splitlines()) == n_calls

    assert main(["replay", *base]) == 0
    replay = json.loads((run_dir / "replay.json").read_text())
    assert replay["calls"] == 4 and replay["bucket_violations"] == [] and replay["latency_ms"]["p95"] is not None
    assert replay["per_question"]["D1"]["forbidden_hits"] == []      # Work grant never sees the Personal row
    assert replay["per_question"]["D2"]["forbidden_hits"] == ["match#0"]  # broader grant does

    assert main(["judge", *base]) == 0
    jk = {j["id"]: j for j in map(json.loads, (run_dir / "judged-kp.jsonl").read_text().splitlines())}
    assert jk["A1"]["verdict"] == "correct" and jk["A1"]["method"] == "llm"
    assert (jk["D1"]["verdict"], jk["D1"]["method"]) == ("correct", "mechanical")
    assert jk["D2"]["verdict"] == "wrong"

    assert main(["report", *base]) == 0
    md = (run_dir / "report.md").read_text()
    assert "Penpal correct, bash not" in md and "p95" in md
    assert "g1" not in md and "q1" not in md  # no question or answer text in the report


def test_unparseable_judge_reply_yields_error_verdict(tmp_path, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_JUDGE_REPLY", "I think it is correct")
    data = tmp_path / "evals"
    (data / "runs" / "r1").mkdir(parents=True)
    q = {"id": "A1", "group": "A", "grant": ["Work"], "query": "q", "gold_answer": "g"}
    (data / "questions.jsonl").write_text(json.dumps(q) + "\n")
    (data / "runs" / "r1" / "results-kp.jsonl").write_text(json.dumps(
        {"id": "A1", "exit": 0, "answer": "some answer", "tools": [], "seconds": 1, "tool_calls": 0}) + "\n")
    assert main(["judge", "--data-dir", str(data)]) == 0
    j = json.loads((data / "runs" / "r1" / "judged-kp.jsonl").read_text())
    assert j["verdict"] == "error"


# ── transient failures (API/usage errors) are retried and rerun on resume, never graded ──────────

def test_transient_rows_are_dropped_on_resume_and_real_failures_kept(tmp_path):
    from evals.realdata.runner import done_ids, is_transient

    path = tmp_path / "results-bash.jsonl"
    rows = [
        {"id": "Q1", "exit": 0, "is_error": False, "tool_calls": 3, "answer": "ok"},
        {"id": "Q2", "exit": 1, "is_error": True, "tool_calls": 0, "answer": ""},       # API error, nothing done
        {"id": "Q3", "exit": 1, "is_error": True, "tool_calls": 5, "answer": ""},       # failed after working: keep
        {"id": "Q4", "verdict": "error", "method": "llm", "reason": "empty judge output"},
        {"id": "Q5", "verdict": "wrong", "method": "llm", "reason": "x"},
        {"id": "Q6", "exit": 1, "is_error": True, "tool_calls": 7, "answer": "API Error: Connection refused"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert [is_transient(r) for r in rows] == [False, True, False, True, False, True]
    assert done_ids(path) == {"Q1", "Q3", "Q5"}
    assert {json.loads(l)["id"] for l in path.read_text().splitlines()} == {"Q1", "Q3", "Q5"}


def test_parse_stream_error_result_is_not_an_answer():
    from evals.realdata.runner import parse_stream

    out = parse_stream(json.dumps({"type": "result", "is_error": True, "result": "API Error: overloaded"}))
    assert out["is_error"] and out["answer"] == "" and "overloaded" in out["error"]


def test_judge_retries_empty_output(monkeypatch):
    calls = []

    class P:
        def __init__(self, stdout):
            self.stdout, self.returncode = stdout, 0

    def fake_run(*a, **k):
        calls.append(1)
        text = "" if len(calls) < 3 else '{"verdict": "correct", "reason": "r"}'
        return P(json.dumps({"type": "result", "result": text}) if text else "")

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    monkeypatch.setattr(judge.time, "sleep", lambda s: None)
    assert '"correct"' in judge.call_judge("p", model="m", claude="claude", retries=2, wait=0)
    assert len(calls) == 3


# ---- Codex host (cheap model) ----

def _ev(*evs):
    return "\n".join(json.dumps(e) for e in evs) + "\n"


def _msg(text):
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def _mcp(tool, args, server="penpal"):
    return {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": server, "tool": tool,
                                               "arguments": args, "status": "completed"}}


def test_codex_command_is_isolated_and_never_carries_the_token():
    from evals.realdata.runner import CODEX_DISABLE, build_codex_command

    wrapper = Path("/tmp/run/kp-A1.sh")
    cmd = build_codex_command("PROMPT", model="gpt-6-luna", codex="/bin/codex", wrapper=wrapper)
    assert cmd[:2] == ["/bin/codex", "exec"] and cmd[-1] == "PROMPT"
    assert cmd[cmd.index("-m") + 1] == "gpt-6-luna" and cmd[cmd.index("-s") + 1] == "read-only"
    disabled = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--disable"]
    assert disabled == CODEX_DISABLE and "code_mode_host" not in disabled  # MCP tools need code_mode_host
    assert 'web_search="disabled"' in cmd
    assert f'mcp_servers.penpal.command="{wrapper}"' in cmd
    assert 'mcp_servers.penpal.default_tools_approval_mode="approve"' in cmd
    assert "--ignore-user-config" in cmd and "--ephemeral" in cmd and "--json" in cmd
    assert "kp_" not in " ".join(cmd)
    plain = build_codex_command("PROMPT", model="m", codex="codex")
    assert not any("mcp_servers" in a for a in plain)  # judge calls get no MCP server


def test_codex_wrapper_is_0700_quoted_and_exports_the_grant(tmp_path):
    import subprocess

    from evals.realdata.runner import write_codex_wrapper

    tools = tmp_path / "My Tools"
    tools.mkdir()
    fake_kp = tools / "penpal"
    env_out = tmp_path / "env.txt"
    fake_kp.write_text('#!/bin/sh\necho "$PENPAL_TOKEN|$PENPAL_CLIENT|$HOME|$*" > "$FAKE_ENV_OUT"\n')
    fake_kp.chmod(0o700)
    token = "kp_a'b $(touch x)"  # quotes and shell syntax must survive as literal text
    wrapper = tmp_path / "kp-A1.sh"
    write_codex_wrapper(wrapper, keypulse_bin=str(fake_kp), token=token, client="rdeval-work",
                        home=tmp_path / "home")
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
    body = wrapper.read_text()
    assert body.startswith("#!/bin/sh\n") and "exec " in body and "mcp serve" in body
    assert "export PENPAL_TOKEN=" in body and "export HOME=" in body
    res = subprocess.run([str(wrapper)], capture_output=True, text=True, timeout=20,
                         env={"PATH": os.environ["PATH"], "FAKE_ENV_OUT": str(env_out)})
    assert res.returncode == 0, res.stderr
    got = env_out.read_text().strip().split("|")
    assert got[0] == token and got[1] == "rdeval-work" and got[2] == str(tmp_path / "home")
    assert got[3] == "mcp serve"  # the token arrived verbatim: no shell expansion


def test_parse_codex_stream_keeps_last_answer_tools_and_flags_other_tools():
    from evals.realdata.runner import parse_codex_stream

    out = parse_codex_stream(_ev(
        {"type": "thread.started"},
        _msg("thinking out loud"),
        _mcp("search_context", json.dumps({"keywords": ["secretword"]})),
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0}},
        _mcp("get_timeline", {"start": "2026-01-01"}),
        _msg("final answer (Work)"),
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
    ))
    assert out["answer"] == "final answer (Work)" and out["is_error"] is False and out["cost_usd"] is None
    assert [t["name"] for t in out["tools"]] == ["mcp__penpal__search_context", "mcp__penpal__get_timeline"]
    assert out["tools"][0]["args"] == {"keywords": ["secretword"]} and out["tools"][1]["args"] == {"start": "2026-01-01"}
    assert out["non_kp_tools"] == ["command_execution"] and out["usage"] == {"input_tokens": 10, "output_tokens": 4}


def test_parse_codex_stream_failure_and_foreign_servers():
    from evals.realdata.runner import parse_codex_stream

    failed = parse_codex_stream(_ev(_msg("partial"), {"type": "turn.failed", "error": {"message": "rate limit hit"}}))
    assert failed["is_error"] and failed["answer"] == "" and failed["error"] == "rate limit hit"
    err = parse_codex_stream(_ev({"type": "error", "message": "stream disconnected before completion"}))
    assert err["is_error"] and err["error"].startswith("stream disconnected")
    foreign = parse_codex_stream(_ev(_mcp("read_file", {}, server="filesystem"), _msg("x")))
    assert foreign["tools"] == [] and foreign["non_kp_tools"] == ["mcp_tool_call:filesystem"]
    assert parse_codex_stream("not json\n")["answer"] == ""  # garbage lines are skipped, not fatal


def test_codex_errors_are_transient_only_when_api_side():
    from evals.realdata.runner import is_transient

    base = {"host": "codex", "exit": 1, "is_error": True, "answer": "", "tool_calls": 2}
    assert is_transient({**base, "error": "stream disconnected before completion"})
    assert is_transient({**base, "error": "HTTP 429 Too Many Requests"})
    assert is_transient({**base, "error": "server returned 503"})
    assert not is_transient({**base, "error": "model not found"})
    assert not is_transient({**base, "host": "claude", "error": "stream disconnected"})  # claude rows: old rule


def test_judge_codex_path_takes_last_agent_message(monkeypatch):
    seen = []

    class P:
        returncode = 0
        stdout = _ev(_msg("draft"), _msg('{"verdict": "partial", "reason": "missing date"}'))

    def fake_run(cmd, **k):
        seen.append(cmd)
        return P()

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    text = judge.call_judge("grade this", model="gpt-6-luna", host="codex", codex="/bin/codex", retries=0)
    assert judge.parse_judge_output(text)["verdict"] == "partial"
    cmd = seen[0]
    assert cmd[0] == "/bin/codex" and cmd[-1] == "grade this" and "--disable" in cmd
    assert not any("mcp_servers" in a for a in cmd)


def test_codex_cli_refuses_bash_arm_and_all(tmp_path, capsys):
    data = tmp_path / "evals"
    data.mkdir()
    (data / "questions.jsonl").write_text(json.dumps(
        {"id": "A1", "group": "A", "grant": ["Work"], "query": "q", "gold_answer": "g"}) + "\n")
    base = ["--data-dir", str(data)]
    with pytest.raises(SystemExit, match="bash arm stays on Claude"):
        main(["run", *base, "--arm", "bash", "--host", "codex"])
    with pytest.raises(SystemExit, match="stays on Claude"):
        main(["all", *base, "--host", "codex"])
    assert not (data / "runs").exists()  # nothing ran


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    codex = b / "codex"
    codex.write_text(FAKE_CODEX.replace("__PY__", sys.executable))
    codex.chmod(0o700)
    kp = b / "penpal"
    kp.write_text('#!/bin/sh\necho "$PENPAL_TOKEN|$PENPAL_CLIENT|$HOME|$*" > "$FAKE_ENV_OUT"\n')
    kp.chmod(0o700)
    log, env_out = tmp_path / "codex.log", tmp_path / "kp-env.txt"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_ENV_OUT", str(env_out))
    yield {"codex": codex, "kp": kp, "log": log, "env": env_out}
    close()


# Fake `codex exec`: records argv, runs the Penpal MCP command like codex would, emits JSONL events.
FAKE_CODEX = """#!__PY__
import json, os, subprocess, sys
a = sys.argv
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(a[1:]) + "\\n")
def emit(e):
    print(json.dumps(e))
for x in a:
    if x.startswith("mcp_servers.penpal.command="):
        wrapper = json.loads(x.split("=", 1)[1])
        subprocess.run([wrapper], check=True, env=dict(os.environ), timeout=20)
emit({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "penpal", "tool": "search_context",
                                          "arguments": json.dumps({"keywords": ["secretword"]}), "status": "completed"}})
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "found it (Work)"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 7}})
"""


def test_codex_kp_run_end_to_end_with_fake_codex(tmp_path, fake_codex):
    run = tmp_path / "evals" / "runs" / "r1"
    run.mkdir(parents=True)
    _make_db(run / "penpal.db")
    (tmp_path / "evals" / "questions.jsonl").write_text(json.dumps(
        {"id": "A1", "group": "A", "grant": ["Work"], "query": "what?", "gold_answer": "g"}) + "\n")
    assert main(["run", "--data-dir", str(tmp_path / "evals"), "--run-dir", str(run), "--arm", "kp", "--host", "codex",
                 "--codex-bin", str(fake_codex["codex"]), "--penpal-bin", str(fake_codex["kp"])]) == 0
    row = json.loads((run / "results-kp.jsonl").read_text().splitlines()[0])
    assert row["host"] == "codex" and row["model"] == "gpt-6-luna" and row["exit"] == 0
    assert row["answer"] == "found it (Work)" and row["tool_calls"] == 1 and row["non_kp_tools"] == []
    assert row["tools"][0]["args"] == {"keywords": ["secretword"]} and row["usage"] == {"input_tokens": 7}
    token, client, home, _ = fake_codex["env"].read_text().split("|")
    assert token.startswith("kp_") and client == "rdeval-work" and home == str(run / "home")
    argv = json.loads(fake_codex["log"].read_text().splitlines()[0])
    assert token not in " ".join(argv) and token not in (run / "results-kp.jsonl").read_text()  # no token in argv or rows
    assert "gpt-6-luna" in argv and "--disable" in argv
    (override,) = [a for a in argv if a.startswith("mcp_servers.penpal.command=")]
    wrapper_path = Path(json.loads(override.split("=", 1)[1]))
    assert not wrapper_path.exists()  # deleted after the question


def test_report_lists_isolation_violations_and_host_model():
    qs = {"a": _q(id="a"), "b": _q(id="b")}
    results = {"kp": [{"id": "a", "seconds": 1, "tool_calls": 1, "cost_usd": None, "host": "codex",
                       "model": "gpt-6-luna", "non_kp_tools": ["command_execution"]},
                      {"id": "b", "seconds": 2, "tool_calls": 1, "cost_usd": None, "host": "codex",
                       "model": "gpt-6-luna", "non_kp_tools": []}]}
    judged = {"kp": [{"id": "a", "verdict": "correct", "method": "llm", "judge_host": "codex",
                      "judge_model": "gpt-6-luna"},
                     {"id": "b", "verdict": "wrong", "method": "llm", "judge_host": "codex",
                      "judge_model": "gpt-6-luna"}]}
    kp = report.aggregate(qs, results, judged, None)["arms"]["kp"]
    assert kp["isolation_violations"] == ["a"] and kp["isolation_count"] == 1
    assert (kp["host"], kp["model"], kp["judge_host"], kp["judge_model"]) == ("codex", "gpt-6-luna", "codex", "gpt-6-luna")
    md = report.render(report.aggregate(qs, results, judged, None), "run")
    assert "Isolation violations (kp, tools outside Penpal MCP ran): a" in md
    legacy = report.aggregate(qs, {"kp": [{"id": "a", "seconds": 1, "tool_calls": 0, "cost_usd": 0.1}]}, {}, None)
    assert legacy["arms"]["kp"]["host"] == "claude" and legacy["arms"]["kp"]["model"] == "-"


# ---- gold validation ----

def _gold_db(path: Path) -> dict:
    """Synthetic DB with known row ids. Returns {key: raw_events.id}."""
    init_db(path)
    conn = sqlite3.connect(path)
    ids = {}
    for key, text, title in (("alpha", "alpha-gold discussed", None), ("title", "unrelated body", "Secret Title Bar")):
        cur = conn.execute(
            "INSERT INTO raw_events(source,event_type,ts_start,content_text,content_hash,created_at,bucket,window_title) "
            "VALUES ('claude_code','semantic_event','2026-01-01T10:00:00',?,?,'2026-01-01T10:00:00','Work',?)",
            (text, f"gh-{key}", title))
        ids[key] = cur.lastrowid
    conn.commit()
    conn.close()
    close()
    return ids


def _gold_qs() -> list:
    return [parse_question(r) for r in [
        {"id": "G1", "group": "G", "grant": ["Work"], "query": "q", "gold_answer": "a",
         "gold_event_ids": ["evt_1"], "gold_match": ["ALPHA-gold"]},
        {"id": "G2", "group": "G", "grant": ["Work"], "query": "q", "gold_answer": "a",
         "gold_event_ids": [9999], "gold_match": ["alpha-gold"]},
        {"id": "G3", "group": "G", "grant": ["Work"], "query": "q", "gold_answer": "a",
         "gold_event_ids": [1], "gold_match": ["no-such-text"]},
        {"id": "G4", "group": "G", "grant": ["Work"], "query": "q", "gold_answer": "a",
         "gold_event_ids": [2]},
        {"id": "G5", "group": "G", "grant": ["Work"], "query": "q", "gold_answer": "a",
         "gold_event_ids": [2], "gold_match": ["secret title"]},
        {"id": "G6", "group": "G", "grant": ["Work"], "query": "q", "gold_answer": "nothing", "expect": "not_found"},
    ]]


def test_validate_pass_fail_warn_on_synthetic_db(tmp_path):
    db = tmp_path / "penpal.db"
    _gold_db(db)  # ids are 1 (alpha) and 2 (title) in a fresh DB
    res = {r.qid: r for r in validate_mod.validate_gold(_gold_qs(), db)}
    assert "G6" not in res  # not_found is not checked
    assert (res["G1"].status, res["G1"].reasons) == ("PASS", [])  # evt_ prefix, case-insensitive match
    assert (res["G2"].status, res["G2"].reasons) == ("FAIL", ["missing_event_id:9999"])
    assert (res["G3"].status, res["G3"].reasons) == ("FAIL", ["gold_match_not_in_gold_rows:#0"])
    assert (res["G4"].status, res["G4"].reasons) == ("WARN", ["no_gold_match"])
    assert (res["G5"].status, res["G5"].reasons) == ("PASS", [])  # window_title of the gold row counts
    lines = "\n".join(validate_mod.format_lines(list(res.values())))
    assert "G2 FAIL missing_event_id:9999" in lines and "2 fail" in lines
    assert "alpha" not in lines and "no-such-text" not in lines  # no row text, no gold text


def test_validate_window_title_counts_and_answer_without_ids_warns(tmp_path):
    db = tmp_path / "penpal.db"
    _gold_db(db)
    qs = [parse_question({"id": "W1", "group": "W", "grant": ["Work"], "query": "q", "gold_answer": "a",
                          "gold_match": ["secret title"]}),
          parse_question({"id": "W2", "group": "W", "grant": ["Work"], "query": "q", "gold_answer": "a",
                          "gold_event_ids": ["abc"], "gold_match": ["alpha-gold"]}),
          parse_question({"id": "W3", "group": "W", "grant": ["Work"], "query": "q", "gold_answer": "a",
                          "gold_event_ids": [2], "gold_match": ["secret title"]})]
    res = {r.qid: r for r in validate_mod.validate_gold(qs, db)}
    assert (res["W1"].status, res["W1"].reasons) == ("WARN", ["gold_match_unchecked_no_gold_rows", "no_gold_event_ids"])
    assert res["W2"].status == "FAIL" and res["W2"].reasons == ["bad_event_id_format:#0"]  # no extra gold_match noise
    assert (res["W3"].status, res["W3"].reasons) == ("PASS", [])  # window_title of gold row counts


def test_validate_cli_exit_code_follows_fails(tmp_path, capsys):
    db = tmp_path / "penpal.db"
    _gold_db(db)
    data = tmp_path / "evals"
    data.mkdir()
    qpath = data / "questions.jsonl"
    qpath.write_text(json.dumps({"id": "P1", "group": "P", "grant": ["Work"], "query": "q", "gold_answer": "a",
                                 "gold_event_ids": [1], "gold_match": ["alpha-gold"]}) + "\n")
    base = ["validate", "--data-dir", str(data), "--questions", str(qpath), "--run-dir", str(tmp_path)]
    assert main(base) == 0
    assert "P1 PASS" in capsys.readouterr().out
    qpath.write_text(json.dumps({"id": "P1", "group": "P", "grant": ["Work"], "query": "q", "gold_answer": "a",
                                 "gold_event_ids": [424242]}) + "\n")
    assert main(base) == 1
    assert "P1 FAIL missing_event_id:424242" in capsys.readouterr().out


def test_all_stops_on_gold_fail_unless_flag_given(tmp_path, monkeypatch, capsys):
    db = tmp_path / "penpal.db"
    _gold_db(db)
    data = tmp_path / "evals"
    run = data / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "penpal.db").write_bytes(db.read_bytes())
    qpath = data / "questions.jsonl"
    qpath.write_text(json.dumps({"id": "F1", "group": "F", "grant": ["Work"], "query": "q", "gold_answer": "a",
                                 "gold_event_ids": [777777], "gold_match": ["alpha-gold"]}) + "\n")
    calls = []
    for name in ("cmd_run", "cmd_replay", "cmd_judge", "cmd_report"):
        monkeypatch.setattr(cli, name, lambda args, *a, _n=name: calls.append(_n) or 0)
    base = ["all", "--data-dir", str(data), "--questions", str(qpath), "--run-dir", str(run)]

    assert main(base) == 1 and calls == []  # stopped before any arm ran
    assert "F1 FAIL missing_event_id:777777" in capsys.readouterr().out

    assert main([*base, "--allow-invalid-gold"]) == 0
    assert calls == ["cmd_run", "cmd_run", "cmd_replay", "cmd_judge", "cmd_report"]


# ---- judge rubric v2 ----

def test_judge_prompt_has_asked_only_rules_and_still_formats():
    p = judge.JUDGE_PROMPT
    assert "Grade only what the question asks" in p
    assert "must not lower the verdict" in p
    assert '"partial" when the core answer is right' in p
    assert "presents nothing" in p and "related records" in p  # not_found clarification kept
    p.format(query="q", gold="g", expect="answer", answer="a")  # placeholders intact


def test_judged_rows_carry_rubric_version(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    q = parse_question({"id": "J1", "group": "J", "grant": ["Work"], "query": "when?",
                        "gold_answer": "2026-01-01, commit abc1234"})
    (run / "results-kp.jsonl").write_text(json.dumps(
        {"id": "J1", "arm": "kp", "exit": 0, "is_error": False, "answer": "2026-01-01"}) + "\n")
    prompts = []

    def fake_judge(prompt, **kw):
        prompts.append(prompt)
        return '{"verdict": "correct", "reason": "asked fact matches"}'

    monkeypatch.setattr(judge, "call_judge", fake_judge)
    judge.judge_arm("kp", {"J1": q}, run, model="m", log=lambda *a: None)
    row = json.loads((run / "judged-kp.jsonl").read_text().splitlines()[0])
    assert row["rubric_version"] == judge.RUBRIC_VERSION == 2 and row["verdict"] == "correct"
    assert "2026-01-01, commit abc1234" in prompts[0] and "Grade only what the question asks" in prompts[0]



def test_run_parallel_runs_every_item_once_and_jobs_1_keeps_order() -> None:
    import threading
    from evals.realdata.runner import run_parallel
    seen, lock = [], threading.Lock()

    def fn(x):
        with lock:
            seen.append(x)
    run_parallel(fn, list(range(20)), 4)
    assert sorted(seen) == list(range(20))
    seen.clear()
    run_parallel(fn, list(range(5)), 1)
    assert seen == list(range(5))
