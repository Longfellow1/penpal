# 什么是 AI Agent 的本地记忆层？什么时候需要？

本地记忆层让 AI Agent 跨会话检索保存在本机的活动和历史。当上下文没有写入普通文件、来源历史会被清理，或你需要限制并审计每个 AI Host 能读取的内容时，它可能有用。

## 本地记忆与让 Agent 用 bash 读文件

如果所需信息已经在 Agent 有权读取的文件里，额外的记忆服务未必有用。Penpal 面向的是文件读取的空白：

| 使用场景 | Agent 用 bash 读文件 | Penpal MCP |
|---|---|---|
| 通常不会写入文件的活动，例如窗口切换或剪贴板 | 事后无法找回 | 可由已启用的采集来源记录 |
| 可能被来源清理的本地历史 | 只能搜索来源中仍保留的内容 | 导入支持的历史到本机数据库，直到用户删除 |
| 控制多个 AI Host 的访问 | 取决于 Host 的文件权限和沙箱 | 可按客户端、数据分类和来源授权，并撤销、审计和追溯出处 |

当前来源插件 ID 为 `approved_sqlite`、`chrome_history`、`claude_code`、`codex_cli`、`git_log`、`knowledgec`、`leveldb_reader`、`markdown_vault`、`safari_history`、`spotlight`、`wechat` 和 `zsh_history`，覆盖浏览器历史、Claude Code/Codex 会话、Git、终端历史、获批 Markdown/SQLite/LevelDB 数据、Spotlight 和 macOS 活动数据。`wechat` 插件目前只做本机探测，尚未实现消息导入。来源可用性和权限各不相同；可通过首次引导选择要启用的历史来源。

MCP 工具为：`search_context` 搜索；`get_timeline` 按时间查看事件；`get_evidence` 返回原始位置和已存摘录；`rate_result` 记录结果是否有用。Penpal 不内置大模型，问题理解由连接的 AI Host 完成。

## Penpal 如何实现

安装并初始化 CLI、检查权限，再授权一个 Host：

```bash
brew install longfellow1/penpal/penpal && penpal install init
penpal doctor
penpal mcp grant claude --buckets Work,Personal
```

授权命令会打印 Host 配置，token 只显示一次。详见[快速开始](../../README.md#快速开始)、[隐私说明](../privacy.md)和[各 Host 配置](give-claude-code-codex-cursor-memory.zh.md)。
