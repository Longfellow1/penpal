# 第三方记忆通用导入：设计

> 状态：设计已定（负责人 2026-11-10 裁定三项取舍，见 §1）。实现按 §7 分 PR 推进。
> 取代 `memory-write-architecture.md` §3.6「Memmy 导入」中写死 Memmy 的做法。
> 调研：Codex（gpt-6-luna）只读调研 + 主控抽查，出处见 §8。

## 0. 结论

- `penpal import` 从「只认 Memmy」改为**一条共享清洗流水线 + 每个系统一个小适配器**。适配器只负责「从哪读、怎么切成单条、哪些算已提炼」；去敏、标题、去重、版本、撤销、审计全部走同一条流水线。
- 只导入**已提炼的记忆**（事实、偏好、技能/流程），不导入原始对话，也**不导入规则/指令文件**（CLAUDE.md、AGENTS.md、GEMINI.md、Cursor/Kiro/Cline 规则）。
- 首批内置：**Memmy（迁移，行为不变）+ Claude Code 自动记忆 + Codex 合并记忆**。第二批：官方 MCP 记忆服务（JSONL）、Basic Memory。
- 重复导入时与源头对齐：源头改了→出新版本、旧版本标「被取代」；源头删了→标「退役」，搜索不再返回，但数据保留（符合 D32），用户可按来源彻底删除。
- 不改表结构，不加依赖，不调用大模型。

## 1. 负责人裁定（2026-11-10）

| 问题 | 裁定 | 理由 |
|---|---|---|
| 规则/指令文件要不要导入 | **V1 不导入** | 指令、事实、临时要求混在一起，没有稳定编号；各工具本来就读自己的规则文件，项目 `AGENTS.md` 已是多家共用标准。导入后会把过时指令当记忆反复提供给 AI。 |
| 源头改了/删了怎么办 | **改了出新版，删了标退役** | 结果和源头一致，又不违反 D32「不自动删除」。 |
| 首批范围 | **Claude Code + Codex 记忆** | 和 Penpal 目标用户（重度 AI 编程工具用户）重合度最高；格式有官方来源，且已在真机核对。 |

另有一条沿用的产品约束：不能假设用户装了哪个工具（见 brain `product-positioning-memory-infra`）。适配器一律「发现了才提示」，没发现就显示「未发现」，不报错。

## 2. 调研结论：系统 × 格式 × 处理

只列与决策有关的；完整证据见调研报告（§8）。

| 系统 | 本地位置 | 最小单元 | 稳定标识 | 处理 |
|---|---|---|---|---|
| Memmy | `~/.memmy/memory-service/memory.sqlite` | 一行（`L1` 且有摘要、或 `Skill`） | 原生 `id` | **首批**（迁移） |
| Claude Code 自动记忆 | `~/.claude/projects/<项目>/memory/*.md`（可被设置改路径） | 一个主题文件；`MEMORY.md` 是索引，跳过 | 相对路径 | **首批** |
| Codex 合并记忆 | `~/.codex/memories/`：`MEMORY.md`、`skills/*/SKILL.md` | `MEMORY.md` 的一个标题块；一个技能文件 | 标题路径 / 技能目录名 | **首批**（功能默认关闭，多数用户会显示「未发现」） |
| 官方 MCP 记忆服务 | 用户配置的 `MEMORY_FILE_PATH`（JSONL） | 实体下的一条 observation；relation 不导入 | 实体名 + 内容哈希 | 第二批，需 `--path` |
| Basic Memory | 默认 `~/basic-memory/*.md` | `## Observations` 下的一条 `- [类别] 内容` | `permalink` + 内容哈希 | 第二批 |
| Gemini CLI | `GEMINI.md`（全局/项目） | 无：记忆和用户规则混写在同一文件 | 无 | 归入规则文件，V1 不做 |
| Cursor/Windsurf/Cline/Roo/Kiro/Continue/Copilot 规则 | 各自规则目录 | 规则文件 | 无 | V1 不做 |
| Cursor Memories、Windsurf Cascade Memories | 本地存储未公开/功能已撤 | 未知 | 未知 | 不做 |
| ChatGPT / Claude.ai / Gemini 应用记忆 | 云端；官方导出是否含记忆未确认 | — | — | 不做自动导入 |
| Mem0、Letta、Graphiti | 依用户配置的后端/目录 | 各异 | 各异 | 不内置；有真实需求再议 |

各系统明确**排除**的内容（适配器必须测到）：

- Codex：`raw_memories.md`（第一阶段未合并的原料）、`rollout_summaries/`（逐会话摘要）、`memory_summary.md`（索引）、`sessions/*.jsonl`、`memories_*.sqlite`。
- Claude Code：`MEMORY.md` 索引、会话记录 `*.jsonl`。
- Memmy：未提炼的对话轮次（`L1` 无有效摘要）——沿用 #186 的规则。

## 3. 架构

