import pytest

from penpal.config import Config
from penpal.store import buckets


@pytest.mark.parametrize("kind", ["app", "site", "source", "chat"])
def test_failed_config_is_unassigned_and_retried(monkeypatch, kind):
    config = Config()
    config.buckets.app_defaults = {"sample": "Work"}
    config.buckets.site_defaults = {"sample.example": "Work"}
    config.buckets.source_defaults = {"codex_cli": "Work"}
    calls = []

    def load():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("synthetic configuration failure")
        return config

    monkeypatch.setattr(Config, "load", load)
    buckets.reset_cache()
    try:
        if kind == "app":
            classify = lambda: buckets.classify_capture_app("sample")
        elif kind == "site":
            classify = lambda: buckets.resolve_bucket(
                "browser_url", "Chrome", rules=[], overrides={}, url="https://sample.example"
            )
        elif kind == "chat":
            classify = lambda: buckets.classify_capture_app("sample", app_overrides={})
        else:
            classify = lambda: buckets.default_bucket_for_source("codex_cli")
        assert classify() is None
        assert classify() == ("Personal" if kind == "chat" else "Work")
        assert len(calls) == 2
    finally:
        buckets.reset_cache()


def test_site_config_failure_with_unknown_url_does_not_fall_back(monkeypatch):
    def fail():
        raise OSError("synthetic configuration failure")

    monkeypatch.setattr(Config, "load", fail)
    buckets.reset_cache()
    try:
        assert buckets.resolve_bucket("window", "Chrome", rules=[], overrides={}) is None
    finally:
        buckets.reset_cache()


