"""OBS 原始 DAT 文件有界流式解析、动态表头通道映射与质量诊断模块。

本模块负责将现场导出的文本型 `.dat` 文件安全转换为统一的 `Observation` 列表：
1. **逐文件独立识别传感器列**：
   读取每份文件各自的 `Sensor Data Columns` 表头块，按通道名称与单位识别四项必需通道
   （`depth`、`turbidity`、`temperature`、`salinity`）及两项可选通道（`conductivity`、`battery`），
   不回退到固定列下标，支持同一测点下不同文件列顺序不同。
2. **仪器尾注与结束标记过滤（`observation_lines`）**：
   自动过滤文件末尾的 `Bytes read from file.`、`File ended without EOF.`、`OBS>` 提示符，
   以及文件最末尾的全零 `1970/1969` 结束标记行（文件内部的全零或早年有效记录不被误删）。
3. **有界读取与并发修改检测（`bounded_lines` / `read_files`）**：
   全程受 `InputLimits` 与 `Budget` 约束，禁止二进制乱码文件或超大行数撑爆内存，
   并在读取前后校验文件 `inode / size / mtime_ns` 是否一致。
"""

import codecs
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
import math
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO

from .data_types import FileColumns, Observation, ParseEvent
from .input_policy import LIMITS, Budget, InputLimits, check_files, enforce


# ==============================================================================
# 1. 传感器标准字段与表头别名映射表
# ==============================================================================

FIELDS = ("depth", "turbidity", "temperature", "salinity")
FIELD_NAMES = {
    "depth": "深度(Depth)",
    "turbidity": "浊度(OBS-1)",
    "temperature": "温度(Temperature)",
    "salinity": "盐度(Salinity)",
}
ALIASES = {
    "depth": "depth",
    "depth(m)": "depth",
    "obs-1(ntu)": "turbidity",
    "temperature(deg.c)": "temperature",
    "temperature(°c)": "temperature",
    "salinity(psu)": "salinity",
    "conductivity(ms/cm)": "conductivity",
    "battery(v)": "battery",
}


class DATFormatError(ValueError):
    """当 DAT 文件的字符编码、表头传感器声明或数据行结构不符合规范时抛出的格式异常。"""


class ParseDiagnostics(list[ParseEvent]):
    """解析诊断收集器：在列表内最多保留 `limit` 条详细事件，同时通过 `counts` 完整统计全部事件频次。"""

    def __init__(self, limit: int) -> None:
        """初始化详细事件保留上限 `limit`、分类计数器 `counts` 与各文件列映射摘要列表 `files`。"""
        super().__init__()
        self.limit = limit
        self.counts: Counter[str] = Counter()
        self.files: list[FileColumns] = []

    def add(self, path: str | Path, line: int, reason: str) -> None:
        """记录一条行级解析事件：始终累加分类计数，列表未满 `limit` 时保存前 200 字符的原因摘要。"""
        self.counts[reason.split(":")[0]] += 1
        if len(self) < self.limit:
            self.append(dict(file=str(path), line=line, reason=reason[:200]))


def normalize(value: str) -> str:
    """移除传感器名称中的所有空白字符并转为小写，用于与 `ALIASES` 进行精确匹配（不猜测未知单位）。"""
    return re.sub(r"\s+", "", value).lower()


# ==============================================================================
# 2. 有界字节流逐行读取与仪器尾注剥离
# ==============================================================================


def bounded_lines(stream: BinaryIO, budget: Budget) -> Iterator[tuple[int, bytes]]:
    """逐行安全读取二进制流并计量资源开销，返回 `(从 1 开始的物理行号, 原始行字节)`。

    安全检查：
    - 限制单行最大字节数 `line_bytes`、单文件最大字节数 `file_bytes`、测点累计字节数 `point_bytes` 与单文件最大行数 `file_lines`；
    - 拒绝包含除 Tab(`9`)、LF(`10`)、CR(`13`) 以外的 ASCII 控制字符（`< 32`）的二进制或损坏文件。
    """
    size, count = 0, 0
    while True:
        budget.check()
        raw = stream.readline(budget.limits.line_bytes + 1)
        if not raw:
            break
        enforce(len(raw), budget.limits.line_bytes, "单行字节数")
        size += len(raw)
        budget.bytes += len(raw)
        if budget.parent:
            budget.parent.bytes += len(raw)
            budget.check()
        count += 1
        enforce(size, budget.limits.file_bytes, "文件实际读取字节数")
        enforce(budget.bytes, budget.limits.point_bytes, "测点实际读取总字节数")
        enforce(count, budget.limits.file_lines, "文件物理行数")
        if any(byte < 32 and byte not in (9, 10, 13) for byte in raw):
            raise DATFormatError(
                f"第 {count} 行包含无法识别的乱码或非文本内容，请确认文件为标准文本格式的 OBS 原始数据。"
            )
        yield count, raw


