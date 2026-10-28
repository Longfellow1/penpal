# 更新日志

所有重要的项目变更都会记录在这个文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.0] - 2026-10-28

完整说明见 `docs/releases/v0.4.0.md`。

### 变更

- 后台 daemon 由 `~/Applications/Penpal.app` 的原生启动器运行，系统权限列表显示为 Penpal。App 的签名身份在普通升级中保持不变，已授予的权限继续有效。缺少 clang 时回退到稳定入口，权限列表仍显示为 Python。
- D36 运行时：daemon、HUD、MCP 共用一份运行状态记录和一个评估器；升级后原地换成新代码；新增 `penpal restart`；看门狗限制拉起次数；`penpal stop` 之后不会被重新拉起。
- 安装：launchd 配置指向稳定入口，不再指向带版本号的 Homebrew 目录；升级时重写 Penpal 自己写的 plist；关闭没有运行记录的旧版 HUD。
- 隐私：D37 按项目分桶（`penpal rules projects` / `rebucket`）；网站屏蔽命令；排除名单在历史导入中同样生效。
- 新增首次使用引导 `penpal install onboard`、可选菜单栏 App（`penpal app install [--login]`）、各 AI 助手独立 token，并为读不到的来源说明原因。

### 升级须知

- 从 v0.2.0-preview.1 升级需要运行一次 `install.sh` 或 `penpal install init`，然后在系统设置里给 Penpal 授予输入监控、辅助功能和完全磁盘访问。

## [0.2.0-preview.1] - 2026-10-19 - Penpal 改名

### 破坏性变更

- Python 包从 `keypulse` 改为 `penpal`，发行名为 `penpal-mcp`；不再提供 `import keypulse` 兼容层。
- 主命令改为 `penpal`。`keypulse` 保留为 CLI 兼容别名，每次调用会向 stderr 提示新命令。

### 变更

- 首次运行时将 `~/.keypulse` 同盘原子改名为 `~/.penpal`，原位置只保留 `MIGRATED_TO_PENPAL` 标记。若新旧目录冲突或系统返回 `EXDEV`，迁移停止且不复制、不合并数据。
- 旧目录中的数据库文件名和记录不回填；运行时继续读取已有 `keypulse.db` / `keypulse.log`，新安装默认使用 `penpal.db` / `penpal.log`。
- 环境变量改为 `PENPAL_*`；旧 `KEYPULSE_*` 仍可读取并提示弃用，新旧并存时新变量优先。
- launchd label 和 MCP server 配置名改为 `com.penpal.*` / `penpal`；安装流程先卸载旧服务，doctor 会报告旧服务残留。
- 新 locator 和 self-echo metadata 使用 `penpal://event/` 与 `via_penpal`；读取端继续识别旧 locator、metadata 和 `keypulse_server_names` 配置，不回填数据库。

### 桌面 App（可选，D33）

- 菜单栏 HUD 按 v3 交互稿重做：固定状态栏与三个标签（概览 / AI 访问 / 隐私与删除），最多两层；旧日报类面板（今日重点、主题、周回声、成本等）已删除。
- HUD 只调用与 CLI 相同的核心函数；新增 `penpal app install [--login]` / `penpal app uninstall`，生成 `~/Applications/Penpal.app`（仅启动 `penpal hud`，自身不需要系统权限）。

### macOS 权限

- CLI daemon 实际由 Homebrew `Python.app` 解释器运行。只改 CLI、包名、数据目录和 launchd label 不要求重新授权；重建开发 venv 且解释器相同也不要求。若 Homebrew Python 升级并改变解释器身份，可能需要重新授予辅助功能和输入监控；启用 OCR 时还包括屏幕录制。

### 升级与回滚

- 开发安装迁移后运行 `bash install.sh --dev` 重建 venv；旧 venv 内脚本的 shebang 仍指向 `~/.keypulse/venv`，不能随目录移动继续使用。
- 手动回滚时删除只含迁移标记的 `~/.keypulse`，再将 `~/.penpal` 改回 `~/.keypulse`。任何含用户数据的目录都不要删除。

