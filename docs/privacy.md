# Penpal 隐私边界

[English](privacy.en.md)

这份文档写给持怀疑态度的 Mac 用户：每一条都标了代码位置，有测试的标了测试，你可以自己去核对，不必相信这里的措辞。核对基准是 `main` 分支当前代码。

先说最重要的三点：

1. **过滤是尽力而为，不是保证。** 下面所有"不记""打码"都靠规则匹配，规则会漏。别指望 Penpal 保证你的密码、验证码、私密对话一定不进库。
2. **数据只在你本机，但 AI 查到的结果会进入那个 AI 的对话。** 如果你授权的是云端 AI（Claude、ChatGPT 等），被查到的片段可能离开你的电脑。
3. **键盘片段默认开启。** 它存的是过滤后的文字片段，不是逐键记录；你可以一条命令关掉。

路径都相对仓库根目录。"测试"一栏是能证明该行为的现有测试。

## 1. 写入之前先拦什么

所有实时采集的事件在写库前都过同一条链路（`penpal/capture/manager.py` 的 `_process_event`），顺序是：全局暂停/来源开关 → 排除名单 → 域名排除 → 清洗关卡 → 策略规则 → 脱敏 → 写库。结果分三种：

- **丢弃**：整条不写，库里没有任何痕迹。
- **只留元数据**：保留"哪个 App、什么时间"，文字和窗口标题清空，并在元数据里标 `text_dropped` 原因。
- **打码**：保留文字，把敏感片段替换成 `[REDACTED]` 之类的占位。

| 情况 | 结果 | 代码 | 测试 |
|---|---|---|---|
| 你按了 `penpal pause`，或某个来源被 `penpal source disable` 关掉 | 丢弃，且暂停期间排队的事件不会在恢复后补写 | `capture/capture_control.py` `source_writes_allowed`；`manager.py` `_flush_loop` 注释 | `tests/test_capture_switches.py` |
| 默认排除名单里的 App（见第 2 节） | 丢弃，连"用过这个 App"都不记 | `manager.py` `_is_blacklisted`；`capture/hygiene.py` `is_blacklisted_app` | `tests/test_blacklist.py` |
| 聊天 App 且你没放行（见第 3 节） | 丢弃 | `manager.py` `_is_blacklisted`；`capture/chat_apps.py` | `tests/test_chat_app_scope.py`、`tests/test_issue26_capture_chain.py` |
| 浏览器内部页（`chrome://`、`about:` 等） | 丢弃 | `hygiene.py` `is_internal_browser_page` | `tests/test_capture_hygiene.py` |
| 登录界面、锁屏、系统遮罩（loginwindow 等） | 窗口事件丢弃 | `hygiene.py` `is_system_overlay` | `tests/test_capture_hygiene.py` |
| 文字里含 password / passwd / verification code / otp / 2fa / 密码 / 验证码 | 该条文字丢弃 | `hygiene.py` `_PRIVACY_RE` | `tests/test_capture_hygiene.py` |
| 窗口标题含 sign in / 登录 / 验证码 / 1password / keychain 等登录类关键词 | 只留 App 和时间 | `hygiene.py` `_LOGIN_KEYWORDS`、`is_login_context` | `tests/test_capture_hygiene.py` |
| 无痕/隐私浏览窗口 | 只留 App 和时间 | `hygiene.py` `is_private_window` | `tests/test_capture_hygiene.py`（`test_private_windows_are_metadata_only`） |
| 终端里的键盘输入、终端里读到的屏幕文字 | 键盘：只留 App 和时间；屏幕文字：文字清空 | `hygiene.py` `is_terminal_app`；`manager.py`（`drop_terminal_text` 默认开） | `tests/test_capture_hygiene.py`、`tests/test_blacklist.py` |
| 你排除的域名（见第 4 节） | 网址事件丢弃；该标签页里的键入/读取/复制文字只留 App 和时间 | `manager.py`、`hygiene.py` `page_in_denied_domain` | `tests/test_issue26_capture_chain.py` |
| macOS"安全输入"开启（密码框等） | 键盘不记录 | `capture/watchers/keyboard_native.py`（`_is_secure_input_enabled`） | 无专门测试 |
| 带 Cmd 的快捷键 | 键盘不记录 | `keyboard_native.py` | `tests/test_capture_hygiene.py`（`test_tap_keeps_space_and_enter_and_ignores_command_chords`） |

"登录类关键词"和"含密码字样的文字"是子串/词边界匹配，窗口标题里没有这些词、文字里写的是别的叫法（如"口令"），就拦不住。

## 2. 默认排除名单

