# 多 AI 工具记忆：自动接入与写入架构（吃掉 Memmy）

> 状态：方案稿 v0.1（2026-10-28）。决策来源：用户 2026-10-28「吃掉 Memmy 放在当下做」「第一阶段至少覆盖 80%+ 主流的个人助理、Mac 可能用到的 agent，并默认接入，务必实现」。
> 大脑：`product-positioning-memory-infra`（推翻 D30 的"写入延后到 M4 之后"）、`ai-tool-auto-coverage`（覆盖率要求）、`memory-distillation-by-agent`、`core-no-embedded-llm`。

## 0. 结论

1. **采集改成"解析器即数据"**。每个 AI 工具一份声明式配方，写明安装识别、历史位置、格式映射、游标、MCP 配置写法；核心只有一个确定性的配方执行引擎。主流工具的配方随版本内置，长尾工具由宿主 AI 生成配方，引擎校验通过后启用。核心不调用大模型。
2. **默认接入**：检测到已安装的工具就自动开始增量采集，并把 Penpal 的 MCP 写进它的配置（带标记块、可一键撤销）。每个已接入工具都在 HUD 里列出，可单独关掉。
3. **写入**：新增 `commit_memory`。提炼由宿主 AI 做，Penpal 只负责校验、去重、合并、保留历史、带证据检索。通过技能约定，让宿主 AI 在会话开始时读、结束时写。
4. **迁移**：一键导入 Memmy 本地库（L1 事实层 + 技能层），可重跑、不重复。
5. **验收**：按加权的主流 Mac AI 工具清单计算两项覆盖率：自动采集覆盖率和自动接入（MCP）覆盖率。第一阶段两项都要 ≥ 80%。计算口径见 §5。

## 1. 现状与差距（2026-10-28 实测）

| 项 | 现状 | 差距 |
|---|---|---|
| 采集 | 只有 Claude Code、Codex 两个写死的插件（固定路径），按文件字节偏移做增量，后台 2–10 分钟扫一次 | 新工具要发版；格式一变就断；其余工具全部缺失 |
| 通用扫描 | 能把 SQLite/JSONL 列为"候选"，要人工审批，读出来是表格行，不是问答轮次 | 不算接入 |
| MCP 接入 | `penpal mcp grant` 只打印各宿主的配置命令 | 要用户手抄；不知道装了哪些工具 |
| 写入 | 无（D30 把 AT16 移出 V1） | Memmy 类产品的核心能力缺失 |

负责人本机样本（只统计文件数，未读内容）：

- **已采集**：Claude Code（62 个会话）；Codex 只采到 73 个。Codex 实有 314 个会话，其余 241 个是 zstd 压缩的 `.jsonl.zst`，现有插件只读 `.jsonl`，一直漏采。
- **有本地历史但未采集**：
  - Antigravity：200 个会话
  - WorkBuddy：38 个
  - pi：28 个
  - LM Studio：16 个
  - Cursor：编辑器 9 个工作区库，Agent 会话 9 个
  - opencode、Hermes、Kiro、VS Code Copilot：各 1 个库
  - Claude 桌面版：本地 Agent / Claude Code 会话目录
- **已安装但未发现本地会话**：Gemini CLI、Qwen Code、Windsurf、Trae、Qoder、iFlow、Factory Droid、Goose、OpenClaw、Continue、Copilot CLI。
- **Memmy 1.0.9** 在同一台机器上接了 8 个工具，约 3000 条记忆：codex 2041、hermes 267、claude_code 261、workbuddy 226、pi 100、opencode 59、cursor 43、openclaw 25。

## 2. 调研结论：同类项目的最佳实践

调研范围：
- Memmy（拆安装包 + 本机库表结构）
- deja-vu（MIT，约 1.3k 星，被动读盘，约 45 个适配器）
- cass、SpecStory、claude-mem
- Mem0 / OpenMemory、Supermemory、Basic Memory、Letta、Zep / Graphiti、MemOS
- Screenpipe、Pieces、Cline Memory Bank

完整报告不进仓库，结论如下。

### 2.1 采纳

