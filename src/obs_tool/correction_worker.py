"""时间戳整体平移校正工具的独立子进程入口与 GUI 侧进程控制器模块。

核心安全设计：
1. **分析阶段可强制终止**：在预览分析（`operation == "analyse"`）期间，若任务超时或用户取消，不仅写入协作取消标记，还会在超时后调用 `kill()` 防止坏文件卡死后台。
2. **保存提交阶段保护（不强制 `kill()`）**：在写回校正文件与创建 `.bak` 备份（`operation == "save"`）期间，超时仅写入协作取消标记 `cancel`，绝不强行杀死正在执行原子替换的子进程，确保原始观测文件与备份不会处于半写坏状态。
3. **IPC 响应严格校验**：GUI 主线程在接收子进程写回的 `result.json` 时，对曲线点数、匹配整点数、动作候选列表及时间偏移溢出进行全方位校验后再渲染到界面。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import math
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
)

from .exporters import atomic_json
from .input_policy import LIMITS, bounded_json


def correction_main(directory: str | Path) -> int:
    """子进程入口：读取任务目录中的 `request.json`，执行时间校正预览分析或文件保存，并将结果写回 `result.json`。

    Args:
        directory: 时间校正任务临时目录路径。

    Returns:
        进程退出码（0 表示成功，1 表示捕获到异常并已写入错误响应）。
    """
    job_dir = Path(directory)
    try:
        request_payload = bounded_json(
            job_dir / "request.json", max_bytes=8 * 1024 * 1024
        )
        from .time_correction import analyse, save

        is_cancelled = lambda: (job_dir / "cancel").exists()
        operation_type = request_payload["operation"]
        if operation_type == "analyse":
            result_data = analyse(
                request_payload["path"],
                request_payload["parameters"],
                is_cancelled,
            )
        elif operation_type == "save":
            result_data = save(
                request_payload["plan"],
                request_payload.get("target"),
                is_cancelled,
            )
        else:
            raise ValueError("未知的校正操作")
        atomic_json(job_dir / "result.json", dict(data=result_data))
    except Exception as exc:
        atomic_json(job_dir / "result.json", dict(error=str(exc)))
        return 1
    return 0


class CorrectionWorker(QObject):
    """GUI 侧时间校正子进程管理器：负责派生 `--correction` 子进程、控制超时并在完成时校验返回载荷。"""

    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, request: dict[str, Any], parent: QObject | None) -> None:
        """初始化时间校正任务目录，写入 `request.json` 并配置 `QProcess` 与单次超时定时器。"""
        super().__init__(parent)
        base_job_dir = Path(
            os.environ.get("OBS_JOB_DIR")
            or QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
        )
        self.job = base_job_dir / "correction-jobs" / uuid4().hex
        self.job.mkdir(parents=True)
        atomic_json(self.job / "request.json", request)

        self.saving = request["operation"] == "save"
        self.process = QProcess(self)
        self.process.finished.connect(self.on_finished)
        self.process.errorOccurred.connect(self.on_error)
        self.ended = False

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timeout)

    def start(self) -> None:
        """启动 `--correction` 内部子进程，并启动总耗时保护定时器。"""
        is_compiled_binary = "__compiled__" in globals()
        self.process.start(
            (
                QCoreApplication.applicationFilePath()
                if is_compiled_binary
                else sys.executable
            ),
            (
                ["--correction", str(self.job)]
                if is_compiled_binary
                else ["-m", "obs_tool", "--correction", str(self.job)]
            ),
        )
        self.timer.start((LIMITS.seconds + 10) * 1000)

    def cancel(self) -> None:
        """在任务目录创建 `cancel` 协作取消标记文件，由子进程在安全检查点感知并中止。"""
        (self.job / "cancel").touch()

    def timeout(self) -> None:
        """任务超时处理：先写入协作取消标记；仅对非保存（即分析预览）任务执行强制 `kill()`。"""
        self.cancel()
        if not self.saving:
            self.process.kill()

    def on_error(self, error: QProcess.ProcessError) -> None:
        """捕获子进程启动失败事件，转换为用户可读的中文错误提示并结束任务生命周期。"""
        if error == QProcess.ProcessError.FailedToStart and not self.ended:
            self.failed.emit("无法启动校正任务，请重试。")
            self.finish()

    def on_finished(self, *args: Any) -> None:
        """子进程退出后有界读取 `result.json`，严格校验结构与数值边界后发射 `completed` 或 `failed`。"""
        if self.ended:
            return
        try:
            response_payload = bounded_json(
                self.job / "result.json", max_bytes=8 * 1024 * 1024
            )
            if not isinstance(response_payload, dict):
                raise ValueError("校正记录不是对象")

            if response_payload.get("error"):
                self.failed.emit(response_payload["error"])
            else:
                result_data = response_payload["data"]
                if not isinstance(result_data, dict):
                    raise ValueError("校正记录结构无效")

                if not self.saving:
                    self._validate_analysis_payload(result_data)
                elif not all(
                    required_key in result_data
                    for required_key in ("path", "inplace", "backup")
                ):
                    raise ValueError("保存结果结构无效")

                self.completed.emit(result_data)
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            self.failed.emit("无法读取校正结果：" + str(exc))
        self.finish()

    @staticmethod
    def _validate_analysis_payload(data: dict[str, Any]) -> None:
        """校验时间校正预览分析结果字典的必填字段、容量上限、有限数值与时间偏移合法性。"""
        for required_key in (
            "path",
            "parameters",
            "records",
            "columns",
            "curve",
            "valid",
            "old_start",
            "old_end",
            "matches",
        ):
            if required_key not in data:
                raise ValueError("校正记录缺少字段")

        if (
            not isinstance(data["parameters"], dict)
            or not isinstance(data["columns"], dict)
            or not isinstance(data["curve"], list)
            or not isinstance(data["matches"], list)
        ):
            raise ValueError("校正预览列表结构无效")

        if len(data["curve"]) > 8001 or len(data["matches"]) > LIMITS.targets:
            raise ValueError("校正预览超限")

        for curve_point in data["curve"]:
            datetime.fromisoformat(curve_point["timestamp"])
            if not isinstance(
                curve_point["depth"], (int, float)
            ) or not math.isfinite(curve_point["depth"]):
                raise ValueError("曲线数值无效")

        sensor_labels = data["columns"].get("sensors")
        if (
            not isinstance(sensor_labels, list)
            or len(sensor_labels) > LIMITS.fields
            or any(
                not isinstance(sensor_name, str)
                or len(sensor_name) > LIMITS.field_bytes
                for sensor_name in sensor_labels
            )
        ):
            raise ValueError("传感器说明无效")

        for match_row in data["matches"]:
            datetime.fromisoformat(match_row["target"])
            if (
                not all(
                    field_key in match_row
                    for field_key in (
                        "action",
                        "representative",
                        "residual_seconds",
                        "reasons",
                    )
                )
                or not isinstance(match_row["reasons"], list)
                or any(not isinstance(reason_str, str) for reason_str in match_row["reasons"])
            ):
                raise ValueError("目标整点记录无效")

        action_groups = data.get("actions", [])
        if not isinstance(action_groups, list) or len(action_groups) > LIMITS.segments:
            raise ValueError("动作列表无效")
        for action_item in action_groups:
            datetime.fromisoformat(action_item["timestamp"])
            if not math.isfinite(float(action_item["depth"])):
                raise ValueError("动作水深无效")

        recognition_payload = data.get("recognition", {})
        if not isinstance(recognition_payload, dict):
            raise ValueError("候选识别记录无效")
        candidate_list = recognition_payload.get("candidates", [])
        uncertainty_list = recognition_payload.get("uncertainties", [])
        if (
            not isinstance(candidate_list, list)
            or len(candidate_list) > LIMITS.segments
            or not isinstance(uncertainty_list, list)
            or len(uncertainty_list) > LIMITS.segments
            or any(not isinstance(issue_str, str) for issue_str in uncertainty_list)
        ):
            raise ValueError("候选识别列表无效")

        for candidate_item in candidate_list:
            for time_field in ("timestamp", "start", "end"):
                datetime.fromisoformat(candidate_item[time_field])
            if (
                type(candidate_item["id"]) is not int
                or not isinstance(candidate_item["file"], str)
                or type(candidate_item["line"]) is not int
                or not math.isfinite(float(candidate_item["depth"]))
            ):
                raise ValueError("候选来源无效")

        for action_item in action_groups:
            if (
                not isinstance(action_item.get("candidate_ids", []), list)
                or any(
                    type(cid) is not int
                    for cid in action_item.get("candidate_ids", [])
                )
                or not isinstance(action_item.get("notes", []), list)
                or any(
                    not isinstance(note_str, str)
                    for note_str in action_item.get("notes", [])
                )
            ):
                raise ValueError("动作分组说明无效")

        for boundary_key in ("old_start", "old_end", "new_start", "new_end"):
            if boundary_key in data:
                datetime.fromisoformat(data[boundary_key])

        if (
            not isinstance(data["path"], str)
            or type(data["valid"]) is not bool
            or type(data["records"]) is not int
            or not (0 < data["records"] <= LIMITS.file_records)
        ):
            raise ValueError("文件预览字段无效")

        delta_seconds = data.get("delta_seconds", 0)
        if type(delta_seconds) is not int:
            raise ValueError("时差无效")

        # 验证所有曲线点加上偏移秒数后不会发生 datetime 范围溢出
        shift_delta = timedelta(seconds=delta_seconds)
        for curve_point in data["curve"]:
            datetime.fromisoformat(curve_point["timestamp"]) + shift_delta

    def finish(self) -> None:
        """停止超时定时器并确保 `finished` 信号仅发射一次，避免重复恢复界面状态。"""
        if self.ended:
            return
        self.ended = True
        self.timer.stop()
        self.finished.emit()
