"""GUI 侧主批处理子进程控制器（`Worker`）与全局状态标签中文文案映射模块。

职责说明：
1. `STATUS`：将算法层输出的状态码（如 `missing_segment`、`segment_reused` 等）映射为用户界面统一展示的中文标签；
2. `DiskCancellation`：在任务临时目录中创建 `cancel` 标记文件，向后台子进程传递协作式取消信号；
3. `Worker`：管理 `--worker` 批处理子进程的启动、`progress-*.json` 顺序轮询、总超时看门狗及最终 `result.json` 校验发布。
"""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import time
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QProcess,
    QStandardPaths,
    QTimer,
    Signal,
    Slot,
)

from .ipc_reader import read_json
from .models import Batch

# 算法层状态标识码 -> 界面显示的中文名称映射表
STATUS: dict[str, str] = {
    "missing_segment": "缺少匹配片段",
    "missing_layer": "缺层",
    "segment_reused": "片段复用",
    "cross_layer_reused": "跨层复用",
    "tolerance_unset": "未检查深度偏差是否超限",
    "depth_out_of_tolerance": "深度超差",
}


class DiskCancellation:
    """基于任务目录标记文件的跨进程协作式取消触发器。"""

    def __init__(self, job: Path) -> None:
        """绑定当前任务的临时目录路径；仅在调用 `set()` 时创建 `cancel` 文件。"""
        self.job = job

    def set(self) -> None:
        """在任务目录下创建 `cancel` 空文件，通知子进程在下一个安全检查点停止，避免中断正在原子提交的文件。"""
        (self.job / "cancel").touch()


class Worker(QObject):
    """管理主批处理后台子进程（`--worker`）、定时轮询磁盘进度并校验发布处理结果。"""

    progress = Signal(int, int, object)
    done = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, batch: Batch) -> None:
        """将当前批次配置 `Batch` 序列化至独立任务目录 `task.json`，并初始化进程与 150ms 进度轮询定时器。"""
        super().__init__()
        import uuid
        from .exporters import atomic_json

        base_job_dir = Path(
            os.environ.get("OBS_JOB_DIR")
            or QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
        )
        self.job = base_job_dir / "jobs" / uuid.uuid4().hex
        self.job.mkdir(parents=True, exist_ok=False)
        atomic_json(self.job / "task.json", asdict(batch))

        self.cancel = DiskCancellation(self.job)
        self.process = QProcess(self)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)

        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.read_progress)

        self.last_progress: int = 0
        self.ended: bool = False
        self.codes: set[str] = {point.code for point in batch.points}
        self.started: float = time.monotonic()

    def start(self) -> None:
        """启动源码或打包态的 `--worker` 内部子进程，并启动进度轮询定时器。"""
        import sys

        if "__compiled__" in globals():
            executable_path = QCoreApplication.applicationFilePath()
            process_args = ["--worker", str(self.job)]
        else:
            executable_path = sys.executable
            process_args = ["-m", "obs_tool", "--worker", str(self.job)]
        self.process.start(executable_path, process_args)
        self.timer.start()

    def isRunning(self) -> bool:
        """返回后台子进程当前是否仍处于运行状态。"""
        return self.process.state() != QProcess.ProcessState.NotRunning

    def wait(self) -> None:
        """阻塞等待后台子进程退出（最长等待 5 分钟），供窗口关闭清理或测试同步使用。"""
        if self.isRunning():
            self.process.waitForFinished(300000)

    @Slot()
    def read_progress(self) -> None:
        """按递增序号依次读取子进程原子发布的 `progress-*.json`；遇到未就绪或异常记录时结束本轮轮询。"""
        from .input_policy import LIMITS

        if self.isRunning() and time.monotonic() - self.started > LIMITS.seconds + 10:
            self.cancel.set()
            self.process.kill()

        while True:
            next_progress_file = (
                self.job / f"progress-{self.last_progress + 1:06d}.json"
            )
            if not next_progress_file.exists():
                break
            try:
                progress_data = read_json(next_progress_file)
                if (
                    progress_data["index"] != self.last_progress + 1
                    or not isinstance(progress_data.get("total"), int)
                    or not (
                        progress_data["index"]
                        <= progress_data["total"]
                        <= LIMITS.batch_files
                    )
                    or not isinstance(progress_data.get("point"), dict)
                    or progress_data["point"].get("point") not in self.codes
                    or not isinstance(progress_data["point"].get("status"), str)
                ):
                    break
                self.last_progress = progress_data["index"]
                self.progress.emit(
                    progress_data["index"],
                    progress_data["total"],
                    progress_data["point"],
                )
            except (OSError, ValueError, KeyError, TypeError):
                break

    @Slot()
    def process_error(self) -> None:
        """捕获子进程启动失败并发射 `error` 信号，随后进入统一收尾流程。"""
        if self.process.error() == QProcess.ProcessError.FailedToStart:
            self.error.emit("无法启动后台处理：" + self.process.errorString())
            self.finish()

    @Slot(int)
    def process_finished(self, exit_code: int) -> None:
        """子进程退出后执行最后一次进度读取，校验 `result.json` 后发射 `done`，或读取 `error.json` 发射 `error`。"""
        self.read_progress()
        result_file = self.job / "result.json"
        if exit_code == 0 and result_file.exists():
            try:
                result_payload = read_json(result_file)
                from .ipc_reader import validate_result

                validate_result(result_payload, self.codes)
                point_results: dict[str, Any] = result_payload["results"]
                # 注意：UI 层保留 ISO 时间字符串，仅在绘制当前选中片段曲线时按需解析，
                # 避免在 Qt 主线程为整批成千上万条原始观测逐一构造 datetime 对象造成界面卡顿。
                self.done.emit(
                    (
                        Path(result_payload["directory"]),
                        result_payload["report"],
                        point_results,
                    )
                )
            except Exception as exc:
                self.error.emit("无法读取后台结果：" + str(exc))
        else:
            error_file = self.job / "error.json"
            try:
                error_message = (
                    str(read_json(error_file)["error"])
                    if error_file.exists()
                    else f"后台处理异常退出或超过分析时限（{exit_code}）"
                )
            except (OSError, ValueError, KeyError, TypeError):
                error_message = "后台处理记录无效，请缩小输入范围后重试。"
            self.error.emit(error_message)
        self.finish()

    def finish(self) -> None:
        """停止进度轮询定时器并确保 `finished` 信号仅发射一次。"""
        if not self.ended:
            self.ended = True
            self.timer.stop()
            self.finished.emit()


# 保持向后兼容的导入别名：允许主窗口与旧测试继续从 gui_components 导入 Curve 组件
from .curve_view import Curve as Curve  # noqa: E402, F401
