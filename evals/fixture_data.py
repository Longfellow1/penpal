"""Synthetic fixture events (single source of truth for the eval fixture library).

Everything here is invented. Names, URLs, "secrets" and amounts are fake; no real
user data was copied. ``evals.fixtures`` materialises these events into the on-disk
formats that each real source plugin reads (jsonl / history files / sqlite / repos /
markdown vaults), inside a temp directory.

Buckets (D23) are the *intended* classification of each event: Work / Personal /
Private / Financial. The mainline currently only assigns a bucket for claude_code,
codex_cli and git_log (by source default); the others are reported as unbucketed
until #19 lands. The intended bucket is still recorded so coverage can be tracked.

Timestamps are unique per source (the runner resolves fixture ids by (source, time)).
"""
from __future__ import annotations

from dataclasses import dataclass

BUCKETS = ("Work", "Personal", "Private", "Financial")
PROJECT_BUCKET_RULES = (
    ("-tmp-proj-life-admin", "Personal"),
    ("cx-s4", "Personal"),
)


@dataclass(frozen=True)
class FxEvent:
    id: str
    source: str
    ts: str  # UTC, "YYYY-MM-DDTHH:MM:SS"
    bucket: str
    text: str  # message / commit message / command / page title / note title
    actor: str = "user"  # claude_code only: user | assistant
    group: str = ""  # claude_code: project dir; git: repo name; chrome: profile; markdown: relative dir
    url: str = ""  # browser sources
    expect_ingest: bool = True
    app: str = ""  # capture events: the app the record was captured in (drives the bucket, D26)
    title: str = ""  # capture events: window title / page title
    ts_end: str = ""  # capture window sessions: end of the focus session


# Fake credential: matches the sink redaction pattern, must never be searchable.
FAKE_KEY = "sk-test-FAKE0000000000000000000000"

CC = "claude_code"
CX = "codex_cli"
GL = "git_log"
ZS = "zsh_history"
CH = "chrome_history"
SF = "safari_history"
MD = "markdown_vault"

W, P, V, F = "Work", "Personal", "Private", "Financial"


# R02 distractors: later sessions about the same recurring failures. Each one repeats the keywords
# (high term frequency, medium length) but gives a different, wrong-for-the-question explanation.
_EXPORT_TRIES = ("the cron entry", "the disk space", "the network route", "the service account", "the locale setting")
_LOOP_TRIES = ("the load balancer", "the CDN rules", "the sticky session", "the TLS redirect", "the base path")
_R02_NOISE: tuple[FxEvent, ...] = tuple(
    FxEvent(
        f"cc-rn{i + 1}", CC, f"2026-05-{8 + i:02d}T04:00:00", W,
        f"the nightly export failed again, run {i + 1}. The nightly export failed after the retry, the nightly export "
        f"failed after the restart. The quota dashboard says the quota is fine, the bucket is fine, the failed run left "
        f"no quota or bucket error, trying {_EXPORT_TRIES[i]} next",
        group=f"-tmp-proj-pipeline-n{i + 1}",
    )
    for i in range(5)
) + tuple(
    FxEvent(
        f"cc-rl{i + 1}", CC, f"2026-05-{11 + i:02d}T05:00:00", W,
        f"the login redirect loop again, round {i + 1}. The login redirect loop shows up with the cookie and without it. "
        f"The cookie domain looks right, the callback url looks right, no reason found for the redirect loop, "
        f"checking {_LOOP_TRIES[i]} next",
        group=f"-tmp-proj-webapp-n{i + 1}",
    )
    for i in range(5)
)

