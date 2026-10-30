---
id: v1-refactor-plan
title: "V1 refactor plan: milestones, acceptance, sign-off"
category: decision
status: active
tags: [v1, refactor, plan]
created: "2026-10-10T14:19:24"
updated: "2026-10-30T16:15:59"
---

<!-- compiled_truth -->
V1 重构计划（docs/KeyPulse_V1_重构计划.md，v1.0，owner 2026-10-10 批准）。

## 里程碑

| 里程碑 | 范围 (issue) | 验收 / 签字 |
|---|---|---|
| M0 decouple | #16 | 技术负责人（Opus）验收 |
| M1 tracer | #17–#20 | owner 签字：M1 demo |
| M2 data+hosts | #21–#25 | 技术负责人（Opus）验收 |
| M3 capture+controls | #26–#31 | owner 签字：M3 HUD review |
| M4 first-run+release | #32–#33 | owner 签字：M4 release |

技术负责人（Opus）验收 M0 / M2，并发出各里程碑报告。owner 只在 M1 demo、M3 HUD review、M4 release 三处签字。

## 集成方式

- 一个集成分支，每个 issue 一个 PR。
- 合并条件：CI 通过 + 技术负责人 review。

## 回滚

- 里程碑 tag：v1-m0 … v1-m4，作为回滚点。
- 最终兜底：legacy-v0 tag。

## 每个任务的门禁

1. 对应 issue 的验收条件全部勾选。
2. 无新增测试失败（已知 3 个环境失败作为基线）。
3. import-linter 通过。
4. 自 M1 起：安全门禁 G1–G5 = 0；eval 回归 ≤ 2pp。
5. M0 重建 GitNexus 索引，之后每次改动先做 impact analysis。
6. 仓库内不得出现真实数据。
7. 技术负责人 review 通过。

## 关联

- [[v1-build-by-legacy-extraction]]：复用优先，新建仅限 legacy 缺失部分。
- [[eval-prd-separate]]：成功标准与 eval 在独立 Eval PRD 中。
- [[session-handoff-2026-10-10]]：当前交接状态。


## Timeline

- time: 2026-10-10T14:19:24
  kind: decision
  summary: "Created this page: V1 refactor plan: milestones, acceptance, sign-off"
  source: "docs/KeyPulse_V1_重构计划.md v1.0 (owner-approved 2026-10-10)"
  affects: [v1-refactor-plan]

- time: 2026-10-10T14:19:30
  kind: decision
  summary: "Initial truth: V1 refactor plan milestones, per-task gates, sign-off and rollback"
  source: "docs/KeyPulse_V1_重构计划.md (v1.0, owner-approved 2026-10-10)"
  affects: [v1-refactor-plan]

- time: 2026-10-10T14:19:30
  kind: decision
  summary: "Owner approved V1 refactor plan v1.0 on 2026-10-10; milestones M0-M4 mapped to issues #16-#33"
  source: "docs/KeyPulse_V1_重构计划.md"
  affects: [v1-build-by-legacy-extraction, eval-prd-separate, session-handoff-2026-10-10]

- time: 2026-10-10T14:39:39
  kind: evidence
  summary: "M0 done 2026-10-10: PR #34 merged (3b2f726), tag v1-m0. Daemon no longer runs T2/T3 daily/Obsidian triggers or self-heal daily rerun; daily still produced by launchd 'keypulse obsidian sync'. .importlinter 3 contracts + import-lint CI (macos-latest) green. Deferred to M3: 3 capability permission checks still import pyobjc (ignore_imports). gh OAuth token lacks workflow scope; push workflow-file changes via ssh://git@ssh.github.com:443."
  source: "PR #34"
  affects: [v1-refactor-plan]

- time: 2026-10-13T19:29:15
  kind: evidence
  summary: "M2 close-out 2026-10-13: R1 (per-keyword feedback, context labels, #83), R2 (cross-source page folding, superseded typing snapshots, #84), R2b (Chinese pinyin/edit snapshot rules + typed-text source hint, migration v34, #86). Codex gpt-6-luna, bank v2 (50 q), same gold for all rounds: R2 avg 45.7/50, R2b avg 46.7/50 (93%); isolation/bucket violations 0. Below the 95% exit bar. Remaining: Q05 (over-broad question), Q06 (misses a clause from the user's own typed note), Q23 (older vs latest decision), single-round judge flips. Gold fixes with record evidence: Q03 (over-specified), Q05 (ids pointed at intermediate drafts), Q08/Q11 (ambiguous: accept every answer the records support). Eval runs now parallel (--jobs 4, #85): ~10 min/round."
  source: "local evals ~/.keypulse/evals (not in repo)"
  affects: [v1-refactor-plan]

- time: 2026-10-14T09:56:40
  kind: decision
  summary: "2026-10-14 M2 recall accepted by owner under quant+experience rule (see product-positioning-memory-infra reversal). Open from the M2 scope: AX committed-text spike #64 (needs owner to grant Accessibility)."
  source: owner 2026-10-14
  affects: [v1-refactor-plan]

- time: 2026-10-14T19:20:20
  kind: decision
  summary: "D33 (2026-10-14): M3 gate changed from 'HUD fully usable + owner HUD review' to 'CLI control surface complete + failures visible without UI'; HUD/app moved to an optional app track (design baseline: v3 interaction draft). See cli-core-app-optional."
  source: owner decision 2026-10-14
  affects: [v1-refactor-plan]

- time: 2026-10-23T23:22:21
  kind: note
  summary: "2026-10-23 state: project renamed KeyPulse to Penpal (#102-#108); v0.2.0-preview.1 pre-release published 2026-10-19. M0/M1/M2 done (M1 sign-off date unconfirmed). M3 in progress: open #22 #26 #29 #31 #64; HUD #27 and #10 moved to app-track (D33). M4 not started: #32 #33, plus #11 #14 as external-validation prerequisites; release gated by Eval PRD three layers plus D30 external validation (10-15 users, 14 days, zero permission leaks). Plan doc synced."
  source: repo history and issue tracker 2026-10-23
  affects: [v1-refactor-plan]

- time: 2026-10-30T16:15:59
  kind: decision
  summary: "Memmy track (docs/design/memory-write-architecture.md) joins the milestone table. Owner chose all of P2 auto-connect, P3 commit_memory + Memmy import and P4 host-written recipes for 2026-11-03; P1 already at 94.1% session coverage on the owner Mac. Issues #173 (P2), #24 (P3), #174 (P4)."
  source: "owner 2026-10-30 (AskUserQuestion): P2 + P3 + P4 全做"
  affects: [v1-refactor-plan, ai-tool-auto-coverage]