定义在 `penpal/config.py` 的 `PrivacyConfig`：

- **密码管理器与钥匙串**：1Password、Bitwarden、Dashlane、LastPass、钥匙串访问、Microsoft Authenticator。
- **聊天 App**：微信、企业微信、QQ、Telegram、Signal、WhatsApp、Messages、Slack、Discord、钉钉、飞书/Lark、LINE、KakaoTalk（另有 `com.tencent.*` 通配）。
- **银行、券商、加密钱包、健康类**：约 15 个 Bundle ID（工商银行、中国银行、Chase、Wells Fargo、Robinhood、MetaMask、Ledger、MyChart、Headspace、Calm 等）。
- **按 App 名字包含匹配**：微信、WeChat、企业微信、钉钉、DingTalk、飞书、Lark、Feishu、1Password、Keychain Access、钥匙串、密码（名字里带"密码"二字的 App 都会被排除）。

要说清的边界：

- 银行类等 Bundle ID 里有一部分是手机端 App 的 ID，Mac 上是否有同 ID 的 App，没有逐个验证；它们在 Mac 上可能根本不触发。
- 这是内置的默认值。你在 `~/.penpal/config.toml` 的 `[privacy]` 里自己写 `blacklist_bundle_ids`、`blacklist_app_names` 等字段，会**整体替换**对应的默认列表，不是追加；要加就把默认项一起抄上。（pydantic 默认值语义，见 `config.py`。）
- 默认还排除了一批域名（`url_deny_hosts`）：apple.com、appleid.apple.com，以及小红书、微博、B 站、知乎、抖音。规则见第 4 节。

## 3. 聊天 App：默认关，放行后进 Private

- 默认：聊天 App 的窗口、键盘、屏幕文字、剪贴板全部丢弃（`manager.py` `_is_blacklisted`，`chat_apps.py`）。
- 放行：`penpal privacy chat-apps allow Slack`（`penpal privacy chat-apps list` 看清单，`deny` 取消）。写入 `config.toml` 的 `[privacy] chat_apps_opt_in`（`config.py`）。
- 放行后该 App 的数据归入 **Private** 分类（`store/buckets.py`），只有授权了 Private 的 AI 查得到；你自己写的分类规则优先于这个默认。放行前已经丢弃的内容不会补回。
- 测试：`tests/test_chat_app_scope.py`（`test_opted_in_chat_app_is_private_and_hidden_from_work_personal_hosts`）、`tests/test_issue26_capture_chain.py`（AT20）。

另：代码里有一个微信消息来源（`penpal/sources/plugins/wechat.py`），目前只在本机探测，消息读取尚未实现，不读取或导入微信消息、不生成 Daily 报告，也不调用云端 LLM。`penpal source wechat authorize` 只会写入本机授权标记。测试：`tests/test_sources_wechat.py::test_wechat_source_discover_and_read_probe_only`、`tests/test_wechat_consent.py`。

## 4. 你自己排除 App 和域名

- **App**：在 `config.toml` 的 `[privacy]` 里设置 `blacklist_app_names` / `blacklist_bundle_ids`（匹配后整条丢弃）；或用 `penpal rules add --scope-type app --scope-value <名字> --mode deny`。
- **域名**：用 `penpal privacy block-site <host>` 屏蔽网站，`penpal privacy unblock-site <host>` 取消屏蔽，`penpal privacy blocked-sites` 查看清单。命令会去掉 URL 的协议、路径、端口和开头的 `www.`，并统一转成小写；匹配覆盖该域名及其所有子域名（`hygiene.py` `host_matches_denylist`）。
- 新的实时采集事件约 1 分钟内按新清单处理；无需手动重启。已有数据不会因此删除。
- 域名排除现在覆盖三处：
  1. 实时采集的网址事件：丢弃。
  2. **浏览器历史导入**：命中的访问记录不入库（`sources/importer.py`、`sources/registry.py`）。测试：`tests/test_issue26_capture_chain.py::test_at18_browser_history_from_excluded_domain_is_not_stored`。
  3. **该标签页里键入、读取、复制的文字，以及前台窗口标题**：只留 App 和时间（`denied_domain`，窗口标题和元数据里的 `primary_title` 一并清空）。测试：同文件 `test_typing_in_excluded_domain_tab_is_not_stored_as_content`、`test_window_title_on_excluded_domain_tab_is_not_stored`。

第 3 条有前提和缝隙，必须知道：