EVENTS: tuple[FxEvent, ...] = (
    *_R02_NOISE,
    # -- Claude Code ----------------------------------------------------------
    FxEvent("cc-01", CC, "2026-04-20T02:00:00", W, "这个功能的边界我们定了：检索只读，不做写回，写回放到 P1 再说", group="-tmp-proj-memory-core"),
    FxEvent("cc-02", CC, "2026-04-20T02:05:00", W, "确认检索边界：search_context 只读，commit_memory 延后到 P1", "assistant", "-tmp-proj-memory-core"),
    FxEvent("cc-03", CC, "2026-04-21T03:00:00", W, "为什么没用方案 A 的 Redis 队列？因为要本地优先，不想引入常驻服务", group="-tmp-proj-memory-core"),
    FxEvent("cc-04", CC, "2026-04-21T03:04:00", W, "方案 A 的 Redis 队列被否决：需要常驻进程，违背本地优先原则；改用 SQLite WAL 队列", "assistant", "-tmp-proj-memory-core"),
    FxEvent("cc-05", CC, "2026-04-22T01:30:00", W, "昨天和小李聊的那个框架叫 LangGraph，他建议用它做 agent 编排", group="-tmp-proj-memory-core"),
    FxEvent("cc-06", CC, "2026-04-22T01:40:00", W, "Xiao Li suggested we call the vector store layer Harbor internally", group="-tmp-proj-memory-core"),
    FxEvent("cc-07", CC, "2026-04-22T05:00:00", P, "帮我整理澳洲 subclass 600 访客签证需要的材料：护照、资金证明、行程单、邀请函", group="-tmp-proj-life-admin"),
    FxEvent("cc-08", CC, "2026-04-22T05:03:00", P, "签证材料清单：护照扫描件、近三个月银行流水、行程单、体检报告（可选）", "assistant", "-tmp-proj-life-admin"),
    FxEvent("cc-09", CC, "2026-04-23T06:00:00", F, "帮我对比两张信用卡的年费：招行经典白 800 元，交行 Plus 600 元", group="-tmp-proj-life-admin"),
    FxEvent("cc-10", CC, "2026-04-23T14:00:00", V, "我最近在看心理咨询，预约了周四下午三点的咨询师", group="-tmp-proj-life-admin"),
    FxEvent("cc-11", CC, "2026-04-24T02:00:00", W, f"my deploy credential is api_key={FAKE_KEY} please keep the staging rotation notes", group="-tmp-proj-memory-core"),
    FxEvent("cc-12", CC, "2026-04-24T02:30:00", W, "The migration from schema v19 to v20 adds the access_grants and evidence tables", "assistant", "-tmp-proj-memory-core"),
    FxEvent("cc-13", CC, "2026-04-25T01:00:00", W, "排查 launchd 守护进程重启后丢失 cursor 的问题", group="-tmp-proj-memory-core"),
    FxEvent("cc-14", CC, "2026-04-25T01:10:00", W, "根因：cursor 在入库前就提交了；修复方式是入库成功之后才推进 cursor", "assistant", "-tmp-proj-memory-core"),
    FxEvent("cc-15", CC, "2026-04-26T04:00:00", P, "之前买的网球线型号是 Luxilon ALU Power 125，在 Tennis Warehouse 下的单", group="-tmp-proj-life-admin"),
    FxEvent("cc-16", CC, "2026-04-26T08:00:00", W, "发布 deadline 是 5 月 15 日，评审会安排在 5 月 8 日", group="-tmp-proj-memory-core"),
    FxEvent("cc-17", CC, "2026-04-28T08:00:00", W, "更新一下：发布 deadline 改到 5 月 30 日，评审会不变", group="-tmp-proj-memory-core"),
    FxEvent("cc-18", CC, "2026-04-27T02:00:00", W, "评测集要分有答案、无答案、权限受限三类，每个场景至少八条", group="-tmp-proj-memory-core"),
    FxEvent("cc-19", CC, "2026-04-27T03:00:00", W, "Codex 会话是按日期分目录的 rollout 文件，扫描要递归 sessions/YYYY/MM/DD", group="-tmp-proj-memory-core"),
    # -- R02 recurring problem: several sessions about the same failure, different reasons ----
    FxEvent("cc-r1", CC, "2026-05-08T02:00:00", W, "the nightly export failed again. The nightly export failed after the retry, the nightly export failed after the restart. Checked the quota dashboard, the quota is fine; checked the bucket policy, the bucket is fine; failed with a bigger timeout too, so not the quota, not the bucket. Next: the cron entry, since the nightly export failed on the old host schedule, and the failed run left no quota or bucket error in the log", group="-tmp-proj-pipeline-a"),
    FxEvent("cc-r2", CC, "2026-05-09T02:00:00", W, "nightly export failed again, the log shows exit code 137 so it ran out of memory, I will lower the batch size", group="-tmp-proj-pipeline-b"),
    FxEvent("cc-r3", CC, "2026-05-07T02:00:00", W, "nightly export failed because the storage quota on the backup bucket was exceeded", group="-tmp-proj-pipeline-c"),
    FxEvent("cc-r4", CC, "2026-05-12T02:00:00", W, "the login redirect loop is back. The login redirect loop shows up with the session cookie and without it, with the callback url and without it. Checked the cookie, the cookie domain, the callback url, the proxy headers: the redirect loop is still there, no reason found yet, the redirect loop reason may be the load balancer", group="-tmp-proj-webapp-a"),
    FxEvent("cc-r5", CC, "2026-05-10T02:00:00", W, "login redirect loop reason: the cookie domain was set without the leading dot so the callback url never saw the session cookie", group="-tmp-proj-webapp-b"),
    # -- Codex CLI ------------------------------------------------------------
    FxEvent("cx-01", CX, "2026-04-20T09:00:00", W, "refactor the FTS trigram tokenizer config so short CJK terms fall back to LIKE", group="cx-s1"),
    FxEvent("cx-02", CX, "2026-04-21T09:00:00", W, "写一个脚本批量重命名 Obsidian 笔记的 frontmatter tags", group="cx-s2"),
    FxEvent("cx-03", CX, "2026-04-22T09:00:00", W, "continue yesterday's work on the cursor commit fix in the sources registry", group="cx-s3"),
    FxEvent("cx-04", CX, "2026-04-23T09:00:00", F, "calculate the monthly mortgage payment for a 2.4 million yuan loan at 3.1 percent over 30 years", group="cx-s4"),
    FxEvent("cx-05", CX, "2026-04-24T09:00:00", P, "draft a packing list for the Sydney trip in May including adapters and sunscreen", group="cx-s5"),
    FxEvent("cx-06", CX, "2026-04-25T09:00:00", W, "parse the Codex rollout files recursively under sessions by date and skip developer prompts", group="cx-s6"),
    FxEvent("cx-07", CX, "2026-04-25T09:05:00", W, "rollout parser keeps only user and assistant messages and drops tool call noise", "assistant", "cx-s6"),
    # -- R01 same day, several pushes (codex rows) ----
    FxEvent("cx-r1", CX, "2026-05-06T09:10:00", W, "push the build to dev and watch the deploy dashboard, then verify the dev smoke test and compare the commit with the release branch", group="cx-r1"),
    FxEvent("cx-r2", CX, "2026-05-06T11:10:00", W, "after the hotfix push to dev, check the dev logs for the commit that was deployed and the smoke test result", group="cx-r2"),
    FxEvent("cx-r3", CX, "2026-05-06T14:10:00", W, "push to dev done, the commit is deployed and the dev smoke test is green, update the release checklist", group="cx-r3"),
    FxEvent("cx-r4", CX, "2026-05-13T09:10:00", W, "推送 staging 配置并检查 staging 的部署日志，确认提交已经生效", group="cx-r4"),
    FxEvent("cx-r5", CX, "2026-05-13T11:10:00", W, "再次推送 staging，核对提交记录和部署状态", group="cx-r5"),
    # -- Git log --------------------------------------------------------------
    FxEvent("gl-01", GL, "2026-04-20T10:00:00", W, "feat(mcp): add search_context tool with token auth\n\nread-only retrieval, every hit carries an evidence id", group="memory-core"),
    FxEvent("gl-02", GL, "2026-04-21T10:00:00", W, "fix(sources): advance the cursor only after persist succeeds", group="memory-core"),
    FxEvent("gl-03", GL, "2026-04-22T10:00:00", W, "refactor(store): migrate schema v19 to v20 with access_grants", group="memory-core"),
    FxEvent("gl-04", GL, "2026-04-23T10:00:00", W, "docs: record why the Redis queue was rejected (local-first)", group="memory-core"),
    FxEvent("gl-05", GL, "2026-04-24T10:00:00", W, "chore: bump pyobjc to 11.0 for the macOS extras", group="memory-core"),
    FxEvent("gl-06", GL, "2026-04-25T10:00:00", W, "feat(sources): ingest Codex rollout sessions with a byte-offset resume position", group="memory-core"),
    FxEvent("gl-r1", GL, "2026-05-06T09:00:00", W, "chore(release): push build to dev, bump the commit counter in the changelog", group="memory-core"),
    FxEvent("gl-r2", GL, "2026-05-06T11:00:00", W, "fix(release): push hotfix to dev after the smoke test, commit the retry patch", group="memory-core"),
    FxEvent("gl-r3", GL, "2026-05-06T14:00:00", W, "feat(release): push retry budget to dev behind a flag, one commit per flag", group="memory-core"),
    FxEvent("gl-r4", GL, "2026-05-13T10:00:00", W, "chore: 推送 staging 配置，提交说明补充", group="memory-core"),
    FxEvent("gl-r5", GL, "2026-05-13T12:00:00", W, "fix: 推送 staging 修复，提交前先跑冒烟测试", group="memory-core"),
    # -- zsh ------------------------------------------------------------------
    FxEvent("zs-01", ZS, "2026-04-21T11:00:00", W, "sqlite3 demo.db 'PRAGMA wal_checkpoint(TRUNCATE)'"),
    FxEvent("zs-02", ZS, "2026-04-22T11:00:00", W, "launchctl kickstart -k gui/501/com.example.daemon"),
    FxEvent("zs-03", ZS, "2026-04-23T11:00:00", W, "brew install ripgrep"),
    FxEvent("zs-04", ZS, "2026-04-24T11:00:00", W, "docker compose up -d postgres"),
    FxEvent("zs-05", ZS, "2026-04-25T11:00:00", P, "ffmpeg -i interview.mov -vn -acodec mp3 interview.mp3"),
    # -- Chrome ---------------------------------------------------------------
    FxEvent("ch-01", CH, "2026-04-22T04:00:00", P, "Australia Visitor visa (subclass 600) - Home Affairs", group="Default", url="https://immi.example.gov.au/visitor-600"),
    FxEvent("ch-02", CH, "2026-04-22T04:20:00", P, "Visitor visa checklist: financial evidence - Home Affairs", group="Default", url="https://immi.example.gov.au/visitor-600/checklist"),
    FxEvent("ch-03", CH, "2026-04-26T07:00:00", P, "Luxilon ALU Power 125 Tennis String | Tennis Warehouse", group="Default", url="https://shop.example.com/luxilon-alu-power-125"),
    FxEvent("ch-04", CH, "2026-04-20T08:00:00", W, "SQLite FTS5 trigram tokenizer documentation", group="Default", url="https://sqlite.example.org/fts5-trigram"),
    FxEvent("ch-05", CH, "2026-04-23T05:00:00", F, "Credit card annual fee comparison 2026 | CardGuide", group="Default", url="https://cards.example.com/annual-fee-2026"),
    FxEvent("ch-06", CH, "2026-04-21T08:00:00", W, "Model Context Protocol specification - server tools", group="Default", url="https://mcp.example.org/spec/tools"),
    # R03 repeated page titles: the same page visited many times, plus one distinct page
    FxEvent("ch-r1", CH, "2026-05-12T01:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-1"),
    FxEvent("ch-r2", CH, "2026-05-12T02:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-2"),
    FxEvent("ch-r3", CH, "2026-05-12T03:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-3"),
    FxEvent("ch-r4", CH, "2026-05-12T04:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-4"),
    FxEvent("ch-r5", CH, "2026-05-12T05:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-5"),
    FxEvent("ch-r6", CH, "2026-05-12T06:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-6"),
    FxEvent("ch-r7", CH, "2026-05-12T07:00:00", W, "Grafana - Ops dashboard", group="Default", url="https://grafana.example.org/d/ops-7"),
    FxEvent("ch-r8", CH, "2026-05-12T09:00:00", W, "Grafana dashboard JSON export guide - Ops Wiki", group="Default", url="https://wiki.example.org/grafana-json-export"),
    # -- Safari ---------------------------------------------------------------
    FxEvent("sf-01", SF, "2026-04-26T07:30:00", P, "Wirecutter: the best tennis racket strings for spin", url="https://reviews.example.com/tennis-strings"),
    FxEvent("sf-02", SF, "2026-04-23T07:00:00", F, "Mortgage repayment calculator - Example Bank", url="https://bank.example.com/mortgage-calculator"),
    FxEvent("sf-03", SF, "2026-04-23T13:00:00", V, "Therapist directory near me - counselling appointments", url="https://care.example.com/therapists"),
    FxEvent("sf-04", SF, "2026-04-24T07:00:00", P, "Sydney weather in May: averages and what to pack", url="https://weather.example.com/sydney-may"),
    # -- Markdown vault (title only; file mtime = event time) -----------------
    FxEvent("md-01", MD, "2026-04-20T12:00:00", W, "Sprint 1.5 planning: retrieval boundary decisions", group="Projects"),
    FxEvent("md-02", MD, "2026-04-22T12:00:00", P, "Australia trip visa materials checklist", group="Life"),
    FxEvent("md-03", MD, "2026-04-21T12:00:00", W, "Method: agent eval gold set design", group="Projects"),
    FxEvent("md-04", MD, "2026-04-23T12:00:00", F, "2026 household budget and card fees", group="Finance"),
    FxEvent("md-05", MD, "2026-04-23T15:00:00", V, "Journal 2026-04-23 therapy reflections", group="Journal"),
    FxEvent("md-06", MD, "2026-04-26T12:00:00", P, "Tennis string tension log", group="Life"),
)

