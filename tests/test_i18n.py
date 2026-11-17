from __future__ import annotations

import pytest

import penpal.i18n as i18n
from penpal.config import Config, read_config, write_app_language


def _reset_lang_cache(monkeypatch):
    monkeypatch.setattr(i18n, "_LANG_CACHE", None)


def test_detect_lang_prefers_keypulse_lang_env(monkeypatch):
    monkeypatch.setenv("PENPAL_LANG", "zh")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    _reset_lang_cache(monkeypatch)

    assert i18n.detect_lang() == "zh"
    assert i18n.current_lang() == "zh"
    assert i18n.T("中文", "English") == "中文"


def test_detect_lang_uses_lang_env_when_keypulse_lang_missing(monkeypatch):
    monkeypatch.delenv("PENPAL_LANG", raising=False)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    _reset_lang_cache(monkeypatch)

    assert i18n.detect_lang() == "zh"


def test_detect_lang_defaults_to_en(monkeypatch):
    monkeypatch.delenv("PENPAL_LANG", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setattr(i18n._locale, "getlocale", lambda: (None, None))
    _reset_lang_cache(monkeypatch)

    assert i18n.detect_lang() == "en"
    assert i18n.T("中文", "English") == "English"


def test_detect_lang_reads_legacy_keypulse_env_with_notice(monkeypatch, capsys):
    monkeypatch.delenv("PENPAL_LANG", raising=False)
    monkeypatch.setenv("KEYPULSE_LANG", "zh")
    _reset_lang_cache(monkeypatch)

    assert i18n.detect_lang() == "zh"
    assert "Deprecated environment variable KEYPULSE_LANG; use PENPAL_LANG" in capsys.readouterr().err


def test_detect_lang_config_priority_and_fallbacks(monkeypatch):
    monkeypatch.delenv("PENPAL_LANG", raising=False)
    monkeypatch.delenv("KEYPULSE_LANG", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    _reset_lang_cache(monkeypatch)

    # 1. Config app.language = "zh" is preferred over LANG env
    monkeypatch.setattr(Config, "load", lambda: Config.model_validate({"app": {"language": "zh"}}))
    assert i18n.detect_lang() == "zh"

    # 2. PENPAL_LANG overrides config app.language
    monkeypatch.setenv("PENPAL_LANG", "en")
    _reset_lang_cache(monkeypatch)
    assert i18n.detect_lang() == "en"
    monkeypatch.delenv("PENPAL_LANG", raising=False)

    # 3. Config app.language = "en"
    _reset_lang_cache(monkeypatch)
    monkeypatch.setattr(Config, "load", lambda: Config.model_validate({"app": {"language": "en"}}))
    assert i18n.detect_lang() == "en"

    # 4. Config app.language = "auto" falls back to LANG env
    _reset_lang_cache(monkeypatch)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setattr(Config, "load", lambda: Config.model_validate({"app": {"language": "auto"}}))
    assert i18n.detect_lang() == "zh"

    # 5. Invalid config language falls back to auto -> LANG env
    _reset_lang_cache(monkeypatch)
    monkeypatch.setattr(Config, "load", lambda: Config.model_validate({"app": {"language": "unknown"}}))
    assert i18n.detect_lang() == "zh"

    # 6. Config load exception silently falls back to LANG env
    _reset_lang_cache(monkeypatch)
    monkeypatch.setattr(Config, "load", lambda: (_ for _ in ()).throw(RuntimeError("disk error")))
    assert i18n.detect_lang() == "zh"


def test_write_app_language_and_readback(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[app]\nname = "test"\n', encoding="utf-8")

    # Write zh and read back
    write_app_language(cfg_file, "zh")
    assert read_config(cfg_file).app.language == "zh"

    # Write en and read back
    write_app_language(cfg_file, "en")
    assert read_config(cfg_file).app.language == "en"

    # Write auto and read back
    write_app_language(cfg_file, "auto")
    assert read_config(cfg_file).app.language == "auto"

    # Invalid values raise ValueError
    with pytest.raises(ValueError, match="language must be one of"):
        write_app_language(cfg_file, "fr")

    with pytest.raises(ValueError, match="language must be one of"):
        write_app_language(cfg_file, "")


def test_set_lang_clears_cache(monkeypatch):
    monkeypatch.delenv("PENPAL_LANG", raising=False)
    monkeypatch.delenv("KEYPULSE_LANG", raising=False)
    _reset_lang_cache(monkeypatch)

    monkeypatch.setattr(Config, "load", lambda: Config.model_validate({"app": {"language": "zh"}}))
    assert i18n.current_lang() == "zh"

    # Now change config to en; without set_lang() current_lang() remains cached
    monkeypatch.setattr(Config, "load", lambda: Config.model_validate({"app": {"language": "en"}}))
    assert i18n.current_lang() == "zh"

    # With set_lang() the cache is cleared and en is detected
    i18n.set_lang()
    assert i18n.current_lang() == "en"

