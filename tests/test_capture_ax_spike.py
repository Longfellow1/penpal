"""M2-4 spike (#64): pure diff/role/accounting logic, CLI permission and output path guards.

No real AX: every test drives the spike through a fake adapter. Output always goes to tmp_path,
never to ~/.penpal.
"""
from __future__ import annotations

import stat

from click.testing import CliRunner

from penpal.capture.ax_spike import (
    PERMISSION_MISSING_MESSAGE,
    R_NO_FOCUS,
    R_ROLE,
    R_SECURE,
    R_VALUE,
    CommittedTextTracker,
    FocusRead,
    TextRead,
    classify_element,
    committed_text,
    git_worktree_root_for,
    insertion_delta,
    make_hygiene_gate,
    run_ax_text_spike,
    run_spike,
    utf16_to_index,
)
from penpal.foundation.capture_hygiene import DEFAULT_TITLE_DENY_PATTERNS
from penpal.config import Config


# --- fakes -------------------------------------------------------------------------------------

def field(key="field-1", role="AXTextArea", subrole=None) -> FocusRead:
    return FocusRead(ok=True, role=role, subrole=subrole, element_key=key, handle=key)


def text(value, marked=None, supported=True, title="Note") -> TextRead:
    return TextRead(value=value, marked_range=marked, marked_supported=supported, window_title=title)


class FakeAdapter:
    """Each poll is one (FocusRead, TextRead or None) step. read_text() returns that step's TextRead."""

    def __init__(self, steps=(), *, trusted=True, codes=None, resolved_app_name=None):
        self.steps = list(steps)
        self.trusted = trusted
        self.codes = {"AXManualAccessibility": 0} if codes is None else codes
        self.resolved_app_name = resolved_app_name
        self.text_calls = 0
        self.focused_calls = 0
        self.enable_calls = 0

    def is_trusted(self):
        return self.trusted

    def find_app_pid(self, app_name):
        return 4242

    def app_name_for_pid(self, pid):
        return self.resolved_app_name

    def enable_accessibility(self, pid, app_name):
        self.enable_calls += 1
        return dict(self.codes)

    def read_focused(self, pid):
        self.focused_calls += 1
        if not self.steps:
            self._pending = None
            return FocusRead(ok=False, reason=R_NO_FOCUS)
        focus, self._pending = self.steps.pop(0)
        return focus

    def read_text(self, read):
        self.text_calls += 1
        return self._pending


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def default_gate(**overrides):
    kwargs = dict(
        redact_emails=True, redact_phones=True, redact_tokens=True,
        drop_terminal_text=True, max_text_length=2000,
        title_deny_patterns=DEFAULT_TITLE_DENY_PATTERNS,
    )
    kwargs.update(overrides)
    return make_hygiene_gate(**kwargs)


def run_steps(steps, *, gate=None, interval=1.0):
    """One poll per step. The clock advances by the interval on each sleep, so len(steps) polls run."""
    adapter = FakeAdapter(steps)
    clock = FakeClock()
    out = []
    stats = run_spike(
        adapter,
        pid=1,
        app="Obsidian",
        seconds=len(steps) * interval,
        poll_interval_sec=interval,
        gate=gate or default_gate(),
        sink=out.append,
        clock=clock,
        sleep=clock.sleep,
    )
    return stats, out, adapter


def deltas(out):
    return [record["delta"] for record in out if record["kind"] == "delta"]


# --- committed-text diff (pure) ------------------------------------------------------------------

def test_append_is_reported():
    assert insertion_delta("hello", "hello world") == " world"


def test_insert_in_middle_is_reported():
    assert insertion_delta("abz", "abXYz") == "XY"


def test_deletion_is_ignored():
    assert insertion_delta("hello world", "hello") == ""
    assert insertion_delta("你好吗", "你好") == ""


def test_identical_text_gives_nothing():
    assert insertion_delta("同样的内容", "同样的内容") == ""


def test_replacement_counts_only_the_new_text():
    assert insertion_delta("abc", "abXY") == "XY"


def test_chinese_insert_in_middle_and_append():
    assert insertion_delta("我们好", "我们很好") == "很"
    assert insertion_delta("你好", "你好吗") == "吗"


def test_marked_text_is_removed_from_committed_value():
    # "我们" committed, "hao" still composing at UTF-16 offset 2, length 3
    assert committed_text("我们hao", (2, 3)) == "我们"


def test_no_marked_range_keeps_value():
    assert committed_text("我们", None) == "我们"
    assert committed_text("我们", (0, 0)) == "我们"


