---
id: hud-launch-entry
title: "penpal hud start opens Penpal.app; bare penpal hud stays the in-process entry; macOS 26 can hide the icon behind the notch"
category: decision
status: active
tags: [hud, macos, self-heal]
created: "2026-10-29T19:18:40"
updated: "2026-10-30T10:40:41"
---

<!-- compiled_truth -->
- `penpal hud start` opens Penpal.app through LaunchServices (`relaunch_hud`, `open -a`) and waits for a matching HUD record. It never runs the HUD inside the CLI process.
- The bare `penpal hud` must stay the in-process entry:
  - the bundle's native launcher (`hud/launcher/main.c`) and its script fallback both run it;
  - the login item runs the bundle executable;
  - developers use it as the foreground mode.

  If it opened the bundle, the bundle would open itself again and again.
- Why: on 2026-10-29 an agent ran `penpal hud start` from a non-interactive, sandboxed shell. The HUD ran as bare Python.app with its icon visible and its main thread ticking, yet clicks did nothing for six hours, and every health check said running.
- macOS 26 limit:
  - Control Center hosts every status item.
  - An item that does not fit to the right of the notch is hidden on the built-in display, while `status_item_visible` stays True. No public API reports this.
  - Penpal.app (`com.penpal.app`) had no saved `NSStatusItem Preferred Position`, so each relaunch put the icon leftmost, where a crowded bar hides it first.
  - Self-heal cannot fix this. The remedy is moving the icon: ⌘-drag, or a saved preferred position.
- Quit placement (owner, 2026-10-29): the third HUD tab is named 设置 (was 隐私与删除; internal id stays `privacy`) and ends with 退出 HUD, which reuses the existing confirm flow. The right-click menu keeps 退出 HUD.


## Timeline

- time: 2026-10-29T19:18:40
  kind: decision
  summary: "Created this page: penpal hud start opens Penpal.app; bare penpal hud stays the in-process entry; macOS 26 can hide the icon behind the notch"
  source: "manager 2026-10-29, owner report: HUD click-dead, then not shown"
  affects: [hud-launch-entry]

- time: 2026-10-29T19:18:41
  kind: decision
  summary: "HUD launch entry, quit placement, and the notch limit"
  source: "manager 2026-10-29, owner report + CGWindowList check on the owner Mac"
  affects: [hud-launch-entry]

- time: 2026-10-30T09:49:59
  kind: decision
  summary: "Owner approved a notch hint over detection (none possible): doctor, install doctor and the 设置 tab show one shared hint (ICON_OCCLUDED_HINT_ZH): free menu bar space, then ⌘-drag Penpal right. A default preferred position for new installs is a separate, unverified idea."
  source: "owner 2026-10-30 (选择：加提示)"
  affects: [hud-launch-entry]

- time: 2026-10-30T10:40:41
  kind: reversal
  summary: "The notch hint left the HUD 设置 tab (owner: footer too long; anyone reading the panel can already see the icon). It stays in doctor and install doctor, shortened. HUD copy was trimmed to one plain sentence per place, without launchd/daemon/hash jargon."
  source: "owner 2026-10-30: 设置最低下的文本字数太长"
  affects: [hud-launch-entry]
