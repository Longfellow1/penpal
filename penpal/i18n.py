from __future__ import annotations

import locale as _locale

from penpal.utils.env import get_env_compat

import logging
logger = logging.getLogger(__name__)

_LANG_CACHE: str | None = None


def detect_lang() -> str:
    """Detect language. Priority: PENPAL_LANG > LANG/LC_ALL > system locale > en."""
    explicit = (get_env_compat("PENPAL_LANG", "") or "").strip().lower()
    if explicit in {"zh", "en"}:
        return explicit

    import os

    lang_env = os.environ.get("LANG", "") or os.environ.get("LC_ALL", "")
    if "zh" in lang_env.lower():
        return "zh"

    try:
        sys_locale = (_locale.getlocale()[0] or "").lower()
        if "zh" in sys_locale or "chinese" in sys_locale:
            return "zh"
    except Exception as exc:
        logger.debug("detect lang failed: %s", type(exc).__name__)
        pass

    return "en"


def current_lang() -> str:
    """Get cached language detection result."""
    global _LANG_CACHE
    if _LANG_CACHE is None:
        _LANG_CACHE = detect_lang()
    return _LANG_CACHE


def T(zh: str, en: str) -> str:
    """Bilingual helper. Chinese on zh locale, English otherwise."""
    return zh if current_lang() == "zh" else en


# Backward-compat alias expected by existing CLI/module code.
CLI_LANG = current_lang()