```
penpal import <来源> [--path P] [--dry-run]        penpal import --list
          │                                                │
          ▼                                                ▼
   适配器（每个系统一个，≤150 行）      发现：每个适配器的 discover()
   discover(home) -> 来源根 | None            → 未发现 / N 条候选 / 已导入 M 条
   read(根) -> 候选 | 拒绝(原因)、complete
          │
          ▼
   共享流水线 penpal/memory/importing/pipeline.py
   校验 → 去敏 → 标题 → 跨来源去重 → 与已导入行对账 → 一个事务写入 → 审计
```

### 3.1 适配器契约

```text
Candidate:  external_id  稳定定位符（不是内容哈希；见 §4）
            kind         fact | preference | procedure
            text         正文（进 detail）
            title_hint   来源自带的标题（可空）
            scope        global | project；project 时给 project 名
            tool         catalog 里的工具 id（claude_code、codex_cli…），决定默认分桶
            created_at   来源时间；没有则用文件修改时间
Rejected:   reason       missing_id | unsummarized | index_file | empty | …
read() 结束时报告 complete：来源是否完整读完（有文件读失败、库被锁则为 False）
```

- 「是否已提炼」由适配器根据来源自身的标志判断（Memmy 的 layer+summary、Codex 的目录分工、Claude Code 的文件位置），流水线不靠猜文本。
- 适配器只读：SQLite 用 `mode=ro`，文件只 `open('r')`；不启动源工具，不写源目录。

### 3.2 共享流水线（从 `memmy_import.py` 抽出）

1. **校验**：正文非空、时间可解析；不合格计入 `invalid` 并记原因。
2. **去敏**：`redact_secrets` + `desensitize`，与 `commit_memory` 用同一套。**前置条件**：护照/证件号规则（交接清单第 1 项）先合入，否则导入会把证件号原样存进来。
3. **标题**：`title_hint` → frontmatter 的 `name: description` → 第一个标题 → 第一行非噪音文本；200 字截断。Memmy 专属的占位符和 `User:/Agent:` 标签处理留在 Memmy 适配器里。
4. **跨来源去重**：按现有 `content_hash`（类型+作用域+项目+规范化标题）查已有生效记忆，命中则跳过并计 `duplicate`。
5. **对账**（§4）。
6. **写入**：一个事务；`source` = 适配器 id，`source_tool` = 工具 id，`evidence_ids` = `[]`。
7. **审计**：`audit.record(tool="import_<id>")`，只记计数，不记内容。

输出计数：`eligible / new / unchanged / updated / retired / reactivated / duplicate / invalid(+原因) / redacted`。`--dry-run` 只算不写。

### 3.3 同工具回流

从 Claude Code 导入的记忆，Claude Code 自己本来就会读。所以搜索时，若调用方的工具（取自授权里的 client）与记忆的 `source_tool` 相同，就跳过这条导入记忆，避免同一条记忆在宿主上下文里出现两次。宿主自己写入的记忆（`source='host'`）不受影响。

## 4. 版本与退役（与源头对齐）

已有约束：`memories` 表的 `UNIQUE(source, external_id)`；搜索只返回 `status='active' AND superseded_by IS NULL`。

| 本次读到 | Penpal 已有（同来源同 external_id） | 动作 |
|---|---|---|
| 有 | 无 | 新增 |
| 有，去敏后标题和正文都相同 | 生效中 | 不变 |
| 有，内容不同 | 生效中 | 插入新版本；旧行 `status='superseded'`、`superseded_by=新行`，旧行 external_id 改为 `<原值>@<旧行id>` 腾出唯一键 |
| 有 | 已退役 | 恢复为生效（内容不同则按上一行出新版本） |
| 没有 | 生效中 | **仅当 complete=True 时**标 `status='retired'` |

保护措施：

- 读取不完整（`complete=False`）、来源根目录不存在，或本次 0 条而上次大于 0 条时，**一律不退役**，只提示。防止磁盘没挂上、目录被移走时整批误退役。
- 退役不是删除。用户确实要删时用 `penpal memory delete --source <来源>`，会连同旧版本一起删掉。
- 内容比较用去敏后的文本，不另存源哈希，所以不需要迁移。

external_id 的取法（定位符，不含内容）：

- Memmy：原生 `id`。
- Claude Code：`<项目目录名>/<文件名>`。
- Codex：`MEMORY.md#<一级标题>/<二级标题>`；`skills/<目录名>`。标题改名会被视为「旧条退役 + 新条新增」，可以接受。
- MCP JSONL：`<实体名>#<observation 哈希>`（MCP 服务里改 observation 本来就是删一条加一条）。
- Basic Memory：`<permalink>#<observation 哈希>`。

## 5. 首批适配器要点

**Memmy（迁移）**：查询和过滤规则原样搬进适配器；`L1→fact`、`Skill→procedure`、`agent_id` 别名表保留。验收底线：迁移前后对同一个库 dry-run，计数完全一致。

