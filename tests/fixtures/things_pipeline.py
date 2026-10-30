from __future__ import annotations

from datetime import datetime, timezone

from penpal.sources.types import SemanticEvent


def make_fixture_events() -> list[SemanticEvent]:
    return [
        SemanticEvent(
            time=datetime(2026, 4, 28, 0, 42, tzinfo=timezone.utc),
            source="terminal",
            actor="alice",
            intent="在 Codex CLI 调整 Penpal 管道并检查 commit 11a3a9b",
            artifact="commit:11a3a9b",
            raw_ref="terminal://zsh/1",
            privacy_tier="green",
            metadata={"app": "Codex CLI"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 1, 5, tzinfo=timezone.utc),
            source="git_log",
            actor="alice",
            intent="提交 Penpal 管道修复，commit message: Penpal things cleanup，git 已落地",
            artifact="11a3a9b",
            raw_ref="git://commit/11a3a9b",
            privacy_tier="green",
            metadata={"commit": "11a3a9b"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 8, 36, tzinfo=timezone.utc),
            source="chrome_history",
            actor="alice",
            intent="在 Chrome 查看邮件页面 wx.mail.qq.com 并确认收件箱主题",
            artifact="https://wx.mail.qq.com/cgi-bin/frame_html?sid=abc",
            raw_ref="chrome://history/1",
            privacy_tier="amber",
            metadata={"host": "wx.mail.qq.com"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 9, 18, tzinfo=timezone.utc),
            source="wechat",
            actor="alice",
            intent="在微信里同步邮件结论和后续待办",
            artifact="WeChat chat",
            raw_ref="wechat://chat/1",
            privacy_tier="amber",
            metadata={"app": "WeChat"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 13, 22, tzinfo=timezone.utc),
            source="codex_cli",
            actor="alice",
            intent="用 Codex CLI 写 tests/test_things_pipeline_e2e.py 并准备 e2e 验收",
            artifact="tests/test_things_pipeline_e2e.py",
            raw_ref="codex://session/1",
            privacy_tier="green",
            metadata={"app": "Codex CLI"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 14, 2, tzinfo=timezone.utc),
            source="terminal",
            actor="alice",
            intent="运行 pytest 检查 e2e 输出与分配结果",
            artifact="python3 -m pytest",
            raw_ref="terminal://zsh/2",
            privacy_tier="green",
            metadata={},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 19, 11, tzinfo=timezone.utc),
            source="git_log",
            actor="alice",
            intent="整理 Penpal 提交说明并保留 commit 11a3a9b 与测试记录",
            artifact="commit:11a3a9b message: Penpal",
            raw_ref="git://commit/11a3a9b#2",
            privacy_tier="green",
            metadata={"commit": "11a3a9b"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 21, 3, tzinfo=timezone.utc),
            source="chrome_history",
            actor="alice",
            intent="回看 wx.mail.qq.com 邮件并记录后续待办",
            artifact="https://wx.mail.qq.com",
            raw_ref="chrome://history/2",
            privacy_tier="amber",
            metadata={"host": "wx.mail.qq.com"},
        ),
        SemanticEvent(
            time=datetime(2026, 4, 28, 22, 48, tzinfo=timezone.utc),
            source="terminal",
            actor="alice",
            intent="在 Codex CLI 收尾 README 文档说明并记录 Penpal",
            artifact="README.md",
            raw_ref="terminal://zsh/3",
            privacy_tier="green",
            metadata={"app": "Codex CLI"},
        ),
    ]


_SESSION_RENDER_RESPONSE = (
    "### Penpal 提交落地\n"
    "凌晨快一点你在 Codex CLI 里推进 Penpal 的 things 管道修复，"
    "commit 11a3a9b 把改动收口落地，git 那条记录留下了 message。\n\n"
    "### 邮件线索跟进\n"
    "上午在 Chrome 看了 wx.mail.qq.com 的邮件页面，又转到微信里同步邮件结论和后续待办。\n\n"
    "### 测试验收与文档\n"
    "下午在 Codex CLI 写 tests/test_things_pipeline_e2e.py 并跑 pytest 验收，"
    "晚上在 Terminal 收尾 README 把 Penpal 的迭代记录补完整。"
)


_OVERVIEW_RESPONSE = (
    "你今天围绕 Penpal 把 things 管道从评测到改造走通，"
    "先在 Codex CLI 里确认 commit 11a3a9b 的变更脉络，"
    "再处理 wx.mail.qq.com 的邮件线索与沟通同步，"
    "最后把验证结果收口成可复跑的测试闭环。"
)


class StubGateway:
    def __init__(self, *, should_raise: bool = False):
        self.should_raise = should_raise
        self.calls: list[str] = []

    def render(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.should_raise:
            raise RuntimeError("stub gateway failure")

        if "请识别这段时间用户做的" in prompt or "事件流（按时间排序" in prompt:
            return _SESSION_RENDER_RESPONSE

        if "今日做事概览" in prompt:
            return _OVERVIEW_RESPONSE

        return "ok"
