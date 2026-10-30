from __future__ import annotations
import click
from click import Context
from penpal.utils.paths import DataDirMigrationError, get_data_dir
from penpal.i18n import T


@click.group(
    help=T(
        "Penpal — 本地优先的个人活动记忆基础设施（采集 / 数据源同步 / 搜索 / HUD / 健康）。\n\n"
        "\b\n"
        "常用命令：\n"
        "  penpal status                查看 daemon 状态\n"
        "  penpal doctor                诊断安装 / 配置 / 权限问题\n"
        "  penpal start                 启动后台采集\n"
        "  penpal stop                  停止后台采集\n"
        "  penpal health                运行健康检查（同 healthcheck）\n"
        "  penpal search \"<关键词>\"     搜索历史活动\n\n"
        "完整命令列表见下方 Commands。\n\n"
        "> 语言：中文（设置 PENPAL_LANG=en 切英文）",
        "Penpal — local-first personal activity memory infrastructure (capture / source sync / search / HUD / health).\n\n"
        "\b\n"
        "Common commands:\n"
        "  penpal status                Show daemon status and runtime counters\n"
        "  penpal doctor                Diagnose install, config, and permission issues\n"
        "  penpal start                 Start background capture daemon\n"
        "  penpal stop                  Stop background capture daemon\n"
        "  penpal health                Run health diagnostics and emit JSON (alias: healthcheck)\n"
        "  penpal search \"<keyword>\"    Search historical activity records\n\n"
        "See Commands below for the full command list.\n\n"
        "> Language: English (set PENPAL_LANG=zh to switch to Chinese)"
    )
)
@click.pass_context
def main(ctx: Context):
    # install init must unload old KeyPulse jobs before path migration; its subgroup
    # performs that preflight and then resolves the Penpal data directory.
    if ctx.invoked_subcommand == "install":
        return
    try:
        get_data_dir()
    except DataDirMigrationError as exc:
        raise click.ClickException(str(exc)) from exc

def legacy_main():
    """Compatibility entry point for the old command name."""
    click.echo("Deprecated command: use `penpal` instead of `keypulse`.", err=True)
    main()

# Importing command modules registers their commands on the root group.
from . import install as _install  # noqa: F401  # registers CLI commands
from . import daemon as _daemon  # noqa: F401  # registers CLI commands
from . import doctor as _doctor  # noqa: F401  # registers CLI commands
from . import app as _app  # noqa: F401  # registers CLI commands
from . import search as _search  # noqa: F401  # registers CLI commands
from . import data as _data  # noqa: F401  # registers CLI commands
from . import config as _config  # noqa: F401  # registers CLI commands
from . import privacy as _privacy  # noqa: F401  # registers CLI commands
from . import maintenance as _maintenance  # noqa: F401  # registers CLI commands
from . import sources as _sources  # noqa: F401  # registers CLI commands
from . import mcp as _mcp  # noqa: F401  # registers CLI commands
from . import feedback as _feedback  # noqa: F401  # registers CLI commands
from . import spike as _spike  # noqa: F401  # registers CLI commands
