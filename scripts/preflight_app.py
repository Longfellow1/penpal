#!/usr/bin/env python3
"""Preflight check for the built .app bundle via capability registry."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from penpal.capabilities.registry import build_default_registry


def fail(msg: str) -> None:
    print(f"[preflight] FAIL {msg}", file=sys.stderr)


def smoke_test_inner_binary(app_path: Path) -> str | None:
    """Run `<bundle>/Contents/MacOS/<exe> --help` and verify bootstrap succeeds.

    Why: py2app 0.28.10 + macOS 26 (Taheti) has a fatal interaction — any
    bootstrap error (missing module, broken __boot__) triggers py2app's
    `report_error()` → `ensureGUI()` → `NSApplication.sharedApplication()`
    which aborts inside `_RegisterApplication` on macOS 26 (see
    DiagnosticReports Penpal-2026-05-15-111349.ips). The crash dialog is
    never shown; the .app silently aborts. We catch this at install time
    by actually launching the inner binary with a known-good argv that
    exits 0 (so py2app stub never enters the error path).
    """
    macos_dir = app_path / "Contents" / "MacOS"
    candidates = [
        macos_dir / "Penpal",
        *(p for p in macos_dir.glob("*") if p.is_file() and p.name != "python"),
    ]
    inner = next((p for p in candidates if p.exists()), None)
    if inner is None:
        return f"smoke_test: no inner binary under {macos_dir}"

    try:
        result = subprocess.run(
            [str(inner), "--help"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "smoke_test: inner binary --help timed out (>20s)"
    except OSError as exc:
        return f"smoke_test: failed to exec inner binary: {exc}"

    if result.returncode != 0:
        snippet = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "\n  ".join(snippet[-8:]) if snippet else "<no output>"
        return f"smoke_test: inner --help exit={result.returncode}\n  {tail}"
    if "Usage:" not in (result.stdout or ""):
        return "smoke_test: inner --help stdout missing 'Usage:' marker"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: preflight_app.py <path/to/Penpal.app>", file=sys.stderr)
        return 2

    app_path = Path(sys.argv[1]).resolve()
    if not app_path.exists():
        fail(f"{app_path} does not exist")
        return 1

    registry = build_default_registry(app_path=app_path)
    results = registry.precheck_all()
    errors: list[str] = []
    for name, result in results:
        if result.ok:
            continue
        message = result.hint or result.code
        errors.append(f"{name}: {message}")

    smoke_err = smoke_test_inner_binary(app_path)
    if smoke_err:
        errors.append(smoke_err)

    if errors:
        for err in errors:
            fail(err)
        print(f"[preflight] {len(errors)} issue(s) — install aborted.", file=sys.stderr)
        return 1

    print(f"[preflight] OK {app_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
