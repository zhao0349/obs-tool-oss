"""OBS 剖面分段、整点匹配与垂向层位选点核心算法模块（业务规则详见 docs/ALGORITHM.md）。

本模块实现面向水文外业调查数据的观测分段与垂向选点算法，主要包含四个阶段：
1. **垂向分层方案生成（`get_layer_plan` / `custom_layer_plan`）**：
   支持系统标准分层（1 / 2 / 3 / 5 / 6 层）与用户自定义相对水深比例（`0` 代表表层，`1` 代表底层，
   `(0, 1)` 代表相对深度 `relative * H`）。
2. **基于动态分位数阈值的有效观测段识别（`build_segments`）**：
   结合全序列水深的 20% 背景分位数、90% 峰值分位数与参考水深 `H`，动态计算入水触发阈值
   `start_threshold`、出水停止阈值 `stop_threshold` 与边缘裁剪阈值 `resume_threshold`，
   拼接前缓冲 `pre_minutes` 与受限后缓冲 `tail_minutes`，生成完整的下放剖面片段（`Segment`）。
3. **目标整点与观测段匹配（`choose_segment`）**：
   以每个观测段的最深点时刻 `apex` 为锚点，在 `max_offset_minutes` 时间窗内选取离目标整点最近的片段
   （时间差并列时优先选取较早的片段）。
4. **表层加权评分与相对水深层选点（`select_surface_record` / `select_depth_matches`）**：
   - 表层：仅在 `depth > surface_min` 的候选点中最小化深度、距整点分钟数与距片段起点分钟数的加权和；
   - 相对水深层：在不重复选用同一片段下标的前提下，为每个相对层挑选 `|depth - relative * H|` 最小的点。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
import math

from .data_types import LayerConfig, Observation, RelativeDepthMatch, Segment
from .models import Rules


# ==============================================================================
# 1. 垂向分层方案构建（系统标准分层与自定义分层）
# ==============================================================================


def custom_layer_plan(positions: list[float]) -> list[LayerConfig]:
    """校验升序排列的自定义层位比例列表，并将 `0`、`1` 分别转换为表层和底层配置。

    Args:
        positions: 严格递增的层位相对比例列表（取值范围 `[0.0, 1.0]`）。
            - `0`（或 `0.0`）：代表“表层”（`kind="surface"`，按表层评分公式选点）；
            - `1`（或 `1.0`）：代表“底层”（`kind="bottom"`，直接取片段内最深实测点）；
            - `0 < p < 1`：代表相对水深层（`kind="relative"`，目标采样深度为 `p * H`）。

    Returns:
        list[LayerConfig]: 由浅入深排列的层位配置字典列表。

    Raises:
        ValueError: 当层位列表为空、未严格递增或超出 `[0, 1]` 范围时由 `validate_positions` 抛出。
    """
    from .models import validate_positions

    validate_positions(positions)
    plan: list[LayerConfig] = []
    for relative_depth in positions:
        if relative_depth == 0:
            plan.append(dict(kind="surface", label="表层"))
        elif relative_depth == 1:
            plan.append(dict(kind="bottom", label="底层"))
        else:
            plan.append(
                dict(
                    kind="relative",
                    label=f"{relative_depth:g}H",
                    relative=relative_depth,
                )
            )
    return plan


def get_layer_plan(layer_count: int) -> list[LayerConfig]:
    """根据系统标准分层数量返回对应的垂向层位配置列表。

    水文测验标准分层规则：
    - 1 层（`0 < H < 2m`）：`0.6H`
    - 2 层（`2m <= H < 5m`）：`0.2H`、`0.8H`
    - 3 层（`5m <= H < 8m`）：`0.2H`、`0.6H`、`0.8H`
    - 5 层（`8m <= H < 11m`）：`表层`、`0.2H`、`0.6H`、`0.8H`、`底层`
    - 6 层（`H >= 11m`）：`表层`、`0.2H`、`0.4H`、`0.6H`、`0.8H`、`底层`

    Args:
        layer_count: 需要输出的层数，仅支持 1、2、3、5 或 6。

    Returns:
        list[LayerConfig]: 每个元素描述一层的计算方式，包含 `kind`、`label` 及可选的 `relative` 比例。

    Raises:
        ValueError: 当 `layer_count` 不是 1、2、3、5 或 6 时抛出。
    """
    if layer_count == 1:
        return [{"kind": "relative", "label": "0.6H", "relative": 0.6}]
    if layer_count == 2:
        return [
            {"kind": "relative", "label": "0.2H", "relative": 0.2},
            {"kind": "relative", "label": "0.8H", "relative": 0.8},
        ]
    if layer_count == 3:
        return [
            {"kind": "relative", "label": "0.2H", "relative": 0.2},
            {"kind": "relative", "label": "0.6H", "relative": 0.6},
            {"kind": "relative", "label": "0.8H", "relative": 0.8},
        ]
    if layer_count == 5:
        return [
            {"kind": "surface", "label": "表层"},
            {"kind": "relative", "label": "0.2H", "relative": 0.2},
            {"kind": "relative", "label": "0.6H", "relative": 0.6},
            {"kind": "relative", "label": "0.8H", "relative": 0.8},
            {"kind": "bottom", "label": "底层"},
        ]
    if layer_count == 6:
        return [
            {"kind": "surface", "label": "表层"},
            {"kind": "relative", "label": "0.2H", "relative": 0.2},
            {"kind": "relative", "label": "0.4H", "relative": 0.4},
            {"kind": "relative", "label": "0.6H", "relative": 0.6},
            {"kind": "relative", "label": "0.8H", "relative": 0.8},
            {"kind": "bottom", "label": "底层"},
        ]

    raise ValueError(f"暂不支持的分层数量：{layer_count}")


# ==============================================================================
# 2. 表层代表点加权评分选取与分位数计算
# ==============================================================================


def select_surface_record(
    segment_records: Sequence[Observation],
    segment_start: datetime,
    hour_start: datetime,
    rules: Rules = Rules(),
) -> Observation | None:
    """从单个有效观测片段内选出最符合“表层”特征的代表点。

    评分规则：
    - 仅考虑实测水深严格大于 `rules.surface_min`（默认 0.5m）的候选记录，排除水面晃动或空气中读数；
    - 对每条候选记录计算综合评分 `score`（越小越优）：
      `score = depth * depth_weight + hour_offset_min * hour_weight + entry_offset_min * entry_weight`
      其中 `hour_offset_min` 为距目标整点的绝对分钟数，`entry_offset_min` 为距片段起点的分钟数；
    - 若多个候选点 `score` 完全相等，依次按 `(depth, hour_offset_min, entry_offset_min, timestamp)` 升序打破平局。

    Args:
        segment_records: 当前匹配观测片段内的全部观测记录。
        segment_start: 当前观测片段的起始时刻。
        hour_start: 当前匹配的目标整点时刻。
        rules: 包含表层下限 `surface_min` 及三项评分权重的规则配置对象。

    Returns:
        Observation | None: 综合得分最小的表层观测记录；若片段内无任何记录大于 `surface_min` 则返回 `None`。
    """
    candidates: list[
        tuple[tuple[float, float, float, float, datetime], Observation]
    ] = []
    for obs in segment_records:
        try:
            depth = obs["depth"]
            timestamp = obs["timestamp"]
        except KeyError:
            continue

        # 表层候选点必须严格大于表层深度下限（默认 0.5m）
        if depth <= rules.surface_min:
            continue

        entry_offset_min = max(0.0, (timestamp - segment_start).total_seconds()) / 60.0
        hour_offset_min = abs((timestamp - hour_start).total_seconds()) / 60.0
        score = (
            depth * rules.depth_weight
            + hour_offset_min * rules.hour_weight
            + entry_offset_min * rules.entry_weight
        )
        candidates.append(
            ((score, depth, hour_offset_min, entry_offset_min, timestamp), obs)
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _quantile(values: Sequence[float], q: float) -> float:
    """使用线性插值法计算数值序列的指定分位数。

    Args:
        values: 需要统计的浮点数序列。
        q: 分位数位置，取值范围 `[0.0, 1.0]`（如 `0.5` 表示中位数，`0.75` 表示上四分位数）。

    Returns:
        float: 对应分位上的插值结果；当输入序列为空时返回 `0.0`。

    Raises:
        ValueError: 当 `q` 超出 `[0.0, 1.0]` 范围时抛出。
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"分位数 q 必须位于 [0, 1]，当前值：{q}")

    if not values:
        return 0.0

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    pos = q * (len(ordered) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]

    fraction = pos - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