def observation_lines(
    stream: BinaryIO,
    budget: Budget,
    encoding: str,
    diagnostics: ParseDiagnostics,
    path: Path,
    column_count: Callable[[], int],
) -> Iterator[tuple[int, str, int]]:
    """解码文本行并保留原始物理行号，仅剔除位于数据正文末尾的仪器尾注与全零结束标记行。

    为什么使用 `pending` 单行延迟缓存：
        OBS 仪器在导出文件末尾常写入全零的 `00:00:00 01/01/1970 ...` 或 `1 23:59:59 12/31/1969 ...` 结束行。
        若该行之后紧跟文件结尾或 `Bytes read from file.` / `OBS>` 尾注，说明它是仪器结束标记，应记入
        `instrument_end_record` 并忽略；若该行之后又出现了正常观测行，说明它只是文件中间的普通记录，会被如实放出。
    """
    pending = None
    body_seen = False
    for line_no, raw in bounded_lines(stream, budget):
        try:
            line = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise DATFormatError(
                f"第 {line_no} 行字符编码异常，无法按标准文本读取，请检查文件是否损坏。"
            ) from exc
        text = line.strip()
        footer = bool(
            re.fullmatch(
                r"(?:\d+\s+Bytes read from file\.|File ended without EOF\.|OBS>)",
                text,
                re.I,
            )
        )
        if footer and body_seen:
            if pending:
                diagnostics.add(path, pending[0], "instrument_end_record")
                pending = None
            diagnostics.add(path, line_no, "instrument_notice: " + text)
            continue
        if not text:
            yield line_no, line, len(raw)
            continue
        if pending:
            yield pending
            pending = None
        parts = text.split()
        body_seen = body_seen or bool(re.match(r"^\s*\d{1,2}:", line))
        # 特殊前导序号 "1" 仅针对已知的 1969/12/31 仪器结束标记行，不普遍剥离普通数据行前缀
        fields = parts[1:] if parts[:3] == ["1", "23:59:59", "12/31/1969"] else parts
        end_stamp = fields[:2] in (
            ["00:00:00", "01/01/1970"],
            ["23:59:59", "12/31/1969"],
        )
        if end_stamp and len(fields) == column_count() and len(fields) > 2:
            try:
                zero = all(float(value) == 0 for value in fields[2:])
            except ValueError:
                zero = False
            if zero:
                pending = (line_no, line, len(raw))
                continue
        yield line_no, line, len(raw)
    if pending:
        diagnostics.add(path, pending[0], "instrument_end_record")


# ==============================================================================
# 3. 测点多文件读取、去重合并与完整性校验主入口
# ==============================================================================


