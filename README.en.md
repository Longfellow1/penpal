# Penpal MCP — local memory for your AI agents

Penpal MCP is a local-first memory layer and stdio MCP server for AI agents on macOS. It connects to Claude Code, Codex and Cursor, recording activity from sources you enable—such as app and window switches, clipboard content and filtered keyboard text—and importing supported local histories, sessions, Git and Markdown sources. Penpal's service and database run entirely on your Mac, with no built-in LLM; returned context enters the host's conversation and may be sent to its cloud model service.

[中文](README.md) · [Quick start](#quick-start) · [What it records](#what-it-records-and-what-it-doesnt) · [Where it stands](#where-it-stands)

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![macOS](https://img.shields.io/badge/macOS-12+-lightgrey.svg)](https://www.apple.com/macos/)
[![Tests](https://img.shields.io/badge/tests-1443%20collected-blue.svg)](tests/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

---

## What it solves

You ask Claude Code: "Last week I read a page about MCP auth. Where was it?" It can't answer. It has never seen your browser. Even if you let it run bash, browser history expires after 90 days, and the clipboard, the windows you switched through, and what you typed into web forms were never written to disk in the first place.

With Penpal, the same question makes the AI call `search_context`. It gets back that page, which app you were in, and what you were doing before and after. Then `get_evidence` opens the original source so you can check it yourself.

Penpal is not a chat interface and has no built-in model. You keep asking questions in the AI you already use. Penpal's job is to remember, and to hand over the right thing.

## Why not just let the AI use bash

An AI can already read local files, so indexing files that already exist is not worth a separate tool. Penpal exists for three reasons only. Every feature must serve one of them and beat "AI + bash" in a side-by-side eval:

| | AI + bash | Penpal |
|---|---|---|
| **Information that never hits disk**: window switches, clipboard, text typed into web pages | Gone | Recorded at capture time |
| **Records past the source's retention**: Claude Code sessions ~30 days, Chrome history ~90 days | Gone once expired | Kept after import until you delete it |
| **Controlled access** | Giving an AI bash means opening the whole disk; Claude Desktop and sandboxed Codex have no bash at all | Per-AI grants, scoped by category, redacted before storage, revocable, every query logged |

## Two install tiers

| | CLI (required minimum) | Desktop app (optional) |
|---|---|---|
| For | Developers who live in the terminal | People who want to see capture status and manage it visually |
| Includes | Background capture + MCP server + all control commands | Menu-bar HUD: status, source toggles, AI grants, privacy and deletion |
| Install | See Quick start below | Install the CLI first, then `penpal app install` (add `--login` to start at login) |
| Core capability | Everything works in your AI once installed | Same core, different interface |

Anything the app can do, the CLI can do. `penpal app install` compiles a native launcher that runs the HUD inside the installed Penpal environment, so the menu-bar identity should be **Penpal**. If clang or libpython is unavailable, it falls back to a script launcher and warns that the item may appear as **Python**. The app itself needs no system permission; capture permissions stay with the background service. After installing, open `~/Applications/Penpal.app` from Finder and confirm it is allowed and visible in Menu Bar settings. The CLI can still check HUD health through doctor and status.

## Quick start

```bash
# 1. Install the CLI, initialize the runtime, register the background service at login
curl -fsSL https://raw.githubusercontent.com/Longfellow1/penpal/main/install.sh | bash
# or manually: brew install longfellow1/penpal/penpal && penpal install init

# 2. Check system permissions: doctor lists what's missing, which program to grant, and which Settings pane
penpal doctor

# 3. Grant an AI access and add the printed config (it prints Claude Code / Codex / Cursor config)
penpal mcp grant claude --buckets Work,Personal
```

Then ask Claude Code as usual, for example "What was I researching yesterday afternoon?"

Want the menu-bar interface (optional)?

```bash
penpal app install          # compiles the native launcher and builds Penpal.app in ~/Applications; add --login to start at login
penpal app uninstall        # removes the app and its login item; the daemon and your data are untouched
```

Open Penpal.app from Finder after installation. On macOS 26, enable Penpal under System Settings → Menu Bar → Allow in the Menu Bar. If the icon is missing, check `penpal doctor` and `penpal hud status`. Public macOS APIs cannot always detect when System Settings suppresses a status item, so confirm the icon in the menu bar itself.

### Upgrading from KeyPulse

After upgrading, run `penpal doctor`. Its **Legacy files** section reports old KeyPulse.app bundles, old PID/log files, old launchd plists, and a `~/.keypulse` directory left after migration. These are warnings with suggested commands. Doctor never moves or deletes an app; after confirming the old app, run the shown `mv … ~/.Trash/` command yourself or move it to Trash in Finder.

Run `penpal maintenance clean-legacy` to remove only regular `keypulse*.pid` and `keypulse*.log` files inside `~/.penpal`. It does not touch apps, launchd plists, or `~/.keypulse`. Run `penpal doctor` again afterward; items that need manual review remain listed. An old `keypulse.log` is renamed once only when `penpal.log` does not exist. If both files exist, the old file is preserved and reported by doctor.

The daemon alone writes the rotating `penpal.log` (5 MiB with up to 3 backups). Launchd daemon/healthcheck stdout and stderr files are capped at 1 MiB and truncated in place when they exceed the cap.

### Verified hosts

| Host | Version | Notes |
|---|---|---|
| Claude Code | 2.1.303 (`claude --version`) | Printed config checked by MCP protocol-level tests (simulated client) |
| Codex CLI | codex-cli 0.162.1 (`codex --version`) | Same as above |
| Cursor | no `cursor` command detected | Config format checked against official docs; not verified on a real machine |

## Tools the AI can use

| Tool | What it does |
|---|---|
| `search_context` | Search by keyword, time and source; reports which scopes were searched and whether each keyword matched |
| `get_timeline` | Browse what you did over a period of time |
| `get_evidence` | Open the original source of a result |
| `rate_result` | Rate a result to improve ranking |

Read-only: the AI can't write or change memory (`rate_result` only stores a rating locally). Whatever is returned enters that AI's conversation and may be sent to its cloud service. Grants are scoped by category (Work / Personal / Private / Financial). `penpal mcp audit` shows who queried when and how many results came back; query text is not logged.

Note: Work includes every conversation in Claude Code and Codex, including private projects. Run `penpal rules projects` to see imported projects and their current buckets. To move a project to Personal, use its project folder name with `penpal rules add --scope project --value <project-folder> --bucket Personal`, then run `penpal rules rebucket` to update stored events.

## What it records and what it doesn't

**Recorded** (each source can be turned off individually):

- Live capture: foreground window switches, clipboard, keyboard text snippets
- Current web page URL: on in the standard install; the background service needs browser automation permission. To stop it, set `browser_url = false` under `[watchers]` in `~/.penpal/config.toml`
- Imports: Chrome / Safari history, Claude Code and Codex sessions, shell history, Git commits, Spotlight

**Not recorded, or excluded by default:**

- Apps on the default exclusion list, such as password managers, are not recorded. Private browsing windows record only the app and time, not content. Screen-text capture pauses while the camera is on.
- Content from banking and payment apps and sites (a built-in list) goes into the Financial category automatically; an AI without a Financial grant can't see it.
- Chat apps (WeChat, Slack, Feishu, etc.) are not captured by default. Once enabled individually they go into the Private category, visible only to AIs granted Private.
- Keyboard: individual keystrokes are not stored, only filtered text snippets. On by default; like other sources they are searchable by an AI within the categories it is granted. One command turns it off (`penpal source disable keyboard_chunk`).
- Tokens, keys and other credentials are masked before storage. Live-captured content also has the part of emails before @, phone numbers, ID numbers and card numbers masked. Results returned to an AI always mask the part of emails before @.

Data stays on your machine in `~/.penpal/`. No cloud, no telemetry. Nothing is deleted automatically; deletion is your call, and it removes the original text, index, source and related query records together.

A checkable, line-by-line privacy boundary with code and test references: [docs/privacy.en.md](docs/privacy.en.md)

## Day-to-day control

| Task | Command |
|---|---|
| Status and health | `penpal status`, `penpal health`, `penpal doctor` |
| Pause, resume (pause survives restarts) | `penpal pause`, `penpal resume` |
| Toggle a source | `penpal source list`, `penpal source disable keyboard_chunk` |
| Allow a chat app | `penpal privacy chat-apps allow Slack` |
| Block, unblock or list websites | `penpal privacy block-site example.com`, `penpal privacy unblock-site example.com`, `penpal privacy blocked-sites` |
| Manage AI grants | `penpal mcp list`, `penpal mcp revoke <client>`, `penpal mcp rotate <client>` |
| Delete data (preview the count first) | `penpal purge --source chrome_history --dry-run` |

Full reference: `penpal --help`.

Website blocking covers the host and its subdomains. After a change, run `penpal restart`; new events use the updated list after the daemon reloads its config.

## Where it stands

- **Recall accuracy**: the author built a 50-question eval on real data from their own Mac; about a third of the questions give only partial clues. Answered by an independent model and graded automatically, it averaged 46.7 correct over three runs (93%), with 0 out-of-scope returns. The eval set contains personal data and is not published.
- **Works today**: capture, import, first-use onboarding, redaction, per-category grants, exclusion list, search, sources, deletion, pause and source toggles, failure alerts without a UI; plus an optional menu-bar app (`penpal app install`).
- **Not there yet**:
  - Keyboard snippets are reliable only for English; capturing committed text from Chinese input methods is still experimental.
  - macOS only.
  - AIs can read but cannot write back to long-term memory.
  - The desktop app is the menu-bar HUD only for now, and is ad-hoc signed (no developer certificate). The former daily/weekly report, Obsidian export and LLM report pipeline were removed from the current product code and are not current features.

## License

Apache 2.0, see [LICENSE](LICENSE).

## Author

**Harland**, AI-native product manager. Email: Harland5588@outlook.com
