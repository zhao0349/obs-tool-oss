"""测点参考水深（H）智能建议值推算模块（基于相邻观测支持的剖面峰值中位数）。

业务定位与安全边界：
    在未提供现场实测水深时，本模块从测点关联的全部有效观测记录中识别多次下放剖面（Cast），
    提取每次下放的有效峰值水深并取中位数作为建议水深 `H`。
    注意：自动推算结果仅作为辅助建议，当剖面数量少于 3 次或峰值四分位距（IQR）超过中位水深的 20% 时，
    标记为 `confidence="需核对"`，必须由用户在处理前的水深确认弹窗中人工核实。
"""

from datetime import timedelta
import math
from statistics import median

from .algorithm import _quantile
from .data_types import DepthEstimate, Observation

METHOD = "supported-cast-peaks-median-v1"


def estimate_depth(records: list[Observation]) -> DepthEstimate:
    """利用具有前后相邻深水点支持的下放剖面峰值中位数估算参考水深 `H`（纯函数，不修改输入列表）。

    核心算法步骤：
    1. **三点相邻支持过滤（抑制孤立深度毛刺）**：
       遍历所有非负有限深度记录，仅当某条记录与其前后紧邻记录的时间间隔均不超过 5 分钟，
       且前后紧邻记录的深度均达到当前记录深度的 50% 及以上（`min(prev, next) >= 0.5 * current`）时，
       才将该点视为“受相邻观测支持的真实深水点”（排除仅持续 1 个采样点的传感器跳变噪声）。
    2. **动态入水包络与剖面切分**：
       取全部受支持深水点的 99% 分位数作为水深包络 `envelope`，并以 `threshold = max(0.1, 0.35 * envelope)`
       作为入水深度门槛，按 5 分钟断档或深度回落至门槛以下切分独立下放剖面，提取每次剖面的受支持最深点 `peaks`。
    3. **建议值与可信度判定**：
       - 建议水深 `value`：取各次剖面峰值水深的中位数（保留 3 位小数）；
       - 离散度 `peak_iqr_m`：取各次剖面峰值水深的四分位距 `Q75 - Q25`；
       - 可信度 `confidence`：若有效剖面数 `< 3` 或 `peak_iqr_m > 0.2 * value`，标记为 `'需核对'`，否则为 `'稳定'`。

    Args:
        records: 单个测点已按时间排序的有效观测记录列表。

    Returns:
        DepthEstimate: 包含推算水深 `value`、方法标识 `method`、剖面数 `casts`、峰值明细 `peaks`
        及可信度 `confidence` 的字典；若有效观测不足或未识别出剖面则 `value` 为 `None`。
    """
    valid = [
        observation
        for observation in records
        if math.isfinite(observation["depth"]) and observation["depth"] >= 0
    ]
    if len(valid) < 3:
        return dict(value=None, method=METHOD, reason="有效观测不足", casts=0)

    gap = timedelta(minutes=5)

    # 步骤 1：三点相邻支持检查——孤立单点尖峰由于两侧深度不足，不会被纳入峰值候选集合
    supported: list[Observation] = []
    for index in range(1, len(valid) - 1):
        previous, observation, following = valid[index - 1 : index + 2]
        if (
            observation["timestamp"] - previous["timestamp"] <= gap
            and following["timestamp"] - observation["timestamp"] <= gap
            and min(previous["depth"], following["depth"]) >= observation["depth"] * 0.5
        ):
            supported.append(observation)

    if not supported or max(observation["depth"] for observation in supported) <= 0.1:
        return dict(
            value=None, method=METHOD, reason="未找到有相邻观测支持的有效剖面", casts=0
        )

    # 步骤 2：计算 99% 水深包络与 35% 入水阈值，切分连续深水段并提取每段峰值
    envelope = _quantile([observation["depth"] for observation in supported], 0.99)
    threshold = max(0.1, envelope * 0.35)
    supported_ids = {
        (observation["file"], observation["line"]) for observation in supported
    }
    peaks: list[Observation] = []
    active: list[Observation] = []
    previous_time = None

    def flush() -> None:
        """从当前连续深水段中选出具有相邻观测支持的最深记录加入 `peaks`，随后清空活动段。"""
        if active:
            candidates = [
                observation
                for observation in active
                if (observation["file"], observation["line"]) in supported_ids
            ]
            if candidates:
                peaks.append(
                    max(candidates, key=lambda observation: observation["depth"])
                )
            active.clear()

    for observation in valid:
        if previous_time and observation["timestamp"] - previous_time > gap:
            flush()
        previous_time = observation["timestamp"]
        if observation["depth"] >= threshold:
            active.append(observation)
        else:
            flush()
    flush()

    if not peaks:
        return dict(value=None, method=METHOD, reason="未识别出有效剖面", casts=0)

    # 步骤 3：汇总各次剖面峰值的中位数与四分位距（IQR），评估是否需要人工核对
    depths = [observation["depth"] for observation in peaks]
    value = round(median(depths), 3)
    peak_iqr = round(_quantile(depths, 0.75) - _quantile(depths, 0.25), 3)
    return dict(
        value=value,
        method=METHOD,
        casts=len(peaks),
        peak_iqr_m=peak_iqr,
        confidence="需核对" if len(peaks) < 3 or peak_iqr > value * 0.2 else "稳定",
        envelope_m=round(envelope, 3),
        threshold_m=round(threshold, 3),
        observed_start=valid[0]["timestamp"].isoformat(),
        observed_end=valid[-1]["timestamp"].isoformat(),
        peaks=[
            dict(
                depth=observation["depth"],
                timestamp=observation["timestamp"].isoformat(),
                file=observation["file"],
                line=observation["line"],
            )
            for observation in peaks
        ],
    )
