---
id: cli-core-app-optional
title: "D33: CLI is the minimal install (core via MCP); the desktop app is an optional superset"
category: decision
status: active
tags: [product, distribution, hud, app]
created: "2026-10-14T13:52:22"
updated: "2026-10-27T12:22:09"
---

<!-- compiled_truth -->
**Decision (owner, 2026-10-14):** split the product route into two install tiers.
- **CLI (minimal set, required):** capture daemon (launchd) + MCP server + full CLI control. A zsh-only developer installs only this and gets all core value through MCP in their AI host.
- **Desktop app (maximal set, optional):** for users who want visual monitoring and management. It starts as the HUD (interaction draft v3: fixed status bar, bottom tabs Overview / AI access / Privacy & delete, max two levels, one list-row component, one confirm dialog). Later, features that depend on a front-end UI may be built here, e.g. daily/weekly reports and a knowledge-graph view.

**Constraints that follow:**
- One core, two shells. The app adds no core logic of its own; everything the app can do must also be reachable from the CLI (CLI is the source of truth for control).
- CLI-only installs must still surface failures without a UI: `keypulse doctor` detects lost permissions, and MCP responses carry a capture-health warning so the AI host can tell the user.
- Report/graph features in the app are post-V1 and live in the app layer. `core-no-embedded-llm` still holds for core: any LLM work is done by the host AI or by an app-side feature the user opts into, never by the core daemon. This narrows the D24 shutdown: D24 removed reports from core, it does not forbid them in the optional app.
- Open question (unverified): when the app is installed, macOS permission identity. The app should probably launch the same daemon so permissions are attributed to KeyPulse.app; CLI-only installs attribute them to the keypulse/python binary.

**Impact:** M3 "HUD fully usable" becomes "CLI control surface complete + failures visible without UI"; the app/HUD build becomes an optional track. PRD F13 and "control entry is the HUD" need rewording.


## Timeline

- time: 2026-10-14T13:52:22
  kind: decision
  summary: "Created this page: D33: CLI is the minimal install (core via MCP); the desktop app is an optional superset"
  source: owner decision 2026-10-14
  affects: [cli-core-app-optional]

- time: 2026-10-14T13:52:22
  kind: decision
  summary: "CLI minimal set, App optional superset"
  source: owner decision 2026-10-14
  affects: [cli-core-app-optional]

- time: 2026-10-24T12:25:19
  kind: decision
  summary: "HUD v3 built as view-model + controller + HTML renderer + thin AppKit shell; shell holds no logic and the controller calls the same core functions as the CLI. A grant's scope is read-only in the HUD (core has no edit; change = revoke + regrant). New token/config is shown once on a second-level page and never persisted. A never-seen source (file_missing, no rows) is not an alert. Optional install via penpal app install/uninstall (launcher bundle, no own TCC)."
  source: PR feat/27-hud-v3
  affects: [cli-core-app-optional]

- time: 2026-10-24T23:29:12
  kind: decision
  summary: "Website exclusions can be managed with privacy block-site / unblock-site / blocked-sites; the HUD lists configured hosts read-only, and live capture picks changes up after daemon restart."
  source: owner feature request 2026-10-24
  affects: [cli-core-app-optional]

- time: 2026-10-27T12:22:09
  kind: decision
  summary: "Daemon runs from Penpal.app's launcher so grants show as Penpal; bundle identity is stable across upgrades"
  source: "owner decision 2026-10-27 (hand test: privacy list shows python3.13)"
  affects: [penpal/app_bundle.py]
