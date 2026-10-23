"""测点处理结果的只读核查视图投影模块（计算共享片段、跨层复用与来源详情 HTML）。

设计目标：
    在底层算法中，片段复用（`segment_reused`）是在第 2 个及后续整点复用同一片段时记录的；
    而在结果页展示时，用户希望“首次使用该片段的整点”与“后续复用该片段的整点”双向关联并高亮提示。
    `ReviewProjection` 在不修改底层业务结果、CSV 或诊断 JSON 的前提下，基于完整结果建立双向索引，
    并提供转义安全的来源详情说明 HTML。
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
import math
import ntpath
from typing import Any


def observation_key(observation: dict[str, Any] | None) -> tuple[str, int] | None:
    """使用规范化的 Windows 绝对文件路径与正整数物理行号构造原始观测记录的唯一来源标识。

    Args:
        observation: 选中的观测记录字典（含 `"file"` 与 `"line"`）。

    Returns:
        tuple[str, int] | None: 若文件路径与行号均有效，返回 `(规范化路径, 物理行号)`；否则返回 `None`。
    """

    if not observation:
        return None
    file_path = observation.get("file")
    line_number = observation.get("line")
    if (
        not isinstance(file_path, str)
        or not ntpath.isabs(file_path)
        or type(line_number) is not int
        or line_number < 1
    ):
        return None
    return ntpath.normcase(ntpath.normpath(file_path)), line_number


def time_text(value: object) -> str:
    """将 ISO 时间字符串或时间对象格式化为空格分隔的可读文本；缺失时返回 `"未记录"`。

    Args:
        value: 时间字符串、`datetime` 对象或 `None`。

    Returns:
        str: 用于界面展示的日期时间文本。
    """

    return str(value).replace("T", " ") if value is not None else "未记录"


class ReviewProjection:
    """基于单测点完整处理结果构建的只读核查关联索引（双向关联片段复用与跨层观测复用）。"""

    def __init__(self, result: dict[str, Any] | None) -> None:
        """扫描全部整点结果行，建立片段共用分组 `segment_groups` 与单行内跨层共用分组 `layer_groups`。

        Args:
            result: 单测点处理结果字典（`PointResult`），允许为 `None`。
        """

        self.result = result or {}
        self.rows: list[dict[str, Any]] = self.result.get("rows", [])
        self.segment_groups: dict[int, list[int]] = defaultdict(list)
        self.layer_groups: dict[int, dict[tuple[str, int], list[int]]] = {}

        for row_index, row in enumerate(self.rows):
            segment_id = row.get("segment")
            if type(segment_id) is int and segment_id >= 0:
                self.segment_groups[segment_id].append(row_index)

            layers_by_source: dict[tuple[str, int], list[int]] = defaultdict(list)
            for layer_index, selection in enumerate(row.get("selections", [])):
                source_key = observation_key(selection.get("observation"))
                if source_key is not None:
                    layers_by_source[source_key].append(layer_index)

            self.layer_groups[row_index] = {
                source_key: member_indices
                for source_key, member_indices in layers_by_source.items()
                if len(member_indices) > 1
            }

    def shared_segment(self, index: int) -> list[int]:
        """返回与第 `index` 行目标整点共用同一观测片段的所有整点行索引列表。

        Args:
            index: 目标整点在 `self.rows` 中的行索引。

        Returns:
            list[int]: 共用同一有效片段编号的整点行索引列表；未匹配片段时返回 `[]`。
        """

        segment_id = self.rows[index].get("segment")
        return (
            self.segment_groups.get(segment_id, [])
            if type(segment_id) is int and segment_id >= 0
            else []
        )

    def shared_layers(self, index: int) -> set[int]:
        """返回第 `index` 行目标整点中与其他层位共用同一条原始观测记录的层位索引集合。

        Args:
            index: 目标整点在 `self.rows` 中的行索引。

        Returns:
            set[int]: 发生跨层复用的层位序号集合。
        """

        return {
            layer_idx
            for member_group in self.layer_groups.get(index, {}).values()
            for layer_idx in member_group
        }

    def status(self, index: int) -> list[str]:
        """返回第 `index` 行整点的核查状态码列表（将首次使用被复用片段的整点也标记为 `"segment_reused"`）。

        Args:
            index: 目标整点在 `self.rows` 中的行索引。

        Returns:
            list[str]: 合并双向关联推导后的状态码列表副本。
        """

        status_codes = list(self.rows[index].get("status", []))
        if (
            len(self.shared_segment(index)) > 1
            and "segment_reused" not in status_codes
        ):
            status_codes.append("segment_reused")
        if (
            self.layer_groups.get(index)
            and "cross_layer_reused" not in status_codes
        ):
            status_codes.append("cross_layer_reused")
        return status_codes

    def details_html(self, index: int, depth: float | None = None) -> str:
        """为第 `index` 行目标整点生成经过 HTML 转义的“来源详情”富文本说明。

        展示内容包括：
        1. 目标整点时间、匹配片段编号与峰值时间偏移秒数。
        2. 若发生片段复用，列出所有共用该片段的目标整点及该片段的实际起止时间。
        3. 若发生跨层复用，列出共用同一原始记录的层位名称、来源文件路径、物理行号、实测深度及相当于当次水深 H 的比例。
        4. 缺测、缺层、深度超差或未设置深度容差的明确说明。
        5. 逐层列出来源文件路径、原始行号与实际采样时刻。

        Args:
            index: 目标整点在 `self.rows` 中的行索引。
            depth: 当次处理该测点采用的参考水深 H（米），用于换算实测深度对应的相对水深比例。

        Returns:
            str: 可直接交由 `QTextEdit.setHtml()` 展示的安全 HTML 字符串。
        """

        row = self.rows[index]
        html_parts: list[str] = []

        def add_paragraph(text: str, is_problem: bool = False) -> None:
            """对业务文本执行 HTML 转义并包装为 `<p>` 段落（问题提示使用深红警示色 `#B42318`）。"""

            html_parts.append(
                "<p"
                + (' style="color:#B42318"' if is_problem else "")
                + ">"
                + escape(text)
                + "</p>"
            )

        add_paragraph(
            "目标时间："
            + time_text(row.get("target"))
            + "；片段编号："
            + str(row.get("segment") if row.get("segment") is not None else "未匹配")
        )
        offset_seconds = row.get("offset_seconds")
        add_paragraph(
            "峰值偏移："
            + (
                f"{offset_seconds:g} 秒"
                if isinstance(offset_seconds, (float, int))
                else "未记录或不适用"
            )
        )

        status_codes = self.status(index)
        shared_row_indices = self.shared_segment(index)
        if "segment_reused" in status_codes:
            if len(shared_row_indices) > 1:
                shared_times = "、".join(
                    time_text(self.rows[row_idx].get("target"))
                    for row_idx in shared_row_indices
                )
                segments = self.result.get("segments", [])
                segment_id = row.get("segment")
                segment = (
                    segments[segment_id]
                    if type(segment_id) is int and 0 <= segment_id < len(segments)
                    else {}
                )
                seg_start, seg_end = segment.get("start"), segment.get("end")
                span_text = (
                    f"{time_text(seg_start)} — {time_text(seg_end)}"
                    if seg_start is not None and seg_end is not None
                    else "起止时间未记录，无法提供实际片段范围"
                )
                add_paragraph(
                    f"片段复用：{shared_times} 共用了观测片段；实际片段：{span_text}。",
                    True,
                )
            else:
                add_paragraph(
                    "片段复用：旧结果缺少完整关联记录，无法列出全部关联整点及实际片段起止时间。",
                    True,
                )

        selections = row.get("selections", [])
        for _source_key, member_indices in self.layer_groups.get(index, {}).items():
            obs = selections[member_indices[0]]["observation"]
            layer_labels = "、".join(
                str(selections[layer_idx].get("label", "未记录层名"))
                for layer_idx in member_indices
            )
            actual_depth = obs.get("depth")
            ratio_text = "本次水深未记录，无法换算相对深度"
            if (
                isinstance(depth, (int, float))
                and math.isfinite(depth)
                and depth > 0
                and isinstance(actual_depth, (int, float))
                and math.isfinite(actual_depth)
            ):
                ratio_text = (
                    f"相当于本测点水深的 {actual_depth / depth:.3f}H（H＝{depth:.3f} 米）"
                )
            actual_depth_text = (
                f"{actual_depth:.3f} 米"
                if isinstance(actual_depth, (int, float)) and math.isfinite(actual_depth)
                else "未记录"
            )
            add_paragraph(
                f"跨层复用：{layer_labels} 共用了原始文件 {obs['file']} 第 {obs['line']} 行的观测。该观测深度为 {actual_depth_text}，{ratio_text}。",
                True,
            )

        if "cross_layer_reused" in status_codes and not self.layer_groups.get(index):
            add_paragraph(
                "跨层复用：旧结果缺少完整关联层位、文件路径或原始行号，无法可靠还原关联层位。",
                True,
            )

        status_explanations = {
            "missing_segment": "缺测：在设定的时间范围内，未找到可用于该整点的观测片段。",
            "missing_layer": "缺层：片段内缺少可选观测，空值保留为空。",
            "depth_out_of_tolerance": "深度偏差超限：偏差超过本次设置的容差，仍保留原值供核查。",
        }
        for code, explanation in status_explanations.items():
            if code in status_codes:
                add_paragraph(explanation, True)

        if "tolerance_unset" in status_codes:
            add_paragraph("本次未设置深度容差，未检查偏差是否超限。")

        for selection in selections:
            label = str(selection.get("label", "未记录层名"))
            obs = selection.get("observation")
            if not obs:
                add_paragraph(label + "：缺测，未选到观测。")
                continue
            line_no = obs.get("line")
            file_path = obs.get("file")
            add_paragraph(
                f"{label}：{file_path if file_path else '来源文件未记录'}；原始行号：{line_no if line_no is not None else '未记录'}；采样：{time_text(obs.get('timestamp'))}"
            )

        return "".join(html_parts)
