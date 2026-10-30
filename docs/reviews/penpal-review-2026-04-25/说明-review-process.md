# Penpal 阅读、测试与评审记录

日期：2026-04-25
模式：代码评审
评审人：Codex

## 范围

本次评审对象是 `~/Go/penpal` 当前工作区，包含已提交代码和本地未提交改动。评审过程中没有修改业务源码。

重点查看了：

- 产品承诺与实现是否一致，尤其是隐私承诺。
- 采集链路：macOS watchers、策略引擎、脱敏、入库。
- Obsidian 导出和 narrative pipeline。
- 模型网关行为，尤其是云端 fallback 风险。
- maintenance 命令和运维工具。
- 测试套件健康度。

## 仓库状态

当前分支：

```text
main
```

评审时工作区状态：

```text
 M penpal/capture/watchers/ax_text.py
 M penpal/capture/watchers/clipboard.py
 M penpal/obsidian/exporter.py
 M tests/test_capture_ax_text.py
?? config.toml.bak
?? config.toml.bak2
?? penpal/capture/active_app.py
?? penpal/pipeline/fragments.py
?? penpal/pipeline/normalize.py
?? tests/test_capture_active_app.py
?? tests/test_pipeline_fragments.py
?? tests/test_pipeline_normalize.py
```

查看过的近期提交：

```text
2a5ff94 test: 同步 narrative_v2 测试期望与新 prompt（/no_think + 移除 persona）
f5eede6 feat(pipeline): narrative_v2 三段式 + 隐私过滤 + 证据 gate/signals
f14327c chore: gitignore internal design docs
e0599de fix(pipeline): triggers use raw_events.ts_start, pass now through
e817a3c feat(app): wire T2 daemon startup + T3 idle triggers into the capture loop
e7cbb71 feat(obsidian): T1 scheduled triggers at 12/13/18/21 gated by activity
5782f6a feat(store): raw_events gains semantic_weight (v12) and user_present (v13)
48cc0d5 feat(pipeline): LLM trigger decision module with fail-closed gating
```

## 阅读文件

项目与产品文档：

- `README.md`
- `README.en.md`
- `SECURITY.md`
- `TESTING.md`
- `pyproject.toml`
- `config.toml`

核心代码路径：

- `penpal/config.py`
- `penpal/cli.py`
- `penpal/app.py`
- `penpal/store/db.py`
- `penpal/store/migrations.py`
- `penpal/store/models.py`
- `penpal/store/repository.py`
- `penpal/capture/manager.py`
- `penpal/capture/policy.py`
- `penpal/capture/normalizer.py`
- `penpal/capture/active_app.py`
- `penpal/capture/watchers/window.py`
- `penpal/capture/watchers/browser.py`
- `penpal/capture/watchers/clipboard.py`
- `penpal/capture/watchers/keyboard_chunk.py`
- `penpal/capture/watchers/ax_text.py`
- `penpal/capture/watchers/ocr.py`
- `penpal/privacy/detectors.py`
- `penpal/privacy/desensitizer.py`
- `penpal/obsidian/exporter.py`
- `penpal/pipeline/model.py`
- `penpal/pipeline/fragments.py`
- `penpal/pipeline/normalize.py`
- `penpal/pipeline/evidence.py`
- `penpal/pipeline/gates.py`
- `penpal/pipeline/narrative_v2.py`
- `penpal/pipeline/triggers.py`

相关测试：

- `tests/test_capture_ax_text.py`
- `tests/test_capture_active_app.py`
- `tests/test_pipeline_fragments.py`
- `tests/test_pipeline_normalize.py`
- `tests/test_privacy_detectors.py`
- `tests/test_watcher_browser.py`
- `tests/test_capture_keyboard_chunk.py`
- `tests/test_model_gateway.py`
- `tests/test_obsidian_exporter.py`

## 执行命令

文件与仓库检查：

```bash
rg --files
git status --short
git branch --show-current
git log --oneline -n 8
git diff --stat
git diff -- penpal/capture/watchers/ax_text.py penpal/capture/watchers/clipboard.py penpal/obsidian/exporter.py tests/test_capture_ax_text.py
```

针对性搜索：

```bash
rg -n "private|incognito|无痕|隐私" penpal tests README.md README.en.md
rg -n "store_text|keyboard_chunk|content_text=text|normalize_keyboard_chunk_event|not record|raw key|按键" penpal tests README.md README.en.md config.toml
rg -n "TODO|FIXME|XXX|pass|except Exception|shell=True|subprocess|eval\\(|exec\\(|open\\(|write_text|unlink|rmtree|os\\.remove|input\\(|password|token|secret|api_key" penpal tests -g '*.py'
```

测试：

```bash
pytest -q
```

结果：

```text
607 passed, 19 warnings in 3.09s
```

19 个 warning 都是 trigger 测试里的 `datetime.datetime.utcnow()` 弃用警告。

## 评审发现

### 高：Keyboard Chunk 存储行为与隐私承诺冲突

`keyboard_chunk.store_text` 在配置里存在，但没有接到 `CaptureManager` 或 `KeyboardChunkWatcher`。只要 watcher 开启，键入片段会被写入 `raw_events.content_text`。