| 做法 | 出处 | 我们怎么用 |
|---|---|---|
| **被动读盘为主，hook 为辅** | deja-vu、cass 读盘覆盖 30–45 个工具；hook 只在需要实时或结构化事件时加 | 配方引擎读盘；Claude Code / Codex 的 SessionStart 等 hook 只用于"会话开始注入" |
| **声明式同步配方 + 宿主 AI 生成配方** | Memmy：未知工具由宿主 AI 探查存储，写出 jsonl / json / sqlite 字段映射；后端校验后保存，之后的增量同步不再需要 AI | 同构，见 §3.2、§3.3 |
| **安装身份闸门** | Memmy：先证明工具真的装了（.app、可执行文件、包元数据），历史目录和缓存不算证据 | 防止配方被用来读任意数据库，见 §3.3 |
| **每文件状态清单做增量** | deja-vu `manifest.gob`：文件变长只读新增部分；被重写时追加新记录、旧记录作废 | 沿用现有 `sync_cursors`，扩展到 SQLite 水位 |
| **稳定消息 id，重扫幂等** | Memmy messageId；deja-vu | 优先用源记录 id，否则用"路径 + 偏移 / 主键"的哈希 |
| **原始会话是真相源，索引和记忆可重建** | deja-vu；Graphiti 的 episode → 实体 | 记忆层每条都挂证据（event id），可以重跑 |
| **保留历史 + 当前视图，不覆盖** | Graphiti 用失效时间；Supermemory 用 isLatest；Mem0 2026 改为只追加 | 记忆有 `valid_from` / `superseded_by`，查询默认只看当前有效的 |
| **原始层与派生层分开** | MemOS 分 L1 原始轨迹 / L2 策略 / L3 世界模型 / 技能；Memmy 同构 | 原始事件 → 记忆（事实 / 偏好 / 决策 / 流程技能）两层，不做 L2 / L3 自动归纳 |
| **两段式读取 + 字符预算** | claude-mem 先给短索引再按需取详情；Claude Code hook 单条上限 10k 字符 | 会话开始只注入 ≤ 2k 字符的索引，详情走 `search_context` |
| **默认按来源 / 项目隔离** | claude-mem 默认只注入当前工具；Windsurf 限当前工作区 | 注入默认按当前项目；跨工具检索走现有 token 范围 |
| **索引时脱敏，原文不动** | deja-vu 把 key、token、JWT、PEM 替换为 `[redacted:<kind>]` | 复用现有清洗（D27），补齐密钥模式 |
| **一条命令接入所有已检测宿主** | `deja install --auto` 给每个检测到的 agent 写 MCP 配置和技能，重跑只改自己的标记块 | `penpal connect --auto`，安装时默认执行，见 §3.4 |

### 2.2 不采纳

- **核心里跑大模型提炼**（Mem0、Graphiti、Supermemory、claude-mem、Memmy 都这样做）：违反 `core-no-embedded-llm`；抽取规则不透明、版本漂移快，Mem0 一年里改过一次写入算法。提炼交给宿主 AI。
- **L2 策略 / L3 世界模型自动归纳**：Memmy 本机这两层都是空的；MemOS 只在插件里提，论文里没有。需要时以"整理任务"的形式交给宿主 AI 定期跑。
- **屏幕录制作主路径**（Screenpipe、Pieces）：有静默停采、存储不回收的投诉（Screenpipe #7679 / #7770）。Penpal 已有 AX 文字采集作兜底，不扩大。
- **用厂商基准数字选型**：Supermemory 的 97% 是 Recall@20；Mem0 的分数在 66.9 和 92.5 之间来回。我们只看自己的评测（Eval PRD）。

### 2.3 必须防住的失败模式（写进验收）

| 失败模式 | 出处 | 验收条件 |
|---|---|---|
| 静默少采或停采，健康检查却说正常 | SpecStory #174 / #82（只存首条）；Screenpipe #7679 | 每个已接入工具都有"最近新数据时间"和"解析成功率"；工具在活跃、Penpal 却超过 30 分钟没采到 → 黄灯 |
| 上游格式变化导致记忆消失 | Cursor 论坛；SpecStory FAQ | 配方带格式探针，解析成功率跌破 90% → 停用该配方并告警，不写半截数据 |
| SQLite 锁冲突 | SpecStory #54（Cursor `state.vscdb`） | 只用只读或快照方式打开，不阻塞宿主写入 |
| 权限 fail-open | OpenMemory 旧版 `search_memory` | 每个宿主独立 token；无可访问数据时返回空，不跳过过滤（G1） |
| 存储膨胀 | Screenpipe #7770 | 大块工具输出截断入库；记录占用，进 doctor |

