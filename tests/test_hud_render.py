"""HUD renderer: the page script draws the view-model and posts taps back, without a browser.

The JS runs under node with a tiny DOM stand-in (tests/fixtures/hud_dom_harness.js); skipped when node is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from penpal.hud.render import build_html
from penpal.hud.viewmodel import Reveal, UiState, build_model
from tests.test_hud_viewmodel import snap

HARNESS = Path(__file__).parent / "fixtures" / "hud_dom_harness.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(tmp_path, steps):
    page = tmp_path / "hud.html"
    page.write_text(build_html(), encoding="utf-8")
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
    done = subprocess.run(["node", str(HARNESS), str(page), str(steps_file)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_html_embeds_model_safely():
    evil = snap(exclusions=(("app", "</script><img src=x onerror=alert(1)>"),))
    html = build_html(build_model(evil, UiState(tab="privacy", page="exclude")))
    assert "</script><img" not in html  # user text can never close the script tag
    assert html.count("<script>") == 2 and "textContent" in html and "innerHTML" not in html


def test_page_has_the_fixed_regions_and_no_external_resources():
    html = build_html()
    for region in ('id="statusbar"', 'id="titlebar"', 'id="scroll"', 'id="tabs"', 'id="veil"', 'id="toast"'):
        assert region in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "") and "https://" not in html
    assert "width:360px;height:600px" in html


@needs_node
def test_renders_overview_and_posts_taps(tmp_path):
    m = build_model(snap(), UiState())
    m["model_built_at"] = datetime.now(timezone.utc).isoformat()
    out = _run(tmp_path, [{"model": m}, {"click": "实时采集"}, {"click": "AI 访问"}, {"click": "暂停"}])
    shown = out[0]
    assert shown["title"] == "概览" and "正在记录" in shown["status"] and "暂停" in shown["status"]
    assert shown["updated"].startswith("数据更新于 ") and shown["updated"].endswith(" 秒前")
    assert [t["text"] for t in shown["tabs"]] == ["概览", "AI 访问", "设置"] and shown["tabs"][0]["current"]
    assert any("实时采集" in r["text"] and "3 开 · 1 关" in r["text"] for r in shown["rows"])
    assert "1,284" in shown["scroll"] and "新增记录" in shown["scroll"]
    assert out[1]["posted"] == {"action": "nav.page", "args": {"page": "group:live"}}
    assert out[2]["posted"] == {"action": "nav.tab", "args": {"tab": "access"}}
    assert out[3]["posted"] == {"action": "capture.pause", "args": {}}


@needs_node
def test_switch_check_rows_and_confirm_dialog(tmp_path):
    group = build_model(snap(), UiState(page="group:live"))
    host = build_model(snap(), UiState(tab="access", page="host:claude"))
    confirm = build_model(snap(), UiState(tab="access", page="host:claude", confirm={"kind": "revoke", "client_id": "claude"}))
    out = _run(tmp_path, [{"model": group}, {"click": "键盘片段"}, {"model": host}, {"model": confirm}, {"click": "撤销"}, {"click": "@cancel"}, {"click": "@ok"}])
    kb = next(r for r in out[0]["rows"] if "键盘片段" in r["text"])
    assert kb["role"] == "switch" and kb["checked"] == "true" and kb["clickable"]
    assert out[1]["posted"] == {"action": "source.toggle", "args": {"name": "keyboard_chunk", "enable": False}}
    assert out[2]["title"] == "AI 访问Claude Code" and out[2]["dialog"] is None  # back label + page title
    check = next(r for r in out[2]["rows"] if r["text"] == "工作")
    assert check["role"] == "radio" and check["checked"] == "true" and not check["clickable"]  # read-only scope rows
    assert out[3]["dialog"].startswith("撤销授权？|") and "下一次查询会被拒绝" in out[3]["dialog"]
    assert out[4]["posted"] == {"action": "confirm.ask", "args": {"kind": "revoke", "client_id": "claude"}}
    assert out[5]["posted"] == {"action": "confirm.cancel", "args": {}}
    assert out[6]["posted"] == {"action": "confirm.ok", "args": {}}


@needs_node
def test_toast_dim_back_and_reveal_code_block(tmp_path):
    paused = build_model(snap(paused=True), UiState(toast=(1, "已暂停，重启后仍保持")))
    reveal = build_model(snap(), UiState(tab="access", page="reveal", reveal=Reveal("授权成功", "kp_FAKE\nconfig", "claude")))
    out = _run(tmp_path, [{"model": paused}, {"model": reveal}, {"click": "完成"}, {"click": "复制配置"}])
    assert out[0]["dim"] == "dim" and out[0]["toast"] == "已暂停，重启后仍保持" and "继续记录" in out[0]["status"]
    assert out[1]["pre"] == "kp_FAKE\nconfig" and out[1]["title"] == "完成授权成功"  # toast hides on its own timer
    assert out[2]["posted"] == {"action": "nav.back", "args": {}}
    assert out[3]["posted"] == {"action": "reveal.copy", "args": {}}
