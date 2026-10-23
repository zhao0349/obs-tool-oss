"""深度-时间核查曲线快照流式保存、包络降采样与默认视野计算模块。

职责边界：
1. **流式保存（`save_context`）**：在后台批处理期间，将测点的完整有效观测序列流式保存至内部记录目录的 `<code>-curve.json`，不把任何面向显示的抽样数据混入业务结果。
2. **断档切分与极值保留降采样（`prepare_context`）**：在独立子进程中读取曲线快照，先按 `gap_seconds` 切分为连续观测段（`runs`），避免跨时间缺口错误连线；再按每桶保留首尾端点与最小/最大深度的四点包络法降采样，保证波峰与波谷在缩放时绝不失真。
3. **整点视野计算（`time_ranges`）**：根据当前选中整点的状态（正常或含复用/缺测异常）及前后相邻片段，计算曲线组件的初始默认时间视野（`default_view`）与最大可浏览范围（`browse_range`）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
from typing import Any

from .data_types import CurveContext, Observation
from .exporters import atomic_json, atomic_write
from .input_policy import LIMITS, bounded_json, enforce
from .models import Point


def moment(value: str | datetime | None) -> datetime | None:
    """将 ISO 时间字符串解析为 `datetime` 对象；若已是 `datetime` 或 `None` 则原样返回。

    Args:
        value: ISO 格式时间字符串、`datetime` 对象或 `None`。

    Returns:
        datetime | None: 转换后的 `datetime` 对象或 `None`。
    """

    return datetime.fromisoformat(value) if isinstance(value, str) else value


def save_context(
    path: str | Path,
    records: list[Observation],
    point: Point,
    inputs: list[dict],
    gap_minutes: float,
    cancelled: Callable[[], bool],
) -> None:
    """以流式 JSON 方式原子保存测点的完整观测序列与身份元数据，供结果页曲线异步加载。

    Args:
        path: 曲线快照 JSON 保存路径（通常为 `<record_dir>/<code>-curve.json`）。
        records: 按时间排序的完整有效观测记录列表。
        point: 测点配置对象（提供稳定身份 `point_id` 与参考水深 `depth`）。
        inputs: 原始输入文件摘要列表。
        gap_minutes: 判定时间断档的分钟阈值（转换为 `gap_seconds` 存入快照）。
        cancelled: 协作取消回调，每写出 1000 条观测检查一次。

    Raises:
        OSError: 当写入过程中用户取消任务或磁盘写入失败时抛出。
    """

    def write_snapshot_stream(stream) -> None:
        """先写入快照头部元数据，再流式逐条写入 `observations` 数组。"""

        metadata = dict(
            schema_version=1,
            point_id=point.point_id,
            depth=point.depth,
            inputs=inputs,
            gap_seconds=gap_minutes * 60,
            start=str(records[0]["timestamp"]) if records else None,
            end=str(records[-1]["timestamp"]) if records else None,
        )
        stream.write(
            json.dumps(metadata, ensure_ascii=False)[:-1] + ',"observations":['
        )
        for index, record in enumerate(records):
            if index % 1000 == 0 and cancelled():
                raise OSError("核查快照写入已取消")
            if index:
                stream.write(",")
            json.dump(record, stream, ensure_ascii=False, default=str)
        stream.write("]}")

    atomic_write(path, write_snapshot_stream)


def prepare_context(path: str | Path, limit: int = 16000) -> CurveContext:
    """有界读取曲线快照 JSON，按时间断档切分连续观测段（`runs`）并执行包络降采样。

    降采样策略：
    - 相邻两条观测间隔超过 `gap_seconds` 时切分为不同连续段，绘制时互不连线。
    - 每个连续段内按步长 `step` 分桶，每个桶内严格保留“桶首、桶尾、最浅点、最深点”至多 4 个关键点，
      确保下放剖面的真实峰值与浅水回撤在界面缩略图中完整保留。

    Args:
        path: 曲线快照 JSON 文件路径。
        limit: 降采样后期望保留的最大显示点数上限，默认 `16000`。

    Returns:
        CurveContext: 包含降采样后 `runs`、原始总点数 `observation_count` 及显示点数 `display_count` 的上下文字典。

    Raises:
        CapacityError: 当原始观测点数超过 `LIMITS.plot_points` 或 `limit > 16000` 时抛出。
        ValueError: 当快照版本不支持或观测记录时间戳/深度无效时抛出。
    """

    snapshot_data = bounded_json(path)
    if snapshot_data.get("schema_version") != 1:
        raise ValueError("核查快照版本不支持")

    raw_rows = snapshot_data.pop("observations")
    if not isinstance(raw_rows, list):
        raise ValueError("核查观测列表结构无效")

    enforce(len(raw_rows), LIMITS.plot_points, "核查观测数")
    enforce(limit, 16000, "显示点数")

    gap_seconds = snapshot_data["gap_seconds"]
    continuous_runs: list[list[dict[str, Any]]] = []
    current_run: list[dict[str, Any]] = []
    previous_timestamp: datetime | None = None

    for record in raw_rows:
        timestamp = moment(record["timestamp"])
        if (
            not isinstance(timestamp, datetime)
            or not isinstance(record.get("depth"), (int, float))
            or not math.isfinite(record["depth"])
        ):
            raise ValueError("核查快照包含无效观测")

        if (
            previous_timestamp is not None
            and (timestamp - previous_timestamp).total_seconds() > gap_seconds
        ):
            continuous_runs.append(current_run)
            current_run = []
        current_run.append(record)
        previous_timestamp = timestamp

    if current_run:
        continuous_runs.append(current_run)

    # 每桶保留首尾端点及深度极值（至多 4 点）；各连续段独立降采样，不跨断档连线
    bucket_step = max(1, math.ceil(len(raw_rows) / max(1, limit // 4)))
    if len(continuous_runs) > limit // 4:
        max_runs = max(2, limit // 4)
        continuous_runs = [
            continuous_runs[
                round(idx * (len(continuous_runs) - 1) / (max_runs - 1))
            ]
            for idx in range(max_runs)
        ]

    displayed_runs: list[list[dict[str, Any]]] = []
    for run in continuous_runs:
        chosen_points: list[dict[str, Any]] = []
        for start_idx in range(0, len(run), bucket_step):
            block = run[start_idx : start_idx + bucket_step]
            key_positions = {
                0,
                len(block) - 1,
                min(range(len(block)), key=lambda idx: block[idx]["depth"]),
                max(range(len(block)), key=lambda idx: block[idx]["depth"]),
            }
            chosen_points.extend(block[idx] for idx in sorted(key_positions))
        displayed_runs.append(chosen_points)

    snapshot_data.update(
        runs=displayed_runs,
        observation_count=len(raw_rows),
        display_count=sum(map(len, displayed_runs)),
    )
    return snapshot_data


def context_main(path: str | Path, output: str | Path) -> int:
    """`--curve-context` 内部子进程入口：准备曲线显示快照并将结果或错误原子写入 `output`。

    Args:
        path: 输入的原始曲线快照 JSON 路径。
        output: 输出的已降采样结果 JSON 路径。

    Returns:
        int: 进程退出码（`0` 表示成功，`1` 表示发生异常）。
    """

    try:
        atomic_json(output, dict(data=prepare_context(path)))
    except Exception as exc:
        atomic_json(output, dict(error=str(exc)))
        return 1
    return 0


def segment_spans(
    result: dict[str, Any] | None,
) -> list[tuple[int, datetime, datetime]]:
    """从测点处理结果中提取所有有效观测片段的 `(片段编号, 起点时间, 终点时间)` 列表。

    Args:
        result: 单测点处理结果字典。

    Returns:
        list[tuple[int, datetime, datetime]]: 按起点与终点时间升序排列的片段时间跨度三元组列表。
    """

    spans: list[tuple[int, datetime, datetime]] = []
    for segment_id, segment in enumerate((result or {}).get("segments", [])):
        try:
            start_dt = moment(segment.get("start"))
            end_dt = moment(segment.get("end"))
            if start_dt is not None and end_dt is not None and start_dt <= end_dt:
                spans.append((segment_id, start_dt, end_dt))
        except (TypeError, ValueError):
            continue
    return sorted(spans, key=lambda span: (span[1], span[2]))


def time_ranges(
    result: dict[str, Any] | None,
    row: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime], list[int]]:
    """计算当前选中目标整点在曲线组件中的默认时间视野、全局可浏览视野及邻近片段编号。

    视野规则：
    1. 收集覆盖目标整点 `target` 的片段、紧邻 `target` 之前的最后一个片段、紧邻 `target` 之后的第一个片段，
       以及当前整点实际匹配到的片段 `matched`，共同构成邻近上下文片段集合 `neighbors`。
    2. 若当前整点成功匹配片段且无 `"segment_reused"` / `"missing_segment"` / `"missing_layer"` 异常，
       默认视野 `default_view` 直接聚焦于所匹配片段的起止范围。
    3. 若当前整点存在缺测或片段复用异常，默认视野自动扩展至 `target ± 30 分钟` 及前后邻近片段范围，
       若位于数据首尾外侧还额外纳入观测起点/终点留白，方便用户直观判断缺测或复用原因。

    Args:
        result: 单测点处理结果字典。
        row: 当前选中的整点行字典（含 `"target"`、`"segment"`、`"status"`）。
        context: 可选的完整曲线快照上下文（含 `"start"`、`"end"`）。

    Returns:
        tuple: `(默认时间视野 (left, right), 全局可浏览时间范围 (min_t, max_t), 邻近片段编号列表)`。
    """

    target_time = moment(row["target"])
    all_spans = segment_spans(result)
    matched_segment_id = row.get("segment")

    containing_spans = [
        span for span in all_spans if span[1] <= target_time <= span[2]
    ]
    before_spans = [span for span in all_spans if span[2] < target_time]
    after_spans = [span for span in all_spans if span[1] > target_time]

    neighbor_spans = [*containing_spans]
    if before_spans:
        neighbor_spans.append(max(before_spans, key=lambda span: span[2]))
    if after_spans:
        neighbor_spans.append(min(after_spans, key=lambda span: span[1]))

    matched_span = next(
        (span for span in all_spans if span[0] == matched_segment_id), None
    )
    if matched_span and matched_span not in neighbor_spans:
        neighbor_spans.append(matched_span)

    has_abnormal_status = bool(
        {"segment_reused", "missing_segment", "missing_layer"}.intersection(
            row.get("status", [])
        )
    )
    if matched_span and not has_abnormal_status:
        default_view = (
            matched_span[1],
            max(matched_span[2], matched_span[1] + timedelta(seconds=1)),
        )
    else:
        left_candidates = [target_time - timedelta(minutes=30)] + [
            span[1] for span in neighbor_spans
        ]
        right_candidates = [target_time + timedelta(minutes=30)] + [
            span[2] for span in neighbor_spans
        ]
        if not before_spans and context and context.get("start"):
            left_candidates.append(moment(context["start"]) - timedelta(minutes=30))
        if not after_spans and context and context.get("end"):
            right_candidates.append(moment(context["end"]) + timedelta(minutes=30))
        default_view = (min(left_candidates), max(right_candidates))

    browse_boundary_points = [
        target_time - timedelta(minutes=30),
        target_time + timedelta(minutes=30),
        *default_view,
    ]
    browse_boundary_points.extend(
        bound for span in all_spans for bound in span[1:]
    )
    for boundary_key in ("start", "end"):
        if context and context.get(boundary_key):
            browse_boundary_points.append(moment(context[boundary_key]))

    return (
        default_view,
        (min(browse_boundary_points), max(browse_boundary_points)),
        [span[0] for span in neighbor_spans],
    )
