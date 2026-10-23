"""批次任务配置 JSON 的有界读取、相对路径解析与原子保存模块。

本模块用于在主进程与后台 Worker 进程之间持久化传递 `Batch` 配置快照：
1. 读取时通过 `bounded_json` 限制 JSON 体积、嵌套深度与节点数，并将相对于配置文件所在目录的路径统一解析为绝对路径。
2. 保存时使用 `atomic_json` 原子写入，且仅保存配置元数据，绝不读取或修改任何原始 `.dat` 文件。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .input_policy import LIMITS, bounded_json, enforce
from .models import Batch, Point, Rules


def load(path: str | Path) -> Batch:
    """有界读取并校验批次配置 JSON，还原为 `Batch` 实例（允许水深暂为空以支持准备阶段编辑）。

    兼容说明：
    - 若旧版配置包含 `"legacy_export"` 键，会自动映射为 `complete_rows_only`。
    - 若测点 `depth` 为 `None` 且未记录 `depth_source`，默认补为 `"pending"`（待处理时自动估算）。
    - `input_dir`、`output_dir` 与各测点 `files` 中的相对路径均以配置文件所在父目录 `path.parent` 为基准解析。

    Args:
        path: 批次 JSON 配置文件路径。

    Returns:
        Batch: 通过基础结构校验（`require_depth=False`）的批次对象。

    Raises:
        CapacityError: 当配置文件体积或测点/文件条目数超出预算上限时抛出。
        ValueError: 当 JSON 结构、版本号或测点配置无效时抛出。
    """

    config_path = Path(path).resolve()
    raw_data = bounded_json(config_path)
    if not isinstance(raw_data, dict) or not isinstance(raw_data.get("points"), list):
        raise ValueError("任务记录结构无效")

    enforce(len(raw_data["points"]), LIMITS.batch_files, "任务测点数")

    # 兼容早期版本字段名 legacy_export -> complete_rows_only
    if "legacy_export" in raw_data:
        raw_data["complete_rows_only"] = raw_data.pop("legacy_export")

    if raw_data.get("schema_version") != 1:
        raise ValueError("未知批次版本，拒绝读取")

    for point_dict in raw_data["points"]:
        if not isinstance(point_dict, dict) or not isinstance(
            point_dict.get("files"), list
        ):
            raise ValueError("任务必须明确列出每个测点的输入文件")
        enforce(len(point_dict["files"]), LIMITS.point_files, "测点文件条目数")
        if any(
            not isinstance(file_path, str) or len(file_path) > 32760
            for file_path in point_dict["files"]
        ):
            raise ValueError("输入文件路径格式无效")
        if point_dict.get("depth") is None and "depth_source" not in point_dict:
            point_dict["depth_source"] = "pending"

    raw_data["points"] = [Point(**point_dict) for point_dict in raw_data["points"]]
    raw_data["rules"] = Rules(**raw_data.get("rules", {}))
    batch = Batch(**raw_data)

    # 将相对路径统一按配置文件所在目录解析为绝对路径
    for dir_attr in ("input_dir", "output_dir"):
        if getattr(batch, dir_attr):
            setattr(
                batch,
                dir_attr,
                str((config_path.parent / getattr(batch, dir_attr)).resolve()),
            )
    for point in batch.points:
        point.files = [
            str((config_path.parent / file_path).resolve())
            for file_path in point.files
        ]

    batch.validate(require_depth=False)
    return batch


def save(batch: Batch, path: str | Path) -> None:
    """校验批次配置后原子保存为 `.json` 文件（允许水深暂未填写，不触碰原始 DAT 文件）。

    Args:
        batch: 待保存的批次配置对象。
        path: 目标 `.json` 文件路径。

    Raises:
        ValueError: 当批次配置无效或目标文件扩展名不是 `.json` 时抛出。
    """

    batch.validate(require_depth=False)
    target_path = Path(path).resolve()
    if target_path.suffix.lower() != ".json":
        raise ValueError("批次须保存为独立目录内的JSON文件")

    from .exporters import atomic_json

    atomic_json(target_path, asdict(batch))
