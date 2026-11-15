---
id: query-time-document-masking
title: "Document numbers are masked on every MCP response, independent of ingest-time redaction"
category: decision
status: active
tags: [privacy, redaction, mcp]
created: "2026-11-15T19:26:48"
updated: "2026-11-15T23:55:56"
---

<!-- compiled_truth -->
Every text field returned over MCP (events and memory `content`/`detail`) passes `redact_secrets` then `redact_documents` (passport/permit, ID card, bank card) before reaching the host; always on, no config switch.

Why: ingest-time rules only protect new rows. Rows stored before a rule existed, and imported session text (credential-only masking at ingest), would otherwise reach the AI verbatim. Phones stay out of this layer (no checksum, too many false positives).

Stored rows are not rewritten by this; backfill is a separate owner decision.

Passport detection: uppercase-only; shape-matched for CN passport / HK-Macau / home-return permits; keyword-anchored (护照 / passport / travel document…) for Taiwan permits and foreign passports. Accepted cost: rare masking of code ids like `D12345678`.


## Timeline

- time: 2026-11-15T19:26:48
  kind: decision
  summary: "Created this page: Document numbers are masked on every MCP response, independent of ingest-time redaction"
  source: owner session 2026-11-15
  affects: [query-time-document-masking]

- time: 2026-11-15T19:26:59
  kind: decision
  summary: initial truth
  source: owner session 2026-11-15
  affects: [query-time-document-masking]

- time: 2026-11-15T23:55:56
  kind: decision
  summary: "Owner 2026-11-15: no backfill of stored rows for now (38 rows: 2 memories, 36 events); query-time masking already keeps them from the AI"
  source: owner session 2026-11-15
  affects: [query-time-document-masking]
