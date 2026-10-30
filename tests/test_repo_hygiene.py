"""Checks for files suitable for a public repository."""

import fnmatch
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".gitignore", ".importlinter", "AGENTS.md", "BRAIN.md", "CHANGELOG.md",
    "CLAUDE.md", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "LICENSE", "Makefile",
    "README.en.md", "README.md", "SECURITY.md", "TESTING.md", "app_main.py",
    "config.toml", "install.sh", "llms.txt", "pyproject.toml", "setup_app.py",
}
PLACEHOLDERS = {"x", "a", "u", "foo", "some", "someone", "runner", "me", "alice", "demo"}
# A home path starts a path, so "mail.example.com/home/index" in a URL does not count.
HOME_PATTERN = re.compile(r"(?<![\w.-])/(?:Users|home)/([A-Za-z0-9._-]+)")
# Claude Code names its project directories after the path: ~/.claude/projects/-Users-<name>-<repo>.
ENCODED_HOME_PATTERN = re.compile(r"-Users-([A-Za-z0-9._]+)-")


@pytest.fixture(scope="module")
def tracked_files():
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pytest.skip("git unavailable")
    return [Path(name) for name in result.stdout.decode("utf-8").split("\0") if name]


def test_no_real_home_directories(tracked_files):
    violations = []
    for path in tracked_files:
        try:
            content = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "\0" in content:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            names = [m.group(1) for m in HOME_PATTERN.finditer(line)]
            names += [m.group(1) for m in ENCODED_HOME_PATTERN.finditer(line)]
            if any(name not in PLACEHOLDERS for name in names):
                violations.append(f"{path}:{number}")
    assert not violations, "\n" + "\n".join(violations)


def test_root_files_are_allowlisted(tracked_files):
    unexpected = sorted(str(path) for path in tracked_files if len(path.parts) == 1 and path.name not in ROOT_FILES)
    assert not unexpected, "新文件请放进 docs/ 或 scripts/；确需放在根目录，请同时更新这里的白名单\n" + "\n".join(unexpected)


def test_forbidden_files_are_not_tracked(tracked_files):
    patterns = ("*.db", "*.sqlite", "*.sqlite3", "*.log", ".env", ".env.*", "*.pem", "*.key", "*.p12", ".DS_Store", ".codex-rules.md")
    forbidden = sorted(str(path) for path in tracked_files if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns))
    assert not forbidden, "\n" + "\n".join(forbidden)


def test_tracked_files_are_small(tracked_files):
    oversized = sorted(str(path) for path in tracked_files if (ROOT / path).stat().st_size > 512 * 1024)
    assert not oversized, "\n" + "\n".join(oversized)
