"""单测点整点匹配、垂向各层选点与质量状态汇总模块（纯内存计算，不负责文件读写）。

本模块将已解析排序的观测记录 `records`、测点配置 `point` 与全局筛选参数 `rules`
转换为结构化的 `PointResult`：
1. 根据测点起止时间限定（`point.start` / `point.end`）或观测数据首尾时间确定目标整点序列；
2. 调用 `algorithm.build_segments` 识别有效下放剖面片段；
3. 对每个目标整点匹配最近的有效观测段（记录 `missing_segment` 或 `segment_reused` 状态）；
4. 在匹配段内按测点的垂向分层方案（系统标准规则、固定系统层数或自定义规则）选出表层、各相对层与底层代表点，
   并检查缺层（`missing_layer`）、跨层记录复用（`cross_layer_reused`）和深度容差（`depth_out_of_tolerance`）。
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from .algorithm import (
    build_segments,
    choose_segment,
    select_depth_matches,
    select_surface_record,
)
from .data_types import HourResult, LayerSelection, Observation, PointResult
from .input_policy import LIMITS, Budget, enforce
from .models import Point, Rules


def floor_hour(timestamp: datetime) -> datetime:
    """保留日期与小时，将分钟、秒与微秒清零，得到所在小时整点（如 `08:23:45 -> 08:00:00`）。"""
    return timestamp.replace(minute=0, second=0, microsecond=0)


def process(
    records: list[Observation],
    point: Point,
    rules: Rules,
    cancelled: Callable[[], bool] = lambda: False,
) -> PointResult:
    """按测点目标时间窗口匹配下放片段，并从匹配片段中提取各垂向层位的代表观测记录。

    业务与安全约定：
    - `records` 必须已按时间戳升序排列；
    - 缺测、片段复用、跨层复用和深度超差仅写入 `status` 标记列表供界面与用户复核，绝不伪造、平滑或插值观测值；
    - 当观测条数、片段数、目标整点数或计算量超出 `LIMITS` 时抛出 `CapacityError`，由批次执行器按测点隔离报错。

    Args:
        records: 当前测点已按时间排序的有效观测记录列表。
        point: 测点配置对象（含编号 `code`、参考水深 `depth`、分层规则与可选起止时间）。
        rules: 片段识别、表层评分与深度容差参数配置对象。
        cancelled: 协作式取消检查回调，返回 `True` 时抛出 `InterruptedError`。

    Returns:
        PointResult: 包含分层方案、全部观测片段与逐整点选层结果 `rows` 的字典。
    """
    point.validate()
    rules.validate()
    if not records:
        raise ValueError("没有有效观测")

    budget = Budget(cancelled=cancelled)
    enforce(len(records), LIMITS.point_records, "处理观测数")
    budget.check()

    # 1. 识别全部有效下放观测段
    segments = build_segments(records, point.depth, rules)
    enforce(len(segments), LIMITS.segments, "候选片段数")
    budget.check()

    # 2. 计算目标整点起止范围：显式开始时间向上取整点，结束时间包含其所在整点
    start = (
        datetime.fromisoformat(point.start)
        if point.start
        else floor_hour(records[0]["timestamp"])
    )
    end = (
        datetime.fromisoformat(point.end)
        if point.end
        else floor_hour(records[-1]["timestamp"])
    )
    hour = floor_hour(start)
    if hour < start:
        hour += timedelta(hours=1)

    count = max(0, int((end - hour).total_seconds() // 3600) + 1)
    enforce(count, LIMITS.targets, "目标整点数")
    work = count * len(segments)
    enforce(work, LIMITS.work, "整点与片段匹配次数")

    # 3. 逐目标整点执行片段匹配、垂向分层选点与质量状态判定
    plan = point.layer_plan
    rows: list[HourResult] = []
    used_segment_indices: set[int] = set()

    for index in range(count):
        if cancelled():
            raise InterruptedError("用户取消")
        budget.check()

        target = hour + timedelta(hours=index)
        segment, distance = choose_segment(segments, target, rules)
        if distance:
            offset_seconds = distance.total_seconds()
        elif segment:
            offset_seconds = 0
        else:
            offset_seconds = None

        row: HourResult = dict(
            target=target.isoformat(),
            status=[],
            segment=None,
            offset_seconds=offset_seconds,
            selections=[],
        )

        if segment is None:
            row["status"].append("missing_segment")
        else:
            work += len(segment["records"]) * (len(plan) + 1)
            enforce(work, LIMITS.work, "累计选点计算次数")
            segment_index = segments.index(segment)
            row["segment"] = segment_index

            # 标记多个目标整点共用同一有效观测段（片段复用）
            if segment_index in used_segment_indices:
                row["status"].append("segment_reused")
            used_segment_indices.add(segment_index)

            # 预先为所有相对水深层（如 0.2H, 0.6H, 0.8H）匹配最接近目标深度的记录
            matches = {
                relative_depth: observation
                for relative_depth, _, observation in select_depth_matches(
                    segment["records"],
                    [
                        layer["relative"]
                        for layer in plan
                        if layer["kind"] == "relative"
                    ],
                    point.depth,
                )
            }

            used_record_identities: set[tuple[str, int]] = set()
            for layer in plan:
                theoretical_depth = (
                    layer.get("relative", 1) * point.depth
                    if layer["kind"] != "surface"
                    else None
                )
                if layer["kind"] == "relative":
                    observation = matches.get(layer["relative"])
                elif layer["kind"] == "bottom":
                    observation = segment["deepest"]
                else:
                    observation = select_surface_record(
                        segment["records"], segment["start"], target, rules
                    )

                layer_status: list[str] = []
                delta = (
                    observation["depth"] - theoretical_depth
                    if observation and theoretical_depth is not None
                    else None
                )

                if observation is None:
                    layer_status.append("missing_layer")
                else:
                    # 检查同一目标整点的不同层位是否选用了同一文件中的同一物理行记录（跨层复用）
                    identity = (observation["file"], observation["line"])
                    if identity in used_record_identities:
                        layer_status.append("cross_layer_reused")
                    used_record_identities.add(identity)

                    if layer["kind"] == "relative":
                        # 绝对深度容差与相对深度容差同时启用时，取更严格者；均未启用时标记 tolerance_unset
                        allowed_limits = [
                            limit_val
                            for limit_val in (
                                rules.tolerance_m,
                                (
                                    rules.tolerance_ratio * theoretical_depth
                                    if rules.tolerance_ratio is not None
                                    else None
                                ),
                            )
                            if limit_val is not None
                        ]
                        if not allowed_limits:
                            layer_status.append("tolerance_unset")
                        elif abs(delta) > min(allowed_limits):
                            layer_status.append("depth_out_of_tolerance")

                selected: LayerSelection = dict(
                    label=layer["label"],
                    target_depth=theoretical_depth,
                    deviation_m=delta,
                    status=layer_status,
                    observation=observation,
                )
                row["selections"].append(selected)
                row["status"].extend(
                    flag for flag in layer_status if flag not in row["status"]
                )

        rows.append(row)

    # 4. 标识本次处理所采用的分层模式：custom（自定义比例）、standard（随水深自动选择）或 override（固定系统层数）
    if point.layer_positions is not None:
        layer_mode = "custom"
    elif point.layers is None:
        layer_mode = "standard"
    else:
        layer_mode = "override"

    return dict(
        point=point.code,
        layer_count=point.layer_count,
        layer_plan=plan,
        layer_mode=layer_mode,
        rows=rows,
        segments=segments,
    )
