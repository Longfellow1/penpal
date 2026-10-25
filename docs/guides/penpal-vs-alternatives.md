# Penpal vs. Screenpipe, OpenMemory, Basic Memory and Rewind

Penpal is aimed at searchable local activity and history on macOS, while the alternatives below describe different capture or memory workflows. Pick based on whether you want passive screen/audio capture, deliberately written Markdown notes, MCP memory operations, or a Mac activity app; the available source material does not establish every product's licensing or model-key requirements.

## Comparison

| Project | What it captures or stores | How agents access it | Local or cloud | External LLM/API key? | License | Platforms |
|---|---|---|---|---|---|---|
| Penpal MCP | Enabled local activity sources and imported histories, sessions, Git and approved Markdown | Stdio MCP: `search_context`, `get_timeline`, `get_evidence`, `rate_result` | Local server and database on macOS; returned context may enter the host AI conversation | Penpal has no built-in LLM or API key; the host AI has its own requirements | Apache-2.0 ([project](../../LICENSE)) | macOS |
| [Screenpipe](https://screenpi.pe/about) | Screen and audio, recorded locally | Local SQLite and localhost REST API; also ships an MCP server for Claude Desktop, Cursor and VS Code (`npx -y screenpipe-mcp`) | Local capture; the cited page does not establish all cloud options | See project | See project | macOS, Windows, Linux |
| [OpenMemory MCP (Mem0)](https://mem0.ai/blog/introducing-openmemory-mcp) | Memories added to, searched, listed and deleted through MCP tools | MCP tools for add/search/list/delete | Local-first; [self-hosted and hosted Mem0 options](https://docs.mem0.ai/openmemory) are described | [Self-hosted setup](https://docs.mem0.ai/openmemory) uses Docker and an OpenAI API key | See project | Self-hosted Docker; see project for OS details |
| [Basic Memory](https://docs.basicmemory.com/integrations/codex) | Notes you write, stored as plain Markdown | Shared across MCP clients including Codex, Claude Code, Cursor and Claude Desktop | Markdown storage; deployment details are in the project docs | See project | See project | MCP integrations for Codex, Claude Code, Cursor and Claude Desktop |
| [Rewind](https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/) | Mac screen/audio history app | MCP access is not established by the cited report | 9to5Mac reported that the app would be discontinued and screen/audio capture disabled from 2025-12-19 after Limitless was acquired by Meta; current availability is not independently checked here | See project | See project | macOS app; shutdown was announced |

## When to pick something else

- Choose [Screenpipe](https://screenpi.pe/about) if screen and audio recording with a local database and its MCP server matches your need.
- Choose [Basic Memory](https://docs.basicmemory.com/integrations/codex) if you want to deliberately write notes as Markdown and share them across MCP clients.
- Consider [OpenMemory MCP](https://mem0.ai/blog/introducing-openmemory-mcp) if you want explicit add/search/list/delete memory tools and accept its self-hosted Docker plus OpenAI API-key setup, or want to evaluate Mem0's hosted option ([setup docs](https://docs.mem0.ai/openmemory)).
- Rewind should be treated as a historical product option in light of the reported discontinuation ([9to5Mac report](https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/)).

Penpal fits when you want a local macOS memory service that imports supported histories, records enabled activity sources and exposes scoped, evidence-linked retrieval through MCP. See the [README](../../README.en.md), [privacy guide](../privacy.en.md) and [host setup](give-claude-code-codex-cursor-memory.md).

[简体中文](penpal-vs-alternatives.zh.md)
