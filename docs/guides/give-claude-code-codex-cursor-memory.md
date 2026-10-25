# How do I give Claude Code, Codex or Cursor memory of what I did on my Mac?

Install Penpal on the Mac, choose which local history sources to import, then grant the MCP client access. Claude Code, Codex and Cursor use the same local Penpal server; each receives its own grant and host-specific configuration.

## Set up local history

Penpal can capture enabled app/window, clipboard and filtered keyboard sources, and import registered local sources such as Chrome and Safari history, Claude Code and Codex sessions, zsh history, Git commits and approved Markdown directories. It does not automatically read every source: source availability, permission and your onboarding choices determine what is recorded or imported.

Run the setup commands:

```bash
brew install longfellow1/penpal/penpal && penpal install init
penpal install onboard
penpal doctor
```

`penpal install onboard` is the source-selection command in the current CLI. `penpal mcp install` is not a command; use `penpal mcp grant` to create a client grant and print its connection configuration.

## Grant each host access

Use a distinct client ID and matching `--host` value for each host:

```bash
penpal mcp grant claude --buckets Work,Personal --host claude
penpal mcp grant codex --buckets Work,Personal --host codex
penpal mcp grant cursor --buckets Work,Personal --host cursor
```

The command prints Claude Code's CLI setup, a Codex MCP command/config block, or a Cursor JSON config, depending on `--host`. The grant token is shown once; store the printed host configuration securely. Narrow the scope with `--buckets` and optionally `--sources`; review grants with `penpal mcp list`, revoke one with `penpal mcp revoke <client>`, or inspect call counts with `penpal mcp audit`.

For browser-history questions, enable/import the browser source in onboarding and grant the bucket containing its events. Current page URL capture is a separate source and requires browser automation permission for the background service; see the [README recording details](../../README.en.md#what-it-records-and-what-it-doesnt) and [privacy guide](../privacy.en.md).

## How Penpal does it

The README's Homebrew setup and Claude example are:

```bash
brew install longfellow1/penpal/penpal && penpal install init
penpal doctor
penpal mcp grant claude --buckets Work,Personal
```

For Codex and Cursor, use the host-specific commands above. The grant command handles the connection configuration; no built-in model or Penpal API key is needed. Results returned by Penpal enter the host AI's conversation and may be sent to that host's cloud model service.

[简体中文](give-claude-code-codex-cursor-memory.zh.md)
