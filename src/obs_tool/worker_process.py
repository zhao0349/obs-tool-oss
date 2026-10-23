"""主批处理后台子进程入口模块。

通信协议（基于任务临时目录的文件 IPC，不依赖标准输入输出管道）：
1. 主进程在临时任务目录 `job_directory` 下写入 `task.json`（序列化的 `Batch` 配置）；
2. 子进程启动后读取 `task.json` 并调用 `runner.run()` 执行批处理；
3. 每完成一个测点，子进程按递增序号原子写入 `progress-000001.json`，供主进程定时器轮询；
4. 若主进程在任务目录下创建 `cancel` 标记文件，子进程在安全检查点协作中止；
5. 任务结束时原子写入 `result.json`（成功或正常取消）或 `error.json`（未捕获异常）。
"""

from __future__ import annotations

from pathlib import Path
import traceback
from typing import Any

from .config import load
from .exporters import atomic_json
from .runner import run


def main(job_directory: str | Path) -> int:
    """读取任务目录中的批次配置并执行批处理，将最终结果或异常快照发布回任务目录。

    Args:
        job_directory: 主进程分配的本次批处理临时任务目录路径。

    Returns:
        进程退出码（0 表示成功或受控取消，1 表示发生未捕获异常）。
    """
    job_dir = Path(job_directory)
    try:
        batch_config = load(job_dir / "task.json")

        def _report_progress(
            completed_index: int, total_points: int, point_state: dict[str, Any]
        ) -> None:
            """按递增序号原子写入单步进度 JSON，供 GUI 主进程轮询更新进度条。"""
            atomic_json(
                job_dir / f"progress-{completed_index:06d}.json",
                dict(index=completed_index, total=total_points, point=point_state),
            )

        output_directory, run_report, point_results = run(
            batch_config,
            _report_progress,
            lambda: (job_dir / "cancel").exists(),
        )
        atomic_json(
            job_dir / "result.json",
            dict(
                directory=str(output_directory),
                report=run_report,
                results=point_results,
            ),
        )
        return 0
    except Exception as exc:
        atomic_json(
            job_dir / "error.json",
            dict(error=str(exc), traceback=traceback.format_exc()),
        )
        return 1