**Claude Code 自动记忆**：
- 根目录：`~/.claude/projects/*/memory/`。设置项改过路径时，按设置里的路径读。
- 单元：除 `MEMORY.md` 外的每个 `*.md`。
- frontmatter：`name` + `description` 作标题提示；类型字段可能在顶层，也可能在 `metadata` 下，两处都要支持。真机核对过的格式里，`type` 嵌在 `metadata:` 下面。
- 类型：`user / feedback → preference`，`project / reference → fact`。
- 作用域：project。项目名从目录名（编码后的路径）还原：按文件系统逐段尝试还原；还原不了就取最后一段。这一步需要在真机验证。

**Codex 合并记忆**：
- 根目录：`$CODEX_HOME/memories`，默认 `~/.codex/memories`。
- 单元：`MEMORY.md` 按标题切块，`skills/*/SKILL.md` 每个一条（procedure）。
- 作用域：global。
- **fixture 必须照 Codex 源码的合并模板（`codex-rs/memories/write/templates/memories/consolidation.md`）造**，不能凭想象。
- 负责人本机这个目录是空的（功能没开），正好用来验证「未发现」路径。

## 6. 命令行与撤销

- `penpal import --list`：只读，逐个适配器显示：
  - 状态：未发现 / 发现 N 条候选 / 已导入 M 条；
  - 上次导入时间；
  - 可以直接复制运行的命令。
- `penpal import <id> [--path P] [--dry-run]`：`penpal import memmy --db` 作为 `--path` 的别名保留，旧用法不受影响。
- `penpal memory list/delete --source`：选项不再写死成 `host|memmy`，改为 `host` 加上所有适配器 id。
- 清除时的来源判断同样泛化：`repository.py` 里的 `m.source = 'memmy'` 改成按参数匹配。
- 导入只能由用户在命令行触发：MCP 不提供导入工具，宿主最多提示用户运行哪条命令。`connect --auto` 之后是否提示可导入，留到交接清单第 3 项再定。

## 7. 分期与验收

| PR | 内容 | 验收 |
|---|---|---|
| 前置 | 护照/证件号去敏规则（交接清单第 1 项，独立 PR） | 夹具含各国护照号格式；存量回填另问负责人 |
| PR-1 | 共享流水线 + Memmy 适配器迁移 + §4 对账 + `--source` 泛化 | 现有 `test_memmy_import.py` 不改即通过；新增对账测试（新增/不变/改动/删除/重现/不完整不退役/dry-run 不写）；负责人真机 `penpal import memmy --dry-run` 计数与迁移前一致 |
| PR-2 | Claude Code、Codex 适配器 + `import --list` + §3.3 同工具回流 | 每个适配器的合成夹具覆盖：排除项、frontmatter 两种写法、标题、作用域；真机 `--list` 下，Claude Code 候选数与本机主题文件数一致，Codex 显示「未发现」 |
| PR-3 | MCP JSONL、Basic Memory 适配器 | 合成夹具；relation 和关系行不导入 |
| 不排期 | 宿主通过 P4 闸门提议记忆配方 | 等出现首批之外的真实用户需求再做。注意 P4 闸门本身会拒绝 `.claude/.codex/.cursor/.memmy` 路径，这些来源只能内置 |

实现交给 Codex（`gpt-6.1-terra`，属于 Agent/记忆类工作），每个 PR 一个工作区，主控按 `docs/DELEGATION.md` §5 验收。

## 8. 风险与未知

- **记忆投毒**：导入内容来自任何进程都能写的本地文件，导入后会提供给所有 AI。缓解：搜索结果带来源和工具；只读用户明确运行导入的来源。完整审查仍在待办「宿主写入提示注入审查」里，公开发布前必须完成。
- **条款**：未逐一审查各产品条款。导入是用户在本机读取自己的数据，不上传，风险判断为低；正式发布前在 Memmy 借鉴台账一并复核。
- **格式漂移**：Codex 记忆仍在快速迭代；Claude Code 的记忆路径可以被设置修改。适配器读不懂时报 `invalid` 并给出原因，不猜。
- **未验证**：
  - Claude Code 项目目录名能否还原成项目名；
  - Codex 合并模板产出的 `MEMORY.md` 的实际标题层级；
  - Memmy 官方表结构（目前依据的是 Penpal 现有查询）。

出处：

- Claude Code 记忆：https://code.claude.com/docs/en/memory
- Codex 记忆管线：https://github.com/openai/codex/blob/main/codex-rs/memories/README.md （主控已读源码核实）
- Gemini CLI 记忆：https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/memory.md （主控已核实：直接改写 Markdown，没有独立的记忆分节）
- MCP 记忆服务：https://github.com/modelcontextprotocol/servers/blob/main/src/memory/README.md
- Basic Memory：https://docs.basicmemory.com/concepts/knowledge-format
- Cursor Memories 撤除：https://forum.cursor.com/t/are-my-memories-gone/144057/3
- Windsurf/Devin 规则：https://docs.devin.ai/desktop/devin-local/customizations
- Cline Memory Bank：https://docs.cline.bot/best-practices/memory-bank
- ChatGPT 记忆与导出：https://help.openai.com/en/articles/8590148-memory-in-chatgpt
- Claude.ai 记忆导出：https://support.claude.com/en/articles/12123587-import-and-export-your-memory-from-claude
