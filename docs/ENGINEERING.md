# Penpal 开发章程

> 适用于所有改动，无论人还是 AI worker（Claude / Codex / agy）。派活时让 worker 先读本文件（给路径，不贴全文）。
> 本章程只写 Penpal 特有的、会改变代码结果的规则；通用的好习惯不重复。
> 上位文件：产品边界看 `docs/Penpal_V2.md`（D 系列决策），评测与发布判定看 `docs/Penpal_Eval_PRD.md`，运行时约定看 `docs/design/runtime-contract.md`（D36）。

## 0. 五条底线（违反即打回）
1. **公开仓库，零真实数据**：代码、测试、fixture、日志样例、issue、PR 里不出现真实用户内容。
2. **不碰真实环境**：开发、测试、手动探针都在临时 HOME 里跑；不读 `~/.penpal`、`~/.keypulse`、`~/.claude`、`~/.codex`、`~/.cursor`、`~/.zsh_history`、`~/Library`、浏览器数据；不运行 `launchctl`、不启动 GUI、不做系统级安装。需要真机验证的，写成手测步骤交给负责人。AI worker 的手动探针一律在 `PENPAL_ISOLATED=1 HOME=$(mktemp -d)` 下运行，结束时关掉自己启动的进程。
3. **一个结论只有一个出处**：同一个用户可见结论（运行状态、权限、来源可用性、可搜索性）只由一个函数产生，其他地方调用它（§3）。
4. **删旧换新，不留双轨**：替换一个机制时，在同一个 PR 里删掉旧的读写路径和兼容分支；确需过渡的，写明删除版本并登记在 §7。
5. **先有失败用例，再修**：修真实环境报告的问题，先把现场写成一条在旧代码上失败的测试。

## 1. 产品边界
- 核心（capture / sources / store / search / mcp / runtime / services）不调用大模型（D24），不依赖 UI 框架。
- CLI 是完整能力；HUD / App 是可选外壳，只调用 CLI 共用的核心函数，不含业务逻辑（D33）。HUD 能做的事，CLI 必须也能做。
- 已下线的报告链（日报 / 周报 / Obsidian / LLM pipeline，见 `legacy-v0` 标签）不得回到核心。

## 2. 模块地图与依赖方向
```
入口层   cli.py · feedback_cli.py · hud/ · app_bundle.py · app.py · mcp/cli.py · mcp/server（stdio）
服务层   services/（purge、activity…）· health/（doctor 与健康检查的呈现）· capabilities/（能力探测与状态汇总）· runtime/（D36：状态与控制）
领域层   capture/（实时采集）· sources/（历史来源插件）· privacy/ · search/ · permissions.py · observability/ · mcp/tools、auth、audit
基础层   store/（SQLite 唯一入口）· foundation/（跨领域共享规则与低层文件原语）· config.py · utils/（paths 唯一路径来源）· i18n.py
```
- 依赖只能从上往下；`lint-imports` 合约强制执行，新增分层规则要同步写进 `.importlinter`。
- pyobjc（AppKit / Quartz / WebKit…）只允许出现在 capture、hud、启动层。
- 路径只从 `penpal.utils.paths` 取；配置只经 `penpal.config` 的读写函数改（文本级编辑、写后回读校验）；数据库只经 `penpal.store`。
- 单文件超过约 800 行时按职责拆分（例如 `cli.py` 按命令组拆成 `cli/` 包），不要继续往里堆。

