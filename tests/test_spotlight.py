from __future__ import annotations

import json
import platform
import plistlib
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from penpal.sources.plugins.spotlight import SpotlightSource, _event_time, _run_mdfind
from penpal.sources.registry import get_source
from penpal.sources.types import DataSourceInstance


def test_spotlight_source_is_registered() -> None:
    assert get_source("spotlight") is not None


def test_spotlight_discover_fixed_instances(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Downloads").mkdir(parents=True)
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    source = SpotlightSource()
    instances = source.discover()

    assert [item.label for item in instances] == ["Documents", "Downloads", "Desktop"]
    assert [Path(item.locator) for item in instances] == [
        (home / "Documents").resolve(),
        (home / "Downloads").resolve(),
        (home / "Desktop").resolve(),
    ]


def test_spotlight_read_maps_mdls_metadata(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    root.mkdir(parents=True)

    doc_path = root / "architecture.md"
    doc_path.write_text("# should not be read", encoding="utf-8")

    ignored_path = root / ".Trash" / "gone.md"
    ignored_path.parent.mkdir(parents=True)
    ignored_path.write_text("x", encoding="utf-8")

    run_calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        run_calls.append(command)
        if command[:1] == ["mdfind"]:
            assert command[1:3] == ["-onlyin", str(root)]
            query = command[3]
            assert "$time.iso(2026-04-28T00:00:00Z)" in query
            assert '>= "' not in query
            assert "kMDItemLastUsedDate >=" in query
            assert "kMDItemFSContentChangeDate >=" in query
            assert "kMDItemContentModificationDate >=" in query
            assert "kMDItemFSCreationDate >=" in query
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{doc_path}\n{ignored_path}\n",
                stderr="",
            )
        if command[:2] == ["mdls", "-plist"]:
            payload = {
                "kMDItemDisplayName": "Architecture Note",
                "kMDItemContentCreationDate": datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
                "kMDItemLastUsedDate": datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc),
                "kMDItemAuthors": ["alice"],
                "kMDItemSubject": "design",
                "kMDItemPath": str(doc_path),
                "kMDItemContentType": "net.daringfireball.markdown",
            }
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=plistlib.dumps(payload),
                stderr=b"",
            )
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr("penpal.sources.plugins.spotlight.subprocess.run", fake_run)

    source = SpotlightSource()
    instance = DataSourceInstance(plugin="spotlight", locator=str(root), label="Documents", metadata={})

    events = list(
        source.read(
            instance,
            datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 28, 23, 59, tzinfo=timezone.utc),
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event.source == "spotlight"
    assert event.actor == "user"
    assert event.intent == "Architecture Note"
    assert event.artifact == str(doc_path)
    assert event.raw_ref.startswith("spotlight:")
    assert event.metadata["kMDItemContentType"] == "net.daringfireball.markdown"
    assert event.metadata["shape"] == "document_file"

    mdls_calls = [call for call in run_calls if call and call[0] == "mdls"]
    assert len(mdls_calls) == 1


def test_run_mdfind_query_uses_time_iso(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    root.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("penpal.sources.plugins.spotlight.subprocess.run", fake_run)

    _run_mdfind(root=root, since=datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc))

    assert len(calls) == 1
    query = calls[0][3]
    assert "$time.iso(2026-05-08T00:00:00Z)" in query
    assert '>= "' not in query


def test_event_time_falls_back_across_metadata_fields() -> None:
    expected = datetime(2026, 5, 10, 11, 30, tzinfo=timezone.utc)
    metadata = {
        "kMDItemLastUsedDate": None,
        "kMDItemFSContentChangeDate": expected,
        "kMDItemContentModificationDate": datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc),
        "kMDItemFSCreationDate": datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc),
    }

    assert _event_time(metadata) == expected


def test_event_metadata_is_json_serializable(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    root.mkdir(parents=True)
    doc_path = root / "design.md"
    doc_path.write_text("x", encoding="utf-8")

    def fake_run(command, **kwargs):
        if command[:1] == ["mdfind"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{doc_path}\n", stderr="")
        if command[:2] == ["mdls", "-plist"]:
            payload = {
                "kMDItemDisplayName": "Design Note",
                "kMDItemLastUsedDate": datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc),
                "kMDItemPath": str(doc_path),
                "kMDItemContentType": "net.daringfireball.markdown",
                "kMDItemContentTypeTree": ["net.daringfireball.markdown", "public.plain-text"],
            }
            return subprocess.CompletedProcess(command, 0, stdout=plistlib.dumps(payload), stderr=b"")
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr("penpal.sources.plugins.spotlight.subprocess.run", fake_run)

    source = SpotlightSource()
    instance = DataSourceInstance(plugin="spotlight", locator=str(root), label="Documents", metadata={})
    events = list(
        source.read(
            instance,
            datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert len(events) == 1
    json.dumps(events[0].metadata)
    assert isinstance(events[0].metadata["kMDItemLastUsedDate"], str)


def test_excludes_library_containers_paths(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    root.mkdir(parents=True)
    safe_path = root / "project.md"
    safe_path.write_text("safe", encoding="utf-8")
    blocked_path = root / "Library" / "Containers" / "com.foo" / "secret.md"
    blocked_path.parent.mkdir(parents=True)
    blocked_path.write_text("secret", encoding="utf-8")

    mdls_targets: list[str] = []

    def fake_run(command, **kwargs):
        if command[:1] == ["mdfind"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{blocked_path}\n{safe_path}\n", stderr="")
        if command[:2] == ["mdls", "-plist"]:
            target = command[-1]
            mdls_targets.append(target)
            payload = {
                "kMDItemDisplayName": Path(target).name,
                "kMDItemLastUsedDate": datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc),
                "kMDItemPath": target,
                "kMDItemContentType": "net.daringfireball.markdown",
                "kMDItemContentTypeTree": ["net.daringfireball.markdown", "public.plain-text"],
            }
            return subprocess.CompletedProcess(command, 0, stdout=plistlib.dumps(payload), stderr=b"")
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr("penpal.sources.plugins.spotlight.subprocess.run", fake_run)

    source = SpotlightSource()
    instance = DataSourceInstance(plugin="spotlight", locator=str(root), label="Documents", metadata={})
    events = list(
        source.read(
            instance,
            datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert [event.artifact for event in events] == [str(safe_path)]
    assert mdls_targets == [str(safe_path)]


@pytest.mark.system_smoke
def test_run_mdfind_smoke_real(tmp_path: Path) -> None:
    if platform.system() != "Darwin":
        pytest.skip("mdfind smoke test requires macOS")

    mdfind_bin = shutil.which("mdfind")
    if mdfind_bin is None:
        pytest.skip("mdfind is unavailable")

    mdimport_bin = shutil.which("mdimport")
    if mdimport_bin is None:
        pytest.skip("mdimport is unavailable")

    root = tmp_path / "spotlight-smoke"
    root.mkdir(parents=True)
    target = root / "penpal-spotlight-smoke.txt"
    target.write_text("spotlight smoke", encoding="utf-8")

    import_result = subprocess.run(
        [mdimport_bin, str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    if import_result.returncode != 0:
        pytest.skip("mdimport failed to index temporary directory")

    since = datetime.now(timezone.utc) - timedelta(days=7)
    resolved_target = target.resolve(strict=False)
    found_paths: list[Path] = []
    for _ in range(8):
        found_paths = _run_mdfind(root=root, since=since)
        if resolved_target in found_paths:
            break
        time.sleep(0.5)
    else:
        pytest.skip("Spotlight index did not include temporary directory in time")

    assert resolved_target in found_paths