## 3. 架构

```
已安装工具检测 ─┐
                ├─► 工具目录（内置配方 + AI 生成配方）─► 配方执行引擎 ─► 原始事件（现有 events 表）
通用候选扫描 ───┘          ▲                              │ 游标 / 稳定 id / 脱敏
                           │ propose_recipe（MCP）          ▼
宿主 AI（Claude Code / Codex / Cursor …）◄── search_context / get_timeline ── 记忆层（新）
          │  commit_memory（MCP）─────────────────────────────▲
          └─ 技能约定：会话开始读、结束写
Memmy 导入 ──────────────────────────────────────────────────┘
```

模块落在现有分层里（`docs/ENGINEERING.md`）：
- 采集侧：`penpal/sources/` 新增 `catalog/`（数据文件）和 `plugins/ai_tools.py`（一个插件执行全部配方）。
- 接入侧：`penpal/mcp/` 新增 `connect.py`。
- 记忆侧：`penpal/store/` 新增记忆表和仓储，`penpal/mcp/tools.py` 新增工具。

### 3.1 工具目录（Tool Catalog）

每个工具一份 TOML，随版本内置在 `penpal/sources/catalog/`，不含可执行代码：

```toml
id = "workbuddy"
name = "WorkBuddy"
kind = "agent"                      # agent | ide | assistant
detect.any = [{ app_bundle = "com.tencent.workbuddy" }, { dir = "~/.workbuddy" }]

[[history]]
format = "jsonl"
glob = "${WORKBUDDY_CONFIG_DIR:-~/.workbuddy}/projects/*/*.jsonl"
fields = { message_id = "uuid", conversation_id = "sessionId", role = "message.role",
           content = "message.content", created_at = "timestamp", workspace = "cwd" }
role_map = { human = "user" }
cursor = "file_offset"              # file_offset | sqlite_rowid | sqlite_column:<col> | file_mtime
probe = { require_fields = ["message.role", "timestamp"], min_parse_rate = 0.9 }

[mcp]
config = "~/.workbuddy/mcp.json"    # 配置文件位置与格式，供 §3.4 写入
style = "json_mcpServers"
```

- 支持格式（zstd 解压需要新增依赖 `zstandard`：Python 3.13 标准库没有 zstd，Codex、OpenClaw、Zed、DeepSeek Harness 都在用它；Homebrew 公式要同步加 resource）：
  - `jsonl`：每行一条，字段用点路径。
  - `json`：用 `records_path` 选消息数组。
  - `sqlite`：一条只读 `SELECT`，可用 SQLite JSON 函数展平，例如 Cursor 的 `cursorDiskKV`。
  - 以上三种都可以叠加 zstd 压缩。
- **逃生口**：protobuf 或数组展开等声明式写不出来的格式，允许"代码适配器"。适配器仍登记在目录里，现有 Claude Code / Codex 插件迁为这一类，行为不变。
- **明确不读**：加密存储（如 TRAE IDE 的数据库）和只在云端的历史，目录里标 `readable = false` 并写明原因，接入只走 MCP。

### 3.2 配方执行引擎（Recipe Engine）

- **增量**：沿用 `sync_cursors`。每个文件记录偏移和 inode：文件变长只读新增；文件变短或 inode 变了从头读，靠稳定 id 去重。SQLite 用 rowid 或指定列做水位。
- **稳定 id**：`external_id` 取源记录 id，没有就用"相对路径 + 行号 / 主键"的哈希。同一条消息永远同一个 id，重扫幂等。
- **只读打开**：用 SQLite `mode=ro&immutable=1`；遇到 WAL 或锁时复制快照到临时目录再读。永远不写宿主文件。
- **对话结构**：输出的事件带 `conversation_id`、`role`、`turn` 序号，`kind = "message"`。检索层能按"一问一答"组装。
- **保底**：未知字段原样放进 `metadata.raw`（截断）。超大工具输出只保留前 2k 字符和长度。
- **脱敏**：入库前过现有清洗，再补密钥模式（API key、token、JWT、PEM、URL 中的密码）。
- **格式探针**：每轮同步统计解析成功率。低于 `min_parse_rate` 时，本轮不提交游标，配方标为"格式可能已变"，HUD 黄灯；可以让宿主 AI 修配方（§3.3）。

### 3.3 自适应发现