- 它靠"浏览器网址监视器"知道你当前在哪个页面。**如果没有这个监视器在运行，Penpal 不知道你在哪个网站，这一条就不生效**，文字照常记（`test_unknown_page_is_not_blocked`）。代码里 `WatchersConfig.browser_url` 的默认值是关（`config.py`），但 `penpal` 安装时写出的 `~/.penpal/config.toml` 模板里把它设成了开（`cli.py` 的 `_DEFAULT_USER_CONFIG`）。所以正常安装的用户是开着的；你可以在 `penpal source list` 里确认。
- 网址是每 3 秒轮询一次、网址变化时才上报，切换标签页后最多有约一个轮询周期的延迟，这几秒里的文字按上一个页面判断。
- 窗口切换事件也按同一个"最后已知网址"判断，但有两处缝隙：一是切到新标签页时，窗口事件可能先于网址监视器的上报到达，这几秒内会按上一个页面判断，所以从允许的网站切到被排除网站时，标题可能先被记一次；二是历史迁移（`store/migration_v24.py`）只处理文字，不处理已入库的历史窗口记录。

## 5. 键盘

决策依据：D35（2026-10-24；默认开启为 2026-10-14）。这条**修订**了更早的"默认关闭""打标记、默认不进 AI 搜索"的说法，记录在 `docs/Penpal_V2.md` 的 D35。

**记什么**

- 不是逐键记录。监听按键后在内存里拼成文字，停顿 2 秒或连续输入满 20 秒就切成一段，退格会被应用，控制字符会被去掉（`capture/watchers/keyboard_chunk.py`；`config.py` `KeyboardChunkConfig`）。
- 库里只有过滤后的片段、所在 App 和时间（`tests/test_purge_cascade.py::test_keyboard_chunk_rows_are_deleted_with_their_derived_data` 可看到入库的行结构）。

**过滤规则**（`hygiene.py` `capture_gate`，键盘专用部分按顺序）：

| 规则 | 动作 |
|---|---|
| 含 password / otp / 2fa / 密码 / 验证码 等词 | 丢弃 |
| 窗口标题带登录类关键词 | 只留 App 和时间 |
| 在终端 App 里输入 | 只留 App 和时间 |
| 少于 5 个字符（`y`、`n`、`ok`、`yes`、`no`、`嗯`、`好`、`对`、`是`、`不` 除外，两个及以上汉字也保留） | 丢弃 |
| 8 个字符及以上且信息熵 ≤ 2.0（如 `aaaaaaaa`、`abababab`） | 丢弃 |
| 不含中日韩文字、最长无分隔片段 ≥ 12 个字符的小写"一坨"（疑似输入法拼音中间态、哈希、令牌） | 丢弃 |
| 全 ASCII、长于 12 个字符、无空格、字母占比 ≥ 70%、没有句末标点、不像代码的单段文字 | 丢弃 |
| 通过以上后 | 再做第 6 节的脱敏，截断到 2000 字符 |

测试：`tests/test_capture_hygiene.py`（`test_keyboard_gate`、`test_keyboard_in_terminal_is_metadata_only`、`test_english_sentence_through_buffer_passes_gate_and_pinyin_does_not`）。

**开关与提示**

- 默认开启；安装/初始化时展示一次风险提示（`capture/keyboard_notice.py`，测试 `tests/test_keyboard_notice.py`）。提示明说：过滤是尽力而为、可能记录到敏感内容、AI 只能读已授权分类。
- 一条命令关闭：`penpal source disable keyboard_chunk`；`penpal source enable keyboard_chunk` 重开。关闭后键盘监听静默，且不再写入（`manager.py` `_sync_source_switches`）。
- 已经存下的片段不会因关闭而消失，要删用 `penpal purge --source keyboard_chunk`（第 10 节）。

**能不能被 AI 搜到（D35）**

- 能，和其他来源一样：只在这个 AI 被授权的分类内。片段按所在 App 归类，编辑器、终端归 Work，没有预置归类的 App 归 Personal（`store/buckets.py`；测试 `tests/test_buckets_grants.py`、`tests/test_capture_mcp.py::test_capture_streams_are_searchable_with_kind_and_snippet`）。
- 新授权默认只含 Work（`mcp/auth.py` `DEFAULT_BUCKETS`）。
- 搜索里隐藏的只有两类：schema v24 修复前存下的乱码键盘行（`pre_fix_keyboard`），以及 AI 复述 Penpal 结果的回答（`via_penpal`）；后者针对"回声"，不是键盘（`search/engine.py`）。

**中文输入法**：目前只可靠覆盖英文，拼音中间态会被丢弃，中文上屏文字的采集还在试验，进展见 issue #64。别假定你的中文输入会被记下来，也别假定不会。

## 6. 字段级打码

