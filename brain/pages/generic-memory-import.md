---
id: generic-memory-import
title: "Third-party memory import is generic: one shared pipeline + one small adapter per system; distilled memories only, no rule files"
category: decision
status: active
tags: [memory, import]
created: "2026-11-10T18:17:07"
updated: "2026-11-10T18:17:07"
---

<!-- compiled_truth -->
`penpal import` is generic (owner 2026-11-10: hardcoding Memmy was wrong). Design: docs/design/memory-import-architecture.md.

- One shared pipeline does validation, redaction, headline, cross-source dedupe, reconciliation, a single-transaction write and the audit. A small read-only adapter per system yields candidates with a stable locator id, kind, scope and tool, plus a `complete` flag.
- Only distilled memories are imported. Rule and instruction files are NOT imported in V1: CLAUDE.md, AGENTS.md, GEMINI.md, Cursor/Kiro/Cline/Copilot rules (owner ruling). Gemini CLI writes memories into GEMINI.md mixed with rules, so it falls in this class.
- Re-import reconciles with the source (owner ruling):
  - Changed → new version; the old row becomes superseded.
  - Gone → status `retired`, hidden from search but kept (D32). Retire only when the read was complete and is not a 0-after->0 drop.
  - `penpal memory delete --source` purges.
- First batch (owner ruling): Memmy migrated with identical counts, Claude Code auto-memory, and Codex consolidated memories (`MEMORY.md` blocks + `skills/`; exclude `raw_memories.md`, `rollout_summaries/`, `memory_summary.md`). Second batch: MCP memory-server JSONL and Basic Memory.
- Not supported: Cursor/Windsurf internal memories, cloud ChatGPT/Claude.ai/Gemini memories, Mem0/Letta/Graphiti until real demand.
- Import is CLI-only, never an MCP tool. Search skips imported memories whose `source_tool` equals the calling client (the host already has them).
- The P4 gate denies `.claude/.codex/.cursor/.memmy`, so those adapters can only be built in.
- Prerequisite: the passport / ID-document redaction pattern lands first.


## Timeline

- time: 2026-11-10T18:17:07
  kind: decision
  summary: "Created this page: Third-party memory import is generic: one shared pipeline + one small adapter per system; distilled memories only, no rule files"
  source: owner 2026-11-10
  affects: [generic-memory-import]

- time: 2026-11-10T18:17:07
  kind: decision
  summary: Generic import design settled
  source: owner 2026-11-10
  affects: [generic-memory-import]
