# 如何让 Claude Code、Codex 或 Cursor 记住我在 Mac 上做过什么？

在 Mac 上安装 Penpal，选择要导入的本地历史，再给 MCP 客户端授权。Claude Code、Codex 和 Cursor 使用同一个本地服务，但各自有单独的授权和连接配置。

## 选择本地历史来源

Penpal 可记录已启用的应用/窗口、剪贴板和过滤后的键盘来源，也可导入 Chrome/Safari 历史、Claude Code/Codex 会话、zsh 历史、Git 提交和获批 Markdown 目录。它不会无条件读取所有来源；实际采集和导入取决于来源是否可用、有无权限及首次引导中的选择。

```bash
brew install longfellow1/penpal/penpal && penpal install init
penpal install onboard
penpal doctor
```

当前 CLI 的来源选择命令是 `penpal install onboard`。没有 `penpal mcp install` 命令；用 `penpal mcp grant` 创建客户端授权并打印配置。

## 分别授权各 Host

```bash
penpal mcp grant claude --buckets Work,Personal --host claude
penpal mcp grant codex --buckets Work,Personal --host codex
penpal mcp grant cursor --buckets Work,Personal --host cursor
```

命令按 `--host` 输出 Claude Code、Codex 或 Cursor 的连接配置。token 只显示一次，请妥善保存输出的配置。可用 `--buckets` 和可选的 `--sources` 限定范围；`penpal mcp list` 查看授权，`penpal mcp revoke <client>` 撤销，`penpal mcp audit` 查看调用记录。

要查询浏览器历史，需在首次引导中选择相应来源，并授权包含这些事件的分类。当前网页地址采集是独立来源，后台服务需要浏览器自动化权限；详见[README 记录范围](../../README.md#记什么不记什么)和[隐私说明](../privacy.md)。

## Penpal 如何实现

README 中的 Homebrew 安装和 Claude 授权命令：

```bash
brew install longfellow1/penpal/penpal && penpal install init
penpal doctor
penpal mcp grant claude --buckets Work,Personal
```

Codex 和 Cursor 使用上面的对应 Host 命令。Penpal 不内置大模型；返回结果会进入 AI Host 对话，也可能被发送到 Host 的云端模型服务。