def read_files(
    files: Iterable[str | Path],
    cancelled: Callable[[], bool] = lambda: False,
    *,
    limits: InputLimits = LIMITS,
    preserve_order: bool = False,
    allow_early: bool = False,
    budget: Budget | None = None,
) -> tuple[list[Observation], ParseDiagnostics]:
    """读取单个测点关联的完整 DAT 文件集合，返回有效观测记录列表与解析诊断信息。

    行为模式说明：
    - **默认常规模式（`preserve_order=False, allow_early=False`）**：
      先按文件路径升序读取，跳过 2000 年以前的无效初始年份行，自动剔除时间戳与四项核心指标完全相同的重复行
      （记为 `duplicate`），对同一秒不同观测值保留记录但标记 `timestamp_conflict`，最后按 `(timestamp, file, line)` 稳定排序。
    - **时间校正分析模式（`preserve_order=True, allow_early=True`）**：
      严格保留输入文件顺序、文件内物理行顺序及重复行，并允许 1970 年等早期未校准年份参与时间校正。
    - **测点整体完整性原则**：
      同一测点下的任一文件若缺失表头、缺失必需通道或损坏行过多，立即抛出异常使该测点整体失败，
      绝不使用其余部分文件拼凑残缺的“成功”结果。
    """
    budget = budget or Budget(limits, cancelled)
    paths = check_files(files, limits)
    records, diagnostics = [], ParseDiagnostics(limits.diagnostics)
    seen, timestamps = set(), {}

    for path in paths if preserve_order else sorted(paths):
        budget.check()
        before = path.stat()
        mapping, descriptions = [], []
        table = False
        started = False
        header_size = file_records = bad = physical = 0
        encoding = None
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (before.st_dev, before.st_ino):
                    raise DATFormatError("文件在读取前已发生变动，请重新选择该文件。")
                enforce(opened.st_size, limits.file_bytes, "文件字节数")

                # 探测文件头部编码：优先尝试 UTF-8（含 BOM），其次回退至中文 GB18030
                probe = stream.read(min(limits.header_bytes, opened.st_size))
                budget.bytes += len(probe)
                if budget.parent:
                    budget.parent.bytes += len(probe)
                    budget.check()
                enforce(budget.bytes, limits.point_bytes, "测点实际读取总字节数")
                for candidate in ("utf-8-sig", "gb18030"):
                    try:
                        codecs.getincrementaldecoder(candidate)().decode(
                            probe, final=False
                        )
                        encoding = candidate
                        break
                    except UnicodeDecodeError:
                        pass
                if encoding is None:
                    raise DATFormatError(
                        "文件文本编码不受支持（仅支持 UTF-8 或中文 GB18030 编码的文本文件）。"
                    )

                stream.seek(0)
                for line_no, line, raw_size in observation_lines(
                    stream,
                    budget,
                    encoding,
                    diagnostics,
                    path,
                    lambda: len(mapping) + 2,
                ):
                    is_data = bool(re.match(r"^\s*\d{1,2}:", line))
                    if not started and not is_data:
                        header_size += raw_size
                        enforce(header_size, limits.header_bytes, "头部探测字节数")
                        enforce(line_no, limits.header_lines, "头部探测行数")
                    if not line.strip():
                        continue

                    # 阶段 A：解析 Sensor Data Columns 表头声明块
                    if not started and not is_data:
                        if re.fullmatch(r"\s*Sensor\s+Data\s+Columns\s*", line, re.I):
                            if table:
                                raise DATFormatError(
                                    "文件表头中存在重复的传感器通道声明（Sensor Data Columns）。"
                                )
                            table = True
                            continue
                        if table and re.match(r"^\s*\d+\s+", line):
                            match = re.fullmatch(
                                r"\s*\d{1,3}\s+(.+?)\s+Mean\s*", line, re.I
                            )
                            if not match:
                                raise DATFormatError(
                                    f"第 {line_no} 行传感器声明格式不受支持（数据列须为单列 Mean 平均值格式）。"
                                )
                            description = match[1].strip()
                            enforce(
                                len(description), limits.field_bytes, "传感器描述长度"
                            )
                            key = ALIASES.get(normalize(description))
                            if key is None and any(
                                normalize(description).startswith(prefix)
                                for prefix in (
                                    "depth",
                                    "obs-",
                                    "temperature",
                                    "salinity",
                                    "conductivity",
                                    "battery",
                                )
                            ):
                                raise DATFormatError(
                                    f"第 {line_no} 行传感器通道名称或单位无法识别：{description}"
                                )
                            if key is not None and key in mapping:
                                raise DATFormatError(
                                    f"第 {line_no} 行存在重复的传感器通道声明：{description}"
                                )
                            mapping.append(key)
                            descriptions.append(description)
                            enforce(len(mapping) + 2, limits.fields, "声明字段数")
                        continue

                    # 阶段 B：首次进入数据正文前，确认四项必需传感器通道均已声明
                    if not started:
                        missing = set(FIELDS) - set(mapping)
                        if not table or missing:
                            missing_labels = (
                                "、".join(
                                    FIELD_NAMES[k] for k in FIELDS if k in missing
                                )
                                or "传感器声明表头"
                            )
                            raise DATFormatError(
                                "缺少可识别的传感器表头（Sensor Data Columns）或缺少必需观测通道："
                                + missing_labels
                            )
                        started = True

                    # 阶段 C：解析单行观测记录（HH:MM:SS MM/DD/YYYY + 传感器数值列）
                    physical += 1
                    parts = line.split()
                    enforce(len(parts), limits.fields, "数据字段数")
                    if len(parts) != len(mapping) + 2:
                        raise DATFormatError(
                            f"第 {line_no} 行数据列数（{len(parts)} 列）与表头声明（{len(mapping)+2} 列）不一致。"
                        )
                    if any(
                        len(part.encode("utf-8")) > limits.field_bytes for part in parts
                    ):
                        raise DATFormatError(f"第 {line_no} 行存在长度异常的字段内容。")

                    reason = None
                    try:
                        if not re.fullmatch(
                            r"\d{2}:\d{2}:\d{2}", parts[0]
                        ) or not re.fullmatch(r"\d{2}/\d{2}/\d{4}", parts[1]):
                            raise ValueError("日期时间格式应为HH:mm:ss MM/dd/yyyy")
                        dt = datetime.strptime(
                            parts[1] + " " + parts[0], "%m/%d/%Y %H:%M:%S"
                        )
                        values = {}
                        for key, text in zip(mapping, parts[2:]):
                            if key is None:
                                continue
                            try:
                                number = float(text)
                            except ValueError:
                                if key in FIELDS:
                                    raise
                                continue
                            if not math.isfinite(number):
                                if key in FIELDS:
                                    raise ArithmeticError("nonfinite")
                                continue
                            values[key] = number

                        if dt.year < 2000 and not allow_early:
                            reason = "invalid_year"
                        else:
                            file_records += 1
                            budget.records += 1
                            if budget.parent:
                                budget.parent.records += 1
                                budget.check()
                            enforce(file_records, limits.file_records, "文件观测数")
                            enforce(budget.records, limits.point_records, "测点观测数")
                            identity_values = tuple(values[key] for key in FIELDS)
                            identity = (dt, *identity_values)
                            if not preserve_order and identity in seen:
                                reason = "duplicate"
                            else:
                                if (
                                    dt in timestamps
                                    and timestamps[dt] != identity_values
                                ):
                                    diagnostics.add(path, line_no, "timestamp_conflict")
                                if not preserve_order:
                                    seen.add(identity)
                                timestamps[dt] = identity_values
                                records.append(
                                    dict(
                                        timestamp=dt,
                                        **values,
                                        file=str(path),
                                        line=line_no,
                                        raw_time=parts[0],
                                        raw_date=parts[1],
                                    )
                                )
                    except ArithmeticError:
                        reason = "nonfinite"
                        bad += 1
                    except ValueError as exc:
                        from .input_policy import CapacityError

                        if isinstance(exc, CapacityError):
                            raise
                        reason = "bad_data: " + str(exc)
                        bad += 1

                    if reason:
                        diagnostics.add(path, line_no, reason)
                    if bad > max(8, physical // 20):
                        raise DATFormatError(
                            "文件中格式错误或数值异常的数据行过多，无法作为有效观测文件处理。"
                        )

                # 读取结束后再次校验文件状态，确保读取期间文件未被并发改写或替换
                after = os.fstat(stream.fileno())
                current = path.stat()
                if (after.st_size, after.st_mtime_ns, after.st_ino) != (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ino,
                ) or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                ):
                    raise DATFormatError(
                        "读取期间文件内容发生变动，请确认文件不再写入后重试。"
                    )

            if not started or file_records == 0:
                raise DATFormatError("没有通过校验的OBS观测数据。")
            diagnostics.files.append(
                dict(
                    path=str(path),
                    encoding=encoding,
                    sensors=descriptions,
                    mapping=mapping,
                )
            )
        except (UnicodeError, DATFormatError) as exc:
            raise DATFormatError(f"{path.name}：{exc}") from exc

    budget.check()
    if not preserve_order:
        records.sort(key=lambda r: (r["timestamp"], r["file"], r["line"]))
    budget.check()
    return records, diagnostics