# ==============================================================================
# 3. 有效下放观测段（Segment）识别与前后缓冲拼接
# ==============================================================================


def build_segments(
    records: Sequence[Observation], h_value: float, rules: Rules = Rules()
) -> list[Segment]:
    """基于动态分位数阈值与水深包络，从连续观测序列中识别每次有效的仪器下放观测段。

    分段原理与边缘处理：
    1. **动态三级深度门槛**：
       - `low_q`：全序列水深的 20% 分位数（不低于 `rules.min_depth`），代表水面或甲板背景深度；
       - `high_q`：全序列水深的 90% 分位数，代表正常下放剖面的典型深水水平；
       - `span = max(high_q - low_q, 0.15 * H, 0.5)`：有效水深动态波动幅度；
       - `start_threshold`（触发下放门槛）：`max(min_depth, low_q + 0.65 * span, 0.35 * H)`；
       - `stop_threshold`（判定回收结束门槛）：`max(min_depth, low_q + 0.45 * span, 0.25 * H)`；
       - `resume_threshold`（裁剪片段首尾浅水边缘门槛）：`max(min_depth, low_q + 0.30 * span, 0.20 * H)`。
    2. **前后缓冲与断档切分**：
       - 在尚未触发片段时，使用滑动双端队列 `prebuffer` 保留最近 `pre_minutes` 分钟的观测；
       - 一旦实测水深达到 `start_threshold`，将 `prebuffer` 连同当前点转入活动片段 `active`；
       - 在活动片段中，若相邻两点间隔超过 `gap_minutes` 分钟，或连续 `stop_count` 个点低于 `stop_threshold`，
         则调用 `flush()` 闭合当前片段，并保留核心区末尾最多 `tail_minutes` 分钟的尾部记录。

    Args:
        records: 已按时间升序排列的原始观测记录序列。
        h_value: 测点参考总水深 `H`（单位：米），用于约束深度阈值下限。
        rules: 片段识别参数配置（含 `min_depth`, `gap_minutes`, `pre_minutes`, `tail_minutes`, `stop_count`）。

    Returns:
        list[Segment]: 识别出的全部有效观测片段列表，每个片段包含 `records`、`start`、`end`、`apex`、`deepest` 和 `key`。

    Raises:
        RuntimeError: 当输入观测记录字典缺少 `depth` 或 `timestamp` 关键字段时抛出。
    """
    if not records:
        return []

    try:
        depths = [item["depth"] for item in records]
    except KeyError as exc:
        raise RuntimeError(f"观测记录缺少深度字段：{exc}") from exc

    low_q = max(rules.min_depth, _quantile(depths, 0.2))  # 20% 分位背景水深
    high_q = max(low_q, _quantile(depths, 0.9))  # 90% 分位典型下放深度
    span = max(high_q - low_q, h_value * 0.15, 0.5)  # 动态波动幅度下限保护

    start_threshold = max(
        rules.min_depth, low_q + span * 0.65, h_value * 0.35
    )  # 触发进入有效段的深度门槛
    stop_threshold = max(
        rules.min_depth, low_q + span * 0.45, h_value * 0.25
    )  # 判定回收结束的深度门槛
    resume_threshold = max(
        rules.min_depth, low_q + span * 0.30, h_value * 0.20
    )  # 裁剪片段首尾核心区的深度门槛

    segments: list[Segment] = []
    prebuffer: deque[Observation] = deque()
    active: list[Observation] = []
    last_time: datetime | None = None
    below_counter = 0
    peak_depth = rules.min_depth

    def drain_prebuffer(limit_ts: datetime) -> None:
        """从前置缓冲队列头部移除早于 `limit_ts - pre_minutes` 的过期浅水观测。"""
        while prebuffer and limit_ts - prebuffer[0]["timestamp"] > timedelta(
            minutes=rules.pre_minutes
        ):
            prebuffer.popleft()

    def flush() -> None:
        """裁剪当前活动片段的首尾边缘、拼接允许的前后缓冲并写入 `segments` 列表，随后重置检测状态。"""
        nonlocal active, below_counter, peak_depth
        if not active:
            return

        if peak_depth < start_threshold:
            active = []
            below_counter = 0
            peak_depth = rules.min_depth
            return

        # 向前裁剪低于 resume_threshold 的前导浅水记录，定位核心段起点
        start_idx = 0
        while start_idx < len(active) and active[start_idx]["depth"] < resume_threshold:
            start_idx += 1
        trimmed = active[start_idx:]
        if not trimmed:
            active = []
            below_counter = 0
            peak_depth = rules.min_depth
            return

        # 向后裁剪低于 resume_threshold 的尾部浅水记录，定位核心段终点
        end_idx = len(trimmed) - 1
        while end_idx >= 0 and trimmed[end_idx]["depth"] < resume_threshold:
            end_idx -= 1
        if end_idx < 0:
            active = []
            below_counter = 0
            peak_depth = rules.min_depth
            return

        # 保留核心段末尾之后不超过 tail_minutes 分钟的尾部记录
        core = trimmed[: end_idx + 1]
        tail_candidates = trimmed[end_idx + 1 :]
        tail = [
            item
            for item in tail_candidates
            if item["timestamp"] - core[-1]["timestamp"]
            <= timedelta(minutes=rules.tail_minutes)
        ]
        seg_records = core + tail

        # 拼接核心段起点之前 pre_minutes 分钟内的前置缓冲记录
        start_time = seg_records[0]["timestamp"]
        if prebuffer:
            drain_prebuffer(start_time)
            prefix = [
                item
                for item in prebuffer
                if timedelta(0)
                < start_time - item["timestamp"]
                <= timedelta(minutes=rules.pre_minutes)
            ]
            if prefix:
                seg_records = prefix + seg_records
                start_time = seg_records[0]["timestamp"]

        end_time = seg_records[-1]["timestamp"]
        deepest = max(seg_records, key=lambda item: item["depth"])

        from .input_policy import LIMITS, enforce

        enforce(len(segments) + 1, LIMITS.segments, "候选片段数")
        segments.append(
            {
                "records": seg_records,
                "start": start_time,
                "end": end_time,
                "apex": deepest["timestamp"],
                "deepest": deepest,
                "key": (start_time, end_time),
            }
        )

        prebuffer.clear()
        active = []
        below_counter = 0
        peak_depth = rules.min_depth

    for obs in records:
        try:
            ts = obs["timestamp"]
            depth = obs["depth"]
        except KeyError as exc:
            raise RuntimeError(f"观测记录缺少关键字段：{exc}") from exc

        # 若相邻两条有效记录的时间差超过断档上限 gap_minutes，立即结算已有片段并清空前缓冲
        if last_time is not None and ts - last_time > timedelta(
            minutes=rules.gap_minutes
        ):
            flush()
            prebuffer.clear()

        last_time = ts

        if active:
            active.append(obs)
            peak_depth = max(peak_depth, depth)
            if depth < stop_threshold:
                below_counter += 1
                if below_counter >= rules.stop_count:
                    flush()  # 连续出现达到 stop_count 个低于停止阈值的点，结束当前片段
            else:
                below_counter = 0
        else:
            prebuffer.append(obs)
            drain_prebuffer(ts)
            if depth >= start_threshold:
                active = list(prebuffer)
                peak_depth = max(item["depth"] for item in active)
                below_counter = 0

    flush()
    return segments


