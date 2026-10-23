"""有界原子文件写入、JSON 持久化与单测点 CSV 成果导出模块。

核心安全保证：
1. **输出字节上限（`BoundedWriter`）**：在写入过程中实时累计编码后字节数，超过 `LIMITS.json_bytes` 立即中止并清理临时文件。
2. **同目录临时文件 + `fsync` + `os.replace`（`atomic_write`）**：保证目标文件要么完整更新，要么保持原状，绝不留下半截损坏文件；针对 Windows 短暂文件占用（winerror 5 / 32）提供最多 3 次退避重试。
3. **不覆盖保护（`export_point`）**：导出测点 CSV 与诊断 JSON 前先按大小写不敏感规则检查目标路径，若已存在同名文件则拒绝覆盖；且若 CSV 写入失败，会自动撤回刚写入的诊断 JSON。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import csv
import json
import os
from pathlib import Path
import tempfile
import time
from typing import TextIO

from .data_types import PointResult
from .models import Batch, Point


class BoundedWriter:
    """包装底层文本流并实时统计已写入字节数的有界写入器（兼容 `csv.writer` 与 `json.dump`）。"""

    def __init__(self, stream: TextIO, encoding: str) -> None:
        """初始化有界写入器。

        Args:
            stream: 底层已打开的文本写入流。
            encoding: 当前文本流使用的字符编码名称（用于精确计算编码后字节数）。
        """

        self.stream = stream
        self.encoding = encoding
        self.bytes = 0

    def write(self, text: str) -> int:
        """累计本次写入文本的编码字节数，检查未超限后写入底层流。

        Args:
            text: 待写入的字符串片段。

        Returns:
            int: 底层流实际写入的字符数。

        Raises:
            CapacityError: 当累计写入字节数超过 `LIMITS.json_bytes` 时抛出。
        """

        from .input_policy import LIMITS, enforce

        self.bytes += len(text.encode(self.encoding))
        enforce(self.bytes, LIMITS.json_bytes, "单份输出字节数")
        return self.stream.write(text)


def atomic_write(
    path: str | Path,
    writer: Callable[[BoundedWriter], object],
    encoding: str = "utf-8",
) -> None:
    """在目标路径同目录下创建临时文件写入并刷盘，随后原子替换目标路径。

    说明：
    - 临时文件创建在 `path.parent` 同目录下，以确保 `os.replace` 在同一文件系统内具备原子性。
    - 针对 Windows 平台杀毒软件或索引器造成的短暂文件锁（`winerror` 5 或 32），最多退避重试 3 次（共 4 次尝试）。
    - 无论成功或异常退出，`finally` 块都会清理残留的未替换临时文件。

    Args:
        path: 最终目标文件路径。
        writer: 接收 `BoundedWriter` 并执行实际内容写入的回调函数。
        encoding: 文本写入编码，默认 `"utf-8"`。
    """

    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="." + target_path.name, suffix=".tmp", dir=target_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as stream:
            writer(BoundedWriter(stream, encoding))
            stream.flush()
            os.fsync(stream.fileno())

        for attempt in range(4):
            try:
                os.replace(temp_name, target_path)
                break
            except PermissionError as exc:
                if (
                    os.name != "nt"
                    or getattr(exc, "winerror", None) not in (5, 32)
                    or attempt == 3
                ):
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_json(path: str | Path, data: object) -> None:
    """以 UTF-8 编码和缩进格式原子保存 JSON 文件（`datetime` 等非内置类型自动转为字符串）。

    Args:
        path: 目标 JSON 文件路径。
        data: 待序列化的 Python 对象。
    """

    atomic_write(
        path,
        lambda stream: json.dump(
            data, stream, ensure_ascii=False, indent=2, default=str
        ),
    )


def planned_names(point: Point) -> tuple[str, str, str]:
    """返回指定测点计划生成的三份文件名：`(CSV 成果文件名, 诊断 JSON 文件名, 曲线快照 JSON 文件名)`。

    Args:
        point: 测点配置对象。

    Returns:
        tuple[str, str, str]: 三元组 `("<code >#_output.csv", "<code>-diagnostics.json", "<code>-curve.json")`。
    """

    return (
        point.code + "#_output.csv",
        point.code + "-diagnostics.json",
        point.code + "-curve.json",
    )


def preflight_exports(points: Iterable[Point]) -> None:
    """在正式开始批处理前，按大小写不敏感规则预检所有测点的计划导出文件名是否冲突。

    Args:
        points: 批次内的测点配置序列。

    Raises:
        ValueError: 当任意两个计划产物的名称在 `casefold()` 后发生碰撞时抛出。
    """

    seen_names: dict[str, str] = {}
    for point in points:
        for file_name in planned_names(point):
            normalized_key = file_name.casefold()
            if normalized_key in seen_names:
                raise ValueError(
                    f"导出文件名冲突：{seen_names[normalized_key]} 与 {file_name}"
                )
            seen_names[normalized_key] = file_name


def export_point(
    directory: str | Path,
    result: PointResult,
    point: Point,
    batch: Batch,
    trace_directory: str | Path | None = None,
) -> None:
    """导出单个测点的 CSV 成果表与内部诊断 JSON（若 CSV 写出失败则自动回滚诊断文件）。

    CSV 格式约定：
    - 第 1 行（元信息行）：`["输入深度", "<H:.3f>", "<层1标签>", "<层1参考深度>", ...]`
      其中表层参考深度写为 `">{surface_min:.3f}"`，相对层写为 `"{H * relative:.3f}"`，底层写为 `"{H:.3f}"`。
    - 第 2 行（列标题行）：`["日期", "整点", "<各层> 实际深度", ..., "<各层> 温度", "<各层> 盐度", "<各层> 浊度", ...]`。
    - 第 3 行起（数据行）：每行对应一个目标整点，所有浮点指标格式化为 3 位小数，缺测层写空字符串 `""`（绝不填 0）。
      若 `batch.complete_rows_only` 为 `True`，则跳过任意层缺测的整点行（诊断 JSON 中仍保留完整记录）。

    Args:
        directory: 用户选择的 CSV 输出目录。
        result: `processing.process` 生成的单测点完整结果字典。
        point: 当前测点配置（`point.depth` 必须已确定为正数）。
        batch: 当前批次配置（提供 `rules.surface_min`、`encoding` 与 `complete_rows_only`）。
        trace_directory: 可选的内部追溯记录目录覆盖路径；缺省时由 `record_directory` 计算。

    Raises:
        ValueError: 当目标 CSV 或诊断文件已存在（大小写不敏感）时抛出，拒绝覆盖。
    """

    from .record_store import record_directory

    trace_dir = (
        Path(trace_directory)
        if trace_directory is not None
        else record_directory(directory, legacy=False)
    )
    target_paths = [
        Path(directory) / (point.code + "#_output.csv"),
        trace_dir / (point.code + "-diagnostics.json"),
    ]
    for target_path in target_paths:
        if target_path.parent.exists() and any(
            existing.name.casefold() == target_path.name.casefold()
            for existing in target_path.parent.iterdir()
        ):
            raise ValueError(f"结果文件已经存在，拒绝覆盖：{target_path}")

    layer_plan = point.layer_plan
    layer_labels = [layer["label"] for layer in layer_plan]

    # 构造第 1 行：输入水深与各层理论参考深度元信息
    meta_row = ["输入深度", f"{point.depth:.3f}"]
    for layer in layer_plan:
        meta_row.extend(
            [
                layer["label"],
                (
                    f">{batch.rules.surface_min:.3f}"
                    if layer["kind"] == "surface"
                    else f"{point.depth * layer.get('relative', 1):.3f}"
                ),
            ]
        )

    # 构造第 2 行：日期、整点、各层实际深度、各层温盐浊度表头
    header_row = (
        ["日期", "整点"]
        + [label + " 实际深度" for label in layer_labels]
        + [
            label + " " + metric_label
            for label in layer_labels
            for metric_label in ("温度", "盐度", "浊度")
        ]
    )

    # 构造第 3 行起的数据行
    csv_rows: list[list[str]] = []
    for hour_row in result["rows"]:
        selected_by_label = {
            sel["label"]: sel["observation"] for sel in hour_row["selections"]
        }
        layer_observations = [selected_by_label.get(label) for label in layer_labels]
        if batch.complete_rows_only and any(
            obs is None for obs in layer_observations
        ):
            continue

        cell_values = [
            f"{obs['depth']:.3f}" if obs else "" for obs in layer_observations
        ]
        cell_values += [
            f"{obs[metric_key]:.3f}" if obs else ""
            for obs in layer_observations
            for metric_key in ("temperature", "salinity", "turbidity")
        ]
        csv_rows.append(
            [hour_row["target"][:10], hour_row["target"][11:19], *cell_values]
        )

    def write_csv_stream(stream: BoundedWriter) -> None:
        """向有界文本流依次写入元信息行、表头行与整点数据行。"""

        csv_writer = csv.writer(stream)
        csv_writer.writerow(meta_row)
        csv_writer.writerow(header_row)
        csv_writer.writerows(csv_rows)

    diagnostic_path = trace_dir / (point.code + "-diagnostics.json")
    atomic_json(diagnostic_path, result)
    try:
        atomic_write(
            Path(directory) / (point.code + "#_output.csv"),
            write_csv_stream,
            batch.encoding,
        )
    except Exception:
        diagnostic_path.unlink(missing_ok=True)
        raise
