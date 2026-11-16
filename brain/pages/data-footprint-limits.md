---
id: data-footprint-limits
title: "D34: Bound the non-memory footprint (backups, caches, logs, eval artifacts); memory itself stays under D32"
category: decision
status: active
tags: [retention, data-lifecycle, storage, doctor]
created: "2026-10-16T17:27:17"
updated: "2026-11-16T09:48:48"
---

<!-- compiled_truth -->
Decision (user, 2026-10-16): the data directory must not grow without bound. Memory rows stay under [[retention-keep-forever]] (D32): never deleted automatically. Everything that is **not** user memory gets an explicit size or count limit and is cleaned automatically. Implementation is assigned to a separate agent; this page is the spec.

Evidence (author's machine, 2026-10-16, `~/.keypulse` = 4.9G):

| Item | Size | Producer | Cleanup today |
|---|---|---|---|
| Memory DB (`keypulse.db` + WAL) | 128M + 96M WAL | daemon | user `purge` only (D32) — **unchanged** |
| Pre-migration backups `*.db.v<N>-<ts>.bak` | 688M, 12 files, grows each schema bump | `store/migrations.py::_backup_before_migration` | none |
| LLM cache `cache/llm` | 507M | eval / LLM tooling | none |
| Eval artifacts `evals/` (`runs` 2.9G, `runs_invalid` 302M, `qgen` 105M) | 3.3G | developer eval harness, not end users | none |
| Logs `healthcheck.log`, `keypulse.log` | ~86M | launchd stdout/stderr redirect | no rotation found in code |

Required behaviour:
1. **Migration backups**: keep the newest 2 (configurable); delete older ones only after the new migration commits. Never delete the backup taken for the migration currently running.
2. **LLM cache**: size cap (default e.g. 200M), evict least-recently-used.
3. **Logs**: rotate by size (e.g. 10M × 3), including launchd-redirected files.
4. **Eval artifacts**: move out of the user data dir (dev-only location, e.g. repo-local or `~/Library/Caches`), or cap them; end users must never accumulate them.
5. **WAL**: periodic checkpoint so the WAL does not stay near the DB size.
6. **doctor / health**: report a footprint breakdown (memory vs backups vs cache vs logs vs evals) and flag anything over its cap.
7. Every automatic cleanup is logged (what, how much) and has a dry-run; config can raise or disable each cap.

Boundaries: no code path may delete raw_events, evidence, sessions or other memory rows (D32). "Compress old memory into summaries with provenance" is a separate product feature, deferred to the M3 Control Center.

Acceptance: tests for each cap (over/under limit, the in-flight backup is never removed, memory tables untouched); on a copy of a large data dir the non-memory footprint ends within caps; doctor shows the breakdown.

Sequencing: independent of the Penpal rename (branch `rename/penpal`); whichever lands second rebases. Paths above use the pre-rename `~/.keypulse`.


## Timeline

- time: 2026-10-16T17:27:17
  kind: decision
  summary: "Created this page: D34: Bound the non-memory footprint (backups, caches, logs, eval artifacts); memory itself stays under D32"
  source: user 2026-10-16
  affects: [data-footprint-limits]

- time: 2026-10-16T17:27:17
  kind: decision
  summary: "Record D34 scope, evidence and acceptance"
  source: "user 2026-10-16; du on ~/.keypulse 2026-10-16"
  affects: [data-footprint-limits]

- time: 2026-11-16T09:48:48
  kind: decision
  summary: "2026-11-16 review P0-4: purge also deletes every pre-migration backup (backups are whole-DB snapshots, so purged data would survive in them); data dir 0700 and its files 0600; backup keep-count stays 2 by default"
  source: architecture review 2026-11-16
  affects: [data-footprint-limits]