def test_utf16_offsets_handle_astral_characters():
    # "😀" is two UTF-16 units but one Python character.
    assert utf16_to_index("a😀b", 1) == 1
    assert utf16_to_index("a😀b", 3) == 2
    assert committed_text("a😀b", (1, 2)) == "ab"


def test_tracker_resets_when_element_changes():
    tracker = CommittedTextTracker()
    assert tracker.observe("A", "你好") == ""  # first observation is the baseline
    assert tracker.observe("B", "完全不同的内容") == ""  # other field: no cross-field delta
    assert tracker.observe("B", "完全不同的内容吗") == "吗"


def test_tracker_with_unknown_key_never_emits():
    tracker = CommittedTextTracker()
    assert tracker.observe(None, "你好") == ""
    assert tracker.observe(None, "你好世界") == ""


# --- IME composition end to end --------------------------------------------------------------------

def test_composition_is_excluded_and_only_committed_chinese_is_emitted():
    steps = [
        (field(), text("我们")),                       # baseline
        (field(), text("我们hao", (2, 3))),            # composing "hao": no delta
        (field(), text("我们好", None)),               # committed as 好
    ]
    stats, out, _ = run_steps(steps)
    assert deltas(out) == ["好"]
    assert stats.marked_range_excluded_reads == 1
    assert all("hao" not in record["delta"] for record in out if record["kind"] == "delta")


def test_unsupported_marked_range_is_counted():
    steps = [
        (field(), text("你", None, supported=False)),
        (field(), text("你好", None, supported=False)),
    ]
    stats, out, _ = run_steps(steps)
    assert deltas(out) == ["好"]
    assert stats.marked_range_unsupported_reads == 2


# --- role filtering and secure fields -----------------------------------------------------------

def test_secure_role_is_skipped_and_never_read():
    steps = [(field(role="AXSecureTextField"), text("hunter2 secret"))]
    stats, out, adapter = run_steps(steps)
    assert stats.reads_failed[R_SECURE] == 1
    assert adapter.text_calls == 0  # read_text must not run for a secure field
    assert out == []


def test_secure_subrole_on_textfield_is_skipped():
    # Chrome reports password fields as AXTextField with subrole AXSecureTextField.
    steps = [(field(role="AXTextField", subrole="AXSecureTextField"), text("pw123"))]
    stats, out, adapter = run_steps(steps)
    assert stats.reads_failed[R_SECURE] == 1
    assert adapter.text_calls == 0


def test_non_text_role_is_skipped():
    steps = [(field(role="AXWebArea"), text("页面正文"))]
    stats, out, adapter = run_steps(steps)
    assert stats.reads_failed[R_ROLE] == 1
    assert adapter.text_calls == 0


def test_classify_element_roles():
    assert classify_element("AXTextField", None) is None
    assert classify_element("AXTextArea", None) is None
    assert classify_element("AXSecureTextField", None) == R_SECURE
    assert classify_element("AXTextField", "AXSecureTextField") == R_SECURE
    assert classify_element("AXGroup", None) == R_ROLE
    assert classify_element(None, None) == R_ROLE


def test_value_none_is_counted_as_value_unsupported():
    steps = [(field(), text(None))]
    stats, out, _ = run_steps(steps)
    assert stats.reads_failed[R_VALUE] == 1


# --- accounting -----------------------------------------------------------------------------------

def test_summary_counts_add_up():
    steps = [
        (FocusRead(ok=False, reason=R_NO_FOCUS), None),
        (field(role="AXSecureTextField"), None),
        (field(), text("你")),
        (field(), text("你好")),
        (field(), text("你好")),
    ]
    stats, out, _ = run_steps(steps)
    assert stats.polls == 5
    assert stats.reads_ok == 3
    assert stats.reads_failed == {R_NO_FOCUS: 1, R_SECURE: 1}
    assert stats.reads_ok + sum(stats.reads_failed.values()) == stats.polls
    assert stats.deltas == 1
    summary = stats.summary(app="Obsidian", cpu_seconds=0.5, manual_accessibility_set=True, duration_sec=5.0)
    assert summary["kind"] == "summary"
    assert summary["polls"] == 5
    assert summary["deltas"] == 1
    assert summary["manual_accessibility_set"] is True
    assert summary["reads_failed"] == {R_NO_FOCUS: 1, R_SECURE: 1}


