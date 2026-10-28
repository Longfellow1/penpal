from __future__ import annotations

import pytest

from penpal.foundation.runtime_identity import RuntimeIdentity
from penpal.hud.reload import HUDReloader, hud_exec_command


def test_hud_identity_change_execs_original_command_with_injected_exec():
    started = RuntimeIdentity("code-a", "1.0", "config-a", 34, 34)
    changed = RuntimeIdentity("code-b", "1.0", "config-a", 34, 34)
    current = [started]
    clock = [0.0]
    calls = []
    reloader = HUDReloader(
        started,
        lambda: current[0],
        execv=lambda executable, argv: calls.append((executable, list(argv))),
        executable="/stable/penpal",
        argv=["/stable/penpal", "hud"],
        interval_sec=2,
        clock=lambda: clock[0],
    )

    clock[0] = 2
    current[0] = changed
    assert reloader.check() is True
    reloader.exec()

    assert calls == [("/stable/penpal", ["/stable/penpal", "hud"])]


def test_native_bundle_reload_execs_penpal_binary_with_original_app_arguments():
    launcher = "/Applications/Penpal.app/Contents/MacOS/Penpal"
    executable, argv = hud_exec_command(launcher, ["/venv/bin/penpal", "hud", "--debug"])
    assert executable == launcher
    assert argv == [launcher, "--debug"]


def test_native_bundle_reload_spawns_relaunch_helper_and_does_not_exec():
    started = RuntimeIdentity("code-a", "1.0", "config-a", 34, 34)
    spawn_calls = []
    exec_calls = []
    launcher = "/X/Penpal.app/Contents/MacOS/Penpal"

    reloader = HUDReloader(
        started,
        lambda: started,
        launcher=launcher,
        spawn=lambda argv: spawn_calls.append(list(argv)),
        execv=lambda exe, argv: exec_calls.append((exe, list(argv))),
        pid=9876,
    )

    result = reloader.reload()

    assert result == "relaunch"
    assert spawn_calls == [[
        "/bin/sh",
        "-c",
        'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; exec /usr/bin/open "$2"',
        "sh",
        "9876",
        "/X/Penpal.app",
    ]]
    assert exec_calls == []


def test_native_bundle_spawn_raising_oserror_falls_back_to_execv_with_native_launcher_command():
    started = RuntimeIdentity("code-a", "1.0", "config-a", 34, 34)
    exec_calls = []
    launcher = "/X/Penpal.app/Contents/MacOS/Penpal"

    def fail_spawn(_argv):
        raise OSError("spawn failed")

    reloader = HUDReloader(
        started,
        lambda: started,
        launcher=launcher,
        executable=launcher,
        argv=[launcher],
        spawn=fail_spawn,
        execv=lambda exe, argv: exec_calls.append((exe, list(argv))),
        pid=9876,
    )

    result = reloader.reload()

    assert result == "exec"
    assert exec_calls == [(launcher, [launcher])]


def test_no_native_launcher_execs_in_place_without_calling_spawn():
    started = RuntimeIdentity("code-a", "1.0", "config-a", 34, 34)
    spawn_calls = []
    exec_calls = []

    reloader = HUDReloader(
        started,
        lambda: started,
        launcher=None,
        executable="/stable/penpal",
        argv=["/stable/penpal", "hud"],
        spawn=lambda argv: spawn_calls.append(list(argv)),
        execv=lambda exe, argv: exec_calls.append((exe, list(argv))),
    )

    result = reloader.reload()

    assert result == "exec"
    assert spawn_calls == []
    assert exec_calls == [("/stable/penpal", ["/stable/penpal", "hud"])]


def test_launcher_path_not_inside_dot_app_falls_back_to_execv():
    started = RuntimeIdentity("code-a", "1.0", "config-a", 34, 34)
    spawn_calls = []
    exec_calls = []
    launcher = "/usr/local/bin/penpal"

    reloader = HUDReloader(
        started,
        lambda: started,
        launcher=launcher,
        executable=launcher,
        argv=[launcher, "hud"],
        spawn=lambda argv: spawn_calls.append(list(argv)),
        execv=lambda exe, argv: exec_calls.append((exe, list(argv))),
    )

    result = reloader.reload()

    assert result == "exec"
    assert spawn_calls == []
    assert exec_calls == [(launcher, [launcher, "hud"])]


def test_default_spawner_refuses_under_penpal_isolated(monkeypatch):
    from penpal.hud.reload import _default_spawn

    monkeypatch.setenv("PENPAL_ISOLATED", "1")
    with pytest.raises(OSError):
        _default_spawn(["echo", "hi"])


def test_default_spawner_refuses_on_non_darwin(monkeypatch):
    from penpal.hud.reload import _default_spawn

    monkeypatch.delenv("PENPAL_ISOLATED", raising=False)
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(OSError):
        _default_spawn(["echo", "hi"])
