"""Windows 原生文件/文件夹选择器子进程入口（`pick_main`）单元测试模块。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obs_tool.exporters import atomic_json
from obs_tool.native_picker import pick_main


class NativePickerTests(unittest.TestCase):
    """验证 `pick_main` 在文件选择、DAT 目录扫描、空目录报错及异常请求文件下的返回码与 JSON 输出。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pick_main_success_file_selection(self) -> None:
        """验证常规文件选择成功后写入 `paths` 列表并返回退出码 0。"""
        atomic_json(
            self.job_dir / "request.json",
            {"title": "Select DAT File", "directory": False},
        )
        with patch(
            "obs_tool.native_shell.choose", return_value=["/path/to/sample.dat"]
        ):
            ret = pick_main(self.job_dir)
        self.assertEqual(ret, 0)
        result = json.loads((self.job_dir / "result.json").read_text("utf-8"))
        self.assertEqual(result, {"paths": ["/path/to/sample.dat"]})

    def test_pick_main_success_dat_directory(self) -> None:
        """验证 `dat_directory=True` 模式下自动枚举所选文件夹内的 `.dat` 文件。"""
        atomic_json(
            self.job_dir / "request.json",
            {"title": "Select Folder", "directory": True, "dat_directory": True},
        )
        with (
            patch("obs_tool.native_shell.choose", return_value=["/folder/path"]),
            patch(
                "obs_tool.input_policy.dat_in_folder",
                return_value=["/folder/path/file1.dat"],
            ) as mock_dat_in_folder,
        ):
            ret = pick_main(self.job_dir)
            self.assertEqual(ret, 0)
            mock_dat_in_folder.assert_called_once()
        result = json.loads((self.job_dir / "result.json").read_text("utf-8"))
        self.assertEqual(result, {"paths": ["/folder/path/file1.dat"]})

    def test_pick_main_error_no_dat_files_in_folder(self) -> None:
        """验证所选文件夹内无 `.dat` 文件时返回退出码 1 并记录友好中文提示。"""
        atomic_json(
            self.job_dir / "request.json",
            {"title": "Select Folder", "directory": True, "dat_directory": True},
        )
        with (
            patch("obs_tool.native_shell.choose", return_value=["/empty/folder"]),
            patch("obs_tool.input_policy.dat_in_folder", return_value=[]),
        ):
            ret = pick_main(self.job_dir)
        self.assertEqual(ret, 1)
        result = json.loads((self.job_dir / "result.json").read_text("utf-8"))
        self.assertEqual(result["paths"], [])
        self.assertIn("所选文件夹中未找到DAT文件。", result["error"])

    def test_pick_main_error_missing_request_json(self) -> None:
        """验证缺失 `request.json` 时安全捕获异常并写入错误结果。"""
        ret = pick_main(self.job_dir)
        self.assertEqual(ret, 1)
        result = json.loads((self.job_dir / "result.json").read_text("utf-8"))
        self.assertEqual(result["paths"], [])
        self.assertTrue(len(result["error"]) > 0)

    def test_pick_main_error_choose_raises_exception(self) -> None:
        """验证底层 COM 选择器抛出异常时返回退出码 1 并保留异常信息。"""
        atomic_json(self.job_dir / "request.json", {"title": "Select File"})
        with patch(
            "obs_tool.native_shell.choose",
            side_effect=RuntimeError("Shell interaction failed"),
        ):
            ret = pick_main(self.job_dir)
        self.assertEqual(ret, 1)
        result = json.loads((self.job_dir / "result.json").read_text("utf-8"))
        self.assertEqual(result["paths"], [])
        self.assertEqual(result["error"], "Shell interaction failed")

    def test_pick_main_error_malformed_request_json(self) -> None:
        """验证 `request.json` 内容损坏时返回退出码 1 并写入错误说明。"""
        (self.job_dir / "request.json").write_text("{invalid_json", encoding="utf-8")
        ret = pick_main(self.job_dir)
        self.assertEqual(ret, 1)
        result = json.loads((self.job_dir / "result.json").read_text("utf-8"))
        self.assertEqual(result["paths"], [])
        self.assertTrue(len(result["error"]) > 0)


if __name__ == "__main__":
    unittest.main()
