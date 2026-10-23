"""OBS 数据处理工具 —— 核心字典数据结构与自定义类型契约（供 IDE 自动补全与开发者阅读）。

本模块集中定义了各业务阶段流转的字典（`dict`）结构契约：
1. **原始 DAT 解析阶段（`readers.py`）**：
   - `Observation`：单条通过校验的传感器观测记录（内存中的 `timestamp` 为 `datetime` 对象）；
   - `ParseEvent`：被跳过、去重或存在同秒冲突的单行解析诊断事件；
   - `FileColumns`：单份 DAT 文件独立识别出的传感器表头列映射信息。
2. **整点分段与垂向选点阶段（`algorithm.py` / `processing.py`）**：
   - `Segment`：包含前后缓冲的完整下放剖面片段（`apex` 为最深观测时刻，`deepest` 为最深记录）；
   - `SurfaceLayer` / `RelativeLayer` / `BottomLayer` / `LayerConfig`：垂向各层计算规则定义；
   - `LayerSelection`：单个目标整点在某一垂向层位上的选点结果与偏差；
   - `HourResult`：单个目标整点的片段匹配与全部层位选点汇总行；
   - `PointResult`：单个测点的完整采样与质量复核结果。
3. **水深智能推算阶段（`depth.py` / `depth_workflow.py`）**：
   - `DepthPeak`：单次有效下放剖面的峰值深度与可追溯文件行号；
   - `DepthEstimate`：测点参考水深 H 的推算值、可信度、四分位距（IQR）及人工确认状态。
4. **时间校正与抛测动作识别阶段（`correction_actions.py` / `time_correction.py`）**：
   - `CastCandidate`：初步提取的单次下放候选片段；
   - `CorrectionAction`：由一个或多个近邻候选片段合并而成的完整抛测动作；
   - `CorrectionUncertainty`：相邻候选片段之间无法安全判定的分界歧义文本；
   - `ActionRecognition`：`identify_actions` 的完整识别结果；
   - `CorrectionParameters` / `CorrectionMatch` / `CorrectionPlan` / `CorrectionSaveResult`：
     时间校正向导的输入参数、整点对齐明细、预览方案与写回结果。
5. **曲线上下文与批处理报告阶段（`curve_context.py` / `runner.py`）**：
   - `CurvePoint` / `CurveContext`：结果核查页过程线的降采样快照数据；
   - `PointRunSummary` / `BatchRunReport`：批处理运行报告与单测点执行摘要。

说明：
    这些 `TypedDict` 类型仅用于静态类型检查、IDE 字段悬停提示与开发者阅读，
    不改变运行时现有的字典、JSON 或 CSV 序列化格式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, NotRequired, TypedDict


# ==============================================================================
# 1. 原始 DAT 文件解析与质量诊断数据结构 (readers.py)
# ==============================================================================


class Observation(TypedDict):
    """单条通过校验的 OBS 有效观测记录；`file` 和 `line` 保留原始文件中的物理位置身份。"""

    timestamp: datetime
    """观测时间戳（由日期列 MM/DD/YYYY 与时间列 HH:MM:SS 解析得到的 `datetime` 对象）。"""

    depth: float
    """实测水深（Depth，单位：米 m）。"""

    turbidity: float
    """实测浊度（OBS-1，单位：散射浊度单位 NTU）。"""

    temperature: float
    """实测水温（Temperature，单位：摄氏度 °C）。"""

    salinity: float
    """实测盐度（Salinity，单位：实用盐度单位 PSU）。"""

    file: str
    """来源 DAT 文件的完整路径字符串（用于跨文件合并溯源与来源详情展示）。"""

    line: int
    """该条观测记录在来源 DAT 文件中的物理行号（从 1 开始计数，包含表头行）。"""

    conductivity: NotRequired[float]
    """可选通道：实测电导率（Conductivity，单位：mS/cm）。缺失或非有限值时不写入该键。"""

    battery: NotRequired[float]
    """可选通道：仪器电池电压（Battery，单位：伏特 V）。缺失或非有限值时不写入该键。"""

    raw_time: NotRequired[str]
    """原始文件中的时间文本（如 `'00:00:00'`），供时间校正与顺序核查使用。"""

    raw_date: NotRequired[str]
    """原始文件中的日期文本（如 `'01/01/2026'`），供时间校正与顺序核查使用。"""


class ParseEvent(TypedDict):
    """有界保存的单条文件解析诊断记录；`line` 是原文件物理行号。"""

    file: str
    """发生该诊断事件的来源 DAT 文件路径。"""

    line: int
    """发生该诊断事件的原文件物理行号（从 1 开始计数）。"""

    reason: str
    """诊断原因标识及说明（如 `'duplicate'`、`'timestamp_conflict'`、`'invalid_year'`、`'bad_data: ...'`）。"""


class FileColumns(TypedDict):
    """单份 DAT 文件独立解析出的表头传感器声明信息；未知附加列在 `mapping` 中映射为 `None`。"""

    path: str
    """DAT 文件路径字符串。"""

    encoding: str
    """实际成功解码该文件所使用的字符编码（`'utf-8-sig'` 或 `'gb18030'`）。"""

    sensors: list[str]
    """表头 `Sensor Data Columns` 声明的原始传感器列名称列表（按文件列顺序排列）。"""

    mapping: list[str | None]
    """各数据列对应的内部标准字段名列表（如 `'depth'`、`'turbidity'`；未识别的扩展列为 `None`）。"""


# ==============================================================================
# 2. 整点分段与垂向分层选点数据结构 (algorithm.py / processing.py / rule_store.py)
# ==============================================================================


class Segment(TypedDict):
    """包含前后缓冲的单次有效下放剖面片段（由 `algorithm.build_segments` 识别生成）。"""

    records: list[Observation]
    """归属于该观测片段的全部有效观测记录列表（已去重并按时间戳升序排列）。"""

    start: datetime
    """片段首条记录的时间戳（含前缓冲纳入的浅水准备记录）。"""

    end: datetime
    """片段末条记录的时间戳（已完成尾部超长浅水拖尾裁剪）。"""

    apex: datetime
    """片段内最深观测记录（`deepest`）的时间戳，作为与目标整点计算时间差的锚点。"""

    deepest: Observation
    """片段内实测水深最大的观测记录字典（同时供底层 `bottom` 直接选用）。"""

    key: tuple[datetime, datetime]
    """片段的起止时间二元组 `(start, end)`，用于标识片段身份。"""


class SurfaceLayer(TypedDict):
    """由表层三项加权评分（深度、距整点时间差、距片段起点时间差）选择代表点的表层配置。"""

    kind: Literal["surface"]
    """层位类型标识：固定为 `'surface'`。"""

    label: str
    """层位显示名称（通常为 `'表层'`）。"""


class RelativeLayer(TypedDict):
    """按相对水深比例选点的中间层位配置，理论目标深度等于 `relative * H`。"""

    kind: Literal["relative"]
    """层位类型标识：固定为 `'relative'`。"""

    label: str
    """层位显示名称（如 `'0.2H'`、`'0.4H'`、`'0.6H'`、`'0.8H'`）。"""

    relative: float
    """相对水深比例（取值范围 `0.0 < relative < 1.0`）。"""


class BottomLayer(TypedDict):
    """直接采用当前匹配片段内最深实测记录（`segment["deepest"]`）的底层配置。"""

    kind: Literal["bottom"]
    """层位类型标识：固定为 `'bottom'`。"""

    label: str
    """层位显示名称（通常为 `'底层'`）。"""


LayerConfig = SurfaceLayer | RelativeLayer | BottomLayer
"""垂向单个采样层位的联合配置类型（表层 `SurfaceLayer`、相对层 `RelativeLayer` 或底层 `BottomLayer`）。"""

RelativeDepthMatch = tuple[float, float, Observation]
"""相对水深层匹配结果三元组：`(相对比例 relative, 目标深度 target_depth, 选中的观测记录 Observation)`。"""


class LayerRule(TypedDict):
    """自定义分层规则库条目（持久化保存在本地 `layer_rules.json` 中）。"""

    name: str
    """自定义规则的唯一名称（不可为空，且不可与系统预置规则重名）。"""

    positions: list[float]
    """严格递增的层位相对比例列表（取值 `0.0 ~ 1.0`，其中 `0.0` 代表表层，`1.0` 代表底层）。"""


class LayerSelection(TypedDict):
    """单个目标整点下某一垂向层位的选点结果；缺测时 `observation` 用 `None` 表示，不以 0 代替。"""

    label: str
    """层位显示名称（如 `'表层'`、`'0.2H'`、`'底层'`）。"""

    target_depth: float | None
    """理论目标采样深度（米）；相对层为 `relative * H`，底层为 `H`，表层为 `None`。"""

    deviation_m: float | None
    """实测深度减目标深度的带符号偏差（米）；表层或缺测时为 `None`，底层也记录偏差。"""

    status: list[str]
    """单层状态标记列表（如 `['missing_layer']`、`['depth_out_of_tolerance']`、`['tolerance_unset']`）。"""

    observation: Observation | None
    """内存中的原始观测记录，时间为 datetime；缺测时为 None。本类型不描述 JSON 副本。"""


class HourResult(TypedDict):
    """单个目标整点的片段匹配与各层选点汇总行，`segment` 是所匹配片段在 `segments` 列表中的索引。"""

    target: str
    """目标整点时刻（ISO-8601 字符串，如 `'2026-01-01T00:00:00'`）。"""

    status: list[str]
    """整点行的质量状态标记列表（如 `'missing_segment'`、`'missing_layer'`、`'segment_reused'`、
    `'cross_layer_reused'`、`'depth_out_of_tolerance'`、`'tolerance_unset'`）。"""

    segment: int | None
    """匹配到的有效观测片段索引（从 0 开始）；未匹配到片段时为 `None`。"""

    offset_seconds: float | None
    """目标整点与所匹配片段最深时刻（`apex`）之间的绝对时间差（单位：秒）；未匹配时为 `None`。"""

    selections: list[LayerSelection]
    """按垂向分层方案由浅入深排列的各层选点结果列表。"""


class PointResult(TypedDict):
    """单个测点的完整采样与质量复核结果；解析诊断由 `runner.run` 在采样完成后补入。"""

    point: str
    """测点编号（如 `'1-1'`）。"""

    layer_count: int
    """本次处理实际采用的垂向分层总数。"""

    layer_plan: list[LayerConfig]
    """本次处理实际执行的垂向分层方案列表。"""

    layer_mode: Literal["standard", "override", "custom"]
    """分层模式：按水深自动 standard、固定系统层数 override、自定义 custom。"""

    rows: list[HourResult]
    """按目标整点时间升序排列的各整点匹配与选点结果列表。"""

    segments: list[Segment]
    """识别出的全部有效下放观测片段列表。"""

    parse_diagnostics: NotRequired[list[ParseEvent]]
    """文件解析阶段记录的诊断明细列表（已按容量上限截断）。"""

    parse_counts: NotRequired[dict[str, int]]
    """文件解析阶段各类诊断原因的完整累计计数。"""


# ==============================================================================
# 3. 参考水深估算与下放剖面识别数据结构 (depth.py / depth_workflow.py)
# ==============================================================================


class DepthPeak(TypedDict):
    """水深自动估算中记录的单次有效下放剖面峰值及其可追溯文件来源。"""

    depth: float
    """该次下放剖面剔除单点毛刺后的有效最大水深（单位：米）。"""

    timestamp: str
    """达到该最大水深的观测时刻（ISO-8601 字符串）。"""

    file: str
    """峰值记录所属的来源 DAT 文件路径。"""

    line: int
    """峰值记录在来源 DAT 文件中的物理行号。"""


class DepthEstimate(TypedDict, total=False):
    """测点参考水深 H 的自动推算建议、统计特征及人工确认状态字典。"""

    value: float | None
    """各次受支持剖面峰值的中位数（米），保留 3 位小数；无法推算时为 None。"""

    method: str
    """推算方法标识 supported-cast-peaks-median-v1，观测不足时仍使用同一标识。"""

    reason: str
    """面向用户的推算依据或需人工确认原因说明。"""

    casts: int
    """识别出的有效下放剖面（Cast）次数。"""

    peak_iqr_m: float
    """各次有效下放峰值水深的四分位距（`Q75 - Q25`，单位：米），衡量多次抛测深度的一致性。"""

    confidence: str
    """成功估算时为稳定或需核对；无法估算时不写入此字段。"""

    envelope_m: float
    """受相邻观测支持的非负有限深度的 99% 分位包络值（米），保留 3 位小数。"""

    threshold_m: float
    """判定有效下放剖面的入水深度阈值（单位：米）。"""

    observed_start: str
    """该测点首条有效观测记录的时间戳（ISO-8601 字符串）。"""

    observed_end: str
    """该测点末条有效观测记录的时间戳（ISO-8601 字符串）。"""

    peaks: list[DepthPeak]
    """全部有效下放剖面的峰值明细；此返回列表不按界面显示数量截断。"""

    parse_events: int
    """解析该测点文件时记录的诊断事件总数。"""

    timestamp_conflicts: int
    """解析该测点文件时发现的同一秒不同观测值冲突条数。"""

    confirmed: bool
    """用户是否已在“水深填写与确认”弹窗中核实并勾选确认该水深。"""

    input_error: bool
    """若为 `True`，代表该测点的输入 DAT 文件无法读取或格式损坏（将在正式处理阶段按测点报错）。"""


# ==============================================================================
# 4. 仪器时间校正与完整抛测动作识别数据结构 (correction_actions.py / time_correction.py)
# ==============================================================================


class CastCandidate(TypedDict):
    """从局部观测包络中初步提取的单次下放候选片段（供时间校正动作合并使用）。"""

    id: int
    """候选片段编号（从 1 开始连续递增）。"""

    start: str
    """候选片段起始时刻（ISO-8601 字符串）。"""

    end: str
    """候选片段结束时刻（ISO-8601 字符串）。"""

    timestamp: str
    """候选片段内最深峰值记录的观测时刻（ISO-8601 字符串）。"""

    depth: float
    """候选片段的有效最大水深（单位：米）。"""

    file: str
    """峰值记录所属的来源 DAT 文件路径。"""

    line: int
    """峰值记录在来源 DAT 文件中的物理行号。"""

    entry_threshold_m: float
    """提取该候选片段时所采用的局部包络入水深度阈值（单位：米）。"""


class CorrectionAction(TypedDict):
    """时间校正“按抛测规律匹配”下，由一个或多个近邻候选片段合并而成的完整抛测动作。"""

    candidate_ids: list[int]
    """归入该次完整抛测动作的原始候选片段编号列表（如 `[1, 2]`）。"""

    timestamp: str
    """抛测动作最深代表锚点的原始仪器记录时刻（ISO-8601 字符串，等深时取较早记录）。"""

    depth: float
    """抛测动作的最大实测水深（单位：米）。"""

    file: str
    """代表锚点记录所属的来源 DAT 文件路径。"""

    line: int
    """代表锚点记录在来源 DAT 文件中的物理行号。"""

    notes: list[str]
    """若合并了多个候选片段，记录每次合并的判定依据说明。"""


CorrectionUncertainty = str
"""分界歧义是可直接展示或拼接的说明文本，不是结构化字典。"""


class ActionRecognition(TypedDict):
    """`correction_actions.identify_actions` 对单文件观测序列执行抛测识别后的返回结构。"""

    method: NotRequired[str]
    """正常完成识别时为 supported-local-candidates-continuity-v1；无受支持观测早退时缺省。"""

    candidates: list[CastCandidate]
    """识别出的全部原始下放候选片段列表。"""

    actions: list[CorrectionAction]
    """合并近邻候选后得到的完整抛测动作列表。"""

    uncertainties: list[CorrectionUncertainty]
    """识别过程中发现的全部候选分界歧义列表。"""


class CorrectionParameters(TypedDict):
    """时间校正请求参数字典；各模式所需的具体字段由 `time_correction.analyse` 校验。"""

    mode: Literal["inspect", "known", "casts"]
    """校正分析模式：`'inspect'`（只读检查）、`'known'`（已知时间对应关系）、`'casts'`（按抛测规律匹配）。"""

    instrument: NotRequired[str]
    """`known` 模式下的仪器记录参考时刻（ISO-8601 字符串）。"""

    correct: NotRequired[str]
    """`known` 模式下的实际正确时刻（ISO-8601 字符串）。"""

    start: NotRequired[str]
    """`casts` 模式下的实际测验开始整点（ISO-8601 字符串，分秒须为 00:00）。"""

    end: NotRequired[str]
    """`casts` 模式下的实际测验结束整点（ISO-8601 字符串，分秒须为 00:00）。"""

    head: NotRequired[int]
    """`casts` 模式下用户指定的开头缺测整点次数。"""

    tail: NotRequired[int]
    """`casts` 模式下用户指定的结尾缺测整点次数。"""

    assume_no_head: NotRequired[bool]
    """`casts` 模式下用户是否选择“不记得，按开头无缺测处理”。"""


class CorrectionMatch(TypedDict):
    """时间校正单个目标整点的匹配明细行；`action` 为从 1 开始的完整抛测动作序号。"""

    target: str
    """目标整点时刻（ISO-8601 字符串）。"""

    action: int | None
    """匹配到的完整抛测动作序号（从 1 开始）；缺测时为 `None`。"""

    representative: str | None
    """匹配动作最深锚点的仪器原始记录时刻（ISO-8601 字符串）；缺测时为 `None`。"""

    residual_seconds: float | None
    """平移后锚点时刻与目标整点之间的残差秒数；缺测时为 `None`。"""

    reasons: list[str]
    """该目标整点缺测或异常的具体原因列表；正常匹配时为空列表 `[]`。"""


class CorrectionPlan(TypedDict):
    """analyse 的实际返回结构；只读检查和提前拒绝时没有时差及新时间范围。"""

    path: str
    parameters: CorrectionParameters
    original_sha256: str
    records: int
    columns: FileColumns
    curve: list["CurvePoint"]
    valid: bool
    old_start: str
    old_end: str
    matches: list[CorrectionMatch]
    reason: NotRequired[str]
    """只读检查或方案受阻时的说明；成功方案不保证含此字段。"""

    delta_seconds: NotRequired[int]
    new_start: NotRequired[str]
    new_end: NotRequired[str]
    """统一平移秒数及平移后的首尾 ISO 时间；完成时差计算后才写入。"""

    recognition: NotRequired[ActionRecognition]
    actions: NotRequired[list[CorrectionAction]]
    target_count: NotRequired[int]
    anchor: NotRequired[str]
    interval_tolerance_minutes: NotRequired[int]
    target_tolerance_minutes: NotRequired[int]
    """抛测模式的识别结果、目标整点数量、锚点说明及分钟容差。"""

    middle_missing: NotRequired[int]
    end_missing_total: NotRequired[int]
    head_missing: NotRequired[int]
    tail_missing: NotRequired[int]
    assumption: NotRequired[str]
    needs_allocation: NotRequired[bool]
    problem_count: NotRequired[int]
    preview_only: NotRequired[bool]
    """抛测模式继续分析后补入的缺测分配、异常数量及只读限制；早退时可能缺省。"""


class CorrectionSaveResult(TypedDict):
    """save 成功后的返回值；不重复携带分析方案中的时差或观测信息。"""

    path: str
    """实际写入校正后数据的目标路径；原位修改时即原文件路径。"""

    backup: str | None
    """原位修改产生的完整备份路径；另存时为 None。"""

    inplace: bool
    """True 表示原位修改，False 表示另存新文件。"""


# ==============================================================================
# 5. 曲线上下文与批处理报告数据结构 (curve_context.py / runner.py)
# ==============================================================================


class CurvePoint(TypedDict):
    """时间校正预览中的精简点；时间已转换为 ISO 文本。"""

    timestamp: str
    depth: float


class SerializedObservation(TypedDict):
    """曲线快照中的观测；与内存 Observation 分开，timestamp 已序列化为文本。"""

    timestamp: str
    depth: float
    turbidity: float
    temperature: float
    salinity: float
    file: str
    line: int
    conductivity: NotRequired[float]
    battery: NotRequired[float]
    raw_time: NotRequired[str]
    raw_date: NotRequired[str]


class InputFileSummary(TypedDict):
    """runner 为已读取测点记录的输入文件摘要；size 单位为字节。"""

    point: str
    path: str
    size: int


class CurveContext(TypedDict):
    """由程序生成的完整曲线快照经 prepare_context 降采样后的返回结构。"""

    schema_version: int
    point_id: str
    depth: float | None
    inputs: list[InputFileSummary]
    gap_seconds: float
    start: str | None
    end: str | None
    runs: list[list[SerializedObservation]]
    observation_count: int
    display_count: int


class PointRunSummary(TypedDict):
    """runner 的单测点执行摘要；成功字段不要求出现在失败、取消或未开始记录中。"""

    point: str
    status: Literal["completed", "failed", "cancelled", "not_started"]
    layer_count: NotRequired[int]
    layer_mode: NotRequired[Literal["standard", "override", "custom"]]
    warnings: NotRequired[int]
    """成功处理后，含至少一个状态标记的目标整点行数。"""

    parse_events: NotRequired[int]
    """成功处理后保存的解析诊断条数，可能已被诊断明细容量上限截断。"""

    error: NotRequired[str]
    """失败时的错误说明。"""


class BatchRunReport(TypedDict):
    """runner 写入 manifest.json 的报告；运行中先落盘，finally 再补 ended。"""

    app_version: str
    algorithm_version: str
    started: str
    ended: NotRequired[str]
    status: Literal[
        "running", "completed", "completed_with_errors", "cancelled", "failed"
    ]
    points: list[PointRunSummary]
    inputs: list[InputFileSummary]
    curve_context_errors: NotRequired[dict[str, str]]
    """曲线快照保存失败时，以测点编号为键记录错误。"""

    error: NotRequired[str]
    """整批编排发生异常时的错误说明；单点错误记在 points 中。"""


# ==============================================================================
# 6. 兼容拓展别名（供 IDE 与文档引用）
# ==============================================================================

ObservationRecord = Observation
ParseDiagnosticItem = ParseEvent
FileParseSummary = FileColumns
CustomRule = LayerRule
ObservationSegment = Segment
HourMatchRow = HourResult
PointProcessResult = PointResult
ActionRecognitionResult = ActionRecognition
CorrectionMatchRow = CorrectionMatch
