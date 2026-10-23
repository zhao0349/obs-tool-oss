"""使用合成观测数据全面验收 Nuitka 编译版预览程序，并打包第三方许可证与 SHA-256 校验清单。

验收流程：
1. **编译完备性检查（`compiled_modules`）**：解析 `nuitka-report.xml`，确保所有 `obs_tool.*` 自有模块均已编译为原生扩展或包，无遗漏解释态源码；
2. **源码与编译版批处理对拍**：生成合成 `.dat` 文件并调用 `compare_preview.py` 验证 CSV 导出与诊断 JSON 完全一致；
3. **编译版时间校正与 `.bak` 备份验证**：在合成数据上调用 `--correction` 子进程验证只读预览不改原文件、正式保存自动生成字节级一致的 `.bak` 备份；
4. **交付资产归档**：输出 `dist/OBS-Data-Tool-<version>-preview.exe`、第三方开源许可压缩包及 `SHA256SUMS.txt`。
"""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import subprocess
import sys
import xml.etree.ElementTree as ET

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 兼容 Python 3.10 测试导入环境
    tomllib = None  # type: ignore[assignment]

from obs_tool import __version__
from obs_tool.config import save
from obs_tool.models import Batch, Point


def compiled_modules(report_path: Path) -> tuple[list[ET.Element], int]:
    """解析 Nuitka 构建报告 XML，校验所有 `obs_tool` 自有模块均已编译，并返回模块节点列表及剔除的控制字符数。"""
    raw_xml_text = report_path.read_text(encoding="utf-8")
    # 编译器控制台输出可能夹带 XML 1.0 禁用的控制字符（如 ANSI 转义序列 \x1b），仅剔除这些控制字符；
    # 若 XML 标签结构本身损坏仍必须抛出 ET.ParseError，绝不能用宽松解析掩盖构建报告异常。
    cleaned_xml, removed_count = re.subn(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", raw_xml_text
    )
    if removed_count:
        print(
            f"Nuitka report: removed {removed_count} XML-forbidden control characters"
        )
    all_modules = ET.fromstring(cleaned_xml).findall(".//module")
    own_modules = [
        module
        for module in all_modules
        if module.get("name", "").startswith("obs_tool")
    ]
    assert own_modules and all(
        module.get("kind") in ("CompiledPythonModule", "CompiledPythonPackage")
        for module in own_modules
    ), [(module.get("name"), module.get("kind")) for module in own_modules]
    return own_modules, removed_count