## 3. 跨进程约定（D36）
Penpal 有多个常驻进程：后台服务（launchd）、健康检查 job、HUD、每个 AI 宿主拉起的 `mcp serve`。它们之间的约定是最容易出错的地方。
- **状态**：只由后台服务写运行记录；只由 `penpal.runtime.evaluate` 给结论。doctor、status、HUD、MCP、健康检查不得自己读 PID、launchctl 或运行记录。
- **存活 = 关键循环有进展**，不是"有心跳"或"有新事件"；没有用户输入的空闲是健康状态。
- **控制**：启动、停止、重启只走 `penpal.runtime.control`（launchd）。看门狗必须有冷却期和重启上限。
- **版本**：每个常驻进程都要能发现"磁盘上的代码比我新"并自我换新；新增常驻进程必须接入这一机制。
- **约定变更协议**：改动运行记录字段、评估规则、权限判定、数据库结构或 MCP 协议时，PR 描述写明"影响的约定 + 受影响的出口"，并补 Eval PRD §3.7 的矩阵 / 场景用例；数据库结构变更必须带迁移，且旧代码遇到更新的结构要按 `outdated` 处理。
- **线程**：HUD 的取数在后台线程，AppKit / WKWebView 操作只在主线程。

## 4. 隐私与数据
- 任何新的采集或导入路径，必须经过现有的脱敏和排除闸门（`registry.accept_event`、`capture_gate`、`url_deny_hosts`）；不得另写一套过滤。
- 令牌明文不落盘、不进日志、不进测试快照。
- 日志只写事件类型、计数、错误类型，不写用户内容。
- 用户可见的隐私承诺写在 `docs/privacy.md` / `docs/privacy.en.md`，每条都引用代码和测试位置；改了行为就同步改文档。

## 5. 代码写法
- 改动最小、贴合周边风格；不顺手重构无关代码（需要重构就单开 PR）。
- 注释只写"为什么"。
- 注释和 docstring 不写改动经过（修了什么、按谁的意见改、由谁添加），这些写进提交信息。
- 错误要具体：抛出或记录能定位问题的信息。不得吞掉宽泛异常（`except Exception: pass` / `continue`，由 ruff `S110` / `S112` 拦截）；只有在“一个读取失败不能拖垮整个界面”这类边界才允许吞，而且要记日志，日志只写异常类型和固定描述，不写 `str(exc)`。
- 用户可见文案用 `T(中文, 英文)` 双语；命令和输出以中文为主。
- 新代码不出现 `keypulse` 命名；遗留名字只允许出现在迁移模块里（路径迁移在 `utils/paths.py`，配置文本迁移在 `config.py`），并登记删除版本。
- Python ≥3.11：不用 3.12+ 语法；CI 有 3.11 编译导入检查。
- 新依赖要写理由，优先标准库。

## 6. 测试与评测
| 层级 | 管什么 | 在哪跑 |
|---|---|---|
| 单元测试 | 纯函数、解析、规则 | CI |
| 约定矩阵 | 同一输入在所有出口给出同一结论（E8 R1 等） | CI，阻断 |
| 真进程场景 | 在临时 HOME 起真实进程，模拟卡死、崩溃、升级（E8 R2–R5） | CI，阻断 |
| 检索评测 | 召回、误命中基线（E3） | CI，阻断 |
| 真机手测 | launchd、TCC 权限、菜单栏、三个 AI 宿主（E8 R7、R4 宿主部分） | 发版前，负责人执行 |
- 测试必须在临时 HOME 里运行，并由 `tests/conftest.py` 的全局守卫保证；测试里出现真实家目录路径即失败。
- 不写按墙钟时间计数的断言（慢机器会抖）；等"条件成立"，并设上限。
- 外部命令（launchctl、ps、系统 API）通过可注入的探针调用，测试里替换掉。
- 测试不依赖本机语言（CI 是英文环境，本机多为中文）：断言用 `T(中文, 英文)` 求值，或让被测代码显式传入语言。出口的语言是约定的一部分：HUD 固定中文，MCP 给宿主的提示固定英文，CLI 跟随系统语言。
- 提交前：`python -m pytest -q` 全绿，`lint-imports` 和 `ruff check penpal tests scripts evals` 通过。

## 7. 发布整洁度（公开仓库）
仓库公开，合进 main 就等于发布。能自动检查的规则一律做成检查；查不了的写进主控验收清单（`docs/DELEGATION.md` §5）。

