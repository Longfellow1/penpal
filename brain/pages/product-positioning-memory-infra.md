---
id: product-positioning-memory-infra
title: "D30: KeyPulse is user-controlled memory infrastructure shared by all AI assistants; hero scenario = verifiable recall"
category: decision
status: active
tags: [positioning, roadmap]
created: "2026-10-12T15:20:36"
updated: "2026-10-30T16:20:34"
---

<!-- compiled_truth -->
Positioning: KeyPulse persists the user's authorized digital trail and turns cross-app, cross-time traces into personal memory that any AI can call safely, with traceable sources. It is not another AI assistant and does not build its own chat entry point; the user keeps asking in Claude, Codex, Cursor, etc.

Three layers: background memory engine (capture, import, clean, bucket, dedup, evidence); menu-bar Memory Control Center (is it recording, which sources/sites/buckets are on, what each AI may see, who queried what, pause/delete/correct/rebucket); the user's existing AI as the entry point.

Hero scenario for V1: in any AI, find something the user once saw, said or wrote but no longer knows where it lives, with a verifiable source.

Guardrail: coverage is not the value. Trust (ranking, evidence deep links, scoped access, correction) and long-term retention are the moat. M1 evidence: all 3 misses came from ranking or capture precision, none from missing sources.

Milestones after M1 (user decision 2026-10-12):
- M2 = trustworthy recall AND the AX committed-text (Chinese) spike, both. Recall: ranking for near-duplicate/repeated events, evidence for all sources (#57), coverage status in tool output, lightweight result feedback, and a re-runnable real-data eval with auto-judging. Exit: answer accuracy ≥95%, evidence openable ≥95%, zero out-of-scope results.
- M3 = Memory Control Center (HUD), including content-level rebucketing/manual correction and per-AI access/audit view.
- M4 = release, gated by external validation: 10-15 heavy AI users on Mac, 14 days, ≥5 real recall questions each; the signal is unprompted return in week 2; any permission leak is a severe failure.

Reversed 2026-10-28 (user): absorbing Memmy-class cross-AI-IDE memory is in scope now, before promotion. Agent write-back of long-term memory and adaptive multi-AI-tool capture are V1 work; design in docs/design/memory-write-architecture.md. Still not adopted: proactive machine discovery outside the user-confirmed candidate inbox.


## Timeline

- time: 2026-10-12T15:20:36
  kind: decision
  summary: "Created this page: D30: KeyPulse is user-controlled memory infrastructure shared by all AI assistants; hero scenario = verifiable recall"
  source: "user 2026-10-12 (third-party review by Codex, adopted with reservations)"
  affects: [product-positioning-memory-infra]

- time: 2026-10-12T15:20:36
  kind: decision
  summary: "Positioning, hero scenario, milestone order after M1"
  source: user 2026-10-12
  affects: [product-positioning-memory-infra]

- time: 2026-10-13T09:36:43
  kind: decision
  summary: "2026-10-13 用户确认：HUD 数据记录观察（各来源采集状态、断流提醒、记录预览、AI 查询记录）与控制能力一起留在 M3 控制中心，M2 只做召回准确率与中文上屏试验。M2 评测题库扩到 50 题，其中至少 15-20 题为只给部分线索的查询。"
  source: chat
  affects: [product-positioning-memory-infra]

- time: 2026-10-13T15:14:00
  kind: decision
  summary: "2026-10-13 多维验收评估：M2 未达标，不进入 M3。用户批准 M2 收口 4 步：保留策略（D32 全部永久保留）→ 修评测（gold 校验、判分只判所问）→ 修检索机制 R1（逐关键词命中反馈、零命中按时间浏览提示、上下文字段可搜）与 R2（入库层合并重复/跨来源同一事件）→ 补 Codex/Cursor 接入并同步 PRD/README。"
  source: user 2026-10-13
  affects: [product-positioning-memory-infra]

- time: 2026-10-14T09:56:40
  kind: reversal
  summary: "2026-10-14 owner: the ≥95% accuracy bar is not a hard gate. Milestone acceptance = quantitative + experience: pass when objective scores (accuracy, evidence, zero out-of-scope) and the owner's subjective experience judgement are both OK. Owner accepted the M2 recall state (R2b: 46.7/50 = 93%, 0 isolation/bucket violations) as passing."
  source: owner 2026-10-14
  affects: [product-positioning-memory-infra]

- time: 2026-10-14T13:52:22
  kind: decision
  summary: "D33: distribution split — CLI minimal install (core via MCP), desktop app optional superset (HUD first, later reports/graph in app layer). See cli-core-app-optional."
  source: owner decision 2026-10-14
  affects: [cli-core-app-optional]

- time: 2026-10-28T20:08:53
  kind: decision
  summary: "Memmy-class write-back and multi-AI-tool capture moved into V1 (reverses the 2026-10-12 deferral)"
  source: "user 2026-10-28: 吃掉memmy放在当下做"
  affects: [product-positioning-memory-infra]

- time: 2026-10-28T20:08:53
  kind: reversal
  summary: "Memmy-class write-back + adaptive multi-AI-tool capture moved into V1, before promotion"
  source: user 2026-10-28
  affects: [product-positioning-memory-infra, memory-distillation-by-agent, docs/Penpal_Eval_PRD.md, issue #24]

- time: 2026-10-30T16:15:59
  kind: decision
  summary: "commit_memory evidence_ids are optional (design §3.5 wins over issue #24's 'no evidence is rejected'); any evidence given must be inside the token's grant, and writes need the 'write' operation. Auto-connected hosts get read,write grants."
  source: "tech lead 2026-10-30, following the owner-approved design of 2026-10-28"
  affects: [product-positioning-memory-infra, mcp-caller-auth-p0]

- time: 2026-10-30T16:20:34
  kind: decision
  summary: "Memory visibility follows the existing buckets (Work/Personal/Private/Financial). A memory citing evidence carries the buckets of that evidence, and a reader must cover all of them. A memory without evidence goes to a bucket the writer names inside its grant, else the writing tool's default bucket. Memmy imports use the source tool's default bucket. scope=project needs an explicit project from the host, normalised by the project rules."
  source: "owner 2026-10-30 (AskUserQuestion): 跟随分区（推荐）"
  affects: [product-positioning-memory-infra, mcp-caller-auth-p0]
