"""结果核查页完整深度-时间曲线快照的后台异步加载器模块（`ContextLoader`）。

并发安全设计（代次号 `generation` 机制）：
1. 用户在结果核查页快速切换不同测点时，前一个测点的曲线读取子进程可能尚未返回；
2. 每次调用 `load()` 或 `cancel()` 时递增 `self.generation`，并在回调 `done()` 中严格核对 `generation == self.generation` 及 `point_id == identity`；
3. 任何已过期代次或测点身份不匹配的回调均被立即丢弃，确保旧请求绝不会覆盖新选中测点的核查曲线。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QProcess,
    QStandardPaths,
    QTimer,
    Signal,
)
from shiboken6 import isValid

from .input_policy import LIMITS, bounded_json


class ContextLoader(QObject):
    """异步启动 `--curve-context` 子进程读取核查曲线快照，并通过代次号阻止旧请求覆盖当前测点。"""

    ready = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        """初始化空加载器状态，初始请求代次号为 0。"""
        super().__init__(parent)
        self.generation: int = 0
        self.process: QProcess | None = None

    def cancel(self) -> None:
        """递增请求代次使既有未完成回调失效，并终止仍在运行的只读曲线加载子进程。"""
        self.generation += 1
        if (
            self.process
            and isValid(self.process)
            and self.process.state() != QProcess.ProcessState.NotRunning
        ):
            self.process.kill()
        self.process = None

    def load(self, path: str | Path | None, identity: str | None) -> None:
        """异步加载指定测点的完整曲线快照文件，并在完成后校验代次号与测点身份指纹。

        Args:
            path: 内部记录目录中的 `*-curve.json` 快照文件路径。
            identity: 当前测点的唯一身份指纹（`point_identity(code, files)`），用于防止读取到过期快照。
        """
        self.cancel()
        expected_generation = self.generation

        if path is None or not Path(path).is_file():
            self.ready.emit(
                None, "这份结果只能查看已保存的片段，无法显示完整观测范围。"
            )
            return

        base_job_dir = Path(
            os.environ.get("OBS_JOB_DIR")
            or QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
        )
        job_dir = base_job_dir / "curve-jobs" / uuid4().hex
        job_dir.mkdir(parents=True)
        output_json_path = job_dir / "result.json"

        worker_process = QProcess(self)
        self.process = worker_process

        def _on_process_done(*_args: object) -> None:
            """接收当前代次子进程写出的降采样曲线视图；若组件已销毁或代次已过期则安全丢弃。"""
            if not isValid(self) or not isValid(worker_process):
                return
            if expected_generation != self.generation:
                worker_process.deleteLater()
                return
            self.process = None
            try:
                response_payload = bounded_json(output_json_path)
                view_data = response_payload.get("data")
                if not view_data:
                    raise ValueError(response_payload.get("error", "无法读取核查快照"))
                if identity and view_data.get("point_id") != identity:
                    raise ValueError("核查快照与本次测点身份不一致")
                status_note = (
                    ""
                    if view_data["observation_count"]
                    else "没有有效观测，无法显示完整观测范围。"
                )
                self.ready.emit(view_data, status_note)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logging.warning("曲线读取失败 %s: %s", path, exc)
                self.ready.emit(None, "无法加载完整曲线，目前只能查看已保存的片段。")
            worker_process.deleteLater()

        worker_process.finished.connect(_on_process_done)
        QTimer.singleShot(
            (LIMITS.seconds + 10) * 1000,
            worker_process,
            lambda: (
                worker_process.kill()
                if worker_process.state() != QProcess.ProcessState.NotRunning
                else None
            ),
        )

        def _on_start_failed(error: QProcess.ProcessError) -> None:
            """若子进程启动失败，统一路由至 `_on_process_done` 降级为仅查看已保存片段。"""
            if error == QProcess.ProcessError.FailedToStart:
                _on_process_done()

        worker_process.errorOccurred.connect(_on_start_failed)
        if "__compiled__" in globals():
            worker_process.start(
                QCoreApplication.applicationFilePath(),
                ["--curve-context", str(path), str(output_json_path)],
            )
        else:
            worker_process.start(
                sys.executable,
                ["-m", "obs_tool", "--curve-context", str(path), str(output_json_path)],
            )
