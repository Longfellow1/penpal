---
id: runtime-contract
title: "D36: One runtime contract — daemon-written record, one evaluator, one launchd control path, per-process code identity"
category: decision
status: active
tags: [runtime, health, hud, doctor, launchd, mcp]
created: "2026-10-24T23:46:07"
updated: "2026-10-26T17:08:01"
---

<!-- compiled_truth -->
Decision (D36, 2026-10-24, owner accepted all recommendations after a deep-fix audit of 80+ liveness/HUD/doctor commits since 2026-04):

- **One record**: only the daemon writes runtime state — an atomic file (not the DB, so storage faults stay visible) every ~10 s with version, code fingerprint, pid + process start time (PID-reuse safe), heartbeat, per-component state. HUD writes its own record (incl. whether the status item is actually visible).
- **One evaluator**: a single function maps records + launchd state to a closed set (not_installed / not_running / starting / running / hung / outdated / paused) plus component issues. doctor, status, HUD, MCP capture_health and the healthcheck job all call it; thresholds live only there; an import-linter contract forbids reading pid files, launchctl or runtime keys elsewhere.
- **One control path**: start/stop/restart only via launchd (kickstart -k / bootout / bootstrap); `penpal restart`; installer, upgrade, HUD restart, doctor fix and the watchdog call it. Watchdog restarts hung/outdated with backoff; success = fresh heartbeat, never 'new events appeared'.
- **Code identity**: every long-lived process (daemon, HUD, mcp serve) compares its fingerprint with the installed code each minute and reloads itself — daemon exits for launchd to relaunch, HUD re-execs, MCP re-execs with an initialize-state handoff so hosts don't notice.
- **HUD**: snapshot collection off the main thread, shows data age; Penpal.app runs a native stub that embeds Python so macOS attributes the menu bar item / permissions to Penpal, not Python.
- **Deleted, not wrapped**: pid files as liveness (flock for single instance), self-heal pid-scan-kill and event-based success, unread alerts.json / capability_status.json, dead install.sh reload, the dev installer's own plist writer, the manual-fork daemon mode, keypulse.log naming (rename + rotation).
- **Eval**: the state matrix (every record/launchd combination → one expected status, identical across all consumers) and real-process scenarios (hung via SIGSTOP, upgrade via fingerprint change, crash via kill) gate CI; HUD main thread must stay responsive with a slow reader.
- **Release**: v0.3.0-preview.1 waits for this.


## Timeline

- time: 2026-10-24T23:46:07
  kind: decision
  summary: "Created this page: D36: One runtime contract — daemon-written record, one evaluator, one launchd control path, per-process code identity"
  source: owner decision 2026-10-24 after deep-fix audit
  affects: [runtime-contract]

- time: 2026-10-24T23:46:07
  kind: decision
  summary: Rewrote compiled_truth to the new best understanding
  source: brain update-truth
  affects: [runtime-contract]

- time: 2026-10-25T01:17:24
  kind: decision
  summary: "PR1 evaluator rules settled: core-loop staleness is measured from max(last progress, last system wake via kern.waketime) so sleep/wake never reads as hung; a live launchd pid without a matching record is starting within the grace window, then hung (alive but not progressing) — not_running only when no live daemon process exists; PENPAL_ISOLATED=1 makes utils.paths refuse real-home Penpal paths and conftest sets it session-wide."
  source: D36 PR1 review 2026-10-25
  affects: [runtime-contract]

- time: 2026-10-25T03:10:23
  kind: decision
  summary: "PR2 control rules settled: penpal.runtime.control is the only launchd mutator (kickstart -k when plist unchanged, else bootout → bounded wait → bootstrap ×3); restart succeeds only when a new generation's core loop progresses. Watchdog acts on hung / outdated / not_running ONLY while launchd still manages the job (penpal stop boots it out, so a user stop is never undone); 2 consecutive failures, 15-min cooldown, 3 restarts/hour, then report-only. Single instance via flock on run/daemon.lock (second serve exits 73); PID files are diagnostics only."
  source: D36 PR2 review 2026-10-25
  affects: [runtime-contract]

