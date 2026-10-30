from __future__ import annotations

import ctypes
import os
import plistlib
from functools import lru_cache
from pathlib import Path

from AppKit import NSBundle, NSURL
import objc

from penpal.capture.watchers.browser import DEFAULT_SUPPORTED_BROWSERS
from penpal.utils.logging import get_logger

logger = get_logger("watcher.browser_discovery")

_APP_ROOTS = (
    Path("/Applications"),
    Path("/System/Applications"),
    Path.home() / "Applications",
)
_HTTPS_SCHEME = "https"
_LS_ROLES_ALL = 0xFFFFFFFF

try:
    _CORE_SERVICES = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    _CORE_SERVICES.LSCopyApplicationURLsForURL.restype = ctypes.c_void_p
    _CORE_SERVICES.LSCopyApplicationURLsForURL.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _CORE_SERVICES.CFArrayGetCount.restype = ctypes.c_long
    _CORE_SERVICES.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    _CORE_SERVICES.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    _CORE_SERVICES.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    _CORE_SERVICES.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
    _CORE_SERVICES.CFURLGetFileSystemRepresentation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_long,
    ]
    _CORE_SERVICES.CFRelease.argtypes = [ctypes.c_void_p]
except Exception:
    _CORE_SERVICES = None


def _cfurl_to_path(url_ref: int) -> str | None:
    if _CORE_SERVICES is None or not url_ref:
        return None
    buffer = ctypes.create_string_buffer(8192)
    ok = _CORE_SERVICES.CFURLGetFileSystemRepresentation(
        ctypes.c_void_p(url_ref),
        True,
        buffer,
        len(buffer),
    )
    if not ok:
        return None
    path = os.fsdecode(buffer.value).strip()
    return path or None


def LSCopyApplicationURLsForURL(url: NSURL | str, role_mask: int = _LS_ROLES_ALL) -> tuple[NSURL, ...]:
    if _CORE_SERVICES is None:
        raise RuntimeError("CoreServices.framework is unavailable")

    url_obj = url
    if isinstance(url, str):
        url_obj = NSURL.URLWithString_(url)
    if url_obj is None:
        raise ValueError("url must be a valid NSURL or URL string")

    raw_array = _CORE_SERVICES.LSCopyApplicationURLsForURL(
        ctypes.c_void_p(int(objc.pyobjc_id(url_obj))),
        ctypes.c_uint32(role_mask),
    )
    if not raw_array:
        return ()

    try:
        count = _CORE_SERVICES.CFArrayGetCount(ctypes.c_void_p(raw_array))
        app_urls: list[NSURL] = []
        for index in range(count):
            item = _CORE_SERVICES.CFArrayGetValueAtIndex(ctypes.c_void_p(raw_array), index)
            path = _cfurl_to_path(int(item))
            if not path:
                continue
            app_url = NSURL.fileURLWithPath_(path)
            if app_url is not None:
                app_urls.append(app_url)
        return tuple(app_urls)
    finally:
        _CORE_SERVICES.CFRelease(ctypes.c_void_p(raw_array))


def _scheme_list_from_bundle_info(info: dict) -> set[str]:
    schemes: set[str] = set()
    for entry in info.get("CFBundleURLTypes") or []:
        if not isinstance(entry, dict):
            continue
        for scheme in entry.get("CFBundleURLSchemes") or []:
            normalized = str(scheme or "").strip().lower()
            if normalized:
                schemes.add(normalized)
    return schemes


def _bundle_has_https_handler(bundle_path: Path) -> bool:
    info_path = bundle_path / "Contents" / "Info.plist"
    if not info_path.is_file():
        return False
    try:
        info = plistlib.loads(info_path.read_bytes())
    except Exception:
        return False
    return _HTTPS_SCHEME in _scheme_list_from_bundle_info(info)


def _discover_application_urls_from_filesystem() -> tuple[NSURL, ...]:
    discovered: list[NSURL] = []
    seen_paths: set[str] = set()

    for root in _APP_ROOTS:
        if not root.is_dir():
            continue
        try:
            bundle_paths = sorted(root.glob("*.app"))
        except Exception as exc:
            logger.debug("discover application urls from filesystem failed: %s", type(exc).__name__)
            continue
        for bundle_path in bundle_paths:
            if not bundle_path.is_dir() or not _bundle_has_https_handler(bundle_path):
                continue
            normalized_path = str(bundle_path.resolve())
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            app_url = NSURL.fileURLWithPath_(normalized_path)
            if app_url is not None:
                discovered.append(app_url)

    return tuple(discovered)


def _discover_application_urls_from_launch_services() -> tuple[NSURL, ...]:
    if _CORE_SERVICES is None:
        return ()
    try:
        https_url = NSURL.URLWithString_("https://example.com")
        if https_url is None:
            return ()
        return tuple(LSCopyApplicationURLsForURL(https_url, _LS_ROLES_ALL))
    except Exception as exc:
        logger.debug("LaunchServices URL discovery failed: %s", exc)
        return ()


def _bundle_display_name(bundle_url: NSURL) -> str | None:
    path = str(bundle_url.path() or "").strip()
    if path:
        bundle_name = Path(path).name
        if bundle_name.endswith(".app"):
            bundle_name = bundle_name[:-4]
        if bundle_name:
            return bundle_name

    bundle = NSBundle.bundleWithURL_(bundle_url)
    if bundle is None:
        return None

    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = bundle.objectForInfoDictionaryKey_(key)
        name = str(value or "").strip()
        if name:
            return name
    return None


def _bundle_path_key(bundle_url: NSURL) -> str | None:
    path = str(bundle_url.path() or "").strip()
    if not path:
        return None
    return str(Path(path).expanduser().resolve())


@lru_cache(maxsize=1)
def discover_http_handler_apps() -> tuple[str, ...]:
    urls: list[NSURL] = []
    seen_paths: set[str] = set()
    for app_url in (
        *_discover_application_urls_from_launch_services(),
        *_discover_application_urls_from_filesystem(),
    ):
        path_key = _bundle_path_key(app_url)
        if path_key and path_key in seen_paths:
            continue
        if path_key:
            seen_paths.add(path_key)
        urls.append(app_url)

    discovered: list[str] = []
    seen_names: set[str] = set()
    for app_url in urls:
        name = _bundle_display_name(app_url)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        discovered.append(name)

    if discovered:
        return tuple(discovered)

    logger.warning(
        "browser_url discovery failed; falling back to default supported browsers"
    )
    return DEFAULT_SUPPORTED_BROWSERS