| 要求 | 由谁拦 |
|---|---|
| 不出现真实用户的家目录路径（`/Users/<名字>`、`/home/<名字>`、`-Users-<名字>-`） | `tests/test_repo_hygiene.py` |
| 不出现密钥、令牌 | GitHub 密钥扫描与推送拦截（已开启） |
| 不入库数据库、日志、密钥文件、`.env`、`.DS_Store`；单个文件不超过 512 KB | `tests/test_repo_hygiene.py` |
| 根目录只放入口、安装与项目元文件（白名单） | `tests/test_repo_hygiene.py` |
| 没有未使用的 import 和变量、重复定义、无占位符的 f-string | ruff `F`（CI） |
| 不吞宽泛异常 | ruff `S110` / `S112`（CI） |
| 分层依赖方向 | `lint-imports`（CI） |
| Python 3.11 能导入 | `py311-import`（CI） |
| 文案、注释、提交信息、PR 描述里没有真实数据和本机路径 | 主控验收 |
| 没有注释掉的代码、调试打印、没人负责的 TODO | 主控验收 |

- 署名只出现在 LICENSE、README、SECURITY；测试和 fixture 一律用占位名（`alice`、`/Users/x`），不用真名、真实路径或真实对话。
- `# noqa` 必须写原因。新增占位名或根目录文件时改白名单，并在 PR 里说明为什么。
- 设计文档进 `docs/`；一次性备忘、会话交接和过时文档删除或归档（git 历史里能找回）。
- 构建产物、缓存、本地配置、日志不入库（`.gitignore` 覆盖）。
- 死代码当场删：没有调用方的函数、没有读取方的文件、注释掉的代码块。
- 过渡兼容代码登记在这里，到期删除：
  | 内容 | 位置 | 删除版本 |
  |---|---|---|
  | KeyPulse → Penpal 数据目录、路径解析、launchd label 迁移 | `penpal/utils/paths.py`、安装器；配置文本路径改写在 `penpal/config.py` | 0.5.0 |
| `keypulse.log` 的一次性迁移到 `penpal.log`（不再回退读取旧名） | `penpal/utils/paths.py` | 0.5.0 |
| MCP 参数 `include_keypulse_echoes` 的弃用别名（替代为 `include_penpal_echoes`） | `penpal/mcp/server.py` | 0.5.0 |
| PR1 daemon runtime record v1 的读取兼容（缺少 config / schema identity 字段） | `penpal/foundation/runtime_record.py` | 0.5.0 |
| 历史 metadata `via_keypulse` 回声标记读取兼容 | `penpal/search/engine.py` | 0.5.0 |
| `keypulse` console-script 迁移 shim | `pyproject.toml`（`penpal.cli:legacy_main`） | 0.5.0 |

## 8. 提交与合并
- 从最新 `origin/main` 开分支，一个 PR 一件事。
- 不用 `git add -A`；不提交 `.codex-rules.md`、`.venv`、日志、本地配置。
- 提交信息用 conventional 前缀（`fix:` / `feat:` / `docs:` / `chore:` / `refactor:` / `test:`），结尾加 `Co-Authored-By`。
- PR 描述包含：改了什么、为什么、影响的约定（没有就写"无"）、验证方式与结果、遗留问题。
- AI worker 不 push、不合并、不打 tag；由主控审 diff、等 CI 全绿后合并。合并用 `--match-head-commit`。

## 9. 给 AI worker 派活
- 派不派、派给谁、怎么写简报、怎么验收、怎么控制 token 成本：见 `docs/DELEGATION.md`（主控用，不必附给 worker）。
- worker 要做到：遵守本章程和简报里的“只做 / 不做 / 验收标准”；不 commit、不 push、不合并；设计和代码对不上时停下来报告，不自己改方向；按简报格式写短报告，写清没做完和拿不准的地方，不贴大段日志或 diff。
- worker 的结论当作证据，主控对照 diff、测试结果核实后才算数。

## 10. 完成的定义
功能在 CLI 可用（如适用，HUD 也可用）→ 测试覆盖正反两面和边界 → 影响到的约定矩阵已更新 → 文档（README 中英、privacy、PRD 决策）同步 → CI 全绿 → 需要真机验证的写进发版手测清单。
