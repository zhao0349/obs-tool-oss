"""批次（Batch）、测点（Point）与筛选规则（Rules）领域模型定义模块。

本模块定义软件中可持久化、可编辑的配置实体：
1. `Rules`：分段检测、整点匹配、表层加权评分与深度偏差容差参数集合。
2. `Point`：单个测点的编号、参考水深、分层方案、起止时间窗口、输入 DAT 文件列表及内部稳定身份 `point_id`。
3. `Batch`：一次完整处理任务的配置快照（包含 schema 版本、测点列表、全局筛选规则与 CSV 导出选项）。

设计原则：
- 所有 `validate()` 方法在参数非法时均抛出可直接向用户展示的中文 `ValueError`。
- `Point.point_id` 是测点在单次会话内的稳定 UUID 标识，用户修改测点显示编号 `code` 时不会改变 `point_id`，从而保证勾选状态与历史运行结果关联不丢失。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
import math
import re
from uuid import uuid4

from .data_types import DepthEstimate, LayerConfig


# Windows 文件系统保留设备名集合（不区分大小写，不可作为测点编号或导出文件名前缀）
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

# 测点编号合法字符正则：仅允许中文、字母、数字、下划线与短横线
VALID_POINT_CODE_RE = re.compile(r"[\w-]+")


def validate_codes(codes: Iterable[str]) -> None:
    """按 Windows 文件名大小写不敏感规则检查测点编号集合是否存在重名冲突。

    Args:
        codes: 待校验的测点编号可迭代序列。

    Raises:
        ValueError: 当存在两个编号在 `casefold()` 规范化后相同时抛出（例如 `"S1"` 与 `"s1"`）。
    """

    seen_codes: dict[str, str] = {}
    for code in codes:
        normalized_key = code.casefold()
        if normalized_key in seen_codes:
            raise ValueError(
                f"测点编号冲突：{seen_codes[normalized_key]} 与 {code}（Windows 不区分文件名大小写），请换一个编号"
            )
        seen_codes[normalized_key] = code


def standard_layer_count(depth: float | None) -> int:
    """根据测点水深 H（米）按系统标准水深分段规则计算推荐采样层数。

    水深分段对应关系：
    - `0 < H < 2` 米：1 层（`0.6H`）
    - `2 <= H < 5` 米：2 层（`0.2H`、`0.8H`）
    - `5 <= H < 8` 米：3 层（`0.2H`、`0.6H`、`0.8H`）
    - `8 <= H < 11` 米：5 层（`表层`、`0.2H`、`0.6H`、`0.8H`、`底层`）
    - `H >= 11` 米：6 层（`表层`、`0.2H`、`0.4H`、`0.6H`、`0.8H`、`底层`）

    Args:
        depth: 测点参考水深 H（单位：米）。

    Returns:
        int: 系统推荐的标准分层数量（1、2、3、5 或 6）。

    Raises:
        ValueError: 当 `depth` 为空、非有限浮点数或小于等于 0 时抛出。
    """

    if depth is None or not math.isfinite(depth) or depth <= 0:
        raise ValueError("请输入大于0的水深（米）。")
    if depth < 2:
        return 1
    if depth < 5:
        return 2
    if depth < 8:
        return 3
    if depth < 11:
        return 5
    return 6


@dataclass
class Rules:
    """OBS 剖面分段、整点匹配、表层评分与深度容差参数配置。

    Attributes:
        min_depth: 分段识别的最低参考深度门槛（米），低于此值视为水面附近噪声，默认 `0.10`。
        gap_minutes: 相邻观测之间的最大允许时间断档（分钟），超过则切分为不同片段，默认 `5`。
        pre_minutes: 片段触发前向前纳入候选的预缓冲时长（分钟），默认 `8`。
        tail_minutes: 片段末尾已进入活动列表的低值观测保留上限（分钟），默认 `8`。
        stop_count: 深度回落阶段判定片段结束所需的连续低于结束阈值观测个数，默认 `4`。
        max_offset_minutes: 目标整点与片段最深峰值时刻（apex）的最大允许时间差（分钟，含边界），默认 `60`。
        surface_min: 表层候选记录的最小深度下限（米，要求严格大于），默认 `0.5`。
        depth_weight: 表层综合评分中的深度项权重，默认 `1`。
        hour_weight: 表层综合评分中的距整点时间偏移权重（按分钟计），默认 `0.1`。
        entry_weight: 表层综合评分中的距片段起点时间偏移权重（按分钟计），默认 `0.05`。
        tolerance_m: 可选的相对层绝对深度偏差容差上限（米）；`None` 表示不启用。
        tolerance_ratio: 可选的相对层相对深度偏差容差比例上限（例如 `0.1` 表示目标层深度的 10%）；`None` 表示不启用。
        reuse: 片段跨整点复用策略，当前固定为 `"allow_warn"`（允许复用并标记核查告警）。
    """

    min_depth: float = 0.10  # 最低有效水深门槛（米）
    gap_minutes: float = 5  # 片段之间允许的最大时间断档（分钟）
    pre_minutes: float = 8  # 片段前缓冲时长（分钟）
    tail_minutes: float = 8  # 片段后缓冲保留上限（分钟）
    stop_count: int = 4  # 连续多少个低深度点判定片段结束
    max_offset_minutes: float = 60  # 整点与片段峰值的最大匹配距离（分钟）
    surface_min: float = 0.5  # 表层提取要求的最小深度下限（米，严格大于）
    depth_weight: float = 1  # 表层评分：深度权重
    hour_weight: float = 0.1  # 表层评分：距目标整点分钟数权重
    entry_weight: float = 0.05  # 表层评分：距片段起点分钟数权重
    tolerance_m: float | None = None  # 绝对深度容差（米），None 表示未启用
    tolerance_ratio: float | None = None  # 相对深度容差（占目标层深度的比例），None 表示未启用
    reuse: str = "allow_warn"  # 片段复用策略：允许复用并产生告警

    def validate(self) -> None:
        """校验全部筛选规则参数的数值合法性与取值范围。

        Raises:
            ValueError: 当存在非有限数、负数、布尔冒充数值、不支持的复用策略，
                或 `stop_count < 1`、`gap_minutes <= 0` 时抛出。
        """

        for key, value in asdict(self).items():
            if key == "reuse":
                if value != "allow_warn":
                    raise ValueError("首版仅支持片段复用并告警")
            elif value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{key} 必须为有限非负数")

        if (
            not isinstance(self.stop_count, int)
            or self.stop_count < 1
            or self.gap_minutes <= 0
        ):
            raise ValueError("连续低值数量须为正整数，断档时间须大于零")


@dataclass
class Point:
    """单个测点的配置与水深估算状态。

    Attributes:
        code: 用户可见的测点编号（如 `"1-1"`），用于界面展示与导出文件名。
        depth: 测点参考水深 H（米）；未填写或待自动估算时为 `None`。
        layers: 显式指定的系统标准分层数（1/2/3/5/6）；为 `None` 且无自定义层位时按 `depth` 自动推荐。
        files: 该测点关联的原始 `.dat` 文件路径列表。
        start: 可选的目标整点开始时间限制（ISO 秒精度字符串，如 `"2026-10-16T08:00:00"`）。
        end: 可选的目标整点结束时间限制（ISO 秒精度字符串）。
        layer_positions: 自定义分层规则的相对水深比例列表（升序，取值 `[0, 1]`）；非 `None` 时优先于系统规则。
        layer_rule_name: 当前选用的自定义分层规则名称；使用系统规则时为 `None`。
        depth_source: 水深来源状态（`"manual"` / `"estimated"` / `"pending"` / `"unavailable"`）。
        depth_estimate: 最近一次水深自动估算的诊断详情字典。
        point_id: 测点在程序内部的稳定唯一标识（UUID hex），不随 `code` 编辑而改变。
    """

    code: str
    depth: float | None
    layers: int | None = None
    files: list[str] = field(default_factory=list)
    start: str | None = None
    end: str | None = None
    layer_positions: list[float] | None = None
    layer_rule_name: str | None = None
    depth_source: str = "manual"
    depth_estimate: DepthEstimate | None = None
    point_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def layer_count(self) -> int:
        """返回当前测点生效的采样层数（优先级：自定义层位列表长度 > 显式系统层数 > 按水深推荐层数）。"""

        if self.layer_positions is not None:
            return len(self.layer_positions)
        return standard_layer_count(self.depth) if self.layers is None else self.layers

    @property
    def layer_plan(self) -> list[LayerConfig]:
        """根据当前测点配置生成有序层位计划列表（不修改测点自身字段）。"""

        from .algorithm import custom_layer_plan, get_layer_plan

        return (
            custom_layer_plan(self.layer_positions)
            if self.layer_positions is not None
            else get_layer_plan(self.layer_count)
        )

    def validate(self, require_depth: bool = True) -> None:
        """校验测点编号、水深、分层配置与起止时间窗口的合法性。

        Args:
            require_depth: 是否强制要求 `depth` 已填写为正数。
                在界面草稿编辑和后台水深预分析阶段可传 `False`；正式进入采样计算前须为 `True`。

        Raises:
            ValueError: 当任一配置项不符合业务或文件系统约束时抛出。
        """

        if not isinstance(self.point_id, str) or not self.point_id:
            raise ValueError("测点内部身份无效")

        if self.code.upper() in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"“{self.code}”是系统保留名称，请换一个测点编号。")

        if not VALID_POINT_CODE_RE.fullmatch(self.code):
            raise ValueError("测点编号只允许字母、数字、汉字、下划线和短横线。")

        if (self.depth is None and require_depth) or (
            self.depth is not None
            and (
                isinstance(self.depth, bool)
                or not isinstance(self.depth, (int, float))
                or not math.isfinite(self.depth)
                or self.depth <= 0
            )
        ):
            raise ValueError(f"{self.code}：请输入大于0的水深（米）。")

        if self.layers is not None and (
            type(self.layers) is not int or self.layers not in (1, 2, 3, 5, 6)
        ):
            raise ValueError(
                f"{self.code}：系统规则支持1、2、3、5、6层；其他层位请创建自定义规则（1—64层）。"
            )

        parsed_times = [
            datetime.fromisoformat(time_str) if time_str else None
            for time_str in (self.start, self.end)
        ]
        if self.layer_positions is not None:
            validate_positions(self.layer_positions)

        if any(dt_val and dt_val.tzinfo for dt_val in parsed_times):
            raise ValueError("请填写仪器记录的日期和时间，不要附加时区。")

        if all(parsed_times) and parsed_times[0] > parsed_times[1]:
            raise ValueError("开始时间不能晚于结束时间")


def validate_positions(positions: list[float]) -> None:
    """校验自定义分层规则的层位比例列表（须为 1—64 个升序、无重复且位于 `[0, 1]` 的有限数值）。

    Args:
        positions: 相对层位比例列表，其中 `0` 代表表层、`1` 代表底层、`0 < r < 1` 代表相对水深 `r * H`。

    Raises:
        ValueError: 当列表长度、元素类型、数值范围或排序去重不符合要求时抛出。
    """

    if not isinstance(positions, list) or not 1 <= len(positions) <= 64:
        raise ValueError("自定义规则须包含1至64个不同层位")

    if any(
        isinstance(pos, bool)
        or not isinstance(pos, (int, float))
        or not math.isfinite(pos)
        or not 0 <= pos <= 1
        for pos in positions
    ):
        raise ValueError("层位须为0到1之间的有限数值")

    if positions != sorted(set(positions)):
        raise ValueError("层位须升序排列且不能重复")


@dataclass
class Batch:
    """一次批处理任务的完整配置快照（支持序列化为 `schema_version=1` 的任务 JSON）。

    Attributes:
        schema_version: 批次 JSON 结构版本号，当前固定为 `1`。
        input_dir: 历史兼容保留字段（不再用于自动扫描目录，实际输入以各测点 `files` 为准）。
        output_dir: CSV 结果保存根目录。
        points: 本批次包含的测点配置列表。
        rules: 本批次共用的筛选与容差规则。
        encoding: 导出 CSV 的字符编码（`"utf-8-sig"`、`"gb18030"` 或 `"mbcs"`）。
        complete_rows_only: 是否仅向 CSV 导出所有层位均不缺测的整点行。
    """

    schema_version: int = 1
    input_dir: str = ""
    output_dir: str = "results"
    points: list[Point] = field(default_factory=list)
    rules: Rules = field(default_factory=Rules)
    encoding: str = "utf-8-sig"
    complete_rows_only: bool = False

    def validate(self, require_depth: bool = True) -> None:
        """校验整批任务的版本、筛选规则、测点身份唯一性、编号冲突及导出编码。

        Args:
            require_depth: 是否要求批次内每个测点都已具备正数水深 `depth`。

        Raises:
            ValueError: 当批次结构或任意测点配置不合法时抛出。
        """

        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("不支持的批次版本")

        self.rules.validate()

        if len({point.point_id for point in self.points}) != len(self.points):
            raise ValueError("测点内部身份重复")

        if not self.points:
            raise ValueError("需要至少一个测点")

        validate_codes(point.code for point in self.points)
        for point in self.points:
            point.validate(require_depth)

        if self.encoding not in ("utf-8-sig", "gb18030", "mbcs"):
            raise ValueError("不支持的导出编码")

        if not isinstance(self.complete_rows_only, bool):
            raise ValueError("仅导出各层齐全整点的选项须为布尔值")
