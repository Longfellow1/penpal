from __future__ import annotations

import os
import subprocess
from pathlib import Path


_HELPER = Path(__file__).parents[1] / "scripts" / "compat-wrapper.sh"


def _install_wrapper(home: Path) -> subprocess.CompletedProcess[str]:
    target = home / ".local" / "bin" / "keypulse"
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; penpal_install_compat_wrapper "$2"',
            "compat-wrapper-test",
            str(_HELPER),
            str(target),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_compat_wrapper_rewrites_recognized_legacy_wrapper(tmp_path):
    home = tmp_path / "home"
    target = home / ".local" / "bin" / "keypulse"
    target.parent.mkdir(parents=True)
    target.write_text(
        '#!/usr/bin/env bash\n'
        'source "$HOME/.keypulse/secrets.env"\n'
        'exec "$HOME/.keypulse/venv/bin/keypulse" "$@"\n',
        encoding="utf-8",
    )

    result = _install_wrapper(home)

    assert result.returncode == 0, result.stderr
    wrapper = target.read_text(encoding="utf-8")
    assert 'source "$HOME/.penpal/secrets.env"' in wrapper
    assert 'exec "$VENV_KEYPULSE" "$@"' in wrapper
    assert 'VENV_KEYPULSE="$HOME/.penpal/venv/bin/keypulse"' in wrapper
    assert target.stat().st_mode & 0o111


def test_compat_wrapper_creates_wrapper_when_target_is_missing(tmp_path):
    home = tmp_path / "home"

    result = _install_wrapper(home)

    target = home / ".local" / "bin" / "keypulse"
    assert result.returncode == 0, result.stderr
    assert target.is_file()
    assert 'source "$HOME/.penpal/secrets.env"' in target.read_text(encoding="utf-8")


def test_compat_wrapper_preserves_unrecognized_custom_file(tmp_path):
    home = tmp_path / "home"
    target = home / ".local" / "bin" / "keypulse"
    target.parent.mkdir(parents=True)
    custom_content = "#!/bin/sh\necho custom command\n"
    target.write_text(custom_content, encoding="utf-8")

    result = _install_wrapper(home)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == custom_content
    assert "keeping custom file" in result.stderr


def test_compat_wrapper_reinstall_refreshes_its_own_wrapper_without_a_warning(tmp_path):
    home = tmp_path / "home"
    assert _install_wrapper(home).returncode == 0

    result = _install_wrapper(home)

    assert result.returncode == 0, result.stderr
    assert "keeping custom file" not in result.stderr
