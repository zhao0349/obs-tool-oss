"""时间校正专用的下放候选片段识别、完整抛测动作合并与分界歧义判定模块。

设计目标与业务背景：
    本模块服务于“实用工具 → 时间校正 → 按抛测规律匹配”模式，不改变主界面普通水深建议或 CSV 分层采样逻辑。
    在外业抛测时，单次整点施测可能因水流冲击或中途停顿产生双峰或多段局部深水候选；
    若机械地将每个深水候选都当作一次独立整点抛测，会导致后续整点对齐发生小时级累积漂移。

核心算法准则：
1. **局部包络阈值计算（`excursions`）**：
   沿用三点相邻支持、99% 分位的 35% 包络阈值（底限 0.1m）与 5 分钟断档规则；
   但先按真实时间断档（> 5 分钟）或连续 3 条近零水深（`<= 0.1m`）将观测划分为局部过程包络，
   分别计算入水阈值，防止同一文件中深水抛测的高阈值吞掉浅水抛测。
2. **基于连续性与回撤证据的动作合并**：
   对峰值时间差 `<= 15` 分钟且中间无 5 分钟断档的两个相邻候选片段：
   - 若中间出现连续 3 条 `<= 0.1m` 的近零浅水停留（`surface == True`），判定为两次独立抛测；
   - 若中间未回到水面，且最浅处未低于前峰水深的一半（`not lows`），或虽低于前峰一半但仅含 1～2 条
     满足两侧正常采样节奏的短暂浅值（`short_returns == True`），合并为同一次完整抛测动作；
   - 若中间未回到水面，但回撤较深且持续过久或缺乏两侧正常采样节奏支持，记入 `uncertainties` 分界不确定列表，
     阻止后续自动改写文件。
"""

from datetime import timedelta
from statistics import median

from .algorithm import _quantile
from .input_policy import LIMITS, Budget, enforce


def identify_actions(records, cancelled=lambda: False):
    """从按文件物理顺序排列的观测序列中识别下放候选片段，合并完整抛测动作并标记分界歧义。

    Args:
        records: 按原始文件行顺序排列的观测记录列表。
        cancelled: 协作式取消检查回调。

    Returns:
        dict: 包含以下键的识别结果字典：
            - `actions`: 合并后的完整抛测动作列表（每项取合并组内水深最大的峰值点作为代表锚点）；
            - `candidates`: 初步识别出的全部原始下放候选片段列表（带 1-based `id` 与局部进入阈值）；
            - `uncertainties`: 无法可靠区分“中途回撤”与“两次独立抛测”的分界歧义说明列表；
            - `method`: 算法版本标识 `'supported-local-candidates-continuity-v1'`。
    """
    budget = Budget(cancelled=cancelled)
    rows = [rec for rec in records if rec["depth"] >= 0]
    gap = timedelta(minutes=5)

    # 步骤 1：三点相邻支持过滤，排除无前后深水点支撑的单点跳变毛刺
    supported: set[int] = set()
    for idx in range(1, len(rows) - 1):
        if idx % 256 == 0:
            budget.check()
        prev_row, curr_row, next_row = rows[idx - 1 : idx + 2]
        if (
            curr_row["timestamp"] - prev_row["timestamp"] <= gap
            and next_row["timestamp"] - curr_row["timestamp"] <= gap
            and min(prev_row["depth"], next_row["depth"]) >= curr_row["depth"] * 0.5
        ):
            supported.add(idx)

    if not supported or max(rows[idx]["depth"] for idx in supported) <= 0.1:
        return dict(actions=[], candidates=[], uncertainties=[])

    candidates: list[dict] = []
    active: list[int] = []
    threshold = 0.1

    def flush() -> None:
        """闭合当前连续深水候选段，仅当段内存在受相邻观测支持的峰值点时保留为候选片段。"""
        if active:
            peaks = [idx for idx in active if idx in supported]
            if peaks:
                # 选取段内最深点作为候选峰值（等深时 max 默认保留列表中较早出现的物理位置）
                peak = max(peaks, key=lambda idx: rows[idx]["depth"])
                candidates.append(
                    dict(
                        peak=peak,
                        first=active[0],
                        last=active[-1],
                        threshold=threshold,
                    )
                )
                enforce(len(candidates), LIMITS.segments, "校正候选片段数")
            active.clear()

    def excursions():
        """按 >5 分钟真实时间断档或连续 3 条近零水深观测（<= 0.1m）切分局部包络计算区间。"""
        begin, shallow_count = 0, 0
        for idx, rec in enumerate(rows):
            if idx % 256 == 0:
                budget.check()
            if idx and rec["timestamp"] - rows[idx - 1]["timestamp"] > gap:
                yield begin, idx
                begin, shallow_count = idx, 0
            shallow_count = shallow_count + 1 if rec["depth"] <= 0.1 else 0
            if shallow_count == 3:
                yield begin, idx - 2
                begin = idx + 1
        yield begin, len(rows)

    # 步骤 2：在每个局部过程包络内独立计算入水阈值并提取原始候选片段
    for begin, end in excursions():
        peaks = [rows[idx]["depth"] for idx in range(begin, end) if idx in supported]
        if not peaks or max(peaks) <= 0.1:
            continue
        threshold = max(0.1, _quantile(peaks, 0.99) * 0.35)
        for idx in range(begin, end):
            if idx % 256 == 0:
                budget.check()
            if rows[idx]["depth"] >= threshold:
                active.append(idx)
            else:
                flush()
        flush()

    # 步骤 3：检查近邻候选片段之间的过渡区间，决定合并为同一抛测动作、独立分界还是记录分界歧义
    groups: list[dict] = []
    uncertainties: list[str] = []
    for index, candidate in enumerate(candidates):
        budget.check()
        if not groups:
            groups.append(dict(members=[index], notes=[]))
            continue

        previous = candidates[index - 1]
        before, after = previous["peak"], candidate["peak"]
        span = rows[after]["timestamp"] - rows[before]["timestamp"]
        continuous = all(
            rows[idx]["timestamp"] - rows[idx - 1]["timestamp"] <= gap
            for idx in range(before + 1, after + 1)
        )
        merge, reason = False, ""

        if span <= timedelta(minutes=15) and continuous:
            between = range(previous["last"] + 1, candidate["first"])
            # 至少存在连续 3 条 <= 0.1m 的近水面观测，才认定仪器已明确出水完成上一次抛测
            surface = any(
                all(rows[j]["depth"] <= 0.1 for j in (i - 1, i, i + 1))
                for i in between
                if 0 < i < len(rows) - 1
            )
            lows = [
                i for i in between if rows[i]["depth"] < rows[before]["depth"] * 0.5
            ]
            transient = bool(lows) and short_returns(
                rows, lows, rows[before]["depth"] * 0.5
            )
            if not surface and (not lows or transient):
                merge = True
                reason = (
                    "相邻候选片段间观测连续，中途回撤未超过前峰水深的一半"
                    if not lows
                    else "相邻候选片段间仅含 1～2 条短暂浅水记录，且前后迅速回到原水深范围"
                )
            elif not surface:
                uncertainties.append(
                    f"候选 {index} 与候选 {index+1} 之间水深回撤较大且缺乏明确的浅水停留过程，"
                    "无法可靠区分是中途回撤还是两次独立抛测。"
                )

        if merge:
            groups[-1]["members"].append(index)
            groups[-1]["notes"].append(reason)
        else:
            groups.append(dict(members=[index], notes=[]))

    def observation(index: int) -> dict:
        """提取指定记录下标的 ISO 时间文本、水深及来源文件行号，供校正方案展示与溯源。"""
        row = rows[index]
        return dict(
            timestamp=row["timestamp"].isoformat(),
            depth=row["depth"],
            file=row["file"],
            line=row["line"],
        )

    public_candidates = [
        dict(
            id=idx + 1,
            **observation(cand["peak"]),
            start=rows[cand["first"]]["timestamp"].isoformat(),
            end=rows[cand["last"]]["timestamp"].isoformat(),
            entry_threshold_m=cand["threshold"],
        )
        for idx, cand in enumerate(candidates)
    ]

    actions: list[dict] = []
    for group in groups:
        selected = max(
            group["members"], key=lambda idx: rows[candidates[idx]["peak"]]["depth"]
        )
        actions.append(
            dict(
                **observation(candidates[selected]["peak"]),
                candidate_ids=[idx + 1 for idx in group["members"]],
                notes=group["notes"],
            )
        )

    return dict(
        actions=actions,
        candidates=public_candidates,
        uncertainties=uncertainties,
        method="supported-local-candidates-continuity-v1",
    )


