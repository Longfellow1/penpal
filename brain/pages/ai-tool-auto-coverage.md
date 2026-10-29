---
id: ai-tool-auto-coverage
title: "Phase 1 auto-covers >=80% of mainstream Mac AI assistants/agents: auto-discovered, incremental, connected by default"
category: decision
status: active
tags: [capture, mcp, coverage, memmy]
created: "2026-10-28T20:18:26"
updated: "2026-10-29T10:25:03"
---

<!-- compiled_truth -->
Requirement (user, 2026-10-28, must ship): Phase 1 of the Memmy-class work covers at least 80% of the mainstream AI agents and IDEs a Mac user may run. Tools are discovered automatically, synced incrementally, and connected by default, with no per-tool setup or approval.

- Scope (owner, 2026-10-29): personal-assistant apps whose chats are encrypted locally or live only in the cloud (ChatGPT desktop, Doubao, Kimi, DeepSeek desktop) are not captured. There is no screen capture and no export import. They are listed as unreadable and left out of the capture denominator. If they support MCP, they are still connected automatically.
- Coverage is measured in two ways:
  - primary: session coverage on a real Mac, via 'penpal sources coverage';
  - secondary: a weighted list of mainstream Mac AI tools, kept with its weights in docs/design/memory-write-architecture.md.
- Discovery is adaptive, not one hand-written parser per tool:
  - a recipe engine runs built-in declarative recipes for the main tools;
  - host-AI-written recipes cover the long tail;
  - core executes recipes deterministically, which keeps [[core-no-embedded-llm]].
- Incremental sync uses per-source cursors (file offset, record index, SQLite watermark) and stable message ids, so rescans are idempotent.
- Default-on covers both capture and MCP registration for detected hosts. Every connected tool stays visible and can be switched off.

Baseline on 2026-10-28: Penpal captured only Claude Code and Codex CLI, and 77% of Codex sessions (zstd-compressed) were missed. Session coverage on the owner Mac was about 20%. Memmy 1.0.9 there covered 8 tools.


## Timeline

- time: 2026-10-28T20:18:26
  kind: decision
  summary: "Created this page: Phase 1 auto-covers >=80% of mainstream Mac AI assistants/agents: auto-discovered, incremental, connected by default"
  source: "user 2026-10-28: 第一阶段至少覆盖80%+主流的个人助理、mac可能用到的agent，并默认接入。这个务必实现掉"
  affects: [ai-tool-auto-coverage]

- time: 2026-10-28T20:18:49
  kind: decision
  summary: Requirement and baseline
  source: user 2026-10-28
  affects: [ai-tool-auto-coverage]

- time: 2026-10-29T10:24:51
  kind: decision
  summary: "Phase 1 drops encrypted/cloud-only personal assistants (ChatGPT, Doubao, Kimi, DeepSeek): no screen capture, no export import; listed unreadable, out of the capture denominator; MCP still auto-connected"
  source: "owner 2026-10-29: 用读屏幕抓不到完整数据……直接放弃就好了"
  affects: [docs/design/memory-write-architecture.md §5.3, penpal/sources/catalog]

- time: 2026-10-29T10:25:03
  kind: decision
  summary: "scope: agents/IDEs; encrypted/cloud-only personal assistants excluded (owner 2026-10-29)"
  source: owner 2026-10-29
  affects: [ai-tool-auto-coverage]
