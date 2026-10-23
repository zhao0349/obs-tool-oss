"""单份 DAT 文件时间校正方案分析（只读预览）与带自动备份的事务级保存模块。

核心功能与安全契约：
1. **只读方案分析（`analyse`）**：
   - `mode="inspect"`：第 1 步仅读取并检查文件基本结构、时间范围与传感器列；
   - `mode="known"`：按用户提供的“仪器记录时间”与“实际正确时间”计算固定秒数时差；
   - `mode="casts"`：调用 `correction_actions.identify_actions` 识别每次完整抛测动作，
     结合相邻抛测间隔自动统计中间缺测次数，并在确认首尾缺测分配后生成整点对齐明细与预览过程线。
   - **拦截保护**：若文件内存在坏行、非有限值、时间倒退/重置、抛测分界不确定、动作间隔偏差 `> 15` 分钟，
     或问题整点达到总整点数一半及以上（`2 * len(problems) >= count`），则标记 `valid=False`（仅限只读预览）。
2. **事务级写回与自动备份（`save`）**：
   - 仅当 `plan["valid"] is True` 且用户最终点击“确认修改”时才允许调用；
   - 写入前重新校验源文件 SHA-256 指纹与磁盘剩余空间（至少 `3 * 文件大小 + 1MB`）；
   - 原位修改前先创建不覆盖旧备份的 `.bak` / `.bak.1` 原始备份并校验 SHA-256，
     仅更新有效观测行的日期时间前缀（保留前导空白、传感器数值与仪器尾注），
     原子替换后再次校验文件指纹，若校验异常则自动从备份回滚恢复原文件。
"""

from collections.abc import Callable
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import BinaryIO

from .data_types import CorrectionParameters
from .input_policy import LIMITS, Budget, check_files, enforce
from .readers import bounded_lines, read_files

# 相邻抛测动作间隔偏离整数小时的最大允许容差（15 分钟 = 900 秒，含边界）
INTERVAL_TOLERANCE = 15 * 60
# 平移后每次抛测代表锚点距目标整点的最大允许残差（15 分钟 = 900 秒，含边界）
TARGET_TOLERANCE = 15 * 60


# ==============================================================================
# 1. 时间文本校验与文件 SHA-256 指纹计算
# ==============================================================================


def stamp(value: str | datetime) -> datetime:
    """将输入转换为无时区且精确到秒的 `datetime` 对象；含时区或微秒时抛出 `ValueError`。"""
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo or value.microsecond:
        raise ValueError("请填写精确到秒的完整日期与时间。")
    return value


def fingerprint(path: str | Path, cancelled: Callable[[], bool] = lambda: False) -> str:
    """在容量限制与取消检查下分块计算文件的 SHA-256 十六进制摘要，用于保存前校验源文件未被篡改。"""
    check_files([path])
    digest = hashlib.sha256()
    budget = Budget(cancelled=cancelled)
    with Path(path).open("rb") as stream:
        while True:
            budget.check()
            block = stream.read(65536)
            if not block:
                break
            budget.bytes += len(block)
            enforce(budget.bytes, LIMITS.file_bytes, "校正文件字节数")
            digest.update(block)
    return digest.hexdigest()


# ==============================================================================
# 2. 时间校正只读分析方案生成 (analyse)
# ==============================================================================


