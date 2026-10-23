"""Windows 原生文件/文件夹选择器（`IFileOpenDialog` / `IFileSaveDialog`）的隔离进程包装模块。

为什么使用独立子进程承载原生文件对话框：
1. 在 Windows 环境下，直接在 Qt 主进程内弹出外壳文件对话框（尤其是挂载了慢速网络盘、第三方云盘 Shell 扩展或包含成千上万个 `.dat` 文件的目录）时，COM/Shell 扩展可能阻塞 GUI 事件循环甚至引发崩溃；
2. `FilePicker` 将原生 COM 对话框调用与文件夹 `.dat` 枚举完全隔离在 `--native-picker` 子进程中执行，Qt 主线程只负责管理进程生命周期与接收最终路径列表。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QProcess,
    QStandardPaths,
    QTimer,
    Signal,
    Slot,
)

from .exporters import atomic_json
from .input_policy import LIMITS, bounded_json, enforce
from .ipc_reader import read_json


def pick_main(job_directory: str | Path) -> int:
    """子进程入口：读取 `request.json`，调用 Windows 原生文件对话框，并将选定路径或错误写回 `result.json`。

    若请求中指定了 `dat_directory=True`（按文件夹导入 DAT），则在用户选定目录后，
    直接在子进程内执行受控的 `.dat` 文件扫描（`dat_in_folder`），避免阻塞 GUI 线程。
    """
    from .native_shell import choose

    job_dir = Path(job_directory)
    try:
        request_payload = bounded_json(job_dir / "request.json")
        scan_dat_directory = request_payload.pop("dat_directory", False)
        selected_paths = choose(**request_payload)
        if scan_dat_directory and selected_paths:
            from .input_policy import dat_in_folder

            selected_paths = dat_in_folder(
                selected_paths[0],
                cancelled=lambda: (job_dir / "cancel").exists(),
            )
            if not selected_paths:
                raise ValueError("所选文件夹中未找到DAT文件。")
        atomic_json(job_dir / "result.json", dict(paths=selected_paths))
        return 0
    except Exception as exc:
        atomic_json(job_dir / "result.json", dict(error=str(exc), paths=[]))
        return 1


class FilePicker(QObject):
    """以 Qt 信号包装 Windows 原生文件/目录选择子进程的异步控制器。"""

    filesSelected = Signal(list)
    rejected = Signal()
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        parent: Any,
        title: str,
        folder: str = "",
        directory: bool = False,
        save: bool = False,
        dat_directory: bool = False,
    ) -> None:
        """记录对话框标题、初始目录及选择模式，建立独立任务目录与取消轮询定时器（调用 `open()` 时才真正弹出窗口）。"""
        super().__init__(parent)
        self.parent_window = parent
        self.title = title
        self.folder = folder
        self.directory = directory
        self.save = save
        self.dat_directory = dat_directory
        self.initial_names: list[str] | None = None
        self.ended = False
        self.want_cancel = False

        base_job_dir = Path(
            os.environ.get("OBS_JOB_DIR")
            or QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
        )
        self.job = base_job_dir / "pickers" / uuid4().hex
        self.job.mkdir(parents=True, exist_ok=False)

        self.process = QProcess(self)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)

        self.cancel_timer = QTimer(self)
        self.cancel_timer.setInterval(100)
        self.cancel_timer.timeout.connect(self.cancel_if_requested)

    def open(self) -> None:
        """写入 `request.json` 并启动 `--native-picker` 子进程打开原生文件对话框。"""
        atomic_json(
            self.job / "request.json",
            dict(
                title=self.title,
                folder=self.folder,
                directory=self.directory,
                save=self.save,
                dat_directory=self.dat_directory,
                owner=int(self.parent_window.winId()),
                initial_names=self.initial_names,
            ),
        )
        is_compiled_binary = "__compiled__" in globals()
        executable_path = (
            QCoreApplication.applicationFilePath()
            if is_compiled_binary
            else sys.executable
        )
        process_args = (
            ["--native-picker", str(self.job)]
            if is_compiled_binary
            else ["-m", "obs_tool", "--native-picker", str(self.job)]
        )
        self.process.start(executable_path, process_args)
        self.cancel_timer.start()

    def isVisible(self) -> bool:
        """以选择器子进程是否仍在运行来表示原生文件对话框是否处于打开状态。"""
        return self.process.state() != QProcess.ProcessState.NotRunning

    def reject(self) -> None:
        """请求关闭正在显示的原生文件对话框并取消本次选择。"""
        self.want_cancel = True
        (self.job / "cancel").touch()
        self.cancel_if_requested()

    @Slot()
    def cancel_if_requested(self) -> None:
        """若 GUI 侧已发出取消请求且子进程仍在运行，则向子进程的原生 `#32770` 对话框窗口发送关闭消息。"""
        if self.want_cancel and self.isVisible():
            from .native_shell import command_dialog

            command_dialog(int(self.process.processId()))

    @Slot()
    def process_error(self) -> None:
        """捕获子进程无法启动的错误并转换为用户可见的 `error` 信号。"""
        if self.ended:
            return
        if self.process.error() == QProcess.ProcessError.FailedToStart:
            logging.error("文件选择器启动失败：%s", self.process.errorString())
            self.error.emit("无法打开文件选择窗口，请重试。")
            self.end()

    @Slot(int)
    def process_finished(self, exit_code: int) -> None:
        """子进程退出后读取并校验 `result.json`，根据结果发射 `filesSelected`、`rejected` 或 `error` 信号。"""
        if self.ended:
            return
        try:
            result_payload = read_json(self.job / "result.json")
            if self.want_cancel:
                self.rejected.emit()
            elif result_payload.get("error"):
                logging.error("文件选择器失败：%s", result_payload["error"])
                self.error.emit(str(result_payload["error"])[:500])
            elif result_payload.get("paths"):
                selected_paths = result_payload["paths"]
                if not isinstance(selected_paths, list) or any(
                    not isinstance(path_str, str) or len(path_str) > 32760
                    for path_str in selected_paths
                ):
                    raise ValueError("选择器返回路径无效")
                enforce(
                    len(selected_paths),
                    LIMITS.selection_entries,
                    "选择器返回条目数",
                )
                self.filesSelected.emit(selected_paths)
            else:
                self.rejected.emit()
        except (OSError, ValueError) as exc:
            logging.error("文件选择器退出 code=%s: %s", exit_code, exc)
            self.error.emit("无法打开文件选择窗口，请重试。")
        self.end()

    def end(self) -> None:
        """停止取消轮询定时器并确保 `finished` 信号仅发射一次。"""
        if self.ended:
            return
        self.ended = True
        self.cancel_timer.stop()
        self.finished.emit()
