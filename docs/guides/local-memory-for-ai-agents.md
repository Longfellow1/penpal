# What is a local memory layer for AI agents, and when do you need one?

A local memory layer gives an AI agent searchable access to selected facts and activity stored on your machine across sessions. You may need one when useful context is not saved in ordinary files, source histories expire, or you want each AI host to have a narrow and auditable view of local records.

## Local memory vs. asking an agent to use bash

If your agent can already read the files you need, a separate memory service may not help. Penpal's use case is the gap between what exists in files and the activity you want an agent to retrieve later:

| Reason | Agent reading files with bash | Penpal MCP |
|---|---|---|
| Activity that is not ordinarily written to a file, such as window changes or clipboard content | Cannot recover it after the fact | Can record it through enabled capture sources |
| Local history that may be removed or rotated by its source | Can only search what remains at that source | Imports supported histories into Penpal's local store; records remain until you delete them |
| Access control across AI hosts | File access depends on the host's filesystem permissions and sandbox | Per-client grants can be scoped by bucket and source, revoked, and audited; results can link to evidence |

Registered source plugin IDs are `approved_sqlite`, `chrome_history`, `claude_code`, `codex_cli`, `git_log`, `knowledgec`, `leveldb_reader`, `markdown_vault`, `safari_history`, `spotlight`, `wechat` and `zsh_history`. They cover Chrome/Safari history, Claude Code/Codex sessions, Git commits, zsh history, approved Markdown/SQLite/LevelDB data, Spotlight metadata and macOS activity data. The `wechat` plugin currently probes locally; message importing is not implemented. Availability and permissions vary by source; onboarding lets you choose which history sources to enable.

The MCP server exposes four tools: `search_context` searches with keywords and optional time/source filters; `get_timeline` lists events in a time range; `get_evidence` resolves a result to its original location and stored excerpt; `rate_result` records whether a result was useful. Penpal does not include a language model—the connected AI host interprets the question and chooses which tool to call.

## How Penpal does it

Install the CLI, initialize its local runtime, check permissions and grant a host access:

```bash
brew install longfellow1/penpal/penpal && penpal install init
penpal doctor
penpal mcp grant claude --buckets Work,Personal
```

The grant command prints the connection configuration for the host and shows its token once. See the [README quick start](../../README.en.md#quick-start), [privacy notes](../privacy.en.md) and [host setup guide](give-claude-code-codex-cursor-memory.md).

[简体中文](local-memory-for-ai-agents.zh.md)