def short_returns(rows, lows, level) -> bool:
    """验证夹在两个深水候选之间的浅水点（`depth < level`）是否属于“仅 1～2 条且迅速折返”的短暂浅值。

    判定准则：
    1. 将下标序列 `lows` 按连续相邻关系分组，每组连续浅值条数不得超过 2 条（`len(group) <= 2`）；
    2. 每组浅值紧邻的前一点 `a = group[0] - 1` 与后一点 `b = group[-1] + 1` 必须存在，
       且两侧水深均已回到 `level` 及以上；
    3. 浅值段前后两侧各最多 3 个相邻采样间隔必须均包含正向递增的正常采样点（`intervals`），
       且从 `rows[a]` 到 `rows[b]` 的总耗时不得超过周围正常采样间隔中位数的 3 倍（`<= 3 * median(intervals)`）。
    """
    groups: list[list[int]] = []
    for idx in lows:
        if groups and idx == groups[-1][-1] + 1:
            groups[-1].append(idx)
        else:
            groups.append([idx])

    for group in groups:
        before_idx, after_idx = group[0] - 1, group[-1] + 1
        if (
            len(group) > 2
            or before_idx < 0
            or after_idx >= len(rows)
            or min(rows[before_idx]["depth"], rows[after_idx]["depth"]) < level
        ):
            return False

        # 无论是 1 条还是 2 条浅值，都必须有前后两侧正常正采样间隔支持，不能仅凭条数强行合并
        intervals: list[float] = []
        for side_range in (
            range(max(1, before_idx - 2), before_idx + 1),
            range(after_idx + 1, min(len(rows), after_idx + 4)),
        ):
            nearby = [
                (rows[j]["timestamp"] - rows[j - 1]["timestamp"]).total_seconds()
                for j in side_range
                if rows[j]["timestamp"] > rows[j - 1]["timestamp"]
            ]
            if not nearby:
                return False
            intervals.extend(nearby)

        if (
            rows[after_idx]["timestamp"] - rows[before_idx]["timestamp"]
        ).total_seconds() > 3 * median(intervals):
            return False

    return True
