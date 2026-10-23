import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch, MagicMock

from obs_tool.input_policy import LIMITS, Budget, CapacityError
from obs_tool.readers import (
    normalize,
    ParseDiagnostics,
    bounded_lines,
    observation_lines,
    read_files,
    DATFormatError,
    FIELDS,
)


def make_dat_content(sensors=None, lines=None):
    if sensors is None:
        sensors = [
            ("Depth", "0.5"),
            ("OBS-1 (NTU)", "1.2"),
            ("Temperature (deg. C)", "25.0"),
            ("Salinity (PSU)", "35.0"),
        ]
    if lines is None:
        lines = ["00:00:00 01/01/2026 0.5 1.2 25.0 35.0"]

    header_cols = "\n".join(
        f" {i+1} {name} Mean" for i, (name, _) in enumerate(sensors)
    )
    content = (
        f"OBS Header\nSensor Data Columns\n{header_cols}\n" + "\n".join(lines) + "\n"
    )
    return content


class ReadersUnitTests(unittest.TestCase):
    """单元测试 src/obs_tool/readers.py 中的各个底层与高层函数。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_file(self, filename, content, encoding="utf-8"):
        path = self.root / filename
        if isinstance(content, str):
            path.write_text(content, encoding=encoding)
        else:
            path.write_bytes(content)
        return path

    # ------------------------------------------------------------------
    # 1. normalize(value) 测试
    # ------------------------------------------------------------------
    def test_normalize_basic(self):
        self.assertEqual(normalize(" Depth (m) "), "depth(m)")
        self.assertEqual(normalize("OBS-1 (NTU)"), "obs-1(ntu)")
        self.assertEqual(normalize("Temperature\t(deg.c)\n"), "temperature(deg.c)")
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   \t\n  "), "")

    # ------------------------------------------------------------------
    # 2. ParseDiagnostics 测试
    # ------------------------------------------------------------------
    def test_parse_diagnostics_add_and_limits(self):
        diag = ParseDiagnostics(limit=2)
        self.assertEqual(diag.limit, 2)

        # 添加第一条诊察项
        diag.add("file1.dat", 10, "bad_data: invalid number")
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag.counts["bad_data"], 1)
        self.assertEqual(diag[0]["reason"], "bad_data: invalid number")
        self.assertEqual(diag[0]["line"], 10)

        # 添加第二条诊察项
        diag.add("file1.dat", 12, "bad_data: overflow")
        self.assertEqual(len(diag), 2)
        self.assertEqual(diag.counts["bad_data"], 2)

        # 超出 limit 后不再追加列表元素，但 counts 仍继续统计
        diag.add("file1.dat", 14, "duplicate")
        self.assertEqual(len(diag), 2)
        self.assertEqual(diag.counts["duplicate"], 1)

    def test_parse_diagnostics_reason_truncation(self):
        diag = ParseDiagnostics(limit=5)
        long_reason = "error_code: " + "x" * 300
        diag.add("file1.dat", 1, long_reason)
        self.assertEqual(len(diag[0]["reason"]), 200)

    # ------------------------------------------------------------------
    # 3. bounded_lines(stream, budget) 测试
    # ------------------------------------------------------------------
    def test_bounded_lines_normal(self):
        data = b"line 1\nline 2\r\nline 3\tdata\n"
        stream = io.BytesIO(data)
        budget = Budget(LIMITS)
        lines = list(bounded_lines(stream, budget))
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], (1, b"line 1\n"))
        self.assertEqual(lines[1], (2, b"line 2\r\n"))
        self.assertEqual(lines[2], (3, b"line 3\tdata\n"))

    def test_bounded_lines_line_bytes_limit(self):
        limits = replace(LIMITS, line_bytes=10)
        budget = Budget(limits)
        stream = io.BytesIO(b"this is a very long line that exceeds limit\n")
        with self.assertRaises(CapacityError) as cm:
            list(bounded_lines(stream, budget))
        self.assertIn("单行字节数", str(cm.exception))

    def test_bounded_lines_file_lines_limit(self):
        limits = replace(LIMITS, file_lines=2)
        budget = Budget(limits)
        stream = io.BytesIO(b"line1\nline2\nline3\n")
        with self.assertRaises(CapacityError) as cm:
            list(bounded_lines(stream, budget))
        self.assertIn("文件物理行数", str(cm.exception))

    def test_bounded_lines_binary_garbage(self):
        budget = Budget(LIMITS)
        # 包含 ASCII < 32 且非 9, 10, 13 的控制字符 (如 0x00 或 0x05)
        stream = io.BytesIO(b"valid line\n\x00\x05bad binary\n")
        with self.assertRaises(DATFormatError) as cm:
            list(bounded_lines(stream, budget))
        self.assertIn("乱码或非文本内容", str(cm.exception))

    # ------------------------------------------------------------------
    # 4. observation_lines 测试
    # ------------------------------------------------------------------
    def test_observation_lines_unicode_error(self):
        budget = Budget(LIMITS)
        diagnostics = ParseDiagnostics(10)
        # 包含非法 UTF-8 字节
        stream = io.BytesIO(b"header\n\xff\xfe\xfd bad utf8\n")
        with self.assertRaises(DATFormatError) as cm:
            list(
                observation_lines(
                    stream, budget, "utf-8", diagnostics, Path("a.dat"), lambda: 6
                )
            )
        self.assertIn("字符编码异常", str(cm.exception))

    def test_observation_lines_instrument_footers(self):
        text = (
            "12:00:00 01/01/2026 1 2 3 4\n"
            "00:00:00 01/01/1970 0 0 0 0\n"
            " 1234  Bytes read from file.\n"
            "OBS>\n"
        )
        stream = io.BytesIO(text.encode("utf-8"))
        budget = Budget(LIMITS)
        diagnostics = ParseDiagnostics(10)
        lines = list(
            observation_lines(
                stream, budget, "utf-8", diagnostics, Path("a.dat"), lambda: 6
            )
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], 1)
        self.assertEqual(diagnostics.counts["instrument_end_record"], 1)
        self.assertEqual(diagnostics.counts["instrument_notice"], 2)

    # ------------------------------------------------------------------
    # 5. read_files 各种校验与边界测试
    # ------------------------------------------------------------------
    def test_read_files_valid_single_file(self):
        content = make_dat_content()
        p = self.create_file("test.dat", content)
        records, diag = read_files([p])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["depth"], 0.5)
        self.assertEqual(records[0]["turbidity"], 1.2)
        self.assertEqual(records[0]["temperature"], 25.0)
        self.assertEqual(records[0]["salinity"], 35.0)

    def test_read_files_gb18030_encoding(self):
        # 包含仅在 GB18030 下有效、在 UTF-8 下会导致 UnicodeDecodeError 的非 ASCII 字符
        chinese_header = "OBS 观测数据表头\nSensor Data Columns\n 1 Depth Mean\n 2 OBS-1 (NTU) Mean\n 3 Temperature (deg. C) Mean\n 4 Salinity (PSU) Mean\n00:00:00 01/01/2026 0.5 1.2 25.0 35.0\n"
        p = self.create_file("gb.dat", chinese_header.encode("gb18030"))
        records, diag = read_files([p])
        self.assertEqual(len(records), 1)
        self.assertEqual(diag.files[0]["encoding"], "gb18030")

    def test_read_files_preserve_order_vs_sorted(self):
        # p1 (b.dat) 时间更晚 (00:00:10)，p2 (a.dat) 时间更早 (00:00:00)
        p1 = self.create_file(
            "b.dat", make_dat_content(lines=["00:00:10 01/01/2026 0.5 1.2 25.0 35.0"])
        )
        p2 = self.create_file(
            "a.dat", make_dat_content(lines=["00:00:00 01/01/2026 0.5 1.2 25.0 35.0"])
        )

        # preserve_order = True -> 保留原始输入列表 [p1, p2] 的处理与返回顺序 (p1 在前)
        records_preserve, _ = read_files([p1, p2], preserve_order=True)
        self.assertEqual([r["file"] for r in records_preserve], [str(p1), str(p2)])
        self.assertEqual(records_preserve[0]["raw_time"], "00:00:10")
        self.assertEqual(records_preserve[1]["raw_time"], "00:00:00")

        # preserve_order = False -> 文件先按路径名升序排序为 [p2, p1]，记录按时间戳升序排序 (p2 00:00:00 在前)
        records_sorted, _ = read_files([p1, p2], preserve_order=False)
        self.assertEqual([r["file"] for r in records_sorted], [str(p2), str(p1)])
        self.assertEqual(records_sorted[0]["raw_time"], "00:00:00")
        self.assertEqual(records_sorted[1]["raw_time"], "00:00:10")

    def test_read_files_duplicate_files_and_budget_counting(self):
        # 验证全重复的后续文件在 preserve_order=False 时正常去重，但重复行仍计入资源预算 budget.records
        content = make_dat_content(lines=["00:00:00 01/01/2026 0.5 1.2 25.0 35.0"])
        p1 = self.create_file("a.dat", content)
        p2 = self.create_file("b.dat", content)

        budget = Budget(LIMITS)
        records, diag = read_files([p1, p2], preserve_order=False, budget=budget)
        # 去重后结果只有 1 条记录
        self.assertEqual(len(records), 1)
        self.assertEqual(diag.counts["duplicate"], 1)
        # 资源预算仍然累计读取过的 2 条记录
        self.assertEqual(budget.records, 2)

    def test_read_files_missing_required_sensor_header(self):
        # 缺少 Salinity 传感器
        sensors = [
            ("Depth", "0.5"),
            ("OBS-1 (NTU)", "1.2"),
            ("Temperature (deg. C)", "25.0"),
        ]
        content = make_dat_content(
            sensors=sensors, lines=["00:00:00 01/01/2026 0.5 1.2 25.0"]
        )
        p = self.create_file("missing_sensor.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("缺少必需观测通道", str(cm.exception))

    def test_read_files_duplicate_sensor_columns_statement(self):
        content = (
            "Header\n" "Sensor Data Columns\n" " 1 Depth Mean\n" "Sensor Data Columns\n"
        )
        p = self.create_file("dup_cols.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("重复的传感器通道声明", str(cm.exception))

    def test_read_files_duplicate_sensor_channel(self):
        sensors = [
            ("Depth", "0.5"),
            ("Depth", "0.5"),
            ("OBS-1 (NTU)", "1.2"),
            ("Temperature (deg. C)", "25.0"),
            ("Salinity (PSU)", "35.0"),
        ]
        content = make_dat_content(
            sensors=sensors, lines=["00:00:00 01/01/2026 0.5 0.5 1.2 25.0 35.0"]
        )
        p = self.create_file("dup_sensor.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("存在重复的传感器通道声明", str(cm.exception))

    def test_read_files_unknown_sensor_with_known_prefix(self):
        sensors = [
            ("Depth_Unknown_Unit", "0.5"),
            ("OBS-1 (NTU)", "1.2"),
            ("Temperature (deg. C)", "25.0"),
            ("Salinity (PSU)", "35.0"),
        ]
        content = make_dat_content(
            sensors=sensors, lines=["00:00:00 01/01/2026 0.5 1.2 25.0 35.0"]
        )
        p = self.create_file("unknown_sensor.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("传感器通道名称或单位无法识别", str(cm.exception))

    def test_read_files_column_count_mismatch(self):
        content = make_dat_content(
            lines=["00:00:00 01/01/2026 0.5 1.2 25.0"]
        )  # 少了一列
        p = self.create_file("mismatch.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("数据列数", str(cm.exception))

    def test_read_files_nonfinite_in_required_field(self):
        # 超过 8 行异常数据（bad > 8）会直接触发“格式错误或数值异常的数据行过多”错误
        bad_lines = [f"00:00:{i:02d} 01/01/2026 nan 1.2 25.0 35.0" for i in range(10)]
        content = make_dat_content(lines=bad_lines)
        p = self.create_file("nan_field.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("格式错误或数值异常的数据行过多", str(cm.exception))

    def test_read_files_file_modified_during_read(self):
        content = make_dat_content()
        p = self.create_file("mod.dat", content)

        original_stat = p.stat()
        modified_stat = os.stat_result(
            (
                original_stat.st_mode,
                original_stat.st_ino,
                original_stat.st_dev,
                original_stat.st_nlink,
                original_stat.st_uid,
                original_stat.st_gid,
                original_stat.st_size,
                original_stat.st_atime_ns,
                original_stat.st_mtime_ns + 1000,
                original_stat.st_ctime_ns,
            )
        )

        def mock_stat(self_path):
            if hasattr(mock_stat, "called"):
                return modified_stat
            mock_stat.called = True
            return original_stat

        with patch.object(Path, "stat", autospec=True, side_effect=mock_stat):
            with self.assertRaises(DATFormatError) as cm:
                read_files([p])
            self.assertIn("读取期间文件内容发生变动", str(cm.exception))

    def test_read_files_no_valid_observations(self):
        content = make_dat_content(lines=[])
        p = self.create_file("no_obs.dat", content)
        with self.assertRaises(DATFormatError) as cm:
            read_files([p])
        self.assertIn("没有通过校验的OBS观测数据", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
