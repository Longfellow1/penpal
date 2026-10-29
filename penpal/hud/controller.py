"""HUD controller: UI state + action routing. No AppKit, no business logic.

Every action calls the same core function the matching CLI command calls (looked up on the module at call
time so one monkeypatch covers both shells):

    capture.pause / capture.resume  -> capture_control.set_capture_paused   (penpal pause / resume)
    source.toggle, keyboard.toggle  -> capture_control.set_source_enabled   (penpal source enable / disable)
    chat.toggle                     -> config.set_chat_app_opt_in           (penpal privacy chat-apps allow / deny)
    grant.create / rotate / revoke  -> mcp.auth.create_grant / rotate_grant / revoke_grants (penpal mcp ...)
    purge preview / run             -> services.purge.run_purge             (penpal purge [--dry-run])
    daemon.restart                  -> penpal.runtime.control.restart
    settings.open                   -> permissions.open_settings_url        (pane shown by penpal doctor)
    hud.quit                        -> injected callback (quit_hud)         (penpal.hud.app.confirmQuit_)

A freshly created token is held in ``UiState.reveal`` (memory only) until the user leaves the page; it is
never written to disk, the model of any later page, or the log.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from penpal.hud.snapshot import Snapshot, collect_snapshot
from penpal.hud.viewmodel import NEW_HOST_CHOICES, Reveal, UiState, build_model, delete_options
from penpal.utils.logging import get_logger

LOGGER = get_logger("hud.controller")
SNAPSHOT_TTL_SEC = 3.0
SECRET_PAGES = ("reveal",)


def _spawn(fn: Callable[[], Any]) -> None:
    threading.Thread(target=fn, daemon=True, name="hud-action").start()


class HudModelWorker:
    """Run model reads and actions on one worker; dispatch only rendered results to the UI thread."""

    def __init__(
        self,
        controller: "HudController",
        *,
        main_dispatcher: Callable[[Callable[[], None]], None],
        apply_script: Callable[[str, Optional[str]], None],
        before_model: Optional[Callable[[], bool]] = None,
        model_built: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.controller = controller
        self.main_dispatcher = main_dispatcher
        self.apply_script = apply_script
        self.before_model = before_model
        self.model_built = model_built
        self._condition = threading.Condition()
        self._refresh_requested = False
        self._fresh = False
        self._actions: list[tuple[str, dict]] = []
        self._tasks: list[tuple[Callable[[], Any], Optional[Callable[[Any], None]], bool]] = []
        self._thread = threading.Thread(target=self._run, daemon=True, name="penpal-hud-model")
        self._thread.start()

    def refresh(self, *, fresh: bool = False) -> None:
        with self._condition:
            self._refresh_requested = True
            self._fresh = self._fresh or fresh
            self._condition.notify()

    def push_current_model(self) -> None:
        model = self.controller.model()
        built_at = datetime.now(timezone.utc).isoformat()
        model["model_built_at"] = built_at
        payload = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
        script = f"window.render({payload});"
        badge = model["tabs"][0].get("badge") if model.get("tabs") else None
        self.main_dispatcher(lambda script=script, badge=badge: self.apply_script(script, badge))

    def _push_current_model(self) -> None:
        self.push_current_model()

    def dispatch(self, action: str, args: Optional[dict] = None) -> None:
        with self._condition:
            self._actions.append((action, args if isinstance(args, dict) else {}))
            self._condition.notify()

    def submit(
        self,
        task: Callable[[], Any],
        callback: Optional[Callable[[Any], None]] = None,
        *,
        rebuild: bool = False,
    ) -> None:
        with self._condition:
            self._tasks.append((task, callback, rebuild))
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not (self._refresh_requested or self._actions or self._tasks):
                    self._condition.wait()
                refresh = self._refresh_requested
                fresh = self._fresh
                actions, self._actions = self._actions, []
                tasks, self._tasks = self._tasks, []
                self._refresh_requested = False
                self._fresh = False

            try:
                if self.before_model is not None and self.before_model():
                    continue
                for task, callback, rebuild in tasks:
                    try:
                        result = task()
                    except Exception as exc:
                        LOGGER.warning("hud background task failed: %s", type(exc).__name__)
                        continue
                    if callback is not None:
                        try:
                            self.main_dispatcher(lambda result=result, callback=callback: callback(result))
                        except Exception as exc:
                            LOGGER.warning("hud main-thread dispatch failed: %s", type(exc).__name__)
                    refresh = refresh or rebuild
                model = None
                for action, args in actions:
                    model = self.controller.dispatch(action, args)
                if model is None and (refresh or actions):
                    model = self.controller.model(fresh=fresh)
                if model is not None:
                    built_at = datetime.now(timezone.utc).isoformat()
                    model["model_built_at"] = built_at
                    if self.model_built is not None:
                        try:
                            self.model_built(built_at)
                        except OSError as exc:
                            LOGGER.warning("hud runtime record write failed: %s", type(exc).__name__)
                    payload = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
                    script = f"window.render({payload});"
                    badge = model["tabs"][0].get("badge") if model.get("tabs") else None
                    self.main_dispatcher(lambda script=script, badge=badge: self.apply_script(script, badge))
            except Exception as exc:  # one slow or broken reader must not take down the AppKit run loop
                LOGGER.warning("hud model refresh failed: %s", type(exc).__name__)


class HudController:
    def __init__(
        self,
        load_config: Callable[[], Any],
        *,
        copy_to_clipboard: Optional[Callable[[str], None]] = None,
        quit_hud: Optional[Callable[[], None]] = None,
        spawn: Callable[[Callable[[], Any]], None] = _spawn,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        push_model: Optional[Callable[[], None]] = None,
    ) -> None:
        self._load_config = load_config
        self._copy = copy_to_clipboard
        self._quit = quit_hud
        self._spawn = spawn
        self._clock = clock
        self._push_model = push_model
        self.ui = UiState()
        self._snap: Optional[Snapshot] = None
        self._snap_at = 0.0
        self._toast_seq = 0

    def set_push_model(self, push_model: Optional[Callable[[], None]]) -> None:
        self._push_model = push_model

    # ── model ────────────────────────────────────────────────────────────────

    def snapshot(self, *, fresh: bool = False) -> Snapshot:
        if fresh or self._snap is None or time.monotonic() - self._snap_at > SNAPSHOT_TTL_SEC:
            self._snap = collect_snapshot(self._load_config(), now=self._clock())
            self._snap_at = time.monotonic()
        return self._snap

    def _preview(self, snap: Snapshot) -> tuple[Optional[dict], Optional[dict]]:
        """(counts, selector) for the chosen delete scope, only while the delete page is open. Counts come from
        the same dry run as ``penpal purge --dry-run``."""
        if self.ui.page != "delete":
            return None, None
        from penpal.services import purge

        opt = next((o for o in delete_options(snap) if o.id == self.ui.delete_scope), None)
        if opt is None:
            return None, None
        selector = purge.purge_selector(**opt.kwargs, now=self._clock())
        try:
            return dict(purge.run_purge(selector, dry_run=True)), selector
        except Exception as exc:
            LOGGER.warning("hud delete preview failed: %s", type(exc).__name__)
            return None, selector

    def model(self, *, fresh: bool = False) -> dict[str, Any]:
        snap = self.snapshot(fresh=fresh)
        counts, _ = self._preview(snap)
        return build_model(snap, self.ui, counts)

    def popover_closed(self) -> None:
        """Forget anything transient (secret, open dialog, toast) when the popover closes."""
        self.ui = replace(self.ui, reveal=None, confirm=None, toast=None,
                          page="" if self.ui.page in SECRET_PAGES else self.ui.page)

    # ── actions ──────────────────────────────────────────────────────────────

    def dispatch(self, action: str, args: Optional[dict] = None) -> dict[str, Any]:
        """Run one action and return the new model. Failures become a toast; they never raise to the shell."""
        args = args if isinstance(args, dict) else {}
        handler = getattr(self, "_do_" + action.replace(".", "_"), None) if isinstance(action, str) else None
        if handler is None:
            LOGGER.warning("hud: unknown action %r", str(action)[:40])
            return self.model()
        try:
            changed = handler(args)
        except Exception as exc:  # message is not shown: it could echo user data
            LOGGER.warning("hud action %s failed: %s", action, type(exc).__name__)
            self._say("操作没有成功，详情见 penpal doctor")
            changed = False
        return self.model(fresh=bool(changed) or self._snap is None)

    def _say(self, text: str) -> None:
        self._toast_seq += 1
        self.ui = replace(self.ui, toast=(self._toast_seq, text))

    def _leave_secret(self) -> None:
        if self.ui.reveal is not None:
            self.ui = replace(self.ui, reveal=None)

    # navigation
    def _do_nav_tab(self, args: dict) -> bool:
        tab = args.get("tab")
        if tab not in ("overview", "access", "privacy"):
            return False
        self._leave_secret()
        self.ui = replace(self.ui, tab=tab, page="", confirm=None, toast=None)
        return False

    def _do_nav_page(self, args: dict) -> bool:
        page = args.get("page")
        if not isinstance(page, str) or not page or page in SECRET_PAGES:
            return False
        self._leave_secret()
        self.ui = replace(self.ui, page=page, confirm=None, toast=None)
        return False

    def _do_nav_back(self, args: dict) -> bool:
        self._leave_secret()
        self.ui = replace(self.ui, page="", confirm=None, toast=None)
        return False

    # capture / sources / privacy
    def _do_capture_pause(self, args: dict) -> bool:
        from penpal.capture import capture_control

        capture_control.set_capture_paused(True)
        self._say("已暂停，重启后仍保持")
        return True

    def _do_capture_resume(self, args: dict) -> bool:
        from penpal.capture import capture_control

        capture_control.set_capture_paused(False)
        self._say("已继续记录")
        return True

    def _set_source(self, name: str, enable: bool) -> None:
        from penpal.capture import capture_control

        capture_control.set_source_enabled(name, enable)

    def _do_source_toggle(self, args: dict) -> bool:
        name, enable = args.get("name"), bool(args.get("enable"))
        if name not in {s.name for s in self.snapshot().sources}:  # same check as `penpal source enable`
            return False
        self._set_source(name, enable)
        self._say(("已开启 " if enable else "已关闭 ") + next(s.label for s in self.snapshot().sources if s.name == name))
        return True

    def _do_keyboard_toggle(self, args: dict) -> bool:
        from penpal.capture.keyboard_notice import KEYBOARD_SOURCE

        enable = bool(args.get("enable"))
        self._set_source(KEYBOARD_SOURCE, enable)
        self._say("键盘片段已开启" if enable else "键盘片段已关闭，不再写入")
        return True

    def _do_chat_toggle(self, args: dict) -> bool:
        from penpal import config

        allow = bool(args.get("allow"))
        try:
            key, _ = config.set_chat_app_opt_in(str(args.get("name") or ""), allow)
        except ValueError:
            self._say("不认识这个聊天 App")
            return False
        self._say(("已放行 " if allow else "已取消放行 ") + key + "，约 1 分钟内生效")
        return True

    # AI access
    def _do_host_pick(self, args: dict) -> bool:
        host = args.get("host")
        if host in NEW_HOST_CHOICES:
            self.ui = replace(self.ui, new_host=host)
        return False

    def _do_host_bucket(self, args: dict) -> bool:
        from penpal.store.buckets import BUCKETS

        bucket = args.get("bucket")
        if bucket not in BUCKETS:
            return False
        current = list(self.ui.new_buckets)
        if bucket in current:
            current.remove(bucket)
        else:
            current.append(bucket)
        self.ui = replace(self.ui, new_buckets=tuple(b for b in BUCKETS if b in current))
        return False

    def _do_grant_create(self, args: dict) -> bool:
        from penpal.mcp import auth
        from penpal.mcp import cli as mcp_cli

        host = args.get("host")
        if host not in NEW_HOST_CHOICES:
            return False
        if not self.ui.new_buckets:
            self._say("至少选一个分类")
            return False
        if any(h.client_id == host for h in self.snapshot().hosts):
            self._say("已经连接了，要换范围请先撤销")
            return False
        buckets = list(self.ui.new_buckets)
        try:
            token = auth.create_grant(host, buckets=buckets)
        except ValueError:
            self._say("授权失败")
            return False
        scope = f"buckets={','.join(buckets)}, sources=all in buckets, operations=read"
        lines = mcp_cli.grant_output_lines(
            host, scope, token, host, mcp_cli._penpal_executable(), buckets=buckets,
            enabled_sources=[s.name for s in self.snapshot().sources if s.enabled],
        )
        self.ui = replace(self.ui, page="reveal", reveal=Reveal("授权成功", "\n".join(lines), host), new_buckets=("Work",))
        return True

    def _do_grant_rotate(self, args: dict) -> bool:
        from penpal.mcp import auth
        from penpal.mcp import cli as mcp_cli

        client_id = str(args.get("client_id") or "")
        try:
            token = auth.rotate_grant(client_id)
        except ValueError:
            self._say("没有可轮换的授权")
            return False
        grant = next((g for g in auth.active_grants() if g["client_id"] == client_id), None)
        lines = mcp_cli.rotate_output_lines(
            client_id, token, mcp_cli._penpal_executable(),
            buckets=grant["buckets"] if grant else (), sources=grant["sources"] if grant else None,
            enabled_sources=[s.name for s in self.snapshot().sources if s.enabled],
        )
        self.ui = replace(self.ui, page="reveal", reveal=Reveal("新的密钥", "\n".join(lines), client_id))
        return True

    def _do_reveal_copy(self, args: dict) -> bool:
        if self.ui.reveal is None or self._copy is None:
            return False
        self._copy(self.ui.reveal.text)
        self._say("已复制")
        return False

    # delete
    def _do_delete_pick(self, args: dict) -> bool:
        scope = args.get("scope")
        if scope in {o.id for o in delete_options(self.snapshot())}:
            self.ui = replace(self.ui, delete_scope=scope)
        return False

    # confirm dialog (delete and revoke only)
    def _do_confirm_ask(self, args: dict) -> bool:
        kind = args.get("kind")
        if kind == "revoke":
            client_id = str(args.get("client_id") or "")
            if any(h.client_id == client_id for h in self.snapshot().hosts):
                self.ui = replace(self.ui, confirm={"kind": "revoke", "client_id": client_id})
        elif kind == "purge" and self.ui.page == "delete":
            _, selector = self._preview(self.snapshot())
            if selector is not None:  # the upper bound is fixed now, like `penpal purge`
                self.ui = replace(self.ui, confirm={"kind": "purge", "selector": selector})
        return False

    def _do_confirm_cancel(self, args: dict) -> bool:
        self.ui = replace(self.ui, confirm=None)
        return False

    def _do_confirm_ok(self, args: dict) -> bool:
        confirm, self.ui = self.ui.confirm, replace(self.ui, confirm=None)
        if not confirm:
            return False
        if confirm.get("kind") == "revoke":
            from penpal.mcp import auth

            count = auth.revoke_grants(confirm["client_id"])
            self.ui = replace(self.ui, page="")
            self._say("已撤销授权" if count else "没有可撤销的授权")
            return True
        if confirm.get("kind") == "purge":
            from penpal.services import purge

            counts = purge.run_purge(confirm["selector"])
            self.ui = replace(self.ui, page="", delete_scope="today")
            self._say(f"已删除 {counts.get('raw_events', 0)} 条记录")
            return True
        return False

    # recovery
    def _do_daemon_restart(self, args: dict) -> bool:
        from penpal.runtime.control import restart

        self._spawn(lambda: restart("hud"))
        self._say("正在重启后台服务")
        return False

    def _do_settings_open(self, args: dict) -> bool:
        from penpal import permissions

        pane = args.get("pane")
        if pane == "full_disk":
            url = permissions.FULL_DISK_ACCESS_PANE
        else:
            spec = permissions.spec_by_key(str(pane)) if pane in {i.key for i in self.snapshot().perm_issues} else None
            if spec is None:
                return False
            url = spec.settings_pane
        permissions.open_settings_url(url)
        return False

    def _do_health_recheck(self, args: dict) -> bool:
        local_now = self._clock().astimezone()
        now_str = local_now.strftime("%H:%M:%S")
        self.ui = replace(self.ui, rechecked_at=now_str)
        self._snap = None
        self._snap_at = 0.0
        return True

    def _do_issue_repair(self, args: dict) -> bool:
        from penpal.services.repair import repair

        issue_code = str(args.get("issue_code") or "")
        self._say("正在修复…")
        if self._push_model is not None:
            try:
                self._push_model()
            except Exception as exc:
                LOGGER.warning("hud intermediate push_model failed: %s", type(exc).__name__)
        result = repair(issue_code)
        self._say(result.message_zh)
        local_now = self._clock().astimezone()
        now_str = local_now.strftime("%H:%M:%S")
        self.ui = replace(self.ui, rechecked_at=now_str)
        self._snap = None
        self._snap_at = 0.0
        return True

    def _do_hud_quit(self, args: dict) -> bool:
        if self._quit is not None:
            self._quit()
        return False
