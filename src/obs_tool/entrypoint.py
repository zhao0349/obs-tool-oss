"""OBS 工具桌面主界面与内部后台子进程的统一启动入口。

架构说明：
1. 本软件是纯粹的桌面图形界面工具，不对外提供命令行批处理 CLI；
2. 通过 Nuitka 打包为可执行程序后，主进程通过内部标志参数（如 `--worker`、`--analyse`、`--curve-context`、`--native-picker`、`--correction`）启动自身可执行文件来派生隔离子进程；
3. 本入口负责在最早时机安装故障诊断日志（`diagnostics.install()`），并根据内部参数将执行流路由至对应的子进程处理函数或桌面 GUI。
"""

from __future__ import annotations

from importlib import import_module
import sys
from typing import Sequence


def main(args: Sequence[str] | None = None) -> int | None:
    """根据命令行参数分派内部子进程任务；无参数时启动 PySide6 桌面主界面。

    Args:
        args: 命令行参数列表（默认读取 `sys.argv[1:]`）。

    Returns:
        进程退出码（0 表示成功）。
    """
    cli_args = list(sys.argv[1:] if args is None else args)
    from .diagnostics import install

    install()

    if not cli_args:
        from .qt_compat import configure

        configure()
        from .gui import main as gui_main

        return gui_main()

    if cli_args == ["--verify-ui"]:
        from .qt_compat import configure

        configure()
        # ui_verification 模块在导入时即执行无头 UI 自检逻辑，不可作为未使用导入删除。
        from . import ui_verification  # noqa: F401

        return 0

    # 内部子进程路由表：标志 -> (目标模块名, 入口函数名, 期望附加参数个数)
    internal_operations: dict[str, tuple[str, str, int]] = {
        "--worker": ("worker_process", "main", 1),
        "--analyse": ("analysis_process", "analyse_main", 1),
        "--curve-context": ("curve_context", "context_main", 2),
        "--native-picker": ("native_picker", "pick_main", 1),
        "--correction": ("correction_worker", "correction_main", 1),
    }
    operation_spec = internal_operations.get(cli_args[0])
    if operation_spec is None or len(cli_args) != operation_spec[2] + 1:
        raise SystemExit("不支持此命令；请直接启动图形界面。")

    module_name, function_name, _ = operation_spec
    target_module = import_module("." + module_name, __package__)
    target_callable = getattr(target_module, function_name)
    return target_callable(*cli_args[1:])
