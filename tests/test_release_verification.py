"""验证 Nuitka 编译报告与发行目录的生成物、数据排除规则。"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location(
    "verify_preview",
    Path(__file__).resolve().parents[1] / "scripts/verify_preview.py",
)
assert spec and spec.loader
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class ReportTests(unittest.TestCase):
    """验证 Nuitka XML 报告中的控制字符清洗、全模块 C 编译校验及损坏 XML 拦截。"""

    def check_report(self, xml_content: str) -> tuple[list[ET.Element], int]:
        """将给定 XML 字符串写入临时 `report.xml` 并调用 `compiled_modules` 解析。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.xml"
            path.write_text(xml_content, encoding="utf-8")
            return verification.compiled_modules(path)

    def test_console_controls_do_not_hide_compiled_packages(self) -> None:
        """验证报告中若夹带 XML 1.0 禁用的控制字符（如 `\\x1b`），清洗后仍能正确识别已编译模块并返回移除计数。"""
        modules, removed = self.check_report(
            '<report><module name="obs_tool" kind="CompiledPythonPackage"/><module name="obs_tool.gui" kind="CompiledPythonModule"/><console>\x1b[0m</console></report>'
        )
        self.assertEqual((len(modules), removed), (2, 1))

    def test_uncompiled_or_missing_modules_are_rejected(self) -> None:
        """验证当缺失 `obs_tool` 模块或存在未编译为 C 的 `UncompiledPythonModule` 时立即抛出断言失败。"""
        for content in (
            "",
            '<module name="obs_tool.gui" kind="UncompiledPythonModule"/>',
        ):
            with self.subTest(content=content), self.assertRaises(AssertionError):
                self.check_report("<report>" + content + "</report>")

    def test_malformed_markup_is_not_repaired(self) -> None:
        """验证结构损坏的 XML 标签不会被控制字符清洗逻辑掩盖，仍正常抛出 `ET.ParseError`。"""
        with self.assertRaises(ET.ParseError):
            self.check_report(
                '<report><module name="obs_tool" kind="CompiledPythonPackage"></report>'
            )


class PayloadAuditTests(unittest.TestCase):
    """使用微型合成目录验证发行审计，不构建 EXE 或读取实际观测。"""

    def check_payload(self, relative_path: str, expected_pass: bool) -> None:
        """检查指定目录项的退出码、报告结论及拒绝清单。"""
        script = Path(__file__).resolve().parents[1] / "scripts/audit_release.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            entry = payload / relative_path
            entry.parent.mkdir(parents=True)
            entry.write_bytes(b"synthetic fixture")
            report_path = root / "report.json"
            result = subprocess.run(
                [sys.executable, "-B", str(script), str(payload), str(report_path)],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0 if expected_pass else 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["passed"], expected_pass)
            self.assertEqual(report["files"], 1)
            self.assertEqual(
                report["forbidden"],
                [] if expected_pass else [str(Path(relative_path))],
            )

    def test_data_rejected_independently_of_directory_name(self) -> None:
        """数据文件按类型拦截，不依赖某台电脑的目录名称。"""
        for suffix in (".dat", ".DAT", ".xls", ".xlsx"):
            with self.subTest(suffix=suffix):
                self.check_payload(f"arbitrary-folder/sample{suffix}", False)

    def test_development_directories_rejected(self) -> None:
        """环境和验证目录即使只含普通资源，也不进入发行载荷。"""
        for folder in (
            ".venv",
            ".venv-build",
            ".VENV",
            "__pycache__",
            "validation",
            "RESULTS",
        ):
            with self.subTest(folder=folder):
                self.check_payload(f"{folder}/sample.txt", False)

    def test_runtime_resources_remain_allowed(self) -> None:
        """必要运行资源不因目录排除规则而被拒绝。"""
        self.check_payload("obs_tool/assets/icon.png", True)


if __name__ == "__main__":
    unittest.main()
