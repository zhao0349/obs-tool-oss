"""进程级轮转日志与底层故障栈（faulthandler）初始化模块。

设计原则：
1. **早于 GUI 初始化安装**：在导入 PyQt5 / QApplication 或启动后台子进程之前立即激活 `faulthandler`，确保 C/C++ 扩展层崩溃（如 Qt 渲染或底层驱动异常）也能留下可排查的调用栈。
2. **多进程日志隔离**：按进程角色（`gui`、`worker`、`analyse` 等）分离轮转日志文件，并以独立 PID 命名 `fault-<pid>.log`，避免后台子进程覆盖主界面的故障栈。
3. **隐私与精简约束**：仅记录程序启动信息与操作审计动作名（`action(name)`），绝不记录用户的原始观测数据内容；正常退出时自动清理空故障栈并修剪旧文件。
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
from typing import TextIO

_fault_stream: TextIO | None = None
_directory: Path | None = None


def install() -> Path:
    """安装按角色隔离的轮转日志及当前进程的底层故障栈捕获器。

    若同一进程内重复调用，直接复用首次初始化的日志目录。

    Returns:
        当前生效的日志目录路径（优先读取环境变量 `OBS_LOG_DIR`，否则默认位于 `%LOCALAPPDATA%/OBS工具`）。
    """
    global _fault_stream, _directory
    if _fault_stream is not None and _directory is not None:
        return _directory

    log_directory = Path(
        os.environ.get("OBS_LOG_DIR")
        or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "OBS工具"
    )
    log_directory.mkdir(parents=True, exist_ok=True)
    from .input_policy import LIMITS

    cli_mode = sys.argv[1] if len(sys.argv) > 1 else "gui"
    process_role = (
        cli_mode.removeprefix("--")
        if cli_mode
        in (
            "--worker",
            "--analyse",
            "--curve-context",
            "--native-picker",
            "--correction",
            "--verify-ui",
        )
        else "gui"
    )

    rotating_handler = RotatingFileHandler(
        log_directory / f"obs-tool-{process_role}.log",
        maxBytes=LIMITS.log_bytes,
        backupCount=1,
        encoding="utf-8",
    )
    rotating_handler.setFormatter(
        logging.Formatter("%(asctime)s pid=%(process)d %(levelname)s %(message)s")
    )
    logging.getLogger().addHandler(rotating_handler)
    logging.getLogger().setLevel(logging.INFO)

    # 使用独立 PID 文件名，避免后台子进程覆盖主界面的故障栈输出。
    _fault_stream = (log_directory / f"fault-{os.getpid()}.log").open(
        "a", encoding="utf-8"
    )
    faulthandler.enable(_fault_stream, all_threads=True)

    def _cleanup_fault_logs() -> None:
        """进程正常退出时关闭故障流，删除无崩溃内容的空文件，并保留最近 N 份历史故障栈。"""
        faulthandler.disable()
        if _fault_stream is not None:
            _fault_stream.close()
        current_fault_file = log_directory / f"fault-{os.getpid()}.log"
        try:
            if current_fault_file.stat().st_size == 0:
                current_fault_file.unlink()
            # 仅修剪本程序按 PID 命名的历史故障栈；仍在运行的其他进程文件在 Windows 上受锁保护，忽略删除异常。
            historical_fault_logs = sorted(
                log_directory.glob("fault-*.log"),
                key=lambda file_path: file_path.stat().st_mtime,
                reverse=True,
            )
            for old_fault_path in historical_fault_logs[LIMITS.fault_files :]:
                if old_fault_path.stem.removeprefix("fault-").isdigit():
                    try:
                        old_fault_path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    atexit.register(_cleanup_fault_logs)
    logging.info(
        "启动 executable=%s python=%s mode=%s",
        sys.executable,
        platform.python_version(),
        cli_mode,
    )
    _directory = log_directory
    return log_directory


def action(name: str) -> None:
    """在运行日志中记录用户触发的高层操作名称（不包含任何原始观测数值）。"""
    logging.info("操作 %s", name)
