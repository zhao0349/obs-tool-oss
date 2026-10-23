"""主进程与后台 Worker 进程之间 IPC 结果 JSON 的有界读取与结构校验模块。

安全边界设计：
1. 后台子进程（批处理、预扫描、水深重算、曲线抽稀等）与 Qt 主界面通过临时目录中的 JSON 文件传递结果。
2. 主界面在读取并将数据挂载到 Qt 信号之前，必须通过 `read_json()` 与 `validate_result()` 进行严格的结构、容量与有限浮点数校验，防止损坏或异常膨胀的磁盘记录导致 UI 崩溃或卡死。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .input_policy import LIMITS, bounded_json, enforce


def read_json(path: str | Path) -> dict[str, Any]:
    """有界读取后台子进程写出的 IPC JSON 文件，并强制顶层结构为字典对象。

    Args:
        path: IPC JSON 文件路径。

    Returns:
        顶层 JSON 字典对象（具体业务字段仍需由调用方进一步校验）。

    Raises:
        ValueError: 当文件超出大小/深度预算或顶层不是 JSON 对象时抛出。
    """
    payload_data = bounded_json(path)
    if not isinstance(payload_data, dict):
        raise ValueError("后台记录结构无效")
    return payload_data


def validate_result(data: dict[str, Any], codes: Iterable[str]) -> None:
    """在将后台批处理结果发布到 Qt 信号前，校验磁盘 IPC 记录的结构完整性与容量上限。

    校验内容包括：
    1. 顶层必须包含 `directory`（输出路径）、`report`（运行报告）与 `results`（各测点结果字典）；
    2. `report` 中的启动时间、测点状态列表及测点代号集合必须合法；
    3. 每个测点的整点行数（`rows`）与下放片段数（`segments`）不得超过安全上限；
    4. 所有观测记录的时间戳、来源文件名/行号及四项核心物理量（深度、浊度、温度、盐度）必须为有限有效值。

    Args:
        data: 从后台结果文件读取的顶层字典。
        codes: 当前批次期望的合法测点代号集合。

    Raises:
        ValueError: 任何字段类型、范围或引用索引不合法时抛出。
    """
    from datetime import datetime
    import math

    allowed_point_codes = set(codes)
    if (
        not isinstance(data.get("directory"), str)
        or not isinstance(data.get("report"), dict)
        or not isinstance(data.get("results"), dict)
    ):
        raise ValueError("后台结果结构无效")

    report_payload = data["report"]
    datetime.fromisoformat(report_payload["started"])
    if not isinstance(report_payload.get("status"), str) or not isinstance(
        report_payload.get("points"), list
    ):
        raise ValueError("处理状态无效")

    enforce(len(report_payload["points"]), len(allowed_point_codes), "结果测点数")
    for point_state in report_payload["points"]:
        if point_state["point"] not in allowed_point_codes or point_state[
            "status"
        ] not in (
            "completed",
            "failed",
            "cancelled",
            "not_started",
        ):
            raise ValueError("结果测点身份或状态无效")

    def _validate_observation_record(record_dict: dict[str, Any]) -> None:
        """校验单条观测记录的 ISO 时间戳、源文件行号及四项有限物理量数值。"""
        datetime.fromisoformat(record_dict["timestamp"])
        if (
            not isinstance(record_dict["file"], str)
            or type(record_dict["line"]) is not int
            or record_dict["line"] < 1
        ):
            raise ValueError("观测来源无效")
        for metric_key in ("depth", "turbidity", "temperature", "salinity"):
            metric_val = record_dict[metric_key]
            if not isinstance(metric_val, (int, float)) or not math.isfinite(metric_val):
                raise ValueError("观测数值无效")

    for point_code, point_result in data["results"].items():
        if point_code not in allowed_point_codes or not isinstance(point_result, dict):
            raise ValueError("结果测点无效")

        for list_key, max_items in (
            ("rows", LIMITS.targets),
            ("segments", LIMITS.segments),
        ):
            if not isinstance(point_result.get(list_key), list):
                raise ValueError("结果列表无效")
            enforce(len(point_result[list_key]), max_items, "结果" + list_key)

        for segment_dict in point_result["segments"]:
            datetime.fromisoformat(segment_dict["start"])
            datetime.fromisoformat(segment_dict["end"])
            if not isinstance(segment_dict["records"], list):
                raise ValueError("片段观测列表无效")
            enforce(len(segment_dict["records"]), LIMITS.point_records, "片段观测数")
            for record_item in segment_dict["records"]:
                _validate_observation_record(record_item)

        total_segments = len(point_result["segments"])
        for row_dict in point_result["rows"]:
            datetime.fromisoformat(row_dict["target"])
            if (
                not isinstance(row_dict["selections"], list)
                or not isinstance(row_dict["status"], list)
                or any(not isinstance(status_str, str) for status_str in row_dict["status"])
            ):
                raise ValueError("采样行无效")
            enforce(len(row_dict["selections"]), 64, "采样层数")

            segment_index = row_dict["segment"]
            if segment_index is not None and (
                type(segment_index) is not int
                or not (0 <= segment_index < total_segments)
            ):
                raise ValueError("片段引用无效")

            for selection_dict in row_dict["selections"]:
                if not isinstance(selection_dict["label"], str) or not isinstance(
                    selection_dict["status"], list
                ):
                    raise ValueError("层位记录无效")
                if selection_dict["observation"] is not None:
                    _validate_observation_record(selection_dict["observation"])
