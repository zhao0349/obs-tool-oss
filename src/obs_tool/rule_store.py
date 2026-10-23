"""用户自定义分层规则库的有界读取、名称冲突检测、自动备份与原子持久化模块。

业务约束：
1. 自定义规则库以 `schema_version=1` 的 JSON 文件（`layer_rules.json`）独立保存在用户本地数据目录。
2. 规则名称比较时先执行 NFKC 规范化、去除空白并转为 `casefold()` 小写，防止全半角、空格或大小写近似导致的混淆。
3. 严禁自定义规则冒用 `"系统规则"`、`"默认规则"`、`"根据水深自动选择"` 等内置保留名称。
4. 每次保存修改或删除规则前，若原规则库文件已存在，会先在同目录生成带微秒时间戳的 `.backup-*` 完整副本，再执行原子替换。
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import unicodedata

from .data_types import LayerRule
from .exporters import atomic_json
from .input_policy import bounded_json, enforce
from .models import validate_positions


# 规则库文件最大允许字节数（1 MiB）与最大规则条数（256 条）
MAX_RULE_STORE_BYTES = 1048576
MAX_SAVED_RULES = 256


def store_path() -> Path:
    """返回当前用户的自定义分层规则库文件路径（支持通过环境变量 `OBS_SETTINGS_DIR` 隔离测试环境）。

    Returns:
        Path: `layer_rules.json` 的目标路径。
    """

    settings_dir = Path(
        os.environ.get("OBS_SETTINGS_DIR")
        or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "OBS工具"
    )
    return settings_dir / "layer_rules.json"


def load_rules(path: str | Path | None = None) -> list[LayerRule]:
    """有界读取并校验自定义分层规则库；当文件尚不存在时返回空列表 `[]`。

    Args:
        path: 可选的规则库文件路径覆盖；为 `None` 时使用 `store_path()`。

    Returns:
        list[LayerRule]: 通过层位比例校验的自定义规则字典列表。

    Raises:
        CapacityError: 当规则库文件体积超过 1 MiB 或规则条数超过 256 条时抛出。
        ValueError: 当 JSON 版本号、结构、规则名称或层位比例列表不合法时抛出。
    """

    target_path = Path(path) if path else store_path()
    if not target_path.exists():
        return []

    raw_data = bounded_json(target_path, max_bytes=MAX_RULE_STORE_BYTES)
    if not isinstance(raw_data, dict) or raw_data.get("schema_version") != 1:
        raise ValueError("自定义规则文件版本不支持")

    rules = raw_data.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("规则库结构无效")

    enforce(len(rules), MAX_SAVED_RULES, "保存的规则数")
    for rule in rules:
        if not isinstance(rule.get("name"), str) or not rule["name"].strip():
            raise ValueError("规则名称无效")
        validate_positions(rule["positions"])

    return rules


def normalized_name(name: str) -> str:
    """对规则名称执行 Unicode NFKC 规范化、移除所有空白字符并转为不区分大小写的形式。

    Args:
        name: 原始规则名称字符串。

    Returns:
        str: 规范化后的比较键字符串。
    """

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()


def validate_name(name: str, rules: list[LayerRule]) -> str:
    """校验候选规则名称是否非空、未占用系统保留名且与已有规则列表无重名冲突。

    Args:
        name: 待校验的新规则名称。
        rules: 当前已存在的自定义规则列表。

    Returns:
        str: 去除首尾空白后的合法规则名称。

    Raises:
        ValueError: 当名称为空、命中系统保留名称或与 `rules` 中已有规则重名时抛出。
    """

    cleaned_name = name.strip()
    norm_key = normalized_name(cleaned_name)
    if not norm_key:
        raise ValueError("请填写规则名称")

    if (
        re.fullmatch(r"默认规则(?:\(.*层\))?", norm_key)
        or norm_key.startswith("按行业规范")
        or norm_key.startswith("系统规则")
        or norm_key in ("根据水深自动选择", "恢复按水深自动选择")
    ):
        raise ValueError("这个名称留给默认规则，请换一个名称")

    if any(normalized_name(existing["name"]) == norm_key for existing in rules):
        raise ValueError("已有同名规则，请换一个名称")

    return cleaned_name


def next_name(rules: list[LayerRule]) -> str:
    """生成下一个未被占用的默认自定义规则名称（如 `"自定义规则1"`、`"自定义规则2"`）。

    Args:
        rules: 当前已保存的自定义规则列表。

    Returns:
        str: 首个未发生名称冲突的 `"自定义规则<序号>"` 字符串。
    """

    index = 1
    while any(
        normalized_name(rule["name"]) == normalized_name(f"自定义规则{index}")
        for rule in rules
    ):
        index += 1
    return f"自定义规则{index}"


def name_conflicts(rules: list[LayerRule]) -> list[tuple[int, str]]:
    """按顺序扫描规则列表中的名称冲突或保留名违规，供旧版规则名称迁移界面展示。

    Args:
        rules: 待检查的自定义规则列表。

    Returns:
        list[tuple[int, str]]: 冲突项列表，每项为 `(规则索引, 冲突原因说明)`。
    """

    conflicts: list[tuple[int, str]] = []
    accepted_so_far: list[LayerRule] = []
    for index, rule in enumerate(rules):
        try:
            validate_name(rule["name"], accepted_so_far)
        except ValueError as exc:
            conflicts.append((index, str(exc)))
        accepted_so_far.append(rule)
    return conflicts


def save_rules(rules: list[LayerRule], path: str | Path | None = None) -> None:
    """全量校验规则列表后，先备份已有规则库文件，再原子写入新规则库。

    Args:
        rules: 待持久化的完整自定义规则列表。
        path: 可选的规则库路径覆盖；为 `None` 时使用 `store_path()`。

    Raises:
        ValueError: 当任意规则的层位比例或名称不合法时抛出（此时不会修改磁盘文件）。
    """

    validated_so_far: list[LayerRule] = []
    for rule in rules:
        validate_positions(rule["positions"])
        validate_name(rule["name"], validated_so_far)
        validated_so_far.append(rule)

    target_path = Path(path) if path else store_path()
    # 在原子替换前先复制一份带微秒时间戳的逐字节恢复备份
    if target_path.exists():
        backup_path = target_path.with_name(
            target_path.name
            + ".backup-"
            + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        shutil.copy2(target_path, backup_path)

    atomic_json(target_path, dict(schema_version=1, rules=rules))