# Penpal's own capture streams (D26): inserted straight through the capture repository path
# (normalizer -> insert_raw_event), not through a source plugin. ``source`` is the raw_events source
# (keyboard_chunk / clipboard / window / ax_text / browser_url); ``bucket`` is the intended bucket,
# which the app preset table must reproduce. These records exist nowhere else: no transcript, commit
# or history file contains them, so questions about them are answerable only from Penpal capture.
KB, CB, WN, AX, BU = "keyboard_chunk", "clipboard", "window", "ax_text", "browser_url"

CAPTURE_EVENTS: tuple[FxEvent, ...] = (
    FxEvent("cp-01", KB, "2026-04-29T02:10:00", W, "drafting the release notes for the beta, mention the new retry budget of 3 attempts", app="Cursor"),
    FxEvent("cp-02", KB, "2026-04-29T03:00:00", F, "转账备注 房租 四月 给房东老陈", app="支付宝"),
    FxEvent("cp-03", KB, "2026-04-29T05:00:00", P, "gift ideas for mom's birthday: silk scarf, pottery class voucher", app="Notes"),
    FxEvent("cp-04", KB, "2026-04-30T01:00:00", W, "git rebase onto the quokka branch before opening the pull request", app="Terminal"),
    FxEvent("cp-05", CB, "2026-04-29T04:00:00", P, "Tracking number SF1234567890 for the standing desk shipment", app="Safari"),
    FxEvent("cp-06", CB, "2026-04-30T02:00:00", W, "https://tracker.example.com/issues/KP-482 flaky eval on Friday", app="Slack"),
    FxEvent("cp-07", CB, "2026-04-30T04:00:00", F, "Order 2026043012345 refund of 89.50 yuan to the original card", app="Alipay"),
    FxEvent("cp-08", WN, "2026-04-29T06:00:00", W, "", app="Figma", title="Onboarding flow v3 - Design", ts_end="2026-04-29T07:25:00"),
    FxEvent("cp-09", WN, "2026-04-29T08:00:00", P, "", app="Spotify", title="Spotify", ts_end="2026-04-29T08:40:00"),
    FxEvent("cp-10", WN, "2026-04-30T03:00:00", W, "", app="Cursor", title="eval_runner.py - keypulse", ts_end="2026-04-30T03:30:00"),
    FxEvent("cp-11", AX, "2026-04-30T05:00:00", W, "Q2 OKR review: ship capture-to-MCP, keep recall above 90 percent", app="Notion"),
    FxEvent("cp-12", AX, "2026-04-30T06:00:00", P, "Flight AC088 departs Vancouver 14:35 gate B22", app="Safari"),
    FxEvent("cp-13", BU, "2026-04-29T09:00:00", P, "Best pour-over coffee grinders compared | BeanReview", app="Safari", url="https://reviews.example.com/coffee-grinders"),
    FxEvent("cp-14", BU, "2026-04-30T07:00:00", P, "Postgres partial indexes explained - DB Notes", app="Chrome", url="https://db.example.org/partial-indexes"),
    # R01 clipboard rows (the copied commit hash) + R03 repeated clipboard / window rows
    FxEvent("cp-r1", CB, "2026-05-06T11:05:00", W, "7e41c9d..a93bb10  dev -> dev", app="Slack"),
    FxEvent("cp-r2", CB, "2026-05-13T10:05:00", W, "3b9d0e1..c4417aa  staging -> staging", app="Slack"),
    FxEvent("cp-r3", CB, "2026-05-14T01:30:00", W, "kubectl get pods -n staging", app="Slack"),
    FxEvent("cp-r4", CB, "2026-05-14T02:30:00", W, "kubectl get pods -n staging", app="Slack"),
    FxEvent("cp-r5", CB, "2026-05-14T03:30:00", W, "kubectl get pods -n staging", app="Slack"),
    FxEvent("cp-r6", CB, "2026-05-14T04:30:00", W, "kubectl get pods -n staging", app="Slack"),
    FxEvent("cp-r7", CB, "2026-05-14T05:30:00", W, "kubectl get pods -n staging", app="Slack"),
    FxEvent("cp-r8", CB, "2026-05-14T06:30:00", W, "kubectl get pods -n staging", app="Slack"),
    FxEvent("cp-r9", CB, "2026-05-14T08:30:00", W, "kubectl rollout status deploy/api -n staging", app="Slack"),
    FxEvent("cp-r10", WN, "2026-05-15T01:00:00", W, "", app="Cursor", title="runbook-ops.md - keypulse", ts_end="2026-05-15T01:20:00"),
    FxEvent("cp-r11", WN, "2026-05-15T02:00:00", W, "", app="Cursor", title="runbook-ops.md - keypulse", ts_end="2026-05-15T02:20:00"),
    FxEvent("cp-r12", WN, "2026-05-15T03:00:00", W, "", app="Cursor", title="runbook-ops.md - keypulse", ts_end="2026-05-15T03:20:00"),
    FxEvent("cp-r13", WN, "2026-05-15T04:00:00", W, "", app="Cursor", title="runbook-ops.md - keypulse", ts_end="2026-05-15T04:20:00"),
    FxEvent("cp-r14", WN, "2026-05-15T05:00:00", W, "", app="Cursor", title="runbook-ops.md - keypulse", ts_end="2026-05-15T05:20:00"),
    FxEvent("cp-r15", WN, "2026-05-15T06:00:00", W, "", app="Cursor", title="runbook-ops.md - keypulse", ts_end="2026-05-15T06:20:00"),
    FxEvent("cp-r16", WN, "2026-05-15T08:00:00", W, "", app="Cursor", title="ops-alerts.yaml - keypulse", ts_end="2026-05-15T08:20:00"),
)

BY_ID = {e.id: e for e in (*EVENTS, *CAPTURE_EVENTS)}
assert len(BY_ID) == len(EVENTS) + len(CAPTURE_EVENTS), "duplicate fixture ids"
assert len({(e.source, e.ts) for e in (*EVENTS, *CAPTURE_EVENTS)}) == len(EVENTS) + len(CAPTURE_EVENTS), "fixture timestamps must be unique per source"
