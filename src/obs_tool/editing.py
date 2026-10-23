"""工作区测点列表的原子批量编辑纯逻辑模块（不依赖 PyQt5）。

设计目的：
    将“多选测点后批量修改参考水深、开始时间、结束时间或传感器量程”的校验与状态转换规则抽离为纯函数，
    确保：
    1. 任何一行候选修改校验失败时，原测点列表保持丝毫未改（原子性）；
    2. 当人工批量修改或清空水深时，自动同步更新 `depth_source`（`manual` / `pending`）并清除旧的自动估算证据（`depth_estimate = None`），防止将人工输入误标为自动估算。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from .models import Point, validate_codes


def apply_bulk(
    points: list[Point], rows: Iterable[int], changes: Mapping[str, object]
) -> list[Point]:
    """对选中行应用候选字段修改并执行全表校验，全部通过后返回新测点列表。

    Args:
        points: 当前工作区的完整测点配置列表。
        rows: 需要批量修改的测点行索引（0-based）集合。
        changes: 待应用的字段字典（如 `{"depth": 12.5}` 或 `{"start": "...", "end": "..."}`）。

    Returns:
        校验通过后的新 `Point` 列表副本。

    Raises:
        ValueError: 若任一修改后的测点字段非法或测点代号冲突，抛出异常且原列表不受影响。
    """
    selected_row_indices = set(rows)
    updated_points: list[Point] = []

    for row_index, original_point in enumerate(points):
        if row_index not in selected_row_indices:
            updated_points.append(original_point)
            continue

        patched_fields = dict(changes)
        if "depth" in patched_fields:
            # 人工修改或清空参考水深时，必须重置水深来源并清除历史自动估算快照
            patched_fields["depth_source"] = (
                "manual" if patched_fields["depth"] is not None else "pending"
            )
            patched_fields["depth_estimate"] = None

        candidate_point = replace(original_point, **patched_fields)
        candidate_point.validate(require_depth=False)
        updated_points.append(candidate_point)

    validate_codes(point.code for point in updated_points)
    return updated_points