## [2.0.0] - 2026-02-27

### 🎉 重大升级：智能脱敏技术

这是一个重大版本升级，引入了智能脱敏技术，在保护隐私的前提下记录有价值的工作内容。

### 新增 (Added)

- ✨ **智能脱敏引擎** (`TextDesensitizer.swift`)
  - 根据应用类型自动选择脱敏策略
  - 保留编程关键字和技术术语
  - 提取动作词和关键词
  - 自动过滤敏感内容
  
- ✨ **键盘输入捕获** (`KeystrokeCounter.swift`)
  - 使用 CGEvent 捕获键盘输入
  - 实时脱敏处理
  - 按应用分组缓冲
  - 黑名单保护敏感应用
  
- ✨ **项目名智能提取** (`ProjectExtractor.swift`)
  - 从窗口标题自动提取项目名
  - 支持多种应用的解析规则
  - 智能识别工作上下文
  
- ✨ **增强的日报生成** (`ReportGenerator.swift`)
  - 显示脱敏后的工作内容
  - 按项目分组并显示关键词
  - 按内容类型统计时长
  - 更详细的工作描述

- 📝 **完整的开源文档**
  - LICENSE (MIT)
  - CONTRIBUTING.md
  - CHANGELOG.md
  - 详细的 README

### 改进 (Changed)

- 🔄 **数据库扩展**
  - 新增 `desensitized_text` 字段
  - 新增 `keywords` 字段
  - 新增 `content_category` 字段
  
- 🔄 **ActivityMonitor 重构**
  - 集成键盘输入捕获
  - 优化数据保存逻辑
  - 改进日志输出

- 📊 **日报格式优化**
  - 显示工作内容摘要
  - 显示提取的关键词
  - 按活动类型分类统计

### 安全 (Security)

- 🔒 **隐私保护增强**
  - 智能脱敏，不存储原始输入
  - 黑名单保护敏感应用
  - 敏感内容自动检测和过滤
  - 本地存储，不上传云端

### 性能 (Performance)

- ⚡ 优化内存使用
- ⚡ 减少数据库写入频率
- ⚡ 改进事件处理效率

---

## [1.0.0] - 2026-02-25

### 初始版本

- ✅ 基础活动监控
- ✅ 应用切换记录
- ✅ 窗口标题获取
- ✅ 简单的时间统计
- ✅ CLI 命令支持

### 功能

- `keypulse start` - 启动监控
- `keypulse stop` - 停止监控
- `keypulse status` - 查看状态
- `keypulse timeline` - 查看时间线
- `keypulse summary` - 查看摘要

### 限制

- ❌ 只记录应用和窗口标题
- ❌ 没有工作内容记录
- ❌ 日报价值有限

---

## 版本说明

### 版本号规则

- **主版本号 (Major)**: 不兼容的 API 变更
- **次版本号 (Minor)**: 向下兼容的功能新增
- **修订号 (Patch)**: 向下兼容的问题修正

### 标签说明

- `Added` - 新增功能
- `Changed` - 功能变更
- `Deprecated` - 即将废弃的功能
- `Removed` - 已移除的功能
- `Fixed` - Bug 修复
- `Security` - 安全相关

---

## 路线图

### v2.1.0 (计划中)

- [ ] 配置文件支持
- [ ] 自定义脱敏规则
- [ ] 导出 PDF 格式
- [ ] 英文文档

### v2.2.0 (计划中)

- [ ] 数据可视化
- [ ] 周报/月报生成
- [ ] 习惯分析建议
- [ ] 更多编程语言支持

### v3.0.0 (未来)

- [ ] 云同步（可选）
- [ ] 团队版
- [ ] AI 智能总结
- [ ] 移动端查看

---

[2.0.0]: https://github.com/Longfellow1/keypulse/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/Longfellow1/keypulse/releases/tag/v1.0.0
