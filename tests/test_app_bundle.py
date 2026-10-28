"""`penpal app install / uninstall`: builds a launcher bundle in a tmp dir; codesign and launchctl are mocked.

Never touches the real ~/Applications or ~/Library/LaunchAgents.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from penpal import app_bundle
from penpal.app_bundle import AppBundleError, build_native_launcher, install_app, stable_install_path, uninstall_app
from penpal.cli import main


class Recorder:
    """Stands in for subprocess.run: records commands, answers success unless told otherwise."""

    def __init__(self, fail=()):
        self.cmds: list[list[str]] = []
        self.fail = tuple(fail)

    def __call__(self, cmd, **kwargs):
        self.cmds.append(list(cmd))
        code = 1 if any(cmd[:2] == list(f) or cmd[0] == f[0] and f[1] in cmd for f in self.fail) else 0
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="boom" if code else "")


@pytest.fixture()
def dirs(tmp_path):
    return tmp_path / "Applications", tmp_path / "LaunchAgents"


EXE = Path("/opt/homebrew/bin/penpal")


def _install(*args, **kwargs):
    kwargs.setdefault("clang_path", "")
    kwargs.setdefault("codesign_available", True)
    return install_app(*args, **kwargs)


def test_install_builds_a_launcher_bundle(dirs):
    apps, agents = dirs
    run = Recorder()
    result = _install(EXE, apps_dir=apps, agents_dir=agents, run=run)
    bundle = apps / "Penpal.app"
    assert result.bundle == bundle and bundle.is_dir()
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "com.penpal.app" and info["LSUIElement"] is True
    assert info["CFBundleExecutable"] == "Penpal" and info["CFBundleShortVersionString"] == "1"
    assert not [k for k in info if k.startswith("NS") and k.endswith("UsageDescription")]  # needs no permission of its own
    script = bundle / "Contents" / "MacOS" / "Penpal"
    assert os.access(script, os.X_OK)
    assert script.read_text() == "#!/bin/sh\nexec /opt/homebrew/bin/penpal hud\n"
    assert any("may appear as Python" in action for action in result.actions)
    assert run.cmds == [["codesign", "-s", "-", "--force", str(bundle)]]  # ad hoc signature, no launchctl without --login
    assert not agents.exists()


def test_executable_path_with_spaces_is_quoted(dirs):
    apps, _ = dirs
    _install(Path("/Users/some one/bin/penpal"), apps_dir=apps, run=Recorder())
    assert (apps / "Penpal.app/Contents/MacOS/Penpal").read_text().splitlines()[1] == "exec '/Users/some one/bin/penpal' hud"


def test_login_registers_the_hud_launch_agent(dirs):
    apps, agents = dirs
    run = Recorder()
    result = _install(EXE, apps_dir=apps, agents_dir=agents, login=True, run=run)
    plist = plistlib.loads((agents / "com.penpal.hud.plist").read_bytes())
    assert plist["Label"] == "com.penpal.hud" and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [str(apps / "Penpal.app/Contents/MacOS/Penpal")]
    verbs = [c[1] for c in run.cmds if c[0] == "launchctl"]
    assert "bootstrap" in verbs and verbs.index("bootout") < verbs.index("bootstrap")
    assert any("login item" in a for a in result.actions)


def test_reinstall_replaces_our_bundle_but_never_a_foreign_one(dirs):
    apps, _ = dirs
    _install(EXE, apps_dir=apps, run=Recorder())
    _install(EXE, apps_dir=apps, run=Recorder())
    assert plistlib.loads((apps / "Penpal.app/Contents/Info.plist").read_bytes())["CFBundleShortVersionString"] == "1"

    foreign = apps / "Penpal.app"
    (foreign / "Contents").mkdir(parents=True, exist_ok=True)
    (foreign / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.someone.else"}))
    with pytest.raises(AppBundleError, match="not a Penpal app"):
        _install(EXE, apps_dir=apps, run=Recorder())
    assert (foreign / "Contents" / "Info.plist").exists()
    with pytest.raises(AppBundleError):
        uninstall_app(apps_dir=apps, run=Recorder())
    assert foreign.exists()


def test_codesign_failure_leaves_nothing_behind(dirs):
    apps, _ = dirs
    with pytest.raises(AppBundleError, match="codesign failed"):
        _install(EXE, apps_dir=apps, run=Recorder(fail=[("codesign", "codesign")]))
    assert not (apps / "Penpal.app").exists()


def test_launchctl_failure_is_reported(dirs):
    apps, agents = dirs
    with pytest.raises(AppBundleError, match="launchctl"):
        _install(EXE, apps_dir=apps, agents_dir=agents, login=True, run=Recorder(fail=[("launchctl", "bootstrap"), ("launchctl", "load")]))


def test_uninstall_removes_bundle_and_agent_only(dirs):
    apps, agents = dirs
    _install(EXE, apps_dir=apps, agents_dir=agents, login=True, run=Recorder())
    (apps / "Other.app").mkdir()
    run = Recorder()
    result = uninstall_app(apps_dir=apps, agents_dir=agents, run=run)
    assert not (apps / "Penpal.app").exists() and not (agents / "com.penpal.hud.plist").exists()
    assert (apps / "Other.app").exists()
    assert any(c[:2] == ["launchctl", "bootout"] for c in run.cmds)
    assert len(result.actions) == 2
    assert uninstall_app(apps_dir=apps, agents_dir=agents, run=Recorder()).actions == []  # idempotent


def test_cli_install_and_uninstall_in_tmp_dirs(dirs, monkeypatch):
    apps, agents = dirs
    run = Recorder()
    monkeypatch.setattr(app_bundle, "_run", run)
    monkeypatch.setattr(app_bundle.shutil, "which", lambda name: "/usr/bin/codesign" if name == "codesign" else None)
    monkeypatch.setattr("penpal.cli.app._penpal_executable_path", lambda: EXE)
    out = CliRunner().invoke(main, ["app", "install", "--login", "--dir", str(apps), "--agents-dir", str(agents)])
    assert out.exit_code == 0, out.output
    assert (apps / "Penpal.app/Contents/Info.plist").exists() and (agents / "com.penpal.hud.plist").exists()
    text = " ".join(out.output.split())  # rich wraps lines at the terminal width
    assert "授予 Penpal" in text or "grant capture permissions to Penpal" in text
    out = CliRunner().invoke(main, ["app", "uninstall", "--dir", str(apps), "--agents-dir", str(agents)])
    assert out.exit_code == 0, out.output
    assert not (apps / "Penpal.app").exists() and not (agents / "com.penpal.hud.plist").exists()
    assert all(c[0] in {"codesign", "launchctl"} for c in run.cmds)


def test_cli_reports_errors_without_a_traceback(dirs, monkeypatch):
    apps, agents = dirs
    monkeypatch.setattr("penpal.cli.app._penpal_executable_path", lambda: EXE)
    monkeypatch.setattr(app_bundle.shutil, "which", lambda name: "/usr/bin/codesign" if name == "codesign" else None)
    (apps / "Penpal.app" / "Contents").mkdir(parents=True)
    out = CliRunner().invoke(main, ["app", "install", "--dir", str(apps), "--agents-dir", str(agents)])
    assert out.exit_code != 0 and "not a Penpal app" in out.output and "Traceback" not in out.output


def test_default_locations_are_the_users_not_the_repos(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert app_bundle.default_apps_dir() == tmp_path / "Applications"
    assert app_bundle.default_agents_dir() == tmp_path / "Library" / "LaunchAgents"


def test_missing_clang_uses_bilingual_python_fallback(dirs):
    apps, _agents = dirs
    result = install_app(EXE, apps_dir=apps, run=Recorder(), clang_path="", codesign_available=False)
    launcher = apps / "Penpal.app/Contents/MacOS/Penpal"
    assert launcher.read_text(encoding="utf-8").startswith("#!/bin/sh\nexec ")
    fallback = next(action for action in result.actions if "Native launcher unavailable" in action)
    assert "菜单栏项目会显示为 Python" in fallback and "may appear as Python" in fallback


@pytest.mark.skipif(sys.platform != "darwin", reason="native launcher produces Mach-O only on macOS")
def test_native_launcher_builds_a_macho_bundle_without_executing_it(dirs):
    clang = shutil.which("clang")
    if not clang:
        pytest.skip("clang is unavailable")
    apps, _agents = dirs
    result = install_app(EXE, apps_dir=apps, clang_path=clang, codesign_available=False)
    fallback = next((action for action in result.actions if "Native launcher unavailable" in action), None)
    if fallback:
        pytest.skip(fallback)
    bundle = apps / "Penpal.app"
    binary = bundle / "Contents/MacOS/Penpal"
    assert binary.is_file() and os.access(binary, os.X_OK)
    assert binary.read_bytes()[:4] in {
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    }
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Penpal" and info["CFBundleIdentifier"] == "com.penpal.app"
    assert info["LSUIElement"] is True
    assert not [key for key in info if key.startswith("NS") and key.endswith("UsageDescription")]


def test_idempotent_install_skips_rebuild_and_codesign(dirs, tmp_path):
    apps, agents = dirs
    stamp_file = tmp_path / "launcher.json"
    run = Recorder()
    res1 = _install(EXE, apps_dir=apps, agents_dir=agents, run=run, stamp_path=stamp_file)
    assert res1.launcher_state in {"built", "fallback"}
    bundle = apps / "Penpal.app"
    mtime = (bundle / "Contents" / "Info.plist").stat().st_mtime_ns
    codesign_calls1 = [cmd for cmd in run.cmds if cmd[0] == "codesign"]
    assert len(codesign_calls1) == 1

    # Second install with same inputs
    run2 = Recorder()
    res2 = _install(EXE, apps_dir=apps, agents_dir=agents, run=run2, stamp_path=stamp_file)
    assert res2.launcher_state == "unchanged"
    assert (bundle / "Contents" / "Info.plist").stat().st_mtime_ns == mtime
    codesign_calls2 = [cmd for cmd in run2.cmds if cmd[0] == "codesign"]
    assert len(codesign_calls2) == 0


def test_changing_main_c_or_python_minor_rebuilds(dirs, tmp_path, monkeypatch):
    apps, agents = dirs
    stamp_file = tmp_path / "launcher.json"
    run = Recorder()
    _install(EXE, apps_dir=apps, agents_dir=agents, run=run, stamp_path=stamp_file)

    # Change main.c
    monkeypatch.setattr(app_bundle, "launcher_source_bytes", lambda: b"modified main.c bytes")
    run_change = Recorder()
    res_changed = _install(EXE, apps_dir=apps, agents_dir=agents, run=run_change, stamp_path=stamp_file)
    assert res_changed.launcher_state in {"rebuilt", "fallback"}
    assert any(cmd[0] == "codesign" for cmd in run_change.cmds)

    # Change python minor version
    monkeypatch.setattr(app_bundle.sys, "version_info", (3, 99))
    run_change2 = Recorder()
    res_changed2 = _install(EXE, apps_dir=apps, agents_dir=agents, run=run_change2, stamp_path=stamp_file)
    assert res_changed2.launcher_state in {"rebuilt", "fallback"}
    assert any(cmd[0] == "codesign" for cmd in run_change2.cmds)


def test_app_uninstall_keeps_bundle_when_daemon_plist_uses_it(dirs, tmp_path):
    apps, agents = dirs
    res = _install(EXE, apps_dir=apps, agents_dir=agents, run=Recorder())
    bundle = res.bundle
    launcher = bundle / "Contents" / "MacOS" / "Penpal"

    daemon_plist = tmp_path / "com.penpal.daemon.plist"
    daemon_plist.write_bytes(plistlib.dumps({
        "Label": "com.penpal.daemon",
        "ProgramArguments": [str(launcher), "serve"],
    }))

    unres = uninstall_app(apps_dir=apps, agents_dir=agents, daemon_plist_path=daemon_plist, run=Recorder())
    assert bundle.exists()
    assert any("后台服务正在使用" in a or "Preserving" in a for a in unres.actions)


def test_self_reload_exec_target(monkeypatch, tmp_path):
    from penpal import app

    calls = []
    monkeypatch.setattr(app.os, "execv", lambda exe, args: calls.append((exe, args)))
    monkeypatch.setattr("penpal.foundation.runtime_plist.resolve_penpal_executable", lambda: Path("/opt/penpal"))

    launcher_bin = tmp_path / "Penpal"
    launcher_bin.write_text("bin")

    # Started from the launcher -> re-enter through it
    monkeypatch.setattr("penpal.foundation.native_launcher._LAUNCHER", str(launcher_bin))
    with pytest.raises(SystemExit):
        app._reload_in_place()
    assert calls[-1] == (str(launcher_bin), [str(launcher_bin), "serve"])

    # Launcher gone -> stable entry
    monkeypatch.setattr("penpal.foundation.native_launcher._LAUNCHER", str(tmp_path / "nonexistent"))
    with pytest.raises(SystemExit):
        app._reload_in_place()
    assert calls[-1] == ("/opt/penpal", ["/opt/penpal", "serve"])


def test_legacy_hud_finder_ignores_daemon_serve():
    from penpal.runtime.control import find_legacy_hud_pids

    def fake_runner(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, stdout="100\n200\n", stderr="")
        if cmd[0] == "ps":
            pid = cmd[2]
            if pid == "100":
                return subprocess.CompletedProcess(cmd, 0, stdout="/Applications/Penpal.app/Contents/MacOS/Penpal serve\n", stderr="")
            if pid == "200":
                return subprocess.CompletedProcess(cmd, 0, stdout="/usr/local/bin/penpal hud\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    pids = find_legacy_hud_pids(runner=fake_runner, current_pid=1, active_pid_finder=lambda: None)
    assert pids == [200]
    assert 100 not in pids


def test_stable_install_path_prefers_the_homebrew_opt_link(tmp_path):
    cellar = tmp_path / "Cellar/penpal/0.4.1/libexec/bin/python"
    opt = tmp_path / "opt/penpal/libexec/bin/python"
    for path in (cellar, opt):
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    assert stable_install_path(cellar) == opt
    assert stable_install_path(tmp_path / "venv/bin/python") == tmp_path / "venv/bin/python"
    opt.unlink()
    assert stable_install_path(cellar) == cellar  # no opt link: keep the real path


@pytest.mark.skipif(sys.platform != "darwin", reason="native launcher produces Mach-O only on macOS")
def test_native_launcher_falls_back_to_the_penpal_command_when_its_python_is_gone(tmp_path):
    clang = shutil.which("clang")
    if not clang:
        pytest.skip("clang is unavailable")
    argv_log = tmp_path / "argv.txt"
    # A separate directory: on case-insensitive APFS "penpal" and "Penpal" would be the same file.
    (tmp_path / "bin").mkdir()
    fallback = tmp_path / "bin/penpal"
    fallback.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {argv_log}\n', encoding="utf-8")
    fallback.chmod(0o755)
    binary = tmp_path / "Penpal"
    built, reason = build_native_launcher(
        binary, python_executable=tmp_path / "removed-by-upgrade/bin/python",
        fallback_executable=fallback, compiler_path=clang,
    )
    if not built:
        pytest.skip(reason)
    # Runs only the stub's fallback branch (a shell script here), never the HUD.
    completed = subprocess.run([str(binary), "--flag"], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert argv_log.read_text(encoding="utf-8").split() == ["hud", "--flag"]


def test_login_agent_registers_through_the_real_runner(tmp_path, monkeypatch):
    # The launchctl helpers pass subprocess.run options themselves; the bundle's runner must accept them
    # (it used to set them too, so `penpal app install --login` died with a duplicate-keyword TypeError).
    calls = []

    def fake_subprocess_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(app_bundle.subprocess, "run", fake_subprocess_run)
    plist = tmp_path / "com.penpal.hud.plist"
    plist.write_bytes(b"")

    app_bundle._bootout(plist, app_bundle._run)
    assert app_bundle._bootstrap(plist, app_bundle._run)

    launchctl = [kwargs for cmd, kwargs in calls if cmd[0] == "launchctl"]
    assert launchctl and all(kw["check"] is False and kw["text"] is True and "timeout" in kw for kw in launchctl)
