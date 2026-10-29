---
id: homebrew-runtime-deps
title: "A new runtime dependency ships only with a matching resource in the tap formula; core imports it lazily"
category: decision
status: active
tags: [release, homebrew, dependencies]
created: "2026-10-29T11:22:03"
updated: "2026-10-29T11:22:50"
---

<!-- compiled_truth -->
- The Homebrew formula in Longfellow1/homebrew-penpal installs Penpal from an explicit list of `resource` blocks. It does not resolve `pyproject.toml`.
- `scripts/upgrade-drill.sh` reuses the tapped formula and rewrites only the url and sha. A dependency added only to `pyproject.toml` is therefore missing in the drill and in every Homebrew install.
- Rule 1: every new runtime dependency gets a `resource` block (sdist url and sha256) in the tap formula at release time, in the same tap commit as the version bump.
- Rule 2: until then, core must start without the new dependency. Import it on first use, degrade only the feature that needs it, and record a specific error. For example, `zstd_unavailable` skips compressed files and leaves their cursor in place.
- The first case is `zstandard`, added for Codex `.jsonl.zst` archives and zstd recipes. It is not in the 0.4.1 tap formula yet.


## Timeline

- time: 2026-10-29T11:22:03
  kind: decision
  summary: "Created this page: A new runtime dependency ships only with a matching resource in the tap formula; core imports it lazily"
  source: "manager 2026-10-29, PR #167 fresh-install drill failure"
  affects: [homebrew-runtime-deps]

- time: 2026-10-29T11:22:50
  kind: decision
  summary: "Tap formula resources gate new runtime deps; core imports them lazily"
  source: "manager 2026-10-29, PR #167 fresh-install drill failure"
  affects: [homebrew-runtime-deps]