# ==============================================================================
# 4. 整点与观测段匹配及相对水深层选点
# ==============================================================================


def choose_segment(
    segments: Sequence[Segment], hour_start: datetime, rules: Rules = Rules()
) -> tuple[Segment | None, timedelta | None]:
    """从全部有效观测段中挑选峰值时刻（`apex`）最接近目标整点 `hour_start` 的片段。

    匹配与平局决胜规则：
    - 计算每个片段最深时刻 `seg["apex"]` 与 `hour_start` 的绝对时间差 `|seg["apex"] - hour_start|`；
    - 若存在两个片段与目标整点的时间差完全相等（如一前一后各相差 60 分钟），优先选择 `apex` 较早的片段；
    - 仅当最小时间差 `<= timedelta(minutes=rules.max_offset_minutes)`（含边界）时视为匹配成功。

    Args:
        segments: 当前测点已识别出的全部观测片段列表。
        hour_start: 待匹配的目标整点时刻。
        rules: 包含最大允许时间差 `max_offset_minutes` 的规则配置对象。

    Returns:
        tuple[Segment | None, timedelta | None]:
        - 无任何候选片段时返回 `(None, None)`；
        - 最小时间差在允许范围内时返回 `(最佳片段, 时间差 timedelta)`；
        - 最小时间差超出上限时返回 `(None, 最小时间差 timedelta)`。
    """
    if not segments:
        return None, None

    best = min(
        segments, key=lambda seg: (abs(seg["apex"] - hour_start), seg["apex"])
    )
    distance = abs(best["apex"] - hour_start)
    if distance <= timedelta(minutes=rules.max_offset_minutes):
        return best, distance
    return None, distance


