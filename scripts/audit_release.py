"""发行产物合规审计脚本：确保构建输出目录或单文件 EXE 不夹带原始测验数据、Python/C++ 源码或调试符号。

审计规则：
1. 禁止包含 `.dat`、`.xls`、`.xlsx` 等测验数据文件；
2. 禁止包含 `.py`、`.pyc`、`.c`、`.cpp`、`.obj`、`.pdb` 等源代码、字节码或调试符号文件；
3. 禁止包含 `.venv*`、`__pycache__`、`validation` 和 `results` 等环境、验证或结果目录；
4. 若审计目标为单文件 `.exe`，额外校验 Windows PE 头（`MZ`）并计算文件字节数与 SHA-256 摘要。
"""

import hashlib
import json
from pathlib import Path
import sys

release_root = Path(sys.argv[1]).resolve()
if not release_root.exists():
    raise SystemExit("发行目录不存在")

forbidden_entries: list[str] = []
candidate_files = (
    sorted(p for p in release_root.rglob("*") if p.is_file())
    if release_root.is_dir()
    else [release_root]
)

for file_path in candidate_files:
    relative_path = (
        file_path.relative_to(release_root)
        if release_root.is_dir()
        else Path(file_path.name)
    )
    if file_path.is_file() and (
        file_path.suffix.lower()
        in (
            ".dat",
            ".xls",
            ".xlsx",
            ".pdb",
            ".py",
            ".pyc",
            ".c",
            ".cpp",
            ".obj",
        )
        or any(
            part.casefold().startswith(".venv")
            or part.casefold() in ("__pycache__", "validation", "results")
            for part in relative_path.parts
        )
    ):
        forbidden_entries.append(str(relative_path))

audit_report = dict(
    directory=str(release_root),
    files=len(candidate_files),
    forbidden=forbidden_entries,
    passed=not forbidden_entries,
)

if release_root.is_file():
    raw_bytes = release_root.read_bytes()
    audit_report.update(
        bytes=release_root.stat().st_size,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        pe_header=raw_bytes[:2] == b"MZ",
    )
    audit_report["passed"] = (
        audit_report["passed"] and audit_report["pe_header"]
    )

report_output_path = (
    Path(sys.argv[2])
    if len(sys.argv) > 2
    else Path("validation/release-audit.json")
)
report_output_path.write_text(
    json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(audit_report)
raise SystemExit(0 if audit_report["passed"] else 1)
