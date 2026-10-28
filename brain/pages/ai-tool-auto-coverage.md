---
id: ai-tool-auto-coverage
title: "Phase 1 auto-covers >=80% of mainstream Mac AI assistants/agents: auto-discovered, incremental, connected by default"
category: decision
status: active
tags: [capture, mcp, coverage, memmy]
created: "2026-10-28T20:18:26"
updated: "2026-10-28T20:18:49"
---

<!-- compiled_truth -->
Requirement (user, 2026-10-28, must ship): Phase 1 of the Memmy-class work covers at least 80% of the mainstream personal AI assistants and agents a Mac user may run, discovered automatically, synced incrementally, and connected by default (no per-tool manual setup or approval).

- Coverage is measured against a written, weighted list of mainstream Mac AI tools: coding agents/IDEs and personal-assistant desktop apps. The list and its weights live in docs/design/memory-write-architecture.md.
- Discovery is adaptive, not one hand-written parser per tool. A recipe engine runs built-in declarative recipes for the main tools and host-AI-written recipes for the long tail. Core executes recipes deterministically, which keeps [[core-no-embedded-llm]].
- Incremental sync uses per-source cursors (file offset, SQLite watermark) and stable message ids, so rescans are idempotent.
- Default-on covers both capture and MCP registration for detected hosts. Every connected tool stays visible in the HUD and can be switched off there.

Baseline on 2026-10-28:
- Only Claude Code and Codex CLI were captured, through hard-coded paths and per-file offset cursors.
- MCP registration printed per-host commands and wrote nothing.
- The owner Mac alone has about 20 AI tools installed (apps, CLIs or data dirs): Claude desktop, ChatGPT, Cursor, Kiro, VS Code (+Copilot/Continue), Windsurf/Codeium, Trae, Antigravity, Qoder, CodeBuddy/WorkBuddy, Doubao, Gemini CLI, Qwen Code, opencode, Goose, Factory Droid, iFlow, OpenClaw, Hermes, pi, LM Studio, Ollama.
- Memmy 1.0.9 there covered 8 tools.


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