1. **检测**：后台服务每 10 分钟（安装时立即一次）检查目录里每个工具的 `detect` 信号：`.app` bundle id、命令行工具、配置目录。命中且历史路径存在 → **自动启用**（默认接入），HUD 出现该工具，可以关掉。
2. **长尾**：通用候选扫描发现某个已安装工具的会话存储，目录里却没有配方时，在 HUD 里列为"可接入"。用户点「让 AI 接入」，或宿主 AI 在对话里被要求接入，宿主 AI 就探查格式，调用 MCP 工具 `propose_source_recipe` 提交配方。
3. **校验闸门**（全部由确定性代码判定）：
   - 安装身份：配方声明的工具必须能找到真实安装证据（.app / 可执行文件 / 包元数据），历史目录本身不算。
   - 路径约束：只能落在该工具自己的目录下；不能命中隐私黑名单（Messages、Mail、钥匙串、浏览器 Cookie 等现有 `sqlite_rules`）。
   - 试抽取：连续两次抽取 id 完全一致；至少有一个完整的"用户 → 助手"轮次；解析成功率 ≥ 90%。
   - 全部通过 → 写入本地 `~/.penpal/catalog/`，自动启用，HUD 提示"已接入 X（AI 生成的配方）"。任一项不通过 → 返回具体原因，不启用。
4. **格式变化自修**：探针报警后，下一次有宿主 AI 会话时，通过技能约定提示它修配方，走同一套校验。

### 3.4 自动接入（MCP）

- `penpal connect --auto` 在安装和升级时默认执行；后台服务检测到新装的工具时也会执行。
- 对每个支持 MCP 的已安装工具：
  - 签发独立 token（现有 `grant`）；
  - 按目录里的 `[mcp]` 写配置，比如 `~/.claude.json`、`~/.codex/config.toml`、`~/.cursor/mcp.json`、Claude 桌面版的 `claude_desktop_config.json`；
  - 写一个技能或规则文件（`~/.agents/skills/penpal/SKILL.md`，或该工具自己的技能目录）。
- **写配置的规矩**：
  - 先备份原文件；
  - 只增改 Penpal 自己的条目或标记块，重跑幂等；
  - 格式解析失败就不写，只提示；
  - 宿主正在运行时也照写，下次会话生效。
- `penpal connect --list` 查看接入情况，`penpal disconnect <tool|--all>` 撤销并恢复，HUD 里有同样的开关。
- 不支持 MCP 的工具（如 Cherry Studio 要在界面里导入）：目录标 `mcp = "manual"`，HUD 给一步操作说明。

### 3.5 记忆写入（commit_memory）

- **MCP 工具** `commit_memory(items[])`，每项：
  - `type`：fact / preference / decision / procedure；
  - `content`：一句话；
  - `scope`：global 或 project，project 时自动取当前项目；
  - `evidence_ids`：可选，指向原始事件；
  - `supersedes`：可选，指向被替代的旧记忆；
  - `confidence`。
- **存储**：新表 `memories`（id、type、content、scope、project、source_tool、evidence_ids、content_hash、valid_from、superseded_by、status、created_at），配 FTS。不用向量，与现有检索一致。
- **核心规则**（确定性）：
  - 规范化后哈希相同 → 合并证据；
  - `supersedes` 写入 `superseded_by`，旧记忆保留；
  - 敏感内容过清洗，命中密钥模式就拒绝；
  - 只能引用该 token 有权看到的证据（G1 / AT16）；
  - 每次写入进审计。
- **读路径**：`search_context` 同时查记忆层和原始事件。记忆命中排在前面，并附证据链接；默认只返回当前有效的记忆。
- **技能约定**：给宿主 AI 的一页说明：
  - 会话开始：调 `search_context` 取本项目要点，不超过 2k 字符；
  - 会话结束或出现关键决策时：`commit_memory`；
  - 不写密钥、不写猜测、不写大段日志。
  - Claude Code / Codex 支持 SessionStart hook 的，由 hook 自动注入索引。
- **审核**：低置信或第一次出现的偏好类记忆进 HUD「待确认」，其余直接生效，可删可改。

### 3.6 Memmy 导入

- `penpal import memmy`，HUD 首次检测到 `~/.memmy/memory-service/memory.sqlite` 时提示：
  - 以只读方式读 `memories`（`memory_layer in ('L1','Skill')`、`status = 'activated'`）；
  - 映射：L1 → fact，Skill → procedure；`app_id` → `source_tool`，`created_at` 原样保留；
  - 以 Memmy 记忆 id 作外部 id，重跑幂等。