- time: 2026-10-25T03:49:13
  kind: decision
  summary: "PR3 self-reload settled: identity = code fingerprint + package version + config.toml hash + DB schema version; schema newer than code → OUTDATED and the store refuses writes. Daemon exits 75 on identity change and launchd relaunches it (KeepAlive SuccessfulExit=false); HUD execv from its timer; MCP serve execs only between requests, replaying the saved initialize params into the new process and filtering the synthetic response, sends tools/list_changed when the tool list differs, and exits on stdin EOF or parent death. Depends on MCP SDK low-level stdio API → pinned mcp>=1.30,<1.31; bumping the SDK requires re-running the handoff tests and the 3-host manual check."
  source: D36 PR3 review 2026-10-25
  affects: [runtime-contract]

- time: 2026-10-25T04:24:57
  kind: decision
  summary: "PR4 settled: HUD builds its model on one background worker, main thread only applies results; HUD writes run/hud.json and evaluate() owns HUD issues (doctor never judges on its own). Menu-bar visibility: report only a confirmed hidden item; unknown visibility is NOT an issue, because macOS 26 has no API for the System Settings → Menu Bar toggle (isVisible may stay true) — covered by manual check R7. HUD tick staleness is anchored at last wake like the daemon. Penpal.app's executable is a native stub embedding libpython (so the menu-bar entry is 'Penpal', not 'Python'); it bakes the Homebrew opt path (not the versioned Cellar path) and falls back to exec the penpal command when its Python is gone after an upgrade."
  source: D36 PR4 review 2026-10-25
  affects: [runtime-contract]

- time: 2026-10-25T05:00:26
  kind: decision
  summary: "PR5 settled (D36 epic complete): the app log is penpal.log (5 MiB x 3 rotating), written only by the daemon (CLI/HUD/MCP log to stderr) so rotation has a single writer; launchd stdout/stderr files are capped at 1 MiB by in-place truncation (launchd keeps them open). A configured keypulse.log in the data dir maps to penpal.log. Legacy artifacts are reported by doctor as WARN with fix commands; Penpal deletes only KeyPulse-named PID/log files in its own data dir (penpal maintenance clean-legacy) and never removes user apps or launchd jobs; the post-migration ~/.keypulse marker dir is not reported."
  source: D36 PR5 review 2026-10-25
  affects: [runtime-contract]

- time: 2026-10-26T11:48:26
  kind: decision
  summary: "Plists with Penpal's Label are Penpal-owned; install always rewrites them (upgrade path); only foreign files need --force"
  source: "0.4.0 hand test, 2026-10-26"
  affects: [penpal/cli/install.py]

- time: 2026-10-26T14:42:11
  kind: evidence
  summary: "launchctl bootstrap may not spawn RunAtLoad jobs (speculative pending); always kickstart after bootstrap; fakes model bootstrap as load-only"
  source: "0.4.0 hand test, 2026-10-26"
  affects: [penpal/runtime/control.py]

- time: 2026-10-26T15:39:26
  kind: decision
  summary: "Read-only commands must work in a fresh process; evaluate() owns its DB init; subprocess smoke guards it"
  source: "0.4.0 hand test, 2026-10-26"
  affects: [penpal/runtime/evaluate.py, tests/test_runtime_scenarios.py]

- time: 2026-10-26T15:51:32
  kind: reversal
  summary: "Daemon self-reload: exec the stable entry in place; exit 75 + KeepAlive relaunch was unreliable on macOS 26"
  source: "0.4.0 hand test, 2026-10-26"
  affects: [penpal/app.py, docs/design/runtime-contract.md]

- time: 2026-10-26T17:08:01
  kind: decision
  summary: Public install.sh drives the Homebrew formula only through its CLI
  source: "PR #149; clean-install 2026-10-26 ModuleNotFoundError"
  affects: [install.sh, clean-install CI]