def main() -> None:
    """核对版本号、模块编译状态与合成数据运行结果，验收通过后生成 `dist/` 发行资产。"""
    build_dir, report_dir = (Path(arg).resolve() for arg in sys.argv[1:])
    repo_root = Path(__file__).resolve().parents[1]
    if tomllib is None:
        raise RuntimeError("verify_preview.main requires Python 3.11+ tomllib")
    assert (
        tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
        == __version__
    )
    exe_path = build_dir / f"OBS 数据处理工具-{__version__}-preview.exe"
    own_modules, removed_chars = compiled_modules(
        report_dir / "build/nuitka-report.xml"
    )

    sys.path.insert(0, str(repo_root / "tests"))
    from dat_samples import HEADER
    from test_core import records

    synthetic_dir = report_dir / "synthetic"
    synthetic_dir.mkdir(parents=True, exist_ok=True)
    synthetic_points: list[Point] = []
    for code, depth_scale in [("A", 1), ("B", 1.4), ("C", 2)]:
        dat_path = synthetic_dir / f"{code}#.dat"
        dat_path.write_text(
            HEADER
            + "\n".join(
                f"{rec['timestamp']:%H:%M:%S %m/%d/%Y} {rec['depth']*depth_scale} 2 20 1 30 6"
                for rec in records()
            ),
            encoding="utf-8",
        )
        synthetic_points.append(
            Point(
                code,
                5 * depth_scale,
                files=[str(dat_path)],
                start="2025-12-31T23:00:00",
                end="2026-01-01T01:00:00",
            )
        )

    batch_task_path = synthetic_dir / "batch.json"
    save(
        Batch(points=synthetic_points, output_dir=str(synthetic_dir / "output")),
        batch_task_path,
    )
    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts/compare_preview.py"),
            str(batch_task_path),
            str(exe_path),
            str(report_dir),
        ],
        check=True,
        timeout=360,
    )

    # 仅针对上方生成的合成 DAT 文件验证编译版时间校正与 `.bak` 原始备份，绝不触碰真实用户数据
    original_dat_bytes = Path(synthetic_points[0].files[0]).read_bytes()

    def run_compiled_correction(job_name: str, request_payload: dict) -> dict:
        """在独立临时任务目录下调用编译版 `--correction` 子进程并返回结果字典。"""
        job_dir = synthetic_dir / job_name
        job_dir.mkdir()
        (job_dir / "request.json").write_text(
            json.dumps(request_payload), encoding="utf-8"
        )
        subprocess.run(
            [str(exe_path), "--correction", str(job_dir)],
            check=True,
            timeout=120,
        )
        response = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
        assert "error" not in response, response
        return response["data"]

    correction_plan = run_compiled_correction(
        "analyse",
        dict(
            operation="analyse",
            path=synthetic_points[0].files[0],
            parameters=dict(
                mode="known",
                instrument="2025-12-31T23:55:00",
                correct="2026-01-01T00:55:00",
            ),
        ),
    )
    assert (
        correction_plan["valid"]
        and Path(synthetic_points[0].files[0]).read_bytes() == original_dat_bytes
    )
    save_result = run_compiled_correction(
        "save", dict(operation="save", plan=correction_plan)
    )
    assert Path(save_result["backup"]).read_bytes() == original_dat_bytes
    assert Path(synthetic_points[0].files[0]).read_bytes() != original_dat_bytes

    verification_summary = dict(
        version=__version__,
        python=platform.python_version(),
        openssl=ssl.OPENSSL_VERSION,
        compiled_modules=len(own_modules),
        report_control_characters_removed=removed_chars,
        correction_backup=True,
        commit=os.environ.get("GITHUB_SHA"),
        exe_sha256=hashlib.sha256(exe_path.read_bytes()).hexdigest(),
    )
    (report_dir / "verification.json").write_text(
        json.dumps(verification_summary, indent=2), encoding="utf-8"
    )

    dist_dir = repo_root / "dist"
    dist_dir.mkdir(exist_ok=True)
    # GitHub Release 会规整非 ASCII 资产文件名，因此使用稳定的英文下载文件名
    shutil.copy2(exe_path, dist_dir / f"OBS-Data-Tool-{__version__}-preview.exe")
    notices_dir = report_dir / "third-party"
    # 保持仓库中的相对目录，确保图标说明到许可正文的链接在 ZIP 内仍有效。
    shutil.copytree(repo_root / "docs/licenses", notices_dir / "docs/licenses")
    # 自有代码和图标采用不同许可，交付时必须连同范围说明保留。
    shutil.copy2(repo_root / "LICENSE", notices_dir)
    shutil.copy2(repo_root / "ASSET_LICENSE.md", notices_dir)
    shutil.copy2(repo_root / "requirements.lock", notices_dir)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, notices_dir / "docs/licenses/PYTHON-LICENSE.txt")
    with (notices_dir / "docs/licenses/SOURCES.txt").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write(
            f"\nThis build: Python {platform.python_version()}, {ssl.OPENSSL_VERSION}.\n"
        )
        stream.write(
            f"https://github.com/python/cpython/tree/v{platform.python_version()}\n"
        )
        stream.write(
            f"https://github.com/openssl/openssl/tree/openssl-{ssl.OPENSSL_VERSION.split()[1]}\n"
        )
    shutil.copy2(repo_root / "docs/THIRD_PARTY_NOTICES.md", notices_dir / "docs")
    shutil.make_archive(
        str(dist_dir / f"OBS-{__version__}-third-party-notices"),
        "zip",
        notices_dir,
    )
    dist_assets = sorted(dist_dir.iterdir())
    (dist_dir / "SHA256SUMS.txt").write_text(
        "".join(
            f"{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}\n"
            for asset in dist_assets
        ),
        encoding="utf-8",
    )
    print(json.dumps(verification_summary))


if __name__ == "__main__":
    main()
