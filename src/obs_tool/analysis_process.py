"""水深自动估算后台子进程入口模块。

设计说明：
1. 当用户在界面点击“自动识别水深”或导入新测点文件时，通过独立子进程执行全序列水深分位数估算；
2. 逐测点隔离输入文件校验错误：单个测点的文件缺失或格式错误仅记录在该测点的 `reason` 中，不中断其余测点的水深识别；
3. 全程共享批次级 `Budget` 与 `cancel` 文件检查点，支持随时取消。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .exporters import atomic_json
from .input_policy import LIMITS, Budget, bounded_json, check_files, enforce


def analyse_main(job_directory: str | Path) -> int:
    """读取水深分析请求 JSON，逐测点隔离输入异常并发布实时进度与水深估算结果。

    Args:
        job_directory: 水深分析任务临时目录路径（内含 `request.json`）。

    Returns:
        进程退出码（0 表示正常完成或协作取消）。

    Raises:
        ValueError: 当 `request.json` 顶层结构或文件总数超出安全上限时抛出。
    """
    job_dir = Path(job_directory)
    request_payload = bounded_json(job_dir / "request.json")
    estimates_by_code: dict[str, dict[str, Any]] = {}

    if not isinstance(request_payload, dict) or not isinstance(
        request_payload.get("points"), list
    ):
        raise ValueError("水深分析任务结构无效")

    point_requests = request_payload["points"]
    enforce(len(point_requests), LIMITS.batch_files, "分析测点数")
    enforce(
        sum(len(point_item["files"]) for point_item in point_requests),
        LIMITS.batch_files,
        "分析文件条目数",
    )

    # 第一阶段：预检各测点的输入文件，将单点文件错误隔离到 input_errors_by_code，
    # 同时汇总所有合法文件以校验整批总文件数与总字节数上限。
    valid_file_paths: list[Path] = []
    input_errors_by_code: dict[str, str] = {}
    for point_item in point_requests:
        try:
            valid_file_paths.extend(check_files(point_item["files"]))
        except (OSError, ValueError) as exc:
            input_errors_by_code[point_item["code"]] = str(exc)

    if valid_file_paths:
        check_files(
            valid_file_paths,
            max_files=LIMITS.batch_files,
            max_bytes=LIMITS.batch_bytes,
        )

    batch_budget = Budget(cancelled=lambda: (job_dir / "cancel").exists())

    # 第二阶段：逐测点执行水深估算并发布进度快照
    for point_index, point_item in enumerate(point_requests):
        if (job_dir / "cancel").exists():
            break
        point_code = point_item["code"]
        try:
            from .depth_workflow import analyse_files

            if point_code in input_errors_by_code:
                raise ValueError(input_errors_by_code[point_code])
            estimates_by_code[point_code] = analyse_files(
                point_item["files"],
                lambda: (job_dir / "cancel").exists(),
                Budget(cancelled=batch_budget.cancelled, parent=batch_budget),
            )
        except InterruptedError:
            break
        except (OSError, ValueError) as exc:
            estimates_by_code[point_code] = dict(
                value=None, reason=str(exc), input_error=True
            )

        atomic_json(
            job_dir / f"progress-{point_index + 1:06d}.json",
            dict(
                index=point_index + 1,
                total=len(point_requests),
                point=dict(point=point_code, status="completed"),
            ),
        )

    atomic_json(
        job_dir / "result.json",
        dict(estimates=estimates_by_code, cancelled=(job_dir / "cancel").exists()),
    )
    return 0
