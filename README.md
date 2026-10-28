# Penpal MCP（笔友）— 给 AI Agent 的本地记忆
｜写下的东西，会留下回响
Penpal MCP 是面向 macOS AI Agent 的本地优先记忆层，也是 stdio MCP server。它连接 Claude Code、Codex、Cursor，按你启用的来源记录应用与窗口切换、剪贴板和过滤后的键盘文本，并导入支持的本地历史、会话、Git 与 Markdown 数据。Penpal 服务与数据库完全在 Mac 本机运行，不内置大模型；返回内容会进入你所用 AI Host 的对话，也可能被发送到该 Host 的云端模型服务。

[English](README.en.md) · [快速开始](#快速开始) · [记什么不记什么](#记什么不记什么) · [现在做到哪](#现在做到哪)

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![macOS](https://img.shields.io/badge/macOS-12+-lightgrey.svg)](https://www.apple.com/macos/)
[![Tests](https://img.shields.io/badge/tests-1443%20collected-blue.svg)](tests/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

---

## 它解决什么

你在 Claude Code 里问："上周我看过一个讲 MCP 鉴权的网页，在哪？" 它答不上来。它没见过你的浏览器；就算让它跑 bash 去翻，浏览器历史 90 天后会过期，剪贴板、切过的窗口、在网页输入框里打过的字，本来就没落盘，事后谁也找不回。

装了 Penpal 之后，同一个问题，AI 调用 `search_context`，返回那条网页记录、你当时在哪个 App 里、前后在做什么，再用 `get_evidence` 打开原始出处，你自己核对是不是它。

Penpal 不做聊天入口，也不内置大模型。你继续在自己常用的 AI 里提问，Penpal 只负责"记得"和"给得准"。

## 为什么不直接让 AI 用 bash

AI 自己能读本机文件，只给已有文件建个索引，没必要单独装一个工具。Penpal 存在的理由只有下面三条，每个功能都要落在其中一条上，并且在对照评测里比"AI + bash"答得好：

| | AI + bash | Penpal |
|---|---|---|
| **本来不落盘的信息**：窗口切换、剪贴板、网页里打过的字 | 找不回 | 采集时就记下 |
| **超过来源保留期的记录**：Claude Code 会话约 30 天、Chrome 历史约 90 天 | 过期就没了 | 入库后一直保留，直到你删 |
| **受控访问** | 给 AI bash 等于开放整盘读取；Claude Desktop、沙箱里的 Codex 根本没有 bash | 按 AI 单独授权、按分类限定范围、入库前脱敏、可撤销、每次查询有记录 |

## 两档安装，按需选

| | CLI（必装的最小集） | 桌面 App（可选） |
|---|---|---|
| 给谁 | 只用终端的开发者 | 想直观看采集状态、做管理的人 |
| 包含 | 后台采集 + MCP 服务 + 全部控制命令 | 菜单栏 HUD：状态、来源开关、AI 授权、隐私与删除 |
| 安装 | 见下方快速开始 | 先装好 CLI，再 `penpal app install`（加 `--login` 开机自启） |
| 核心能力 | 装好就能在 AI 里用全部核心能力 | 不多不少，同一套核心换一种用法 |

App 能做的事，CLI 都能做。Penpal 会在 `~/Applications/Penpal.app` 编译一个原生启动器，在独立应用身份下运行后台服务与 HUD；系统权限与菜单栏均显示为 **Penpal**。如果本机缺少 clang 或 libpython，会退回脚本启动器并提示权限列表与菜单栏可能显示 **Python**。首次安装后从 Finder 打开 `~/Applications/Penpal.app`，并在系统设置的「菜单栏」里确认 Penpal 可见；CLI 仍可通过 doctor/status 检查 HUD 状态。

## 快速开始

```bash
# 1. 安装 CLI，初始化运行环境，注册开机自启的后台服务
curl -fsSL https://raw.githubusercontent.com/Longfellow1/penpal/main/install.sh | bash
# 或手动：brew install longfellow1/penpal/penpal && penpal install init

# 2. 检查系统权限：缺哪个、该给哪个程序授权、去哪个设置面板，doctor 都会列出来
penpal doctor

# 3. 授权一个 AI，并按提示把配置加进去（命令会直接打印 Claude Code / Codex / Cursor 的配置）
penpal mcp grant claude --buckets Work,Personal
```

然后在 Claude Code 里正常提问，比如"我昨天下午在研究什么"。

想要菜单栏界面（可选）：

```bash
penpal app install          # 编译原生启动器并在 ~/Applications 生成 Penpal.app；加 --login 开机自启
penpal app uninstall        # 移除 App 和启动项，不影响后台服务与数据
```

安装后从 Finder 打开 Penpal.app。macOS 26 可在「系统设置 → 菜单栏 → 允许在菜单栏中」开启 Penpal；若图标缺失，先看 `penpal doctor` 和 `penpal hud status`。macOS 对菜单栏项目可见性的公开探测有限；从系统设置关闭项目时，HUD 不一定能自动识别。

### 从 KeyPulse 升级

升级后运行 `penpal doctor`。`遗留文件` 部分会以 WARN 列出旧版 KeyPulse.app、旧 PID/日志、旧 launchd plist 和迁移后留下的 `~/.keypulse` 目录，并给出建议命令。doctor 只报告，不会移动或删除 App；确认旧 App 后，可手动运行它列出的 `mv … ~/.Trash/` 命令，或在 Finder 中移到废纸篓。

运行 `penpal maintenance clean-legacy` 只会删除 `~/.penpal` 内的旧 `keypulse*.pid` 和 `keypulse*.log` 普通文件，不会处理 App、launchd plist 或 `~/.keypulse`。清理后再次运行 `penpal doctor`；仍需处理的项目会继续列出。旧 `keypulse.log` 仅在 `penpal.log` 不存在时迁移一次；两者都存在时保留旧文件并由 doctor 报告。

daemon 单独写入轮转的 `penpal.log`（5 MiB，最多 3 个备份）。launchd 的 daemon/healthcheck stdout 与 stderr 文件限制为 1 MiB，并在运行期间按原文件截断超限内容。

### 已验证的 Host

| Host | 版本 | 说明 |
|---|---|---|
| Claude Code | 2.1.303（`claude --version`） | 打印出的配置已用 MCP 协议层测试（模拟客户端）启动验证 |
| Codex CLI | codex-cli 0.162.1（`codex --version`） | 同上 |
| Cursor | 未检测到 `cursor` 命令 | 配置格式已按官方文档校验，未在真机验证 |

## AI 能用的工具

| 工具 | 做什么 |
|---|---|
| `search_context` | 按关键词、时间、来源搜索，返回本次搜了哪些范围、每个关键词有没有命中 |
| `get_timeline` | 按时间浏览某段时间做了什么 |
| `get_evidence` | 打开某条结果的原始出处 |
| `rate_result` | 对结果打分，用来改进排序 |

只读：AI 不能写入或修改记忆（`rate_result` 只在本机记一条评分）。查到的内容会进入该 AI 的对话，可能发送到它的云端服务。授权时按分类（Work / Personal / Private / Financial）限定，`penpal mcp audit` 能看到谁在什么时候查过、结果有几条，不记录查询内容。

注意：Work 包含 Claude Code / Codex 里的全部对话，也可能包含私人项目。查看已导入的项目和当前分桶用 `penpal rules projects`；要把项目归到 Personal，按项目目录名执行 `penpal rules add --scope project --value <项目目录名> --bucket Personal`，再运行 `penpal rules rebucket` 更新已有记录。

## 记什么不记什么

**会记录**（每个来源都能单独关）：

- 实时采集：前台窗口切换、剪贴板、键盘文字片段
- 当前网页地址：标准安装默认开启，需要给后台服务浏览器自动化权限；不想采的话，在 `~/.penpal/config.toml` 的 `[watchers]` 里设 `browser_url = false`
- 导入：Chrome / Safari 历史、Claude Code 与 Codex 会话、终端命令历史、Git 提交、Spotlight

**不会记录或默认排除**：

- 密码管理器等默认排除名单里的 App 不记录；无痕浏览窗口只记应用和时间，不记内容；摄像头开启期间暂停屏幕文字采集
- 银行、支付类 App 和网站（按内置名单）的内容自动归入 Financial 分类，没有授权 Financial 的 AI 查不到
- 聊天 App（微信、Slack、飞书等）默认不采集；单独开启后进入 Private 分类，只有授权了 Private 的 AI 能查到
- 键盘：不存逐个按键，只存过滤后的文字片段；默认开启，和其他来源一样，在已授权的分类内可被 AI 搜索；一条命令可关（`penpal source disable keyboard_chunk`）
- 令牌、密钥等凭据入库前遮盖；实时采集的内容还会遮盖邮箱 @ 前部分、手机号、证件号、卡号；返回给 AI 时邮箱 @ 前统一打码

数据只在你本机 `~/.penpal/`，没有云端、没有埋点。不会自动删除，删除由你决定，删的时候原文、索引、出处、相关查询记录一起清掉。

逐条可核对的隐私边界（附代码与测试出处）：[docs/privacy.md](docs/privacy.md)

## 日常控制

| 要做的事 | 命令 |
|---|---|
| 看状态、查健康 | `penpal status`、`penpal health`、`penpal doctor` |
| 暂停、继续（重启后仍保持暂停） | `penpal pause`、`penpal resume` |
| 开关某个来源 | `penpal source list`、`penpal source disable keyboard_chunk` |
| 放行某个聊天 App | `penpal privacy chat-apps allow Slack` |
| 屏蔽、取消屏蔽或查看网站 | `penpal privacy block-site example.com`、`penpal privacy unblock-site example.com`、`penpal privacy blocked-sites` |
| 管 AI 授权 | `penpal mcp list`、`penpal mcp revoke <client>`、`penpal mcp rotate <client>` |
| 删除数据（先预览数量） | `penpal purge --source chrome_history --dry-run` |

完整参考：`penpal --help`。

网站屏蔽会覆盖该域名及其子域名。更改后运行 `penpal restart`，新事件会在后台重新加载配置后按新清单处理。

## 现在做到哪

- **召回准确率**：作者在本机真实数据上建了 50 题评测集，其中约三分之一只给部分线索。由独立模型作答、自动判分，三轮平均答对 46.7 题（93%），越权返回 0 次。评测集含个人数据，不公开。
- **已可用**：采集、导入、首次使用引导、脱敏、分类授权、排除名单、搜索、出处、删除、暂停与来源开关、无界面故障提醒；可选的菜单栏 App（`penpal app install`）。
- **还没做到**：
  - 键盘片段目前只可靠覆盖英文，中文输入法上屏文字的采集还在试验。
  - 只支持 macOS。
  - AI 只能读，不能写回长期记忆。
  - 桌面 App 目前只有菜单栏 HUD 一个界面，采用 ad-hoc 签名（无开发者证书）；旧日报、周报、Obsidian 导出和 LLM 报告链已从当前产品代码移除，不属于当前功能。

## 许可

Apache 2.0，见 [LICENSE](LICENSE)。

## 作者

**Harland**，AI Native 产品经理。邮箱：Harland5588@outlook.com
