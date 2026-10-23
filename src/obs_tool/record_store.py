"""内部运行记录与追溯数据目录定位模块。

设计目的：
    为保证用户选择的 CSV 输出文件夹干净整洁（仅存放 `*_output.csv` 最终成果文件），
    运行报告（`run-report.json`）、单点诊断（`*-diagnostics.json`）与曲线快照（`*-curve.json`）
    均集中存放在由程序管理的本地数据目录中，并以输出目录绝对路径的 SHA-256 摘要作为子目录名。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def record_directory(output: str | Path, legacy: bool = True) -> Path:
    """根据 CSV 输出目录定位对应的程序内部运行记录目录。

    定位优先级：
    1. 若 `legacy=True` 且输出目录下已存在旧版 `"追溯记录"` 子文件夹，则直接复用该子文件夹以兼容历史结果。
    2. 否则按环境变量 `OBS_RECORDS_DIR` > `OBS_JOB_DIR/run-records` > `LOCALAPPDATA/OBS工具/运行记录` 确定根目录，
       并在其下以输出目录 `casefold()` 后的 UTF-8 路径 SHA-256 哈希值创建唯一子目录。

    Args:
        output: 本次运行的 CSV 输出目录路径。
        legacy: 是否兼容检测旧版输出目录内的 `"追溯记录"` 子目录，默认 `True`。

    Returns:
        Path: 该输出目录对应的内部运行记录绝对路径。
    """

    output_path = Path(output).resolve()
    if legacy and (output_path / "追溯记录").is_dir():
        return output_path / "追溯记录"

    records_base = Path(
        os.environ.get("OBS_RECORDS_DIR")
        or (
            Path(os.environ["OBS_JOB_DIR"]) / "run-records"
            if os.environ.get("OBS_JOB_DIR")
            else Path(
                os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))
            )
            / "OBS工具"
            / "运行记录"
        )
    )
    directory_hash = hashlib.sha256(
        str(output_path).casefold().encode("utf-8")
    ).hexdigest()
    return records_base.resolve() / directory_hash
