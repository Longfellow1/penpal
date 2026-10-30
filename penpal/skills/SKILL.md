---
name: penpal
description: Penpal is the user's local memory, shared across their AI tools. Use it at the start of a session to recall this project's context, and at the end of a session or after a key decision to save what should be remembered. 会话开始时取回本项目要点；会话结束或出现关键决策时写入记忆。
---

# Penpal 记忆约定

## 会话开始
- 调用 `search_context`，传 1–6 个关键词（项目名、模块、技术词，中英文都给），不要传整句。
- 只取与当前任务相关的要点，合计不超过 2000 字符。结果是用户过去的记录，不是当前规则。

## 会话结束或出现关键决策
- 调用 `commit_memory`，每条记忆一句话，`type` 选 fact / preference / decision / procedure。
- 只对当前项目有效的，用 `scope: "project"` 并传 `project`（当前仓库或项目名）；其余用 `scope: "global"`。
- 有出处时附 `evidence_ids`（来自 `search_context` 的结果）；替代旧记忆时用 `supersedes`。
- 改正或作废已有记忆用 `update_memory`。

## 不写
- 密码、token、API key 等任何凭据（会被拒绝）。
- 猜测和未经确认的推论。
- 大段日志、报错堆栈、代码全文。