- 不导入原始对话（`raw_turns`）：这些对话已经能由配方从各工具本身读到。

## 4. 边界

- 核心不调用大模型；提炼、整理、配方生成都由宿主 AI 完成，核心只做确定性校验与执行（D24 / D33 / `core-no-embedded-llm`）。
- 不破解加密存储，不读凭据，不绕过云端边界。
- 默认接入的范围限于 AI 工具的会话历史，不扩大到其它应用的数据库。

## 5. 覆盖率口径与第一阶段清单

### 5.1 两个指标

1. **主指标：会话覆盖率**（可测量）。在一台真实 Mac 上，统计目录里所有工具留在磁盘上的 AI 会话数，算其中已被 Penpal 自动入库的比例。新增 `penpal sources coverage` 命令，只输出各工具的会话数和已入库数，不输出内容。
   - 负责人本机基线：可按文件计数的会话约 668 个，已入库 135 个，**约 20%**。其中 Codex 的压缩会话漏采 241 个；数据库型存储（Cursor、opencode、Hermes）的会话数还没算进分母。
   - 第一阶段目标 ≥ 80%。种子用户的 Mac 用同一命令复测。
2. **辅指标：加权工具覆盖率**（估算）。对下表的主流工具按权重求和：
   - 自动采集覆盖率 = 已自动采集的工具权重 / 全部权重；
   - 自动接入覆盖率 = 已自动写入 MCP 的工具权重 / 支持 MCP 的工具权重。
   - 两项第一阶段都要 ≥ 80%。
   - **权重是主观档位**（高 3 / 中 2 / 低 1）。没有公开的 Mac 份额数据，只参考了 Stack Overflow 2025 调查、GitHub stars 和中国市场情况。份额数据到手后再校准。

### 5.2 第一阶段清单（存储位置以 deja-vu 公开的路径表和各工具文档为准，入库前用合成夹具验证）

| 档 | 工具 | 本地历史 | 格式 / 位置（摘要） | MCP | 第一阶段做法 |
|---|---|---|---|---|---|
| 高 | Claude Code | 可读 | `~/.claude/projects/**/*.jsonl` | `~/.claude.json` | 已有，迁入目录 |
| 高 | Codex CLI / 桌面 | 可读 | `~/.codex/sessions/**/rollout-*.jsonl(.zst)` | `~/.codex/config.toml` | 已有；**补 zstd（本机 77% 的会话是压缩的）**和 archived_sessions |
| 高 | Cursor | 可读（无官方格式） | `state.vscdb` 中的 `cursorDiskKV`；`~/.cursor/projects/**/agent-transcripts` | `~/.cursor/mcp.json` | 内置配方，只读快照打开 |
| 高 | VS Code Copilot Chat | 部分 | `workspaceStorage/*/chatSessions/*.json(l)` | 用户级 `mcp.json`（键 `servers`） | 内置配方，两种格式都支持 |
| 高 | Claude 桌面 | **不可读** | 聊天在云端；本地 Agent 会话目录只有工作文件，没有对话记录（2026-10-29 实测） | `claude_desktop_config.json` | 只接 MCP，标为不可读 |
| 高 | ChatGPT 桌面 | **不可读**（2024-07 起加密） | — | 未证实 | 不采集（§5.3），标为不可读 |
| 中 | Antigravity（含原 Gemini CLI） | 可读 | `~/.gemini/antigravity*/brain/*/…/transcript.jsonl`；`~/.gemini/tmp/*/chats` | `~/.gemini/…` | 内置配方 |
| 中 | Windsurf | 部分 | `~/.codeium/windsurf/cascade/*.pb`（protobuf） | `mcp_config.json` | MCP 先接；历史用代码适配器，放第二批 |
| 中 | Cline / Roo / Kilo | 可读 | 每个任务一个 `api_conversation_history.json`（整文件重写） | 各自 `mcp_settings.json` | 内置配方，按 mtime 做增量 |
| 中 | 豆包桌面 | 未证实 | — | 未证实 | 不采集（§5.3），标为不可读 |
| 中低 | Kiro、Trae CLI、Qwen Code、opencode、Goose、LM Studio、Continue、Zed、Kimi Code | 大多可读 | JSONL / SQLite / JSON（Zed 需 zstd） | 多数支持 | 内置配方 |
| 低 | WorkBuddy / CodeBuddy、Hermes、pi、OpenClaw、iFlow、Factory Droid、Aider、Amp、Cherry Studio | 大多可读 | 见 deja-vu 路径表 | 部分支持 | 内置配方；没有现成格式的走 §3.3 |
| 低 | Qoder IDE、TRAE IDE | **不可读**（加密） | — | 支持 | 只接 MCP |
| 低 | Perplexity、DeepSeek、Kimi 桌面、Ollama 桌面 | 未证实 | — | 未证实 | 不采集（§5.3），标为不可读；支持 MCP 的照常接入 |

