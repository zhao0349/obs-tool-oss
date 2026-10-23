"""外部输入零信任校验与有界资源预算策略模块。

设计目标：
    对任何来自磁盘的文件、目录、JSON 记录及运行耗时实施集中、可注入的上限保护（`InputLimits`），
    防止超大文件、伪造二进制文件、深层嵌套 JSON 或异常数据耗尽内存与 CPU。
    注意：此处的预算属于工程防卫上限，而非水文测验的业务时长限制；一旦超限立即抛出 `CapacityError`
    安全中止当前任务，绝不静默截断数据后冒充完整成果导出。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import time
from typing import Any


class CapacityError(ValueError):
    """当输入文件体积、记录行数、JSON 复杂度或计算耗时超出工程预算上限时抛出的异常。"""


@dataclass(frozen=True)
class InputLimits:
    """集中定义且支持测试依赖注入的资源预算上限配置。

    各项指标按字节数、行数、条目数、工作量单元和秒数分别独立计量：
    """

    file_bytes: int = 64 * 1024 * 1024  # 单个原始 DAT 文件最大字节数（64 MiB）
    point_bytes: int = 128 * 1024 * 1024  # 单个测点累计读取最大字节数（128 MiB）
    batch_bytes: int = 256 * 1024 * 1024  # 整批任务累计读取最大字节数（256 MiB）
    selection_entries: int = 512  # 单次文件选择器提交或目录扫描的最大候选文件数
    point_files: int = 256  # 单个测点最多关联的 DAT 文件数
    batch_files: int = 1024  # 整批任务最多包含的文件或测点条目数
    directory_entries: int = 10000  # 扫描文件夹时最多检查的目录项总数
    header_bytes: int = 65536  # 传感器表头探测阶段的最大读取字节数（64 KiB）
    header_lines: int = 256  # 传感器表头探测阶段的最大扫描行数
    line_bytes: int = 4096  # 单行文本的最大允许字节数
    fields: int = 64  # 单行或传感器声明表的最大列数
    field_bytes: int = 128  # 单个字段或传感器名称的最大字节长度
    file_lines: int = 600000  # 单个 DAT 文件最多允许的物理行数
    file_records: int = 400000  # 单个 DAT 文件最多允许的有效观测记录数
    point_records: int = 500000  # 单个测点最多允许的累计观测记录数
    batch_records: int = 1000000  # 整批任务最多允许的累计观测记录数
    diagnostics: int = 256  # 单个文件最多保留的逐行解析告警样本条数
    targets: int = 2048  # 单个测点最多生成的目标整点数量
    segments: int = 4096  # 单个测点最多识别的观测片段或候选动作数
    work: int = 20000000  # 整点匹配与层位选点阶段的最大循环工作量单元
    plot_points: int = 500000  # 曲线核查快照允许读取的最大观测点数
    json_bytes: int = 128 * 1024 * 1024  # 单份内部 JSON 文件读写的最大字节数（128 MiB）
    json_nodes: int = 6000000  # 单份 JSON 解码后允许的最大容器/叶子节点总数
    json_depth: int = 32  # 单份 JSON 允许的最大括号嵌套层数
    seconds: int = 180  # 单次后台分析或处理的耗时上限（秒）
    log_bytes: int = 2 * 1024 * 1024  # 单份轮转运行日志的最大字节数（2 MiB）
    fault_files: int = 8  # 最多保留的历史崩溃故障栈文件数


# 全局默认工程资源预算实例
LIMITS = InputLimits()


def enforce(value: int | float, maximum: int | float, label: str) -> None:
    """检查当前计量值是否超过上限（严格大于 `maximum` 时抛出 `CapacityError`，等于上限仍允许）。

    Args:
        value: 当前已累计或待检查的实际数值。
        maximum: 允许的最大上限值。
        label: 用于生成用户可读错误提示的指标名称（如 `"单文件字节数"`）。

    Raises:
        CapacityError: 当 `value > maximum` 时抛出。
    """

    if value > maximum:
        raise CapacityError(
            f"数据量超出软件单次安全处理上限（{label}超过 {maximum}），请分批导入或缩小本次处理范围。"
        )


class Budget:
    """跟踪单测点或整批任务的累计读取字节数、观测记录数、运行耗时与取消状态。

    支持通过 `parent` 级联挂接整批父预算：
    - 子预算在读取过程中由调用方递增 `bytes` 和 `records`，同时同步递增 `parent` 的对应计数。
    - 调用 `check()` 时同时校验父预算总额、用户协作取消标记与单次任务超时限制。
    """

    def __init__(
        self,
        limits: InputLimits = LIMITS,
        cancelled: Callable[[], bool] = lambda: False,
        parent: Budget | None = None,
    ) -> None:
        """初始化预算跟踪器。

        Args:
            limits: 资源上限配置，默认使用全局 `LIMITS`。
            cancelled: 返回用户是否已请求取消任务的无参回调。
            parent: 可选的整批父级 `Budget` 实例，用于跨测点汇总整批消耗。
        """

        self.limits = limits
        self.cancelled = cancelled
        self.started = time.monotonic()
        self.bytes = 0
        self.records = 0
        self.parent = parent

    def check(self) -> None:
        """检查父级整批预算、用户取消状态以及当前任务是否超时。

        Raises:
            CapacityError: 当父预算累计字节/记录数超限，或耗时超过 `limits.seconds` 时抛出。
            InterruptedError: 当 `cancelled()` 返回 `True` 时抛出。
        """

        if self.parent:
            self.parent.check()
            enforce(
                self.parent.bytes, self.parent.limits.batch_bytes, "整批实际读取字节数"
            )
            enforce(
                self.parent.records, self.parent.limits.batch_records, "整批累计观测数"
            )
        if self.cancelled():
            raise InterruptedError("用户取消")
        if time.monotonic() - self.started > self.limits.seconds:
            raise CapacityError(
                "单次分析耗时超出上限，请缩小本次导入的数据范围后重试。"
            )


def file_status(path: str | Path) -> os.stat_result:
    """获取路径的 `os.stat_result` 元数据，并校验其必须是常规文件（拒绝目录、设备或管道）。

    Args:
        path: 待检查的文件路径。

    Returns:
        os.stat_result: 常规文件的系统 stat 状态对象。

    Raises:
        OSError: 当文件不存在或无法访问时抛出。
        ValueError: 当目标路径不是常规文件（如目录、FIFO、设备节点）时抛出。
    """

    resolved_path = Path(path)
    status = resolved_path.stat()
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(f"所选路径不是可读取的常规文件：{resolved_path}")
    return status


def check_files(
    files: Iterable[str | Path],
    limits: InputLimits = LIMITS,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> list[Path]:
    """对候选输入文件列表执行读取前元数据预检与物理文件去重。

    检查顺序：
    1. 限制传入的候选路径条目总数。
    2. 校验路径长度并获取 `stat()` 元数据（确认是常规文件且单文件体积不超限）。
    3. 按 `(st_dev, st_ino)` 识别底层物理文件身份，自动跳过指向同一文件的重复路径。
    4. 累加去重后的总字节数与有效文件数量，确保不超过测点或批次上限。

    Args:
        files: 待检查的文件路径序列。
        limits: 资源预算配置。
        max_files: 可选的文件数量上限覆盖值（默认采用 `limits.point_files`）。
        max_bytes: 可选的总字节数上限覆盖值（默认采用 `limits.point_bytes`）。

    Returns:
        list[Path]: 通过预检且去重后的绝对路径列表。

    Raises:
        CapacityError: 当文件数或字节数超出上限时抛出。
        ValueError: 当路径过长、非常规文件或有效文件列表为空时抛出。
    """

    checked_paths: list[Path] = []
    seen_identities: set[tuple[int, int]] = set()
    total_bytes = 0

    for index, raw_path in enumerate(files, 1):
        enforce(
            index,
            limits.selection_entries if max_files is None else max_files,
            "提交的文件条目数",
        )
        path = Path(raw_path).resolve()
        if len(str(path)) > 32760:
            raise ValueError("文件路径过长")

        status = file_status(path)
        enforce(status.st_size, limits.file_bytes, f"文件 {path.name} 字节数")

        # 使用设备号与 inode 编号去重同一物理文件的重复引用
        file_identity = (status.st_dev, status.st_ino)
        if file_identity in seen_identities:
            continue
        seen_identities.add(file_identity)

        total_bytes += status.st_size
        enforce(
            total_bytes,
            limits.point_bytes if max_bytes is None else max_bytes,
            "输入总字节数",
        )
        checked_paths.append(path)
        enforce(
            len(checked_paths),
            limits.point_files if max_files is None else max_files,
            "文件数",
        )

    if not checked_paths:
        raise ValueError("请至少选择一个 DAT 数据文件。")
    return checked_paths


def bounded_json(
    path: str | Path, limits: InputLimits = LIMITS, max_bytes: int | None = None
) -> Any:
    """在严格限制文件体积、括号嵌套深度与节点总数的条件下安全读取并解析 JSON 文件。

    安全机制：
    1. 先检查文件 `st_size` 及实际读取字节数不超过 `maximum`。
    2. 在调用 `json.loads` 之前，按字节扫描非引号内的 `[` / `{` 嵌套深度，防止超深嵌套导致递归栈溢出。
    3. 解码后使用显式栈迭代统计 dict/list/原子节点总数，防止超大节点图耗尽内存。
    4. 返回的对象仍属于外部数据，调用方需继续验证其具体业务字段结构。

    Args:
        path: JSON 文件路径。
        limits: 资源预算配置。
        max_bytes: 可选的单文件最大字节上限覆盖值（默认采用 `limits.json_bytes`）。

    Returns:
        Any: 解析后的 JSON 数据结构。

    Raises:
        CapacityError: 当文件大小、嵌套深度或节点数超过上限时抛出。
    """

    target_path = Path(path)
    maximum = limits.json_bytes if max_bytes is None else max_bytes
    enforce(file_status(target_path).st_size, maximum, "记录文件字节数")

    with target_path.open("rb") as stream:
        raw_bytes = stream.read(maximum + 1)
    enforce(len(raw_bytes), maximum, "记录文件实际字节数")

    # 在 json.loads 解码前扫描括号嵌套深度（忽略双引号字符串字面量内部的括号）
    in_quotes = False
    is_escaped = False
    nesting_depth = 0
    for byte_val in raw_bytes:
        if in_quotes:
            if is_escaped:
                is_escaped = False
            elif byte_val == 92:  # '\' 转义符
                is_escaped = True
            elif byte_val == 34:  # '"' 双引号结束
                in_quotes = False
        elif byte_val == 34:  # '"' 双引号开始
            in_quotes = True
        elif byte_val in (91, 123):  # '[' 或 '{'
            nesting_depth += 1
            enforce(nesting_depth, limits.json_depth, "记录嵌套层数")
        elif byte_val in (93, 125):  # ']' 或 '}'
            nesting_depth -= 1

    parsed_value = json.loads(raw_bytes.decode("utf-8-sig"))

    # 非递归遍历统计 JSON 树中的总节点数
    pending_stack = [parsed_value]
    node_count = 0
    while pending_stack:
        current_item = pending_stack.pop()
        node_count += 1
        enforce(node_count, limits.json_nodes, "记录元素数")
        if isinstance(current_item, dict):
            pending_stack.extend(current_item.values())
        elif isinstance(current_item, list):
            pending_stack.extend(current_item)

    return parsed_value


def dat_in_folder(
    folder: str | Path,
    limits: InputLimits = LIMITS,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[str]:
    """有界枚举指定文件夹当前层级的 `.dat` 常规文件路径（不递归子文件夹、不跟随符号链接、不读取文件内容）。

    Args:
        folder: 目标文件夹路径。
        limits: 资源预算配置（限制目录总条目数与候选 `.dat` 文件数）。
        cancelled: 协作取消检查回调。

    Returns:
        list[str]: 按路径升序排列的 `.dat` 文件路径字符串列表。

    Raises:
        CapacityError: 当目录条目数或 `.dat` 文件数超过预算上限时抛出。
        InterruptedError: 当用户取消扫描时抛出。
    """

    matched_paths: list[str] = []
    with os.scandir(folder) as dir_entries:
        for entry_count, entry in enumerate(dir_entries, 1):
            if cancelled():
                raise InterruptedError("用户取消")
            enforce(
                entry_count,
                limits.directory_entries,
                "目录条目数（已发现至少此数量）",
            )
            if entry.name.lower().endswith(".dat") and entry.is_file(
                follow_symlinks=False
            ):
                matched_paths.append(entry.path)
                enforce(len(matched_paths), limits.selection_entries, "本次候选文件数")

    return sorted(matched_paths)