def test_delta_record_fields():
    steps = [(field(), text("")), (field(), text("你好"))]
    stats, out, _ = run_steps(steps)
    record = [r for r in out if r["kind"] == "delta"][0]
    assert record["delta"] == "你好"
    assert record["chars"] == 2
    assert record["app"] == "Obsidian"
    assert record["role"] == "AXTextArea"
    assert record["latency_ms"] >= 0
    assert "ts" in record


# --- hygiene gate (D27) ---------------------------------------------------------------------------

def test_placeholder_is_dropped_by_hygiene():
    # "Ask Claude" is a known input placeholder in APP_CHROME_PLACEHOLDERS.
    steps = [(field(), text("")), (field(), text("Ask Claude"))]
    stats, out, _ = run_steps(steps)
    assert deltas(out) == []
    assert stats.deltas_gated == {"app_chrome": 1}


def test_privacy_keyword_is_dropped_by_hygiene():
    steps = [(field(), text("")), (field(), text("my password is"))]
    stats, out, _ = run_steps(steps)
    assert deltas(out) == []
    assert stats.deltas_gated == {"privacy_keyword": 1}


def test_passing_delta_is_desensitized():
    steps = [(field(), text("")), (field(), text("请发到 someone@example.com 谢谢"))]
    stats, out, _ = run_steps(steps)
    emitted = deltas(out)
    assert len(emitted) == 1
    assert "someone@example.com" not in emitted[0]


def test_terminal_app_is_dropped_when_configured():
    gate = default_gate(drop_terminal_text=True)
    result = gate("你好世界", "Terminal", "zsh")
    assert result.text is None and result.reason == "terminal_app"


def test_excluded_current_browser_page_is_denied_by_shared_gate():
    gate = default_gate(url_deny_hosts=["example.test"])

    result = gate("Please review this plan", "Google Chrome", "Notes", "https://sub.example.test/path")

    assert result.text is None
    assert result.reason == "denied_domain"


def test_spike_does_not_read_ax_when_browser_url_is_unknown():
    adapter = FakeAdapter([(field(), text("hello"))])
    clock = FakeClock()
    out = []

    stats = run_spike(
        adapter,
        pid=1,
        app="Google Chrome",
        seconds=1,
        poll_interval_sec=1,
        gate=default_gate(),
        sink=out.append,
        page_url_provider=lambda: None,
        require_page_url=True,
        clock=clock,
        sleep=clock.sleep,
    )

    assert adapter.focused_calls == 0
    assert adapter.text_calls == 0
    assert stats.reads_failed == {"page_url_unknown": 1}
    assert out == []


def test_spike_gates_each_delta_against_the_current_browser_url():
    steps = [(field(), text("")), (field(), text("Please review this plan"))]
    adapter = FakeAdapter(steps)
    clock = FakeClock()
    out = []
    page_urls = iter(("https://allowed.example.test", "https://sub.example.test/private"))

    stats = run_spike(
        adapter,
        pid=1,
        app="Google Chrome",
        seconds=2,
        poll_interval_sec=1,
        gate=default_gate(url_deny_hosts=["example.test"]),
        sink=out.append,
        page_url_provider=lambda: next(page_urls),
        require_page_url=True,
        clock=clock,
        sleep=clock.sleep,
    )

    assert deltas(out) == []
    assert stats.deltas_gated == {"denied_domain": 1}


# --- permission and output path (run_ax_text_spike) -------------------------------------------------

def test_missing_permission_returns_nonzero_with_chinese_instruction(tmp_path):
    messages = []
    code = run_ax_text_spike(
        app_name="Obsidian",
        seconds=1,
        out=str(tmp_path / "out.jsonl"),
        adapter=FakeAdapter(trusted=False),
        cfg_loader=lambda: (_ for _ in ()).throw(AssertionError("config must not load before the permission check")),
        echo=messages.append,
    )
    assert code == 2
    assert messages == [PERMISSION_MISSING_MESSAGE]
    assert "系统设置 → 隐私与安全性 → 辅助功能" in messages[0]
    assert not (tmp_path / "out.jsonl").exists()


def test_out_path_inside_git_worktree_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = repo / "nested" / "out.jsonl"
    messages = []
    code = run_ax_text_spike(
        app_name="Obsidian",
        seconds=1,
        out=str(out),
        adapter=FakeAdapter(),
        cfg_loader=lambda: Config(),
        echo=messages.append,
    )
    assert code == 2
    assert "拒绝写入" in messages[0]
    assert not out.exists()


def test_git_worktree_detection_for_missing_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").touch()  # worktree style: .git is a file
    assert git_worktree_root_for(repo / "a" / "b.jsonl") == repo.resolve()
    assert git_worktree_root_for(tmp_path / "elsewhere.jsonl") is None