def select_depth_matches(
    segment: Sequence[Observation],
    relative_depths: Iterable[float],
    h_value: float,
) -> list[RelativeDepthMatch]:
    """在单个观测段内依次为各相对水深比例（如 `0.2`, `0.6`, `0.8`）挑选深度最接近的观测记录。

    选点规则：
    - 将每个相对水深比例 `rel` 折算为理论目标绝对深度 `target = rel * h_value`；
    - 通过 `used_indices` 记录已被前序相对层占用的片段内下标，优先避免不同相对层选到同一条记录；
    - 对每个目标深度选取 `|depth - target|` 最小的未占用记录（深度差在 `1e-9` 内并列时取时间戳较早的记录）。

    Args:
        segment: 单个匹配片段内的观测记录序列。
        relative_depths: 需要提取的相对水深比例序列（如 `[0.2, 0.6, 0.8]`）。
        h_value: 测点参考总水深 `H`（单位：米）。

    Returns:
        list[RelativeDepthMatch]: `(相对水深比例, 目标绝对深度, 选中的 Observation 记录)` 三元组列表。

    Raises:
        RuntimeError: 当片段内的记录缺少 `depth` 或 `timestamp` 字段时抛出。
    """
    if not segment:
        return []

    chosen: list[RelativeDepthMatch] = []
    used_indices: set[int] = set()
    targets = [(rel, rel * h_value) for rel in relative_depths]

    for relative, target in targets:
        best_idx: int | None = None
        best_score = math.inf
        for idx, obs in enumerate(segment):
            if idx in used_indices:
                continue
            try:
                depth_value = obs["depth"]
                obs_time = obs["timestamp"]
            except KeyError as exc:
                raise RuntimeError(f"片段记录缺少字段：{exc}") from exc

            score = abs(depth_value - target)
            if score < best_score:
                best_score = score
                best_idx = idx
            elif (
                math.isclose(score, best_score, rel_tol=1e-9, abs_tol=1e-9)
                and best_idx is not None
            ):
                try:
                    if obs_time < segment[best_idx]["timestamp"]:
                        best_idx = idx
                except KeyError as exc:
                    raise RuntimeError(f"片段记录缺少时间字段：{exc}") from exc

        if best_idx is None:
            continue
        used_indices.add(best_idx)
        chosen.append((relative, target, segment[best_idx]))

    return chosen
