from __future__ import annotations

import importlib


def test_launcher_path_is_taken_from_the_environment_once(monkeypatch, tmp_path):
    launcher = tmp_path / "Penpal.app" / "Contents" / "MacOS" / "Penpal"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"binary")
    monkeypatch.setenv("PENPAL_NATIVE_LAUNCHER", str(launcher))
    monkeypatch.setenv("PENPAL_HUD_NATIVE_LAUNCHER", str(launcher))

    import penpal.foundation.native_launcher as module

    module = importlib.reload(module)
    try:
        assert module.native_launcher() == str(launcher.resolve())
        # Child processes were not started by the launcher.
        import os

        assert "PENPAL_NATIVE_LAUNCHER" not in os.environ
        assert "PENPAL_HUD_NATIVE_LAUNCHER" not in os.environ
        launcher.unlink()
        assert module.native_launcher() is None
    finally:
        monkeypatch.delenv("PENPAL_NATIVE_LAUNCHER", raising=False)
        importlib.reload(module)
