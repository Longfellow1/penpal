# GitHub Issue Drafts

> Archived pre-V2 public issue drafts. They describe an older Obsidian/report product concept and must not be published as current Penpal MCP plans. See the repository README for current positioning.

These are public issue drafts for making the repository feel active without creating low-signal noise.

Do not create them before the README and install path have been reviewed.

## 1. [README] Tighten first-screen positioning and quick start

```md
## Background

Penpal already has a strong product story, but first-time visitors need to understand the project in about 10 seconds: what it is, what it is not, and why it is worth saving.

## Task

Review the README first screen and tighten the path from positioning to quick start.

## Acceptance criteria

- [ ] First screen explains Penpal as a local-first Mac companion for Obsidian-native daily reflection
- [ ] First screen clearly says it is not a productivity dashboard, screen-time tracker, or cloud surveillance assistant
- [ ] Quick start is visible and matches the current install path
- [ ] Chinese product narrative is preserved below the English-first summary
```

## 2. [Demo] Add HUD, Obsidian daily note, and privacy architecture screenshots

```md
## Background

Penpal needs visual proof. Screenshots help users quickly understand what the menu bar HUD looks like, what the Obsidian output looks like, and why the privacy model is architecture-level rather than policy-only.

## Task

Add clean, privacy-safe visual assets to the README.

## Acceptance criteria

- [ ] HUD screenshot uses a clean demo state
- [ ] Obsidian daily note screenshot uses mock data only
- [ ] Privacy architecture image shows filtering before persistence
- [ ] README renders all images correctly on GitHub
```

## 3. [Install] Validate public install path on a clean macOS environment

```md
## Background

The current install surface is being consolidated around Homebrew + Penpal CLI lifecycle commands. Before public distribution, the flow needs to work on a clean Mac without requiring users to understand repo internals.

## Task

Validate the public install path and document any failures.

## Acceptance criteria

- [ ] Fresh install can run `penpal --help`
- [ ] `penpal install init` or the equivalent lifecycle command initializes runtime state
- [ ] `penpal doctor` gives actionable output
- [ ] launchd jobs point to the installed `penpal` executable, not a developer checkout
- [ ] README quick start matches the validated flow
```

## 4. [Privacy] Document what is filtered before persistence

```md
## Background

Penpal should not ask users to trust a privacy promise after collection. The repository needs to explain what data is dropped, redacted, or paused before it reaches durable storage.

## Task

Document the privacy boundary in a way a skeptical Mac and Obsidian user can inspect.

## Acceptance criteria

- [ ] Document app blacklist behavior
- [ ] Document raw keystroke non-collection
- [ ] Document field-level redaction
- [ ] Document camera-aware capture pause
- [ ] Document private browsing exclusion
- [ ] Link the privacy explanation from the README
```

## 5. [Obsidian] Define the companion plugin scope

```md
## Background

Penpal already writes Markdown into Obsidian. A future companion plugin should stay thin and avoid becoming a second product.

## Task

Draft the minimum useful Obsidian companion plugin scope.

## Acceptance criteria

- [ ] Plugin scope explains what belongs in Obsidian and what belongs in the Mac app / CLI
- [ ] Plugin can open today's Penpal daily note
- [ ] Plugin can show recent review items
- [ ] Plugin can expose template configuration or links to templates
- [ ] Plugin does not duplicate the capture daemon or privacy pipeline
```

## 6. [Feedback] Collect early experience from Mac + Obsidian users

```md
## Background

The first public learning goal is not star count. It is whether Mac and Obsidian users understand the workflow, trust the privacy model, and find the daily note worth reviewing.

## Task

Collect structured early feedback from people who try or inspect Penpal.

## Acceptance criteria

- [ ] Ask whether the README explains the project in 10 seconds
- [ ] Ask whether the privacy model feels credible
- [ ] Ask whether the Obsidian workflow feels native
- [ ] Ask whether the daily note output would be worth reviewing
- [ ] Categorize feedback in `docs/growth-log.md`
```
