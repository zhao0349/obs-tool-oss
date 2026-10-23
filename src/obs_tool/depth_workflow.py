"""测点水深后台预分析、可信度降级与人工确认工作流模块（纯业务层，不依赖 Qt 界面组件）。

本模块连接底层的 `depth.estimate_depth` 与主界面的水深确认弹窗流程：
1. `analyse_files`：读取单个测点关联的全部 DAT 文件并估算水深；若解析时发现同一秒存在不同观测值冲突
   （`timestamp_conflict`），即使剖面本身稳定，也将可信度强制设为 `'需核对'`；
2. `requires_confirmation`：判断测点是否必须在正式处理前由用户填写或勾选确认水深；
   若测点文件本身读取失败或格式损坏（`input_error=True`），返回 `False`，交由正式处理阶段按测点独立报错；
3. `prepare_depths`：在直接调用 `runner` 或脚本处理时，批量为未设置水深的测点推算水深，
   并对仍需确认的测点抛出 `DepthConfirmationRequired` 异常。
"""

from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from .data_types import DepthEstimate
from .depth import estimate_depth
from .input_policy import Budget
from .models import Batch, Point
from .readers import read_files


def analyse_files(
    files: Iterable[str | Path],
    cancelled: Callable[[], bool] = lambda: False,
    budget: Budget | None = None,
) -> DepthEstimate:
    """解析单个测点的完整输入 DAT 文件集合并估算参考水深，将同秒时间戳冲突并入核对依据。

    Args:
        files: 测点关联的 DAT 文件路径可迭代对象。
        cancelled: 协作式取消检查回调。
        budget: 可选的资源预算追踪器。

    Returns:
        DepthEstimate: 包含建议水深、剖面峰值明细、解析诊断条数及同秒冲突数的估算结果字典。
    """
    records, diagnostics = read_files(files, cancelled, budget=budget)
    result = estimate_depth(records)
    result["parse_events"] = len(diagnostics)
    conflicts = diagnostics.counts["timestamp_conflict"]
    if conflicts:
        result.update(confidence="需核对", timestamp_conflicts=conflicts)
    return result


def requires_confirmation(point: Point) -> bool:
    """判断指定测点的参考水深是否缺失或可信度不足，从而需要在处理前弹窗由人工核实。

    判定逻辑：
    - 若 `depth_estimate["input_error"]` 为真（表示文件缺失、无权限或表头损坏），返回 `False`，
      避免让用户为损坏的文件虚填水深，而是交由逐测点批处理报告直接记录失败原因；
    - 若 `point.depth is None`（无法自动推算或尚未推算），返回 `True`；
    - 若水深为自动推算（`depth_source == "estimated"`）、可信度为 `'需核对'` 且尚未勾选确认（`not confirmed`），返回 `True`。
    """
    if (point.depth_estimate or {}).get("input_error"):
        return False  # 交给逐点处理报告结构失败，不要求用户为坏文件虚填水深。
    return point.depth is None or (
        point.depth_source == "estimated"
        and (point.depth_estimate or {}).get("confidence") == "需核对"
        and not (point.depth_estimate or {}).get("confirmed")
    )


class DepthConfirmationRequired(ValueError):
    """当批次中存在尚未填写或尚未人工确认的建议水深时抛出的异常，携带待确认测点供 GUI 弹窗展示。"""

    def __init__(self, points: list[Point]) -> None:
        """保存待确认的测点列表 `self.points`，并构造包含全部待确认测点编号的中文提示消息。"""
        self.points = points
        super().__init__(
            "以下测点的水深需要填写或确认："
            + "、".join(point.code for point in points)
            + "。请填写有效水深后再处理；自动估算不能代替确认。"
        )


def prepare_depths(
    batch: Batch,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[..., None] = lambda *args: None,
) -> Batch:
    """仅对批次中 `depth is None` 的测点自动估算水深并返回更新后的 `Batch` 副本；若有测点仍需确认则抛出异常。

    Args:
        batch: 待准备水深的批次配置对象。
        cancelled: 协作式取消检查回调。
        progress: 进度通知回调 `(已完成数, 总测点数, 状态字典)`。

    Returns:
        Batch: 所有测点水深均已完备且无需人工确认的批次副本。

    Raises:
        InterruptedError: 当用户中途取消水深估算时抛出。
        DepthConfirmationRequired: 当存在无法推算或可信度为 `'需核对'` 的测点时抛出。
    """
    points: list[Point] = []
    for index, point in enumerate(batch.points):
        if point.depth is None:
            if cancelled():
                raise InterruptedError("水深估算已取消，处理未完成")
            files = point.files
            try:
                info = analyse_files(files, cancelled)
            except InterruptedError:
                raise
            except (OSError, ValueError) as exc:
                info = dict(value=None, reason=str(exc))
            point = replace(
                point,
                depth=info.get("value"),
                depth_source=(
                    "estimated" if info.get("value") is not None else "unavailable"
                ),
                depth_estimate=info,
            )
            progress(
                index + 1,
                len(batch.points),
                dict(point=point.code, status="estimating_depth"),
            )
        points.append(point)

    issues = [point for point in points if requires_confirmation(point)]
    if issues:
        raise DepthConfirmationRequired(issues)
    return replace(batch, points=points)
