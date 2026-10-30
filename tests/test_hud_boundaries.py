"""D33 boundaries for the HUD: no legacy report modules, no AppKit outside the shell, old daily-report panels gone."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path


HUD_DIR = Path(__file__).resolve().parents[1] / "penpal" / "hud"
LEGACY = ("penpal.pipeline", "penpal.obsidian", "penpal.quality", "penpal.integrations", "penpal.prompts")
APPKIT = ("AppKit", "Foundation", "WebKit", "objc", "Quartz", "ApplicationServices", "CoreFoundation", "Vision")
LOGIC_MODULES = ("snapshot", "viewmodel", "controller", "render")
REMOVED_PANELS = ("summary", "surface", "monitor_html", "state", "health", "state_reset")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _loaded(import_stmt: str) -> list[str]:
    code = textwrap.dedent(f"""
        import sys
        {import_stmt}
        print("\\n".join(sorted(sys.modules)))
    """)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def test_no_hud_module_imports_a_legacy_report_package():
    for path in HUD_DIR.glob("*.py"):
        bad = [m for m in _imports(path) if m.startswith(LEGACY)]
        assert bad == [], f"{path.name} imports {bad}"


def test_legacy_packages_do_not_get_loaded_by_the_hud():
    loaded = _loaded("import penpal.hud.app")
    assert [m for m in loaded if m.startswith(LEGACY)] == []


def test_logic_modules_never_import_appkit_even_lazily():
    for name in LOGIC_MODULES:
        bad = [m for m in _imports(HUD_DIR / f"{name}.py") if m.split(".")[0] in APPKIT]
        assert bad == [], f"{name}.py imports {bad}"


def test_view_model_controller_renderer_load_without_appkit():
    loaded = _loaded("import penpal.hud.viewmodel, penpal.hud.controller, penpal.hud.render, penpal.hud.snapshot")
    assert [m for m in loaded if m.split(".")[0] in APPKIT] == []
    assert [m for m in loaded if m.startswith(LEGACY)] == []


def test_importing_the_package_does_not_load_the_shell():
    assert "penpal.hud.app" not in _loaded("import penpal.hud")


def test_old_daily_report_panels_are_gone():
    for name in REMOVED_PANELS:
        assert importlib.util.find_spec(f"penpal.hud.{name}") is None, f"penpal.hud.{name} should be removed"
    shipped = {p.stem for p in HUD_DIR.glob("*.py")}
    assert shipped == {"__init__", "app", "approval_window", "controller", "reload", "render", "snapshot", "viewmodel"}


def test_hud_text_has_no_daily_report_panels():
    text = "\n".join(p.read_text(encoding="utf-8") for p in HUD_DIR.glob("*.py"))
    for word in ("today_focus", "weekly", "theme_count", "cost", "top_signals", "surface"):
        assert word not in text, word


def test_shell_declares_no_permission_usage_strings():
    # The App needs no system permission: nothing in the shell asks for one.
    shell = (HUD_DIR / "app.py").read_text(encoding="utf-8")
    for call in ("AXIsProcessTrustedWithOptions", "CGRequestScreenCaptureAccess", "IOHIDRequestAccess", "trigger_required_prompts"):
        assert call not in shell
