---
id: coding-agent-project-buckets
title: "D37: coding-agent sessions bucketed by project rule, not by content"
category: decision
status: active
tags: [privacy, buckets, decision]
created: "2026-10-25T22:46:29"
updated: "2026-10-25T22:46:29"
---

<!-- compiled_truth -->
- D23 stands: core never classifies by content (would need an LLM or an ever-growing keyword list).
- Claude Code / Codex default to Work, so a Work grant reads every coding-agent conversation, including personal ones. Mitigations: (1) project-scoped bucket rules (`penpal rules add --scope project --value <folder>`; order project > app > source > default; `rules rebucket` to apply to history; `rules projects` to discover); (2) every Work grant shown with coding agents enabled says so plainly.
- Project key = folder name from the session `cwd`; older Claude rows with only the encoded dir match by trailing segment (over-match is acceptable because it only moves rows out of shared buckets).
- Eval: the default path's 3 leaks are a recorded known limitation (not a gate); the project-rule variant is hard-gated at 0 leaks.
- Revisit if users routinely mix personal and work topics inside one project — that would need host-side tagging (leverage the host AI), not core NLU.


## Timeline

- time: 2026-10-25T22:46:29
  kind: decision
  summary: "Created this page: D37: coding-agent sessions bucketed by project rule, not by content"
  source: owner-delegated 2026-10-25
  affects: [coding-agent-project-buckets]

- time: 2026-10-25T22:46:29
  kind: decision
  summary: D37 settled
  source: owner-delegated 2026-10-25
  affects: [coding-agent-project-buckets]
