"""Optional desktop App install (D33): `penpal app install` / `penpal app uninstall`.

The bundle contains a small native executable that embeds the installed Penpal environment's Python and
runs `python -m penpal hud`. If clang or libpython is unavailable, it falls back to the legacy script
launcher. The daemon keeps every macOS permission (TCC), and the bundle declares none.

Every location is a parameter (apps dir, LaunchAgents dir, subprocess runner) so tests run entirely in tmp dirs.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from penpal.foundation.runtime_launchd import LAUNCHCTL_TIMEOUT_SEC
from penpal.runtime.control import load_job, unload_job

from penpal.utils.paths import get_apps_dir, get_launchd_agents_dir, get_launcher_stamp_path

APP_NAME = "Penpal.app"
BUNDLE_ID = "com.penpal.app"
EXECUTABLE_NAME = "Penpal"
HUD_LABEL = "com.penpal.hud"
BUNDLE_VERSION = "1"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
LauncherState = Literal["unchanged", "built", "rebuilt", "fallback"]


class AppBundleError(Exception):
    """A readable, user-facing failure (the CLI prints it as is)."""


@dataclass
class AppResult:
    bundle: Path
    actions: list[str] = field(default_factory=list)
    launcher_state: LauncherState = "fallback"
    native: bool = False


def default_apps_dir() -> Path:
    return get_apps_dir()


def default_agents_dir() -> Path:
    return get_launchd_agents_dir()


def _run(cmd: list[str], **kwargs) -> "subprocess.CompletedProcess[str]":
    if cmd and cmd[0] == "launchctl":
        kwargs.setdefault("timeout", LAUNCHCTL_TIMEOUT_SEC)
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kwargs)


def _is_ours(bundle: Path) -> bool:
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return info.get("CFBundleIdentifier") == BUNDLE_ID


def installed_app(apps_dir: Optional[Path] = None) -> Path | None:
    """Our Penpal.app if it is installed, else None."""
    bundle = (apps_dir or default_apps_dir()) / APP_NAME
    return bundle if _is_ours(bundle) else None


def open_app(bundle: Path) -> None:
    if os.environ.get("PENPAL_ISOLATED") == "1":
        raise OSError("opening apps is disabled in isolated environments")
    result = subprocess.run(["open", str(bundle)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise OSError((result.stderr or "open failed").strip()[:200])


def info_plist() -> dict:
    """Carries no package version: any byte change re-identifies the ad-hoc signed bundle and drops its grants."""
    return {
        "CFBundleName": "Penpal",
        "CFBundleDisplayName": "Penpal",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": BUNDLE_VERSION,
        "CFBundleShortVersionString": BUNDLE_VERSION,
        "LSUIElement": True,  # menu bar only, no Dock icon
        "NSHighResolutionCapable": True,
    }


def launch_script(executable: Path) -> str:
    return f"#!/bin/sh\nexec {shlex.quote(str(executable))} hud\n"


def _config_values() -> dict[str, str | None]:
    names = (
        "INCLUDEPY", "LIBDIR", "LIBPL", "LDLIBRARY", "PYTHONFRAMEWORK",
        "PYTHONFRAMEWORKPREFIX", "PYTHONFRAMEWORKDIR", "py_version_short", "LINKFORSHARED",
        "LIBS", "SYSLIBS",
    )
    values = {name: sysconfig.get_config_var(name) for name in names}
    values["INCLUDE"] = sysconfig.get_path("include")
    values["PLATINCLUDE"] = sysconfig.get_path("platinclude")
    return values


def _libpython_path(config: dict[str, str | None]) -> Path | None:
    candidates: list[Path] = []
    framework = config.get("PYTHONFRAMEWORK")
    framework_prefix = config.get("PYTHONFRAMEWORKPREFIX")
    framework_dir = config.get("PYTHONFRAMEWORKDIR") or (f"{framework}.framework" if framework else None)
    version = config.get("py_version_short")
    if framework and framework_prefix and framework_dir and version:
        candidates.append(Path(framework_prefix) / framework_dir / "Versions" / version / framework)
    library = config.get("LDLIBRARY")
    if library:
        for key in ("LIBDIR", "LIBPL"):
            if config.get(key):
                candidates.append(Path(config[key]) / library)
    for value in shlex.split(config.get("LINKFORSHARED") or ""):
        path = Path(value)
        if path.is_absolute():
            candidates.append(path)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


_CELLAR_RE = re.compile(r"^(?P<prefix>.*)/Cellar/(?P<name>[^/]+)/[^/]+/(?P<rest>.+)$")


def stable_install_path(path: Path) -> Path:
    """Map a versioned Homebrew Cellar path to its `opt` link, which survives `brew upgrade`."""
    match = _CELLAR_RE.match(str(path))
    if not match:
        return path
    stable = Path(match["prefix"]) / "opt" / match["name"] / match["rest"]
    return stable if stable.exists() else path


def compute_launcher_stamp(
    source_bytes: bytes,
    config_header_text: str,
    info_plist_bytes: bytes,
    python_minor_version: tuple[int, int] | str,
) -> str:
    h = hashlib.sha256()
    h.update(source_bytes)
    h.update(b"\x00")
    h.update(config_header_text.encode("utf-8"))
    h.update(b"\x00")
    h.update(info_plist_bytes)
    h.update(b"\x00")
    h.update(str(python_minor_version).encode("utf-8"))
    return h.hexdigest()


def read_launcher_stamp(stamp_path: Path | None = None) -> str | None:
    path = stamp_path or get_launcher_stamp_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("stamp")
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_launcher_stamp(stamp: str, stamp_path: Path | None = None) -> None:
    path = stamp_path or get_launcher_stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"stamp": stamp}, ensure_ascii=False)
        path.write_text(payload, encoding="utf-8")
    except OSError:
        pass


def remove_launcher_stamp(stamp_path: Path | None = None) -> bool:
    path = stamp_path or get_launcher_stamp_path()
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def launcher_source_bytes() -> bytes:
    source = Path(__file__).parent / "hud" / "launcher" / "main.c"
    if source.is_file():
        return source.read_bytes()
    return b""


def generate_launcher_config_header(
    python_executable: Path | None = None,
    fallback_executable: Path | None = None,
) -> str:
    interpreter = str(stable_install_path(python_executable or Path(sys.executable)))
    fallback = str(fallback_executable or shutil.which("penpal") or "penpal")
    return (
        f"#define PENPAL_PYTHON_EXECUTABLE {json.dumps(interpreter)}\n"
        f"#define PENPAL_FALLBACK_EXECUTABLE {json.dumps(fallback)}\n"
    )


def build_native_launcher(
    output: Path,
    *,
    python_executable: Path | None = None,
    fallback_executable: Path | None = None,
    compiler_path: str | None = None,
    config: dict[str, str | None] | None = None,
    compiler_runner: Callable[..., "subprocess.CompletedProcess[str]"] | None = None,
) -> tuple[bool, str]:
    """Compile the in-process Python launcher in a temporary directory, then copy it to *output*."""
    compiler = compiler_path if compiler_path is not None else shutil.which("clang")
    if not compiler:
        return False, "clang is unavailable"
    values = config or _config_values()
    include = values.get("INCLUDEPY") or values.get("INCLUDE")
    if not include or not Path(include).is_dir():
        return False, "Python headers are unavailable"
    library_path = _libpython_path(values)
    if library_path is None:
        return False, "libpython was not found from Python sysconfig"
    source = Path(__file__).parent / "hud" / "launcher" / "main.c"
    if not source.is_file():
        return False, "native launcher source is missing from the Penpal package"
    link_args = (
        shlex.split(values.get("LINKFORSHARED") or "")
        + shlex.split(values.get("LIBS") or "")
        + shlex.split(values.get("SYSLIBS") or "")
    )
    if str(library_path) not in link_args:
        link_args.append(str(library_path))
    library_root = values.get("PYTHONFRAMEWORKPREFIX") if values.get("PYTHONFRAMEWORK") else str(library_path.parent)
    if library_root:
        link_args.append(f"-Wl,-rpath,{library_root}")
    run_compiler = compiler_runner or subprocess.run
    try:
        with tempfile.TemporaryDirectory(prefix="penpal-hud-launcher-") as temp:
            temporary = Path(temp)
            header = temporary / "launcher_config.h"
            header_text = generate_launcher_config_header(python_executable, fallback_executable)
            header.write_text(header_text, encoding="utf-8")
            binary = temporary / "Penpal"
            command = [
                compiler, f"-I{include}", "-I" + str(values.get("PLATINCLUDE") or include),
                f"-I{temporary}", str(source), "-o", str(binary), *link_args,
            ]
            result = run_compiler(command, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "clang failed").strip().splitlines()
                return False, detail[-1][:200] if detail else "clang failed"
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, output)
            output.chmod(0o755)
    except OSError as exc:
        return False, f"native launcher build failed ({type(exc).__name__})"
    return True, ""


def hud_agent_plist(bundle: Path) -> dict:
    return {
        "Label": HUD_LABEL,
        "ProgramArguments": [str(bundle / "Contents" / "MacOS" / EXECUTABLE_NAME)],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
    }


def install_app(
    executable: Path,
    *,
    apps_dir: Optional[Path] = None,
    agents_dir: Optional[Path] = None,
    login: bool = False,
    run: Optional[Runner] = None,
    clang_path: str | None = None,
    codesign_available: bool | None = None,
    compiler_runner: Callable[..., "subprocess.CompletedProcess[str]"] | None = None,
    stamp_path: Path | None = None,
) -> AppResult:
    """Build `<apps_dir>/Penpal.app`, ad-hoc sign it, and (with ``login``) register the HUD launch agent.

    Idempotent: if the bundle exists, is ours, contains the launcher and matches the input stamp,
    it is left untouched (no rebuild, no re-sign).
    """
    run = run or _run
    bundle = (apps_dir or default_apps_dir()) / APP_NAME
    result = AppResult(bundle)
    macos = bundle / "Contents" / "MacOS"
    launcher = macos / EXECUTABLE_NAME
    info_file = bundle / "Contents" / "Info.plist"

    python_executable = Path(sys.executable)
    source_bytes = launcher_source_bytes()
    config_header_text = generate_launcher_config_header(python_executable, executable)
    plist_bytes = plistlib.dumps(info_plist())
    py_minor = sys.version_info[:2]

    current_stamp = compute_launcher_stamp(
        source_bytes=source_bytes,
        config_header_text=config_header_text,
        info_plist_bytes=plist_bytes,
        python_minor_version=py_minor,
    )

    if bundle.exists():
        if not _is_ours(bundle):
            raise AppBundleError(f"{bundle} exists and is not a Penpal app; move it away or use --dir.")
        saved_stamp = read_launcher_stamp(stamp_path)
        if launcher.is_file() and info_file.is_file() and saved_stamp == current_stamp:
            result.launcher_state = "unchanged"
            result.native = not launcher.read_bytes().startswith(b"#!/bin/sh")
            result.actions.append(f"bundle at {bundle} is up to date (identity unchanged)")
            if login:
                _ensure_login_agent(bundle, agents_dir, run, result)
            return result

        prior_existed = True
        shutil.rmtree(bundle)
        result.actions.append("replaced existing app")
    else:
        prior_existed = False

    macos.mkdir(parents=True)
    info_file.write_bytes(plist_bytes)
    native, reason = build_native_launcher(
        launcher,
        python_executable=python_executable,
        fallback_executable=executable,
        compiler_path=clang_path,
        compiler_runner=compiler_runner,
    )
    result.native = native
    if native:
        result.launcher_state = "rebuilt" if prior_existed else "built"
        result.actions.append("built native Python launcher")
    else:
        result.launcher_state = "fallback"
        launcher.write_text(launch_script(executable), encoding="utf-8")
        launcher.chmod(0o755)
        result.actions.append("built script launcher")
        result.actions.append(
            f"未构建原生启动器（{reason}），权限列表与菜单栏项目会显示为 Python / "
            f"Native launcher unavailable ({reason}); permission list and menu bar may appear as Python."
        )
    result.actions.append(f"built {bundle}")

    has_codesign = shutil.which("codesign") is not None if codesign_available is None else codesign_available
    if has_codesign:
        signed = run(["codesign", "-s", "-", "--force", str(bundle)])
        if signed.returncode != 0:
            shutil.rmtree(bundle, ignore_errors=True)
            remove_launcher_stamp(stamp_path)
            raise AppBundleError("codesign failed; the app was not installed. " + (signed.stderr or "").strip()[:200])
        result.actions.append("signed ad hoc")
    else:
        result.actions.append("codesign unavailable; bundle is unsigned")

    write_launcher_stamp(current_stamp, stamp_path)

    if login:
        _ensure_login_agent(bundle, agents_dir, run, result)
    return result


def _ensure_login_agent(bundle: Path, agents_dir: Path | None, run: Runner, result: AppResult) -> None:
    agents = agents_dir or default_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{HUD_LABEL}.plist"
    plist.write_bytes(plistlib.dumps(hud_agent_plist(bundle), sort_keys=False))
    _bootout(plist, run)
    if not _bootstrap(plist, run):
        raise AppBundleError(f"wrote {plist} but could not register the login item; retry with `penpal app install --login`.")
    result.actions.append(f"login item registered ({HUD_LABEL})")


def daemon_uses_bundle(bundle: Path, plist_path: Path | None = None) -> bool:
    """Check if the installed daemon plist has ProgramArguments pointing to the launcher in bundle."""
    if plist_path is None:
        from penpal.utils.paths import get_launchd_daemon_plist_path

        plist_path = get_launchd_daemon_plist_path()
    try:
        if not plist_path.is_file():
            return False
        payload = plistlib.loads(plist_path.read_bytes())
        args = payload.get("ProgramArguments") or []
        if args and isinstance(args[0], str):
            launcher = bundle / "Contents" / "MacOS" / EXECUTABLE_NAME
            try:
                target = Path(args[0]).resolve(strict=False)
                return target == launcher.resolve(strict=False)
            except OSError:
                return str(launcher) in args[0]
    except Exception:
        pass
    return False


def uninstall_app(
    *,
    apps_dir: Optional[Path] = None,
    agents_dir: Optional[Path] = None,
    run: Optional[Runner] = None,
    daemon_plist_path: Path | None = None,
    stamp_path: Path | None = None,
) -> AppResult:
    """Remove the login agent and the app bundle.

    If the daemon launchd plist points to the bundle launcher, the bundle is kept
    and a note is added to actions.
    """
    run = run or _run
    bundle = (apps_dir or default_apps_dir()) / APP_NAME
    result = AppResult(bundle)
    plist = (agents_dir or default_agents_dir()) / f"{HUD_LABEL}.plist"
    if plist.exists():
        _bootout(plist, run)
        plist.unlink()
        result.actions.append(f"removed {plist}")
    if bundle.exists():
        if not _is_ours(bundle):
            raise AppBundleError(f"{bundle} is not a Penpal app; leaving it alone.")
        if daemon_uses_bundle(bundle, daemon_plist_path):
            result.actions.append(
                f"保留 {bundle}（后台服务正在使用该启动器） / "
                f"Preserving {bundle} (in use by daemon launchd job)"
            )
        else:
            shutil.rmtree(bundle)
            remove_launcher_stamp(stamp_path)
            result.actions.append(f"removed {bundle}")
    return result


def _bootout(plist: Path, run: Runner) -> None:
    unload_job(plist.stem, plist, runner=lambda command, **kwargs: run(command, **kwargs))


def _bootstrap(plist: Path, run: Runner) -> bool:
    return load_job(plist.stem, plist, runner=lambda command, **kwargs: run(command, **kwargs), spawn=False)


def ensure_app_bundle(
    executable: Path,
    *,
    apps_dir: Optional[Path] = None,
    clang_path: str | None = None,
    codesign_available: bool | None = None,
    compiler_runner: Callable[..., "subprocess.CompletedProcess[str]"] | None = None,
    stamp_path: Path | None = None,
    run: Optional[Runner] = None,
) -> tuple[Path | None, LauncherState, AppResult]:
    """Ensure Penpal.app exists and its launcher is up to date.

    Returns (bundle_launcher_path if native else None, launcher_state, app_result).
    """
    res = install_app(
        executable=executable,
        apps_dir=apps_dir,
        login=False,
        run=run,
        clang_path=clang_path,
        codesign_available=codesign_available,
        compiler_runner=compiler_runner,
        stamp_path=stamp_path,
    )
    launcher = res.bundle / "Contents" / "MacOS" / EXECUTABLE_NAME
    native_launcher = launcher if res.native and launcher.is_file() else None
    return native_launcher, res.launcher_state, res