def test_output_file_is_0600_and_not_overwritten(tmp_path):
    out = tmp_path / "spike" / "out.jsonl"
    adapter = FakeAdapter([(field(), text("")), (field(), text("你好"))])
    clock = FakeClock()
    messages = []
    code = run_ax_text_spike(
        app_name="Obsidian",
        seconds=2,
        out=str(out),
        adapter=adapter,
        cfg_loader=lambda: Config(),
        echo=messages.append,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    lines = [line for line in out.read_text(encoding="utf-8").splitlines() if line]
    records = [__import__("json").loads(line) for line in lines]
    assert records[-1]["kind"] == "summary"
    assert [r["delta"] for r in records if r["kind"] == "delta"] == ["你好"]

    again = run_ax_text_spike(
        app_name="Obsidian",
        seconds=1,
        out=str(out),
        adapter=FakeAdapter(),
        cfg_loader=lambda: Config(),
        echo=messages.append,
        clock=FakeClock(),
        sleep=FakeClock().sleep,
    )
    assert again == 2  # O_EXCL refuses to overwrite the existing file


def test_chrome_gets_enhanced_ui_attempt_and_manual_flag_recorded(tmp_path, monkeypatch):
    from penpal.capture.watchers.browser_url import BrowserUrlWatcher

    monkeypatch.setattr(BrowserUrlWatcher, "read_browser_tab", lambda self, name: ("https://docs.example.test", "Docs"))
    adapter = FakeAdapter(
        [],
        codes={"AXManualAccessibility": -25205, "AXEnhancedUserInterface": 0},
    )
    messages = []
    out = tmp_path / "chrome.jsonl"
    clock = FakeClock()
    run_ax_text_spike(
        app_name="Google Chrome",
        seconds=1,
        out=str(out),
        adapter=adapter,
        cfg_loader=lambda: Config(),
        echo=messages.append,
        clock=clock,
        sleep=clock.sleep,
    )
    summary = __import__("json").loads(out.read_text(encoding="utf-8").splitlines()[-1])
    assert summary["manual_accessibility_set"] is False
    assert any("AXEnhancedUserInterface" in m for m in messages)


def test_browser_spike_refuses_before_sampling_if_current_url_is_unknown(tmp_path, monkeypatch):
    from penpal.capture.watchers.browser_url import BrowserUrlWatcher

    monkeypatch.setattr(BrowserUrlWatcher, "read_browser_tab", lambda self, name: None, raising=False)
    adapter = FakeAdapter([(field(), text("should not be read"))])
    messages = []
    out = tmp_path / "chrome.jsonl"

    code = run_ax_text_spike(
        app_name="Google Chrome",
        seconds=1,
        out=str(out),
        adapter=adapter,
        cfg_loader=Config,
        echo=messages.append,
        clock=FakeClock(),
        sleep=FakeClock().sleep,
    )

    assert code == 2
    assert any("无法确认当前浏览器页面 URL" in message for message in messages)
    assert adapter.enable_calls == 0
    assert adapter.focused_calls == 0
    assert adapter.text_calls == 0
    assert not out.exists()


def test_browser_bundle_id_is_resolved_before_url_requirement(tmp_path, monkeypatch):
    from penpal.capture.watchers.browser_url import BrowserUrlWatcher

    monkeypatch.setattr(BrowserUrlWatcher, "read_browser_tab", lambda self, name: None)
    adapter = FakeAdapter(resolved_app_name="Google Chrome")
    messages = []

    code = run_ax_text_spike(
        app_name="com.google.Chrome",
        seconds=1,
        out=str(tmp_path / "chrome.jsonl"),
        adapter=adapter,
        cfg_loader=Config,
        echo=messages.append,
        clock=FakeClock(),
        sleep=FakeClock().sleep,
    )

    assert code == 2
    assert any("无法确认当前浏览器页面 URL" in message for message in messages)
    assert adapter.enable_calls == 0


def test_cli_permission_missing_exits_nonzero(tmp_path, monkeypatch):
    import penpal.capture.ax_spike_adapter as adapter_module
    from penpal.cli import main

    monkeypatch.setattr(adapter_module, "PyObjCAXAdapter", lambda: FakeAdapter(trusted=False))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("no config"))))
    result = CliRunner().invoke(main, ["spike", "ax-text", "--app", "Obsidian", "--seconds", "1",
                                       "--out", str(tmp_path / "x.jsonl")])
    assert result.exit_code != 0
    assert PERMISSION_MISSING_MESSAGE in result.output