写库时对文字、窗口标题和元数据逐字段处理（`penpal/privacy/desensitizer.py`、`detectors.py`；调用在 `manager.py` `_process_event`）。

| 内容 | 处理 | 备注 |
|---|---|---|
| 邮箱 | `alice@example.com` → `***@example.com`（域名保留） | `git@host` 这类服务账号不打码 |
| 手机号（中国大陆 11 位） | `[REDACTED]` | |
| 身份证号（18 位） | `[ID_CARD]` | 校验位对得上才打码 |
| 银行卡号（16–19 位） | `[BANK_CARD]` | Luhn 校验通过才打码，避免误伤订单号、时间戳 |
| `password=…`、`token: …`、`api_key=…`、`secret=…` | `[REDACTED]` | 整个"键值"到空白为止 |
| Bearer 令牌、JWT、AWS 密钥（`AKIA…`）、`sk-…` 密钥、GitHub 令牌、Slack 令牌、网址里的 `?token=` 等参数、私钥头、UUID v4 | `[REDACTED]` | |
| 以 `sudo`、`export X=…`、`brew/npm/pip install …` 开头的行等 | `[REDACTED_SHELL]` | 防止命令里夹带凭据 |
| Penpal 自己的 MCP 令牌（`kp_…`） | `[REDACTED]` | 防止它出现在导入的会话记录里再被读回 |

测试：`tests/test_privacy_detectors.py`、`tests/test_capture_hygiene.py`（卡号、身份证号校验）、`tests/test_auth_audit.py::test_keypulse_token_in_ingested_text_is_redacted`。

三个边界：

- **实时采集**（窗口、剪贴板、键盘、屏幕文字）走上表全部规则。**导入来源**（Claude Code/Codex 会话、终端历史、Git、Markdown、Spotlight）在入库时只做"凭据类"打码（`sources/sink.py`、`store/repository.py`）：邮箱、手机号、身份证、银行卡在导入的原文里不会被替换（浏览器历史的网址例外，整串走了完整打码）。邮箱会在 AI 查询返回时再打码（`mcp/tools.py`，由 `privacy.redact_emails` 控制，默认开）；手机号、证件号、银行卡对导入内容则没有这一层。
- 另有一道兜底：任何写入路径落库前都会再过一遍凭据类打码（`store/repository.py` `insert_raw_event`），查询返回时也再过一遍。
- 这些全是正则，不理解语义。写成"我的口令是 hunter2"这种没有关键词的句子，就拦不住。

## 7. 摄像头开启时

- 摄像头被任何程序使用时，Penpal 暂停**屏幕文字（辅助功能文字，`ax_text`）**采集，摄像头释放后恢复（`capture/camera_monitor.py` 读系统日志里的摄像头状态，`manager.py` `_start_camera_monitor`；设置项 `privacy.camera_scene_pause`，默认开）。测试：`tests/test_camera_monitor.py`。
- 注意两点：被暂停的只有屏幕文字；**键盘、剪贴板、窗口切换不受影响**。而屏幕文字来源本身默认关闭（`config.py` `ax_text: False`），所以对多数用户来说这条规则是"以防万一"。

## 8. 无痕浏览

标题或网址里带 incognito、无痕、InPrivate、Private Browsing、隐私浏览的窗口，只记 App 和时间，文字、标题清空（`hygiene.py`）。测试覆盖了 Chrome 无痕、中文"无痕"、Edge InPrivate、"Private Browsing"、Safari 隐私浏览这几种标题（`tests/test_capture_hygiene.py::test_private_windows_are_metadata_only`）。这是按窗口标题/网址里的字样判断的，浏览器改了标题措辞就可能识别不到。

## 9. AI 能看到什么

