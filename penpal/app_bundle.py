"""Optional desktop App install (D33): `penpal app install` / `penpal app uninstall`.

The bundle contains a small native executable that embeds the installed Penpal environment's Python and
runs `python -m penpal hud`. If clang or libpython is unavailable, it falls back to the legacy script
launcher. The daemon keeps every macOS permission (TCC), and the bundle declares none.

Every location is a parameter (apps dir, LaunchAgents dir, subprocess runner) so tests run entirely in tmp dirs.
"""
from __future__ import annotations

import json
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
from typing import Callable, Optional

from penpal.foundation.runtime_launchd import LAUNCHCTL_TIMEOUT_SEC
from penpal.runtime.control import load_job, unload_job

from penpal.utils.paths import get_apps_dir, get_launchd_agents_dir

APP_NAME = "Penpal.app"
BUNDLE_ID = "com.penpal.app"
EXECUTABLE_NAME = "Penpal"
HUD_LABEL = "com.penpal.hud"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class AppBundleError(Exception):
    """A readable, user-facing failure (the CLI prints it as is)."""


@dataclass
class AppResult:
    bundle: Path
    actions: list[str] = field(default_factory=list)


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


def info_plist(version: str) -> dict:
    return {
        "CFBundleName": "Penpal",
        "CFBundleDisplayName": "Penpal",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": version,
        "CFBundleShortVersionString": version,
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
            interpreter = str(stable_install_path(python_executable or Path(sys.executable)))
            fallback = str(fallback_executable or shutil.which("penpal") or "penpal")
            header.write_text(
                f"#define PENPAL_PYTHON_EXECUTABLE {json.dumps(interpreter)}\n"
                f"#define PENPAL_FALLBACK_EXECUTABLE {json.dumps(fallback)}\n",
                encoding="utf-8",
            )
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
    version: str,
    apps_dir: Optional[Path] = None,
    agents_dir: Optional[Path] = None,
    login: bool = False,
    run: Optional[Runner] = None,
    clang_path: str | None = None,
    codesign_available: bool | None = None,
    compiler_runner: Callable[..., "subprocess.CompletedProcess[str]"] | None = None,
) -> AppResult:
    """Build `<apps_dir>/Penpal.app`, ad-hoc sign it, and (with ``login``) register the HUD launch agent."""
    run = run or _run
    bundle = (apps_dir or default_apps_dir()) / APP_NAME
    result = AppResult(bundle)
    if bundle.exists():
        if not _is_ours(bundle):
            raise AppBundleError(f"{bundle} exists and is not a Penpal app; move it away or use --dir.")
        shutil.rmtree(bundle)  # rebuild from scratch so a stale signature or script cannot linger
        result.actions.append("replaced existing app")

    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info_plist(version)))
    launcher = macos / EXECUTABLE_NAME
    native, reason = build_native_launcher(
        launcher,
        python_executable=Path(sys.executable),
        fallback_executable=executable,
        compiler_path=clang_path,
        compiler_runner=compiler_runner,
    )
    if native:
        result.actions.append("built native Python launcher")
    else:
        launcher.write_text(launch_script(executable), encoding="utf-8")
        launcher.chmod(0o755)
        result.actions.append("built script launcher")
        result.actions.append(
            f"未构建原生启动器（{reason}），菜单栏项目会显示为 Python / "
            f"Native launcher unavailable ({reason}); the menu bar item may appear as Python."
        )
    result.actions.append(f"built {bundle}")

    has_codesign = shutil.which("codesign") is not None if codesign_available is None else codesign_available
    if has_codesign:
        signed = run(["codesign", "-s", "-", "--force", str(bundle)])
        if signed.returncode != 0:
            shutil.rmtree(bundle, ignore_errors=True)
            raise AppBundleError("codesign failed; the app was not installed. " + (signed.stderr or "").strip()[:200])
        result.actions.append("signed ad hoc")
    else:
        result.actions.append("codesign unavailable; bundle is unsigned")

    if login:
        agents = agents_dir or default_agents_dir()
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / f"{HUD_LABEL}.plist"
        plist.write_bytes(plistlib.dumps(hud_agent_plist(bundle), sort_keys=False))
        _bootout(plist, run)
        if not _bootstrap(plist, run):
            raise AppBundleError(f"wrote {plist} but could not register the login item; retry with `penpal app install --login`.")
        result.actions.append(f"login item registered ({HUD_LABEL})")
    return result


def uninstall_app(
    *,
    apps_dir: Optional[Path] = None,
    agents_dir: Optional[Path] = None,
    run: Optional[Runner] = None,
) -> AppResult:
    """Remove the login agent and the app bundle. Only a bundle with our identifier is deleted."""
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
        shutil.rmtree(bundle)
        result.actions.append(f"removed {bundle}")
    return result


def _bootout(plist: Path, run: Runner) -> None:
    unload_job(plist.stem, plist, runner=lambda command, **kwargs: run(command, **kwargs))


def _bootstrap(plist: Path, run: Runner) -> bool:
    return load_job(plist.stem, plist, runner=lambda command, **kwargs: run(command, **kwargs), spawn=False)
