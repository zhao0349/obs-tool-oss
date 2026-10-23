"""补充边界回归测试：跨年时间窗口、观测片段尾部 4 点浅水兼容状态机及测点间取消后重试。"""

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from dat_samples import HEADER
from obs_tool.algorithm import build_segments
from obs_tool.models import Batch, Point, Rules
from obs_tool.processing import process
from obs_tool.runner import run
from test_core import records


class AdditionalTests(unittest.TestCase):
    """验证跨年整点选点、片段尾部截止语义及批处理协作式取消后的断点续跑。"""

    def test_year_ambiguity_and_explicit_cross_year(self) -> None:
        """验证显式指定跨年起止时间（2025-12-31 23:00 至 2026-01-01 00:00）时目标整点序列正确生成。"""
        result = process(
            records(),
            Point("A", 5, start="2025-12-31T23:00:00", end="2026-01-01T00:00:00"),
            Rules(),
        )
        self.assertEqual(
            [row["target"] for row in result["rows"]],
            ["2025-12-31T23:00:00", "2026-01-01T00:00:00"],
        )

    def test_tail_compatibility_and_identity(self) -> None:
        """验证连续 4 个低于水深阈值的点即终止当前观测片段，不会向后吞入 8 分钟内的后续记录。"""
        first_segment = build_segments(records(), 5)[0]
        self.assertEqual(
            [obs["line"] for obs in first_segment["records"]], list(range(1, 14))
        )
        # 在连续四个浅水点之后追加一条 1 分钟后的记录，确认不会被并入首个片段
        extended_records = records() + [
            dict(records()[-1], timestamp=datetime(2026, 1, 1, 0, 1), line=99)
        ]
        first_extended = build_segments(extended_records, 5)[0]
        self.assertNotIn(99, [obs["line"] for obs in first_extended["records"]])

    def test_cancel_after_completed_point_and_retry(self) -> None:
        """验证首个测点完成后触发取消信号时，已完成测点保留 `completed`、未开始测点标记 `not_started` 且可单独重试。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            for code in ["A", "B"]:
                (input_dir / (code + "#.dat")).write_text(
                    HEADER
                    + "\n".join(
                        f"{obs['timestamp']:%H:%M:%S %m/%d/%Y} {obs['depth']} 2 20 1 30 6"
                        for obs in records()
                    ),
                    encoding="utf-8",
                )
            batch = Batch(
                input_dir=str(input_dir),
                output_dir=str(root / "out"),
                points=[
                    Point("A", 5, files=[str(input_dir / "A#.dat")]),
                    Point("B", 5, files=[str(input_dir / "B#.dat")]),
                ],
            )
            progress_events: list[bool] = []
            _, report, _ = run(
                batch,
                lambda *args: progress_events.append(True),
                lambda: bool(progress_events),
            )
            self.assertEqual(report["status"], "cancelled")
            self.assertEqual(
                [point_summary["status"] for point_summary in report["points"]],
                ["completed", "not_started"],
            )
            batch.points = batch.points[1:]
            retry_dir, retry_report, _ = run(batch)
            self.assertEqual(retry_report["status"], "completed")
            self.assertTrue((retry_dir / "B#_output.csv").exists())


if __name__ == "__main__":
    unittest.main()
