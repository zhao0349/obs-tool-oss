"""源码运行态与 Nuitka 编译版 EXE 的批处理结果一致性对拍验证脚本。

对拍流程：
1. 读取给定的 `<批次JSON>`，在独立的验证输出目录下分别使用 Python 源码（`obs_tool.runner.run`）
   与编译后的 `<EXE路径> --worker` 执行同一批测点处理；
2. 逐测点比对源码版与编译版生成的 `<测点>#_output.csv`（字节级完全一致）和 `<测点>-diagnostics.json`（JSON 结构完全一致）；
3. 将对拍结论与可执行文件 SHA-256 写入 `<验证输出目录>/compiled-comparison.json`，存在任何差异时以断言失败退出。
"""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from obs_tool.config import load, save
from obs_tool.depth_workflow import requires_confirmation
from obs_tool.record_store import record_directory
from obs_tool.runner import run

repo_root = Path(__file__).resolve().parents[1]
if len(sys.argv) != 4:
    raise SystemExit(
        "用法：compare_preview.py <批次JSON> <EXE路径> <验证输出目录>"
    )

input_batch_file = Path(sys.argv[1]).resolve()
batch = load(input_batch_file)

# 对拍验证前自动将需要人工确认的水深标记为已确认，使无人值守批处理可直接运行
batch = replace(
    batch,
    points=[
        (
            replace(
                point,
                depth_estimate={**point.depth_estimate, "confirmed": True},
            )
            if requires_confirmation(point)
            else point
        )
        for point in batch.points
    ],
)

report_dir = Path(sys.argv[3]).resolve()
report_dir.mkdir(parents=True, exist_ok=True)
check_dir = report_dir / ("compiled-check-" + uuid4().hex[:8])
batch = replace(batch, output_dir=str(check_dir / "runs"))
task_file = check_dir / "task.json"
save(batch, task_file)

# 1. 运行 Python 源码批处理
source_run_dir, source_report, _ = run(batch)
existing_run_dirs = {
    path for path in Path(batch.output_dir).iterdir() if path.is_dir()
}

# 2. 运行编译版 EXE 批处理子进程
exe_path = Path(sys.argv[2]).resolve()
worker_job_dir = check_dir / "worker"
worker_job_dir.mkdir()
save(batch, worker_job_dir / "task.json")
completed_proc = subprocess.run(
    [str(exe_path), "--worker", str(worker_job_dir)],
    cwd=repo_root,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=300,
)

new_run_dirs = {
    path for path in Path(batch.output_dir).iterdir() if path.is_dir()
} - existing_run_dirs
assert len(new_run_dirs) == 1, (
    completed_proc.returncode,
    completed_proc.stdout,
    completed_proc.stderr,
    new_run_dirs,
)
compiled_run_dir = new_run_dirs.pop()

# 3. 逐测点比对 CSV 导出文件与内部诊断 JSON
differences: list[str] = []
for point in batch.points:
    for suffix in ("#_output.csv", "-diagnostics.json"):
        source_file = (
            record_directory(source_run_dir)
            if suffix.endswith(".json")
            else source_run_dir
        ) / (point.code + suffix)
        compiled_file = (
            record_directory(compiled_run_dir)
            if suffix.endswith(".json")
            else compiled_run_dir
        ) / (point.code + suffix)
        if suffix.endswith(".json"):
            is_equal = json.loads(
                source_file.read_text(encoding="utf-8")
            ) == json.loads(compiled_file.read_text(encoding="utf-8"))
        else:
            is_equal = source_file.read_bytes() == compiled_file.read_bytes()
        if not is_equal:
            differences.append(point.code + suffix)

compiled_manifest = json.loads(
    (record_directory(compiled_run_dir) / "manifest.json").read_text(
        encoding="utf-8"
    )
)
comparison_result = dict(
    source=str(source_run_dir),
    compiled=str(compiled_run_dir),
    source_status=source_report["status"],
    compiled_status=compiled_manifest["status"],
    points=len(batch.points),
    files_compared=len(batch.points) * 2,
    differences=differences,
    passed=not differences and compiled_manifest["status"] == "completed",
    executable_sha256=hashlib.sha256(exe_path.read_bytes()).hexdigest(),
)
(report_dir / "compiled-comparison.json").write_text(
    json.dumps(comparison_result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print(json.dumps(comparison_result, ensure_ascii=False))
assert comparison_result["passed"]
