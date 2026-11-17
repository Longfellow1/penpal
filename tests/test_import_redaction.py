from datetime import datetime, timezone
from pathlib import Path

import pytest

from penpal.config import Config
from penpal.mcp import auth, tools
from penpal.sources import sink
from penpal.sources.types import SemanticEvent
from penpal.store.db import close, get_conn, init_db


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "test.db")
    yield
    close()


@pytest.fixture(autouse=True)
def _fresh_privacy_flags(monkeypatch):
    monkeypatch.setattr(sink, "_privacy_flags_cache", None)


def _event(text):
    return SemanticEvent(
        time=datetime(2026, 1, 1, tzinfo=timezone.utc), source="git_log", actor="user",
        intent=text, artifact="", raw_ref="", privacy_tier="normal",
    )


@pytest.mark.parametrize("disable_phone", [False, True])
def test_import_stores_masked_text_respecting_phone_flag(isolated_store, monkeypatch, disable_phone):
    config = Config()
    config.privacy.redact_phones = not disable_phone
    monkeypatch.setattr(Config, "load", lambda: config)
    text = "email alice@example.com phone 13812345678 ID 110101199003071233 passport E12345678 card 6222021234567890128"
    row_id = sink.persist_semantic_event(_event(text), async_named_entities=False)
    stored = get_conn().execute("SELECT content_text FROM raw_events WHERE id = ?", (row_id,)).fetchone()[0]
    assert "alice@example.com" not in stored
    assert ("13812345678" in stored) is disable_phone
    assert all(value not in stored for value in ("110101199003071233", "E12345678", "6222021234567890128"))
    assert all(marker in stored for marker in ("[ID_CARD]", "[PASSPORT]", "[BANK_CARD]"))


def test_import_config_failure_defaults_to_strict(isolated_store, monkeypatch):
    def fail():
        raise OSError("synthetic configuration failure")

    monkeypatch.setattr(Config, "load", fail)
    row_id = sink.persist_semantic_event(_event("alice@example.com 13812345678 E12345678"),
                                         async_named_entities=False)
    stored = get_conn().execute("SELECT content_text FROM raw_events WHERE id = ?", (row_id,)).fetchone()[0]
    assert all(value not in stored for value in ("alice@example.com", "13812345678", "E12345678"))


def test_unknown_fields_and_list_strings_are_masked_but_structure_survives():
    payload = {"id": "E12345678", "status": "ok", "occurred_at": "2026-01-01T00:00:00Z",
               "new_field": "password=syntheticSecret E12345678", "nested": ["E12345678"]}
    result = tools._redact_payload(payload)
    assert result["id"] == payload["id"]
    assert result["status"] == payload["status"]
    assert result["occurred_at"] == payload["occurred_at"]
    assert "syntheticSecret" not in result["new_field"]
    assert "E12345678" not in result["new_field"]
    assert result["nested"] == ["[PASSPORT]"]


def test_redaction_error_fails_closed(monkeypatch):
    def fail(_text):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(tools, "_redact_text", fail)
    assert tools._redact_payload({"unknown": "private data"}) == tools.redaction_failed()


def test_notice_for_success_only(isolated_store):
    sink.persist_semantic_event(_event("pelican planning"), async_named_entities=False)
    token = auth.create_grant("host", buckets=["Work"])
    search = tools.search_context(token, ["pelican"], client="host")
    timeline = tools.get_timeline(token, "2026-01-01", "2026-01-02", client="host")
    assert search["content_notice"] == tools.CONTENT_NOTICE
    assert timeline["content_notice"] == tools.CONTENT_NOTICE
    for result in (tools.search_context(None, ["pelican"]), tools.search_context(token, [], client="host"),
                   tools.get_timeline(token, "bad", "bad", client="host"),
                   tools.get_evidence(token, "invalid", client="host"), tools.get_evidence(None, "evd_1")):
        assert "content_notice" not in result


def test_imported_commands_are_kept_but_their_secrets_are_masked(isolated_store, monkeypatch):
    monkeypatch.setattr(Config, "load", lambda: Config())
    text = "brew install ripgrep\nexport API_TOKEN=ghp_" + "a" * 36
    row_id = sink.persist_semantic_event(_event(text), async_named_entities=False)
    stored = get_conn().execute("SELECT content_text FROM raw_events WHERE id = ?", (row_id,)).fetchone()[0]
    assert "brew install ripgrep" in stored
    assert "ghp_" + "a" * 36 not in stored


def test_privacy_switches_are_read_once_per_interval(isolated_store, monkeypatch):
    calls = []

    def load():
        calls.append(1)
        return Config()

    monkeypatch.setattr(Config, "load", load)
    for i in range(5):
        sink.persist_semantic_event(_event(f"synthetic note {i}"), async_named_entities=False)
    assert len(calls) == 1