估算：内置配方做完后，加权自动采集覆盖率约 78%；再加上 Windsurf 适配器约 82%。差距集中在个人助理类：ChatGPT、豆包。按 §5.3 的决定，这类工具移出采集分母，所以实际估算会更高，以 `penpal sources coverage` 实测为准。

**P1 实际落地（2026-10-29）。** 配方只有在真实安装上核对过字段结构（只看键名和数量）才放进 catalog：
- **已上线（8 个）**：Antigravity、Cursor（agent-transcripts + `state.vscdb`）、WorkBuddy、Kiro（IDE 会话）、opencode、Hermes、pi、Cline。另有 Claude Code、Codex 两个原有插件。
- **标为不可读（5 个）**：Claude 桌面、ChatGPT、豆包、Kimi、DeepSeek。
- **草稿待真实样本（10 个）**：Continue、Gemini CLI、Qwen Code、Copilot CLI、CodeBuddy、Goose、Kilo Code、Roo Code、OpenClaw、Kimi Code。负责人本机没有这些工具的数据，需要种子用户的样本核对后再上线。
- **需要代码适配器，放第二期（6 个）**：LM Studio、VS Code Copilot Chat、Zed、Crush、Aider、Trae CLI。暂不计入 `penpal sources coverage` 的分母。
- 没有逐条时间的格式（Cursor agent-transcripts、Kiro）用文件修改时间作为消息时间。

### 5.3 个人助理类：第一阶段不覆盖（负责人 2026-10-29 决定）

ChatGPT 桌面的本地会话是加密的。豆包、Kimi、DeepSeek 桌面版查不到公开的本地格式，大概率只存在云端。按 §4 不破解、不读凭据，所以这类工具没有"读盘"这条路。

曾考虑三种补法：读屏幕上可见的对话、导入官方导出、MCP 接入。负责人的决定是：读屏幕抓不到完整对话，放弃这一类，不做替代采集。落地口径：
- 这些工具在 catalog 里标 `readable = false` 并写明原因，`penpal sources coverage` 会照实显示为"不可读"，不计入采集分母。
- 支持 MCP 的照常自动接入，让它们能读 Penpal；这不算采集。
- 不做屏幕对话采集，也不做导出包导入。以后要重开，需要负责人重新决定。

## 6. 分期与验收

| 期 | 内容 | 估时 | 验收 |
|---|---|---|---|
| P1 | 工具目录 + 配方引擎 + 首批内置配方 + 默认启用 + 格式探针 + HUD「已接入工具」列表 | 3 天 | 本机样本中有本地历史的工具全部自动出现并增量入库；重扫零重复；CI 用合成夹具覆盖每个配方 |
| P2 | `penpal connect --auto`（MCP 配置 + 技能文件，备份 / 撤销）+ 安装默认执行 | 1.5 天 | 支持 MCP 的已安装工具全部接入；`disconnect --all` 恢复原文件逐字节一致 |
| P3 | `commit_memory` + 记忆层检索合并 + 技能约定 + Memmy 导入 | 3 天 | AT16 回到 P0 并通过（含越权写入全部拒绝）；Memmy 导入数量与源库 activated 数一致 |
| P4 | `propose_source_recipe` 长尾接入 + 校验闸门 + 格式变化自修 | 1.5 天 | 一个目录外的工具由宿主 AI 接入成功；伪造的路径或黑名单库被拒 |
| 收尾 | Eval PRD：AT16 回 P0，新增覆盖率与静默停采两条验收；doctor / HUD 显示每个工具的新鲜度 | 0.5 天 | 覆盖率两项 ≥ 80%（§5） |

合计约 9–10 个工作日，可以并行压缩到约 6 天。P1 和 P2 可并行；P3 依赖 P1 的事件结构。