证据：

- `penpal/config.py` 定义了 `KeyboardChunkConfig.store_text`。
- `config.toml` 里 `store_text = true`。
- `penpal/capture/manager.py` 构造 `KeyboardChunkWatcher` 时没有传入 `store_text`。
- `penpal/capture/watchers/keyboard_chunk.py` 会 flush 标准化文本。
- `penpal/capture/normalizer.py` 会写入 `content_text=text`。
- `README.md` 声称产品不记录键盘输入。

建议：

- 默认把 `store_text` 设为 `false`。
- 当 `store_text=false` 时，只保留边界、时间、来源、semantic weight，必要时保留不可逆签名。
- 如果仍支持保存文本，需要把 README/SECURITY 的表述改成“脱敏后的键入片段”，不能写“不记录键盘输入”。

### 高：`metadata_json` 入库前没有脱敏

`CaptureManager` 只脱敏了 `content_text` 和 `window_title`，没有递归处理 `metadata_json`。browser metadata 可能包含原始 title；window session metadata 可能包含 `primary_title`。

证据：

- `penpal/capture/manager.py` 只脱敏 content 和 window title。
- `penpal/capture/normalizer.py` 会把 browser `title` 放进 metadata。
- `penpal/capture/watchers/window.py` 会把 `primary_title` 放进 metadata。

建议：

- 在 `insert_raw_event` 前增加统一的持久化边界 sanitizer，递归脱敏 metadata 里的字符串。
- 增加测试，覆盖 browser、window session、OCR、AX 事件里的 `metadata_json` secret 脱敏。

### 高：README 声称排除无痕 / 隐私窗口，但代码未实现

README 声称会排除 Safari/Chrome/Firefox 的隐私窗口。实际 browser watcher 通过 AppleScript 读取前台 tab 的 URL/title，然后直接标准化成事件，没有隐私窗口检测。

证据：

- `README.md` 有隐私窗口排除承诺。
- `penpal/capture/watchers/browser.py` 只读取 URL/title 并产生事件。
- 没找到 private/incognito browser detection 的测试。

建议：

- 在实现前，先删除或弱化 README 里的承诺。
- 如果要实现，按浏览器分别用 AX/window 信号做隐私窗口识别。
- 尽可能给支持的浏览器补回归测试。

### 中：`local-first` 仍可能 fallback 到云端

`ModelGateway._backend_candidates("local-first")` 返回 `[local, cloud]`。本地模型失败时，个人活动摘要可能被发到云端。

证据：

- `penpal/pipeline/model.py` 在 `local-first` 下返回 cloud fallback。
- `README.md` 声称除非明确导出，否则不发起网络请求。

建议：

- `local-first` 默认只允许本地模型。
- 把“本地优先、本地失败后云端”的行为放到 `auto`。
- 任何 cloud fallback 都必须显式 opt-in。

### 中：`maintenance scrub-secrets --apply` 实际不可用

命令里 `--dry-run` 默认是 `True`；即使传了 `--apply`，`dry_run` 仍然是 `True`，于是命令报 “Cannot use both”。

证据：

- `penpal/cli.py` 里 `--dry-run` 和 `--apply` 的参数定义。

建议：

- 改成单一 mode flag。
- 或者让没传 `--apply` 时默认 dry-run，不要同时维护两个互斥布尔。

### 中：vault secret scrub 路径不可达

`scrub-secrets` 检查 `hasattr(cfg, 'obsidian_vault_path_expanded')`，但 `Config` 暴露的是 `cfg.obsidian.vault_path`，没有这个属性。

建议：

- 用 `Path(cfg.obsidian.vault_path).expanduser()` 解析 vault 路径。
- 增加临时 vault 测试，放一个含 fake secret 的 Markdown 文件，验证 scrub 生效。

### 中：未跟踪的配置备份文件不应该可提交

`config.toml.bak` 和 `config.toml.bak2` 当前是未跟踪文件。这类文件容易泄露本地路径、模型 endpoint 或未来 token。

建议：

- 在 `.gitignore` 里加 `*.bak` 或 `config.toml.bak*`。
- 把已有备份文件移出仓库目录。

## 产品观察

Penpal 的产品 thesis 是成立的：本地优先的个人活动记忆，以 Obsidian 作为阅读界面，并带有“笔友”式 narrative 层。当前代码骨架也比较完整：

- capture source 是模块化的。
- storage model 简单，便于审计。
- narrative pipeline 有 quality gates 和 deterministic fallback。
- Obsidian 输出是产品化的，不只是普通 export。
- 对早期项目来说，测试数量和覆盖面不错。

主要缺口是信任加固。因为产品需要敏感的 macOS 权限，隐私行为必须比营销文案更严格。后续产品方向应该先由隐私合同约束：明确什么可以落盘、什么可以摘要、什么可以离开本机、什么必须在入库前排除。

## 后续调研问题

竞品和开源方案调研需要回答：

- 现有开源个人知识库、本地记忆、AI second brain 项目到底在做什么？
- 它们创造的价值来自哪里：捕获、搜索、重现、反思、图谱、agent，还是写作辅助？
- Penpal 应该与哪类产品竞争，应该避免与哪类产品竞争？
