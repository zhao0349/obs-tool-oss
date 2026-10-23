"""水深过程线时间范围计算（`time_ranges`）、极值保真抽稀（`prepare_context`）与 Windows 文件锁重试回归测试。"""

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dat_samples import HEADER
from obs_tool.curve_context import prepare_context, save_context, time_ranges
from obs_tool.models import Batch, Point
from obs_tool.record_store import record_directory
from obs_tool.runner import run


def segments() -> dict[str, list[dict[str, str]]]:
    """构造包含 3 个跨年观测片段（22:00、00:00、02:00）的测试字典。"""
    return {
        "segments": [
            {"start": start_iso, "end": end_iso}
            for start_iso, end_iso in [
                ("2025-12-31T22:00:00", "2025-12-31T22:10:00"),
                ("2026-01-01T00:00:00", "2026-01-01T00:10:00"),
                ("2026-01-01T02:00:00", "2026-01-01T02:10:00"),
            ]
        ]
    }


class CurveTests(unittest.TestCase):
    """验证水深曲线默认视窗、前后邻近片段范围、极值抽稀保真度及跨平台原子写入重试。"""

    def test_normal_defaults_and_browse_half_hour(self) -> None:
        """验证正常命中片段的整点行默认视图聚焦于所属片段，且可浏览范围至少向外扩展半小时。"""
        row = dict(target="2026-01-01T00:00:00", segment=1, status=[])
        default, browse, _ = time_ranges(segments(), row)
        self.assertEqual(default, (datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 10)))
        self.assertLessEqual(browse[0], datetime(2025, 12, 31, 23, 30))
        self.assertGreaterEqual(browse[1], datetime(2026, 1, 1, 0, 30))

    def test_inside_between_and_matched_outside(self) -> None:
        """验证片段复用（`segment_reused`）与缺失片段（`missing_segment`）时自动纳入相邻片段供对比。"""
        row = dict(target="2026-01-01T00:05:00", segment=1, status=["segment_reused"])
        default, _, neighbors = time_ranges(segments(), row)
        self.assertEqual(set(neighbors), {0, 1, 2})
        self.assertEqual(default[0], datetime(2025, 12, 31, 22))
        self.assertEqual(default[1], datetime(2026, 1, 1, 2, 10))
        row.update(
            target="2025-12-31T23:00:00", segment=None, status=["missing_segment"]
        )
        self.assertEqual(set(time_ranges(segments(), row)[2]), {0, 1})

    def test_boundaries_distinct_from_last_segment(self) -> None:
        """验证当整点超出最后一个观测片段时，结合全测点起止边界（`context`）计算合理浏览窗口。"""
        context = dict(start="2025-12-31T20:00:00", end="2026-01-01T04:00:00")
        row = dict(
            target="2026-01-01T06:00:00", segment=None, status=["missing_segment"]
        )
        default, browse, neighbors = time_ranges(segments(), row, context)
        self.assertIn(2, neighbors)
        self.assertGreaterEqual(default[1], datetime(2026, 1, 1, 6, 30))
        self.assertEqual(browse[0], datetime(2025, 12, 31, 20))
        row.update(target="2026-01-01T02:05:00", segment=2, status=["segment_reused"])
        self.assertEqual(
            time_ranges(segments(), row, context)[0][1], datetime(2026, 1, 1, 4, 30)
        )

    def test_snapshot_sources_gaps_and_extrema(self) -> None:
        """验证 `prepare_context` 按长时间断档切分连续折线段（`runs`），并在抽稀时严格保留水深极值点（50m）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            rows = [
                dict(
                    timestamp=datetime(2025, 12, 31, 23, 58) + timedelta(seconds=idx),
                    depth=50 if idx == 49 else idx % 5,
                    file="a.dat",
                    line=idx + 1,
                )
                for idx in range(100)
            ]
            rows.append(
                dict(timestamp=datetime(2026, 1, 2), depth=0, file="b.dat", line=1)
            )
            context_path = Path(temp_dir) / "context.json"
            save_context(context_path, rows, Point("A", 6), [], 5, lambda: False)
            data = prepare_context(context_path, limit=20)
            self.assertEqual(len(data["runs"]), 2)
            self.assertIn(
                50, [pt["depth"] for run_points in data["runs"] for pt in run_points]
            )
            self.assertEqual(data["end"], "2026-01-02 00:00:00")
            self.assertEqual(data["runs"][-1][0]["file"], "b.dat")

    def test_only_csv_export_and_internal_records(self) -> None:
        """验证用户输出目录内仅生成纯洁的 `.csv` 结果文件，曲线与诊断 JSON 归档在外部独立记录目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "in"
            input_dir.mkdir()
            dat_path = input_dir / "A#.dat"
            dat_path.write_text(
                HEADER
                + "00:00:00 01/01/2026 0 2 20 1 30 6\n00:01:00 01/01/2026 3 2 20 1 30 6\n",
                encoding="utf-8",
            )
            directory, report, _ = run(
                Batch(
                    points=[Point("A", 3, files=[str(dat_path)])],
                    output_dir=str(root / "csv"),
                )
            )
            self.assertEqual([f.suffix for f in directory.rglob("*")], [".csv"])
            self.assertEqual(report["status"], "completed")
            records_dir = record_directory(directory)
            self.assertFalse(records_dir.is_relative_to(directory))
            self.assertTrue((records_dir / "A-curve.json").is_file())
            self.assertTrue((records_dir / "A-diagnostics.json").is_file())

    def test_old_missing_range_no_invented_boundary(self) -> None:
        """验证无观测片段时不会虚构邻近片段索引，默认显示目标整点前后共 1 小时窗口。"""
        row = dict(
            target="2026-01-01T00:00:00", segment=None, status=["missing_segment"]
        )
        default, _, neighbors = time_ranges({}, row)
        self.assertEqual(neighbors, [])
        self.assertEqual((default[1] - default[0]).total_seconds(), 3600)

    def test_transient_windows_record_lock_preserves_atomic_write(self) -> None:
        """验证 Windows 下遇到短暂文件占用（`winerror=32`）时，`atomic_json` 自动退避重试并成功替换目标文件。"""
        import os

        from obs_tool.exporters import atomic_json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.json"
            path.write_text('{"old":true}', encoding="utf-8")
            real_replace = os.replace
            calls: list[object] = []

            def locked(source: object, target: object) -> None:
                calls.append(source)
                if len(calls) < 3:
                    exc = PermissionError("合成临时占用")
                    exc.winerror = 32  # type: ignore[attr-defined]
                    raise exc
                real_replace(source, target)  # type: ignore[arg-type]

            # 仅模拟 exporters 模块持有的 os 命名空间，避免修改全局 os.name 导致 POSIX Path 选错实现
            with patch("obs_tool.exporters.os", wraps=os) as windows_os:
                windows_os.name = "nt"
                windows_os.replace.side_effect = locked
                atomic_json(path, {"completed": True})
                self.assertEqual(windows_os.replace.call_count, 3)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), {"completed": True}
            )
            self.assertEqual(len(list(Path(temp_dir).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
