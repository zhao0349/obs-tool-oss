"""整批数据处理编排引擎：输出目录隔离校验、逐测点错误隔离、CSV 导出与内部追溯记录持久化。

核心设计原则：
1. **原始数据目录隔离保护（`protect_output`）**：
   强制校验 CSV 输出目录不得等于或位于任何原始 DAT 文件所在目录之内，防止导出文件混入原始观测目录；
2. **独占带时间戳运行目录（`create_run_directory`）**：
   每次批处理自动新建形如 `OBS处理结果_YYYY-MM-DD_HHMM` 的独立结果文件夹（同名冲突时递增 `（2）`、`（3）`），
   仅向用户结果目录写入纯净的 `.csv` 表格，而将 `config.json`、`manifest.json`、诊断与曲线上下文存入独立的内部记录目录；
3. **单测点故障隔离与状态收尾**：
   单个测点若因文件损坏或缺少通道而报错，仅将该测点记为 `failed`，其余合格测点继续完成处理与导出；
   `finally` 块确保任何未启动的测点均补齐 `not_started` 状态并原子落盘最终 `manifest.json`。
"""

from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from . import ALGORITHM_VERSION, __version__
from .exporters import atomic_json, export_point
from .models import Batch
from .processing import process
from .readers import read_files


def protect_output(output: str | Path, input_paths: Iterable[str | Path] = ()) -> Path:
    """校验输出目录路径，拒绝将输出目录设置在任何原始输入目录或其子目录内部。

    Args:
        output: 用户指定的 CSV 保存根目录路径。
        input_paths: 本次任务涉及的全部原始数据目录路径集合。

    Returns:
        Path: 解析后的安全输出目录绝对路径。

    Raises:
        ValueError: 当 `output` 等于某个原始数据目录或位于其子目录内时抛出。
    """
    output = Path(output).resolve()
    protected = [Path(p).resolve() for p in input_paths]
    if any(output == p or p in output.parents for p in protected):
        raise ValueError("输出必须位于原始数据目录之外的独立目录")
    return output


def create_run_directory(root: str | Path, now: datetime | None = None) -> Path:
    """在输出根目录下创建不与已有结果或内部追溯记录冲突的独占运行目录（重名时自动附加递增序号）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    name = (now or datetime.now()).strftime("OBS处理结果_%Y-%m-%d_%H%M")
    index = 1
    while True:
        directory = root / (name if index == 1 else f"{name}（{index}）")
        from .record_store import record_directory

        if record_directory(directory, legacy=False).exists():
            index += 1
            continue
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            index += 1


def run(
    batch: Batch,
    progress: Callable[..., None] = lambda *args: None,
    cancelled: Callable[[], bool] = lambda: False,
):
    """执行整批测点的数据读取、整点筛选、CSV 导出与内部运行状态持久化。

    单点隔离与取消保证：
    - 单个测点发生格式或读取异常时，记录 `status="failed"` 与具体错误原因，继续处理后续测点；
    - 用户触发取消（`cancelled() == True`）时立即停止后续测点处理，已导出的测点结果予以保留；
    - `finally` 块始终将未开始处理的测点补齐为 `not_started` 并原子更新 `manifest.json`。

    Args:
        batch: 待执行的批次配置对象（包含测点列表 `points`、筛选参数 `rules` 与输出路径 `output_dir`）。
        progress: 每完成一个测点后触发的进度回调 `(已完成测点数, 总测点数, 最新测点状态字典)`。
        cancelled: 协作式取消检查回调。

    Returns:
        tuple[Path, dict, dict]: `(本次新建的输出目录 Path, 运行报告 report 字典, 成功测点结果字典 {code: PointResult})`。
    """
    batch.validate(require_depth=False)
    from .exporters import preflight_exports

    preflight_exports(batch.points)
    files_by_point = {
        point.code: [Path(f).resolve() for f in point.files] for point in batch.points
    }
    folders = {f.parent for files in files_by_point.values() for f in files}
    root = protect_output(batch.output_dir, folders)
    from .input_policy import LIMITS, Budget, enforce

    enforce(
        sum(len(point.files) for point in batch.points),
        LIMITS.batch_files,
        "整批文件条目数",
    )
    batch_budget = Budget(cancelled=cancelled)
    directory = create_run_directory(root)
    report = dict(
        app_version=__version__,
        algorithm_version=ALGORITHM_VERSION,
        started=datetime.now().isoformat(),
        status="running",
        points=[],
        inputs=[],
    )
    from .record_store import record_directory

    trace = record_directory(directory, legacy=False)
    atomic_json(trace / "config.json", asdict(batch))
    atomic_json(trace / "manifest.json", report)
    results = {}

    try:
        for index, point in enumerate(batch.points):
            if cancelled():
                report["status"] = "cancelled"
                break
            try:
                files = files_by_point[point.code]
                records, parse_diagnostics = read_files(
                    files,
                    cancelled,
                    budget=Budget(cancelled=cancelled, parent=batch_budget),
                )
                batch_budget.check()
                enforce(batch_budget.records, LIMITS.batch_records, "整批观测数")
                enforce(batch_budget.bytes, LIMITS.batch_bytes, "整批输入字节数")

                # 若测点尚未设置参考水深 H，则自动推算并检查是否需要人工确认
                if point.depth is None:
                    from .depth import estimate_depth
                    from .depth_workflow import (
                        DepthConfirmationRequired,
                        requires_confirmation,
                    )

                    info = estimate_depth(records)
                    point = replace(
                        point,
                        depth=info.get("value"),
                        depth_source="estimated",
                        depth_estimate=info,
                    )
                    if requires_confirmation(point):
                        raise DepthConfirmationRequired([point])

                for path in files:
                    report["inputs"].append(
                        dict(point=point.code, path=str(path), size=path.stat().st_size)
                    )

                result = process(records, point, batch.rules, cancelled)
                result["parse_diagnostics"] = parse_diagnostics
                result["parse_counts"] = dict(parse_diagnostics.counts)
                if cancelled():
                    raise InterruptedError("用户取消")

                export_point(directory, result, point, batch, trace_directory=trace)
                from .curve_context import save_context

                try:
                    save_context(
                        trace / (point.code + "-curve.json"),
                        records,
                        point,
                        [
                            entry
                            for entry in report["inputs"]
                            if entry["point"] == point.code
                        ],
                        batch.rules.gap_minutes,
                        cancelled,
                    )
                except (OSError, ValueError) as exc:
                    report.setdefault("curve_context_errors", {})[point.code] = str(exc)

                results[point.code] = result
                report["points"].append(
                    dict(
                        point=point.code,
                        status="completed",
                        layer_count=point.layer_count,
                        layer_mode=result["layer_mode"],
                        warnings=sum(bool(row["status"]) for row in result["rows"]),
                        parse_events=len(parse_diagnostics),
                    )
                )
            except InterruptedError:
                report["status"] = "cancelled"
                report["points"].append(dict(point=point.code, status="cancelled"))
                break
            except Exception as exc:
                report["points"].append(
                    dict(point=point.code, status="failed", error=str(exc))
                )
            atomic_json(trace / "manifest.json", report)
            progress(index + 1, len(batch.points), report["points"][-1])

        if report["status"] == "running":
            report["status"] = (
                "completed_with_errors"
                if any(p["status"] == "failed" for p in report["points"])
                else "completed"
            )
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        completed = {p["point"] for p in report["points"]}
        report["points"].extend(
            dict(point=p.code, status="not_started")
            for p in batch.points
            if p.code not in completed
        )
        report["ended"] = datetime.now().isoformat()
        atomic_json(trace / "manifest.json", report)

    return directory, report, results
