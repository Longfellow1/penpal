---
id: hud-worker-stays-fast
title: "The HUD model worker only does fast work; slow scans run on BackgroundScan"
category: decision
status: active
tags: [hud, performance, threading]
created: "2026-10-30T10:40:40"
updated: "2026-10-30T10:40:41"
---

<!-- compiled_truth -->
- `HudModelWorker` is one thread. Every panel refresh and every tap queues on it, so one slow job there freezes the panel: stale content, taps ignored, "数据更新于 N 秒前" climbing.
- 2026-10-30, owner Mac:
  - the pending-candidate count (`safe_pending_candidate_count` → `discover_all_candidates`) walked the home directory: ~120k `scandir` calls, about 18 s;
  - it ran on that worker every 60 s, so the panel was frozen 20–35 s out of every ~75 s;
  - `sample` during the stalls showed the worker in `os_scandir` → `opendir`.
- The approval window also ran the same discovery on the main thread when it opened.
- Rule:
  - anything that walks the filesystem or can take seconds runs on `BackgroundScan` (single-flight per key, result delivered on the main thread), never on the model worker or the main thread;
  - the pending count refreshes every 10 minutes (`PENDING_COUNT_SEC`);
  - the worker logs any turn ≥ 3 s (`SLOW_WORKER_SEC`) to `~/.penpal/logs/hud.log`.
- The HUD keeps its own log file. The shared app log (`penpal.log`) has a single writer, the daemon.
- Open: 518 pending candidates on the owner Mac is more than anyone will review by hand; candidate volume is a product question.


## Timeline

- time: 2026-10-30T10:40:40
  kind: decision
  summary: "Created this page: The HUD model worker only does fast work; slow scans run on BackgroundScan"
  source: "manager 2026-10-30, stall samples on the owner Mac"
  affects: [hud-worker-stays-fast]

- time: 2026-10-30T10:40:41
  kind: decision
  summary: "Why the HUD panel froze every minute, and the rule that prevents it"
  source: "manager 2026-10-30, sample(1) of the HUD during stalls + timing safe_pending_candidate_count"
  affects: [hud-worker-stays-fast]
