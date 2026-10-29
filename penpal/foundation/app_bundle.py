"""Shared discovery for the installed Penpal.app bundle."""
from __future__ import annotations

import plistlib
from pathlib import Path

from penpal.utils.paths import get_apps_dir

APP_NAME = "Penpal.app"
BUNDLE_ID = "com.penpal.app"


def is_our_bundle(bundle: Path) -> bool:
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    return info.get("CFBundleIdentifier") == BUNDLE_ID


def find_installed_app(apps_dir: Path | None = None) -> Path | None:
    """Our Penpal.app if it is installed, else None."""
    target_dir = apps_dir or get_apps_dir()
    bundle = target_dir / APP_NAME
    return bundle if bundle.is_dir() and is_our_bundle(bundle) else None
