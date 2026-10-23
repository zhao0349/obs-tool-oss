"""GUI 侧水深自动识别子进程控制器（`AnalysisWorker`）。

设计说明：
    继承自通用后台任务基类 `Worker`（见 `gui_components.py`），复用其临时任务目录管理、
    `progress-*.json` 定时轮询、`cancel` 文件协作取消及超时清理机制，
    仅将子进程启动参数替换为 `--analyse` 并在完成时发射 `analysed` 信号。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Signal, Slot

from .exporters import atomic_json
from .gui_components import Worker
from .ipc_reader import read_json

if TYPE_CHECKING:
    from .models import Batch


class AnalysisWorker(Worker):
    """通过独立子进程执行批量水深自动识别，并向主界面回传进度与估算结果字典。"""

    analysed = Signal(object)

    def __init__(self, batch: Batch) -> None:
        """将待识别测点的编号与输入文件列表序列化为任务目录中的 `request.json`。"""
        super().__init__(batch)
        atomic_json(
            self.job / "request.json",
            dict(
                points=[
                    dict(code=point.code, files=point.files) for point in batch.points
                ]
            ),
        )

    def start(self) -> None:
        """启动 `--analyse` 内部子进程并激活进度轮询定时器。"""
        is_compiled_binary = "__compiled__" in globals()
        executable_path = (
            QCoreApplication.applicationFilePath()
            if is_compiled_binary
            else sys.executable
        )
        process_args = (
            ["--analyse", str(self.job)]
            if is_compiled_binary
            else ["-m", "obs_tool", "--analyse", str(self.job)]
        )
        self.process.start(executable_path, process_args)
        self.timer.start()

    @Slot(int)
    def process_finished(self, exit_code: int) -> None:
        """子进程退出后读取最终水深识别结果并发射 `analysed` 信号；若异常退出则发射 `error`。"""
        self.read_progress()
        try:
            if exit_code != 0:
                raise ValueError(f"水深识别进程退出（{exit_code}）")
            self.analysed.emit(read_json(self.job / "result.json"))
        except (OSError, ValueError) as exc:
            self.error.emit(str(exc))
        self.finish()
