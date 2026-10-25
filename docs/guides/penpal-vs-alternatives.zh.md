# Penpal 与 Screenpipe、OpenMemory、Basic Memory、Rewind 对比

Penpal 面向 macOS 本地活动与历史检索；下列产品对应不同的采集或记忆方式。可按需选择屏幕/音频记录、手写 Markdown 笔记、MCP 记忆操作或 Mac 活动记录应用；现有资料没有说明每个产品的许可证和模型密钥要求。

## 对比

| 项目 | 记录或保存什么 | Agent 如何访问 | 本地或云端 | 是否需要外部 LLM/API Key | 许可证 | 平台 |
|---|---|---|---|---|---|---|
| Penpal MCP | 已启用的本机活动来源，以及导入的历史、会话、Git 和获批 Markdown | stdio MCP：`search_context`、`get_timeline`、`get_evidence`、`rate_result` | macOS 本地服务和数据库；返回内容会进入 Host AI 对话 | Penpal 不内置 LLM，也不需它自己的 API Key；Host AI 有各自要求 | Apache-2.0（[项目许可](../../LICENSE)） | macOS |
| [Screenpipe](https://screenpi.pe/about) | 本地录制屏幕和音频 | 本地 SQLite、localhost REST API；也提供 Claude Desktop、Cursor、VS Code 的 MCP server（`npx -y screenpipe-mcp`） | 本地采集；引用页面未说明所有云端选项 | 见项目 | 见项目 | macOS、Windows、Linux |
| [OpenMemory MCP（Mem0）](https://mem0.ai/blog/introducing-openmemory-mcp) | 通过 MCP 添加、搜索、列出和删除记忆 | MCP 工具提供上述操作 | 本地优先；支持[自托管和 Mem0 托管版](https://docs.mem0.ai/openmemory) | [自托管配置](https://docs.mem0.ai/openmemory)需要 Docker 和 OpenAI API Key | 见项目 | Docker 自托管；操作系统支持见项目 |
| [Basic Memory](https://docs.basicmemory.com/integrations/codex) | 用户主动撰写的纯 Markdown 笔记 | Codex、Claude Code、Cursor、Claude Desktop 等 MCP 客户端共享 | Markdown 存储；部署细节见项目文档 | 见项目 | 见项目 | 上述 MCP 客户端 |
| [Rewind](https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/) | Mac 屏幕/音频历史应用 | 引用报道未说明 MCP 接入 | 9to5Mac 报道其被 Meta 收购后将停止 Mac 应用，并计划从 2025-12-19 起停用屏幕/音频采集；此处未独立核实当前可用性 | 见项目 | 见项目 | macOS 应用；已发布停运计划 |

## 什么时候选其他工具

- 需要屏幕与音频记录、本地数据库和 MCP server，可看 [Screenpipe](https://screenpi.pe/about)。
- 想把自己主动写的笔记保存为 Markdown，并供多个 MCP 客户端共享，可看 [Basic Memory](https://docs.basicmemory.com/integrations/codex)。
- 想要明确的记忆增删查列工具，并接受自托管所需的 Docker 与 OpenAI API Key，可看 [OpenMemory MCP](https://mem0.ai/blog/introducing-openmemory-mcp)；它也介绍了 Mem0 托管方案（[配置文档](https://docs.mem0.ai/openmemory)）。
- Rewind 的状态需结合其[停运报道](https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/)看待。

如果你需要 macOS 本地记忆服务、导入支持的历史、按来源采集活动，并通过 MCP 做授权检索和证据回溯，可评估 Penpal。详见[README](../../README.md)、[隐私说明](../privacy.md)和[Host 配置](give-claude-code-codex-cursor-memory.zh.md)。
