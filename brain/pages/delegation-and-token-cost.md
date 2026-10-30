---
id: delegation-and-token-cost
title: "How the manager delegates, accepts worker output, and spends tokens"
category: decision
status: active
tags: [process, agents, cost, hygiene]
created: "2026-10-30T12:01:51"
updated: "2026-10-30T12:01:51"
---

<!-- compiled_truth -->
- The rules are in `docs/DELEGATION.md` (dispatch, briefs, acceptance, token cost) and `docs/ENGINEERING.md` §7 (release hygiene gates). Change them there; this page records why they exist.
- Evidence, 2026-10, one 20-day main session (5,688 turns) plus its 83 subagents, priced at API list rates:
  - the main session is about 80% of Claude spend;
  - within it, re-reading context is about half, and writing context and output are about a quarter each;
  - median context was 164k tokens, and each token that entered context was re-read about 50 times;
  - subagents ran long: median 77 turns, 20 ran over 100 turns, and 43% of Haiku turns had more than 100k input, where Haiku 5.5 costs 5×.
- Decisions that follow:
  - one session per workstream;
  - treat tool output like output tokens;
  - delegate read-heavy work and keep each subagent under ~50 turns and 100k context;
  - push bulk mechanical work to Codex or agy, which have separate quotas;
  - the manager re-runs every gate itself, and a worker's "passed" is only a lead.
- Hygiene, decided the same day:
  - the repo is public, so rules that can be checked become CI gates: ruff `F`/`E9`/`S110`/`S112` and `tests/test_repo_hygiene.py`;
  - only LICENSE, README and SECURITY carry the owner's name;
  - fixtures use placeholders.


## Timeline

- time: 2026-10-30T12:01:51
  kind: decision
  summary: "Created this page: How the manager delegates, accepts worker output, and spends tokens"
  source: "owner 2026-10-30: 关注发布代码是否干净、token 经济学"
  affects: [delegation-and-token-cost]

- time: 2026-10-30T12:01:51
  kind: decision
  summary: "Rules live in docs/DELEGATION.md and docs/ENGINEERING.md §7; this page keeps the evidence behind them"
  source: "manager 2026-10-30, usage totals from one 20-day main session and its 83 subagents"
  affects: [delegation-and-token-cost]
