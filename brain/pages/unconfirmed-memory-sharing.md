---
id: unconfirmed-memory-sharing
title: "Unconfirmed AI-written memories: procedures stay with the authoring host, other kinds are shared but labelled and ranked lower"
category: decision
status: active
tags: [memory, security, mcp, prompt-injection]
created: "2026-11-16T09:48:42"
updated: "2026-11-16T20:31:41"
---

<!-- compiled_truth -->
Decision (owner, 2026-11-16, architecture review P0-1 "memory poisoning"): a memory written by an AI host through MCP and not yet confirmed by the user is handled by kind.

- **Procedure-type memories** (reusable steps, "how I do X") are visible only to the host that wrote them until the user confirms them. Reason: a host manipulated by injected content could write a harmful procedure that other hosts would then follow as the user's habit; procedures carry the highest risk because they are ranked first and read as instructions.
- **All other kinds** (facts, preferences, decisions) are shared with every connected host right away but are marked unconfirmed and ranked below confirmed memories, so cross-tool sharing stays immediate.
- Supporting rules from the same review: every memory records its authoring host; a host may update or supersede only memories it wrote (or confirmed ones the user lets it edit); recipes proposed by a host wait for user approval and never auto-enable or delete the user's `.disabled` marker.

Rejected: (A) share everything labelled only, which still spreads poisoned procedures; (B) isolate every kind, which is safest but delays everyday sharing; (C) read-only by default, which breaks the shared-memory value proposition.

Related: [[memory-distillation-by-agent]], [[user-curates-llm-proposes]].


## Timeline

- time: 2026-11-16T09:48:42
  kind: decision
  summary: "Created this page: Unconfirmed AI-written memories: procedures stay with the authoring host, other kinds are shared but labelled and ranked lower"
  source: "owner decision 2026-11-16, architecture review P0-1"
  affects: [unconfirmed-memory-sharing]

- time: 2026-11-16T09:48:42
  kind: decision
  summary: Rewrote compiled_truth to the new best understanding
  source: owner decision 2026-11-16
  affects: [unconfirmed-memory-sharing]

- time: 2026-11-16T09:48:42
  kind: decision
  summary: "Owner chose the compromise: procedures isolated to the authoring host until confirmed, other kinds shared but labelled unconfirmed and ranked lower"
  source: owner 2026-11-16
  affects: [unconfirmed-memory-sharing]

- time: 2026-11-16T20:31:41
  kind: decision
  summary: "Implementation choices (round 2): host memories that existed before schema v37 start unconfirmed; Memmy imports count as confirmed because the user ran them; a correction is always a new unconfirmed row; a host's recipe proposal is stored as catalog/<id>.proposed and only the user's 'penpal sources approve' enables it."
  source: "P0 round 2, branch fix/p0r2-round2"
  affects: [unconfirmed-memory-sharing]