| 声明 | 依据 |
|---|---|
| 每个 AI（Host）一个独立令牌，库里只存哈希，明文只在创建时显示一次 | `mcp/auth.py` `create_grant`、`hash_token`；`tests/test_auth_audit.py::test_g5_no_plaintext_token_or_hash_in_db_wal_logs_audit_or_cli_output` |
| 授权按分类（Work / Personal / Private / Financial）限定，默认只给 Work；还可以限定来源和到期时间 | `mcp/auth.py` `DEFAULT_BUCKETS`、`Grant`；`tests/test_buckets_grants.py` |
| Work 会包含已启用的 Claude Code / Codex 来源中的全部对话；按项目目录名添加 project 桶规则并执行 `penpal rules rebucket` 可把已有对话改归 Personal | `store/buckets.py`、`sources/plugins/claude_code.py`、`sources/plugins/codex_cli.py`、`penpal rules projects`；`tests/test_buckets_grants.py::test_project_rule_precedes_app_source_and_default_and_matches_exactly`、`test_project_rule_rebucket_applies_to_legacy_project_dir_metadata` |
| 查询时必须同时满足：令牌有效、声明的客户端名与授权一致、该客户端在你批准的名单内 | `mcp/auth.py` `check_access`；`tests/test_auth_audit.py` |
| 随时撤销，下一次调用立刻失效（不缓存）；`rotate` 换新令牌，旧令牌立刻失效 | `revoke_grants`、`rotate_grant`；`tests/test_auth_audit.py::test_revoke_is_immediate_and_removes_whitelist`、`test_rotate_keeps_grant_and_scope_and_kills_old_token` |
| 审计日志记：谁（客户端名）、什么时候、哪个工具、放行还是拒绝、授权的分类名、放行时返回几条；**不记查询内容、片段、令牌** | `mcp/audit.py`（表里没有能放查询文字的列）；`penpal mcp audit`；`tests/test_auth_audit.py` |
| 被拒绝的调用也进审计，且对调用方返回统一的拒绝内容，不透露原因 | `tests/test_auth_audit.py::test_at15_every_denial_has_identical_body_and_an_audit_row` |
| 返回给 AI 的文字再过一遍凭据打码和邮箱打码 | `mcp/tools.py` `_redact_text` |
| 现有工具只有四个：`search_context`、`get_timeline`、`get_evidence`、`rate_result` | `mcp/server.py` |

要说清的边界：

- **只读，有一个例外**：`rate_result` 会在本机写一条评分记录（你的 AI 传来的评分、可选的备注和查询文字，限长）。它只需要读权限，只能评它自己拿到过的结果，`penpal purge` 会连带清掉。授权里虽然有 `write` 这个操作名，但目前没有任何工具要求它。
- **客户端身份是自报的**。通过 stdio 连接时，Penpal 无法证明对端进程是谁；`PENPAL_CLIENT` 只是 Host 配置里的一个名字，真正的凭证是令牌。能读到你 Host 配置文件的程序，就能同时拿到令牌和名字（`mcp/auth.py` 文件头注释）。保护好那份配置文件。
- **被查到的内容会进入那个 AI 的对话**。如果它是云端服务（Claude、ChatGPT、Cursor 的后端等），这些内容可能离开你的电脑。Penpal 无法控制 Host 之后怎么处理。授权前想清楚给哪个分类。
- 这里的"AI"指你自己授权的 Host。Penpal 核心不内置大模型，自己不调用任何模型。

## 10. 数据在哪、怎么删

- **位置**：`~/.penpal/`，数据库 `penpal.db`、日志 `penpal.log`、配置 `config.toml`（`config.py` `AppConfig`、`utils/paths.py`）。
- **无埋点**：`penpal/` 源码里没有使用 `requests`、`httpx`、`urllib.request`、`http.client`、`aiohttp` 的网络代码，也没有任何统计 SDK。这是检索代码得出的结论，没有抓包验证；安装 Penpal 时下载（如 Homebrew）不在此列。
- **不自动删除**：启动、停止都不会删数据，旧的保留天数配置已被忽略。测试：`tests/test_d32_no_auto_delete.py`（D32）。删除只在你主动执行时发生。
- **清除**：`penpal purge --source <来源>` / `--app <App>` / `--today` / `--last-hours N`，加 `--dry-run` 先看会删多少（`cli.py` `purge`，`services/purge.py`）。一次事务里同时删：原始记录、全文索引、出处（evidence）、返回给 AI 的结果记录、评分记录、剪贴板/手动记录的搜索文档，以及随之清空的会话（只删掉部分事件的会话会被重建，不残留被删文字）。没带任何筛选条件会被拒绝，不会"全删"。出错整体回滚。测试：`tests/test_purge_cascade.py`。
- 日志文件里是否可能出现内容片段，这里没有逐行核查；代码约定是日志只记原因计数、不记内容（`hygiene.py` 文件头注释）。

## 11. 不承诺的事

- 不承诺任何过滤一定拦得住。规则按关键词、正则、校验位判断，没有语义理解；中文、方言写法、改过名的 App、不常见的网站都可能漏。
- 不承诺别的程序读不到 `~/.penpal/`。数据库没有额外加密，保护它靠 macOS 账户权限和磁盘加密（FileVault）。
- 不承诺云端 AI 如何处理它查到的内容。
- 不承诺中文输入法的采集行为（issue #64，试验中）。
- 不承诺 Windows/Linux；只支持 macOS。

发现和这份文档不符的行为，请到仓库提 issue；安全问题见 [SECURITY.md](../SECURITY.md)。