def analyse(
    path: str | Path,
    parameters: CorrectionParameters,
    cancelled: Callable[[], bool] = lambda: False,
):
    """只读分析单份 DAT 文件，生成固定时差校正方案、预览过程线、整点匹配明细及可保存性结论。

    Args:
        path: 待分析的原始 DAT 文件路径。
        parameters: 校正参数字典（支持 `'inspect'`、`'known'`、`'casts'` 三种 `mode`）。
        cancelled: 协作式取消检查回调。

    Returns:
        dict: 包含 `valid`、`reason`、`delta_seconds`、`curve`、`matches` 等信息的校正方案字典。
    """
    path = check_files([path])[0]
    rows, diagnostics = read_files(
        [path], cancelled, preserve_order=True, allow_early=True
    )

    # 抽稀生成最多约 8000 个点的深度过程线供核对页绘制，并确保最后一条观测点始终包含在内
    curve = [
        dict(timestamp=row["timestamp"].isoformat(), depth=row["depth"])
        for row in rows[:: max(1, (len(rows) + 7999) // 8000)]
    ]
    if curve[-1]["timestamp"] != rows[-1]["timestamp"].isoformat():
        curve.append(
            dict(timestamp=rows[-1]["timestamp"].isoformat(), depth=rows[-1]["depth"])
        )

    plan = dict(
        path=str(path),
        parameters=parameters,
        original_sha256=fingerprint(path, cancelled),
        records=len(rows),
        columns=diagnostics.files[0],
        curve=curve,
        valid=False,
        old_start=rows[0]["timestamp"].isoformat(),
        old_end=rows[-1]["timestamp"].isoformat(),
        matches=[],
    )

    if parameters.get("mode") == "inspect":
        plan["reason"] = "文件读取成功，请设置校正方式并填写时间参数。"
        return plan

    if diagnostics.counts["bad_data"] or diagnostics.counts["nonfinite"]:
        plan["reason"] = (
            "文件中存在日期格式错误或必需观测项异常；为保证数据完整，不能仅修改部分数据行。"
        )
        return plan

    if any(
        curr_row["timestamp"] < prev_row["timestamp"]
        for prev_row, curr_row in zip(rows, rows[1:])
    ):
        plan["reason"] = (
            "文件内记录时间存在倒退或多次重置，无法通过单一固定时差统一校准。"
        )
        return plan

    if parameters.get("mode") == "known":
        delta = stamp(parameters["correct"]) - stamp(parameters["instrument"])
    elif parameters.get("mode") == "casts":
        from .correction_actions import identify_actions

        recognition = identify_actions(rows, cancelled)
        actions = recognition["actions"]
        plan["recognition"] = recognition
        enforce(len(actions), LIMITS.segments, "校正动作数")

        start, end = stamp(parameters["start"]), stamp(parameters["end"])
        if start.minute or start.second or end.minute or end.second or end < start:
            raise ValueError(
                "测验开始整点与结束整点的分、秒均须为 00，且开始时间不能晚于结束时间。"
            )
        count = int((end - start).total_seconds() // 3600) + 1
        enforce(count, LIMITS.targets, "校正目标整点数")
        plan.update(
            actions=actions,
            target_count=count,
            anchor="每次抛测最深观测时刻",
            interval_tolerance_minutes=15,
            target_tolerance_minutes=15,
        )
        if not actions:
            plan["reason"] = (
                "未在文件中识别出有效的抛测下放剖面；当前仅支持查看曲线，不能修改文件。"
            )
            return plan

        # 计算相邻完整抛测动作之间的整数小时步长，统计中间时段缺测次数与间隔超差项
        positions, bad_intervals, middle = [0], set(), 0
        for index in range(1, len(actions)):
            gap = (
                stamp(actions[index]["timestamp"])
                - stamp(actions[index - 1]["timestamp"])
            ).total_seconds()
            hours = round(gap / 3600)
            if hours < 1 or abs(gap - hours * 3600) > INTERVAL_TOLERANCE:
                bad_intervals.add(index)
            else:
                middle += hours - 1
            positions.append(positions[-1] + hours)

        remainder = count - len(actions) - middle
        plan.update(middle_missing=middle, end_missing_total=remainder)

        # 检查开头缺测与结尾缺测分配状态
        head, tail = parameters.get("head"), parameters.get("tail")
        allocated = remainder == 0 or (
            type(head) is int
            and type(tail) is int
            and head >= 0
            and tail >= 0
            and head + tail == remainder
        )
        if parameters.get("assume_no_head"):
            head, tail, allocated = 0, remainder, remainder >= 0
            plan["assumption"] = "已选择“不记得”，按测验开头无缺测处理"

        head_offset = head if allocated and head is not None else 0
        plan.update(
            head_missing=head_offset,
            tail_missing=max(0, remainder - head_offset),
        )
        delta = start + timedelta(hours=head_offset) - stamp(actions[0]["timestamp"])

        # 将抛测动作映射到目标整点槽位（若发生槽位冲突，保留首个动作并记录冲突，不把后续动作推迟一小时）
        mapped, collisions = {}, set()
        for index, (position, action) in enumerate(zip(positions, actions)):
            key = head_offset + position
            if key in mapped:
                collisions.add(key)
            else:
                mapped[key] = (index, action)

        problems = set()
        for index in range(count):
            target = start + timedelta(hours=index)
            match = mapped.get(index)
            reasons = []
            residual = None
            if match is None:
                reasons.append("缺测")
            else:
                residual = (
                    stamp(match[1]["timestamp"]) + delta - target
                ).total_seconds()
                if abs(residual) > TARGET_TOLERANCE:
                    reasons.append("距目标整点偏差超过 15 分钟")
                if match[0] in bad_intervals:
                    reasons.append("相邻抛测间隔偏离整数小时超过 15 分钟")
            if index in collisions:
                reasons.append("相邻抛测动作对应同一目标整点，分界不确定")
            if reasons:
                problems.add(index)
            plan["matches"].append(
                dict(
                    target=target.isoformat(),
                    action=match[0] + 1 if match else None,
                    representative=match[1]["timestamp"] if match else None,
                    residual_seconds=residual,
                    reasons=reasons,
                )
            )

        plan["problem_count"] = len(problems)
        if (
            remainder < 0
            or len(actions) > count
            or any(idx < 0 or idx >= count for idx in mapped)
        ):
            plan["reason"] = (
                "识别到的有效抛测次数或时间跨度超出所填测验起止整点范围，软件不会截除多余抛测记录，请核对测验起止时间。"
            )
        elif recognition["uncertainties"]:
            plan["reason"] = (
                "部分抛测过程分界不确定，当前仅供查看过程线与明细，不能自动修改文件："
                + "；".join(recognition["uncertainties"][:4])
            )
        elif not allocated:
            plan["needs_allocation"] = True
            plan["reason"] = (
                f"测验中间时段缺测 {middle} 次，请在下方确认剩余 {remainder} 次缺测在开头和结尾的分配。"
            )
        elif 2 * len(problems) >= count or bad_intervals:
            plan["reason"] = (
                "抛测时间间隔偏差过大或异常整点过多，无法按抛测规律自动匹配；当前仅支持查看，建议使用“已知时间对应关系”或人工处理。"
            )
        else:
            plan["valid"] = True

        if not plan["valid"]:
            plan["preview_only"] = True
    else:
        raise ValueError("未知的时间校正方案。")

    try:
        plan.update(
            delta_seconds=int(delta.total_seconds()),
            new_start=(rows[0]["timestamp"] + delta).isoformat(),
            new_end=(rows[-1]["timestamp"] + delta).isoformat(),
        )
        for row in rows:
            if not 1 <= (row["timestamp"] + delta).year <= 9999:
                raise OverflowError()
    except OverflowError as exc:
        raise ValueError("校正后的日期超出有效年份范围（1—9999年）。") from exc

    if parameters["mode"] == "known":
        plan["valid"] = True
    return plan


# ==============================================================================
# 3. 事务级写回、自动递增备份与失败回滚 (save)
# ==============================================================================


def _copy(
    source: str | Path,
    output: BinaryIO,
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """分块将源文件内容复制到目标二进制流并强制刷盘（`fsync`），返回所写入字节的 SHA-256 摘要。"""
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        budget = Budget(cancelled=cancelled)
        while True:
            budget.check()
            chunk = stream.read(65536)
            if not chunk:
                break
            budget.bytes += len(chunk)
            enforce(budget.bytes, LIMITS.file_bytes, "保存字节数")
            output.write(chunk)
            digest.update(chunk)
    output.flush()
    os.fsync(output.fileno())
    return digest.hexdigest()


def save(plan, target=None, cancelled=lambda: False):
    """在用户最终确认后执行时间校正文件保存（原位修改并创建 `.bak` 备份，或另存为新 `.dat` 文件）。

    事务保证：
    - 写入前再次运行 `analyse` 确认源文件 SHA-256 与平移秒数未发生变化；
    - 先将修改时间戳后的完整内容写入同目录临时文件 `.obs-time-*.tmp`，并调用 `read_files` 验证各列数值完全未变；
    - 原位保存时，先创建不覆盖旧备份的 `.bak` / `.bak.N` 备份并验证哈希，再调用 `os.replace` 原子替换原文件；
    - 原子替换后再次校验源文件哈希，一旦发现写后内容不一致，立即从备份文件自动回滚恢复原文件。
    """
    if not isinstance(plan, dict) or plan.get("valid") is not True:
        raise ValueError("当前预览未通过校验，不允许修改文件。")

    source = check_files([plan["path"]])[0]
    destination = Path(target).resolve() if target else source
    inplace = destination == source
    if not inplace and (destination.suffix.lower() != ".dat" or destination.exists()):
        raise ValueError("请指定尚不存在的 .dat 文件名，已有文件不会被覆盖。")

    lock = source.with_name("." + source.name + ".obs-time-lock")
    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(handle)
    temporary = backup = None

    try:
        fresh = analyse(source, plan["parameters"], cancelled)
        if (
            not fresh["valid"]
            or fresh["original_sha256"] != plan["original_sha256"]
            or fresh["delta_seconds"] != plan["delta_seconds"]
        ):
            raise ValueError("原始文件或校正方案已发生变动，请重新分析并确认。")

        required = source.stat().st_size * 3 + 1024 * 1024
        if shutil.disk_usage(destination.parent).free < required:
            raise OSError(
                "保存位置可用磁盘空间不足，需为校正文件与原始备份预留足够空间。"
            )

        rows, _ = read_files([source], cancelled, preserve_order=True, allow_early=True)
        shifts = {
            row["line"]: row["timestamp"] + timedelta(seconds=fresh["delta_seconds"])
            for row in rows
        }

        # 步骤 1：将更新时间戳后的内容写入临时文件，原样保留前导空白、传感器数值、换行符及仪器尾注
        file_descriptor, temporary = tempfile.mkstemp(
            prefix=".obs-time-", suffix=".tmp", dir=destination.parent
        )
        with os.fdopen(file_descriptor, "wb") as out, source.open("rb") as inp:
            budget = Budget(cancelled=cancelled)
            for line_no, raw in bounded_lines(inp, budget):
                if line_no in shifts:
                    match = re.match(
                        rb"^(\s*)\d{2}:\d{2}:\d{2}(\s+)\d{2}/\d{2}/\d{4}", raw
                    )
                    if match is None:
                        raise ValueError("数据行在保存前发生变化。")
                    shifted_dt = shifts[line_no]
                    raw = (
                        match[1]
                        + shifted_dt.strftime("%H:%M:%S").encode("ascii")
                        + match[2]
                        + f"{shifted_dt.month:02}/{shifted_dt.day:02}/{shifted_dt.year:04}".encode(
                            "ascii"
                        )
                        + raw[match.end() :]
                    )
                out.write(raw)
            out.flush()
            os.fsync(out.fileno())

        # 步骤 2：重新解析临时文件，验证有效记录条数、平移后时间及所有传感器通道数值完全一致
        checked, _ = read_files(
            [temporary], cancelled, preserve_order=True, allow_early=True
        )
        if len(checked) != len(rows) or any(
            checked_row["timestamp"] != shifts[orig_row["line"]]
            or any(
                checked_row.get(key) != orig_row.get(key)
                for key in (
                    "depth",
                    "temperature",
                    "salinity",
                    "turbidity",
                    "conductivity",
                    "battery",
                )
            )
            for checked_row, orig_row in zip(checked, rows)
        ):
            raise ValueError("校正内容校验失败，原文件未修改。")

        corrected_digest = fingerprint(temporary, cancelled)
        if fingerprint(source, cancelled) != plan["original_sha256"]:
            raise ValueError("原文件已变化，请重新分析。")

        # 分支 A：另存为新文件模式（不改动原文件，也不生成 .bak 备份）
        if not inplace:
            created = False
            try:
                with destination.open("xb") as out:
                    created = True
                    if _copy(temporary, out, cancelled) != corrected_digest:
                        raise OSError("另存校验失败")
                return dict(path=str(destination), backup=None, inplace=False)
            except Exception:
                if created:
                    destination.unlink(missing_ok=True)
                raise

        # 分支 B：原位修改模式——先创建递增编号的 .bak 备份并校验 SHA-256
        index = 0
        while True:
            backup = source.with_name(
                source.name + ".bak" + (f".{index}" if index else "")
            )
            try:
                out = backup.open("xb")
                break
            except FileExistsError:
                index += 1
                enforce(index, LIMITS.directory_entries, "已有备份编号查找次数")

        try:
            with out:
                if _copy(source, out, cancelled) != plan["original_sha256"]:
                    raise ValueError("备份与原始预览不一致")
            if fingerprint(backup, cancelled) != plan["original_sha256"]:
                raise OSError("备份落盘校验失败")
        except Exception:
            backup.unlink(missing_ok=True)  # 仅清理本次未完成的备份碎片，绝不删除历史备份
            raise

        if fingerprint(source, cancelled) != plan["original_sha256"]:
            raise ValueError("原文件已变化，完整备份已保留，请重新分析。")

        # 步骤 3：不可中断提交段——原子替换原文件并校验落盘指纹，失败则从备份自动恢复
        os.replace(temporary, source)
        temporary = None
        try:
            if fingerprint(source) != corrected_digest:
                raise OSError("写后内容不一致")
        except Exception as verification_error:
            rollback = None
            try:
                restore_fd, rollback = tempfile.mkstemp(
                    prefix=".obs-restore-", dir=source.parent
                )
                with os.fdopen(restore_fd, "wb") as out:
                    _copy(backup, out)
                os.replace(rollback, source)
            except Exception as rollback_error:
                raise OSError(
                    f"写后校验失败且无法自动恢复。请保留原文件并从完整备份核查恢复：{backup}"
                ) from rollback_error
            finally:
                if rollback:
                    Path(rollback).unlink(missing_ok=True)
            raise OSError(
                "写后校验失败，已恢复原文件，备份保留。"
            ) from verification_error

        return dict(path=str(source), backup=str(backup), inplace=True)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
