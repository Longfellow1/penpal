# GEO prompt panel

Use these 15 fixed prompts each week. Keep the wording unchanged so results can be compared over time. The panel covers category discovery, problem searches, host-specific setup and alternatives.

## Fixed prompts

| ID | Prompt |
|---|---|
| p01 | memory for Claude Code MCP local Mac activity history browser |
| p02 | local memory layer for AI agents macOS MCP open source |
| p03 | Penpal MCP memory Claude Code Codex Cursor |
| p04 | Longfellow1 penpal github |
| p05 | Rewind alternative open source MCP Mac clipboard browser history |
| p06 | OpenMemory alternative local no API key |
| p07 | What is a local memory layer for AI agents, and when do you need one? |
| p08 | How do I give Claude Code memory of my browser history? |
| p09 | How do I give Claude Code, Codex or Cursor memory of what I did on my Mac? |
| p10 | local MCP memory server for Codex |
| p11 | How do I add local memory to Cursor on macOS? |
| p12 | Rewind alternative with MCP |
| p13 | Which local MCP memory servers work on macOS? |
| p14 | 给 Claude Code 加本地记忆 |
| p15 | macOS 上有哪些本地 MCP 记忆服务器？ |

## Scoring by engine

Score each prompt separately for each engine. Record `1` when the condition is met, `0` when it is not, and `not_measured` when the response was not checked.

| Signal | Score 1 when… |
|---|---|
| Retrieved | The Penpal repository or a Penpal page appears in search results. |
| Cited | An AI-generated answer cites the Penpal repository or a Penpal page. |
| Recommended | An AI-generated answer names Penpal as an option for the prompt. |

Search engines can be scored for retrieval. For AI answer engines, record all three signals. A mention without a source link counts as recommended, not cited.

## Baseline: 2026-10-25

| Date | Engine | Retrieval baseline | Notes |
|---|---|---|---|
| 2026-10-25 | Web search (engine unspecified) | 0/6 | Included the Penpal-versus-host query and the repository-name query. Individual result captures were not supplied. |

`docs/geo/results.csv` records the search-only baseline; p06 is marked not yet measured as requested. Citation and recommendation scores were not measured.

## Weekly rerun

Once a week, the owner or an agent with web search should run all 15 prompts against the same named search and AI answer engines. Record the date, engine, prompt ID and each applicable score in `results.csv`; add one row per engine and prompt. Save short notes only when they explain a changed score, and do not put personal data from search results in the repository.
