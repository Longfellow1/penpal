from __future__ import annotations
import click
from penpal.utils.paths import get_config_path
from penpal.i18n import T
from . import main
from ._shared import PLAIN_HELP, console, get_config


@main.group(name="config", help=T("管理配置。", "Manage configuration."))
def config_group():
    """Config group."""
    pass

@config_group.command("show", help=T("查看当前配置。", "Show current configuration."))
@click.option("--plain", is_flag=True, default=False, help=PLAIN_HELP)
def config_show(plain):
    """Show config."""
    cfg = get_config()

    if plain:
        print(f"db_path={cfg.app.db_path}")
        print(f"log_path={cfg.app.log_path}")
        print(f"flush_interval_sec={cfg.app.flush_interval_sec}")
        print("retention=permanent (D32)")
        print(f"watchers_window={cfg.watchers.window}")
        print(f"watchers_idle={cfg.watchers.idle}")
        print(f"watchers_clipboard={cfg.watchers.clipboard}")
        print(f"watchers_manual={cfg.watchers.manual}")
        print(f"watchers_browser={cfg.watchers.browser}")
        print(f"idle_threshold_sec={cfg.idle.threshold_sec}")
        print(f"clipboard_max_text_length={cfg.clipboard.max_text_length}")
        print(f"clipboard_dedup_window_sec={cfg.clipboard.dedup_window_sec}")
    else:
        config_dict = cfg.model_dump()
        import json
        console.print(json.dumps(config_dict, indent=2))

@config_group.command("path", help=T("显示配置文件路径。", "Show config file path."))
def config_path():
    """Config path."""
    path = get_config_path()
    print(str(path))
