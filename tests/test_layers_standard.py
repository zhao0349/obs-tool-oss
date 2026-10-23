"""行业标准分层数自动推断（`standard_layer_count`）与标准分层方案（`get_layer_plan`）单元测试模块。"""

from pathlib import Path
import tempfile
import unittest

from obs_tool.algorithm import get_layer_plan
from obs_tool.config import load, save
from obs_tool.models import Batch, Point, Rules, standard_layer_count
from obs_tool.processing import process
from test_core import records


class StandardTests(unittest.TestCase):
    """验证水深边界（2m / 5m / 8m / 11m）对应的标准分层数、层位标签及配置持久化。"""

    def test_standard_boundaries(self) -> None:
        """验证不同参考水深阈值下自动匹配的行业标准分层数（1 / 2 / 3 / 5 / 6 层）。"""
        for depth, expected_count in [
            (1.999, 1),
            (2, 2),
            (4.999, 2),
            (5, 3),
            (7.999, 3),
            (8, 5),
            (10.999, 5),
            (11, 6),
            (20, 6),
        ]:
            self.assertEqual(standard_layer_count(depth), expected_count)
            self.assertEqual(Point("A", depth).layer_count, expected_count)

    def test_complete_plans(self) -> None:
        """验证 1 / 2 / 3 / 5 / 6 层标准方案生成的垂向层位标签序列。"""
        expected_labels = {
            1: ["0.6H"],
            2: ["0.2H", "0.8H"],
            3: ["0.2H", "0.6H", "0.8H"],
            5: ["表层", "0.2H", "0.6H", "0.8H", "底层"],
            6: ["表层", "0.2H", "0.4H", "0.6H", "0.8H", "底层"],
        }
        for count, labels in expected_labels.items():
            self.assertEqual(
                [layer["label"] for layer in get_layer_plan(count)], labels
            )

    def test_auto_and_explicit_override(self) -> None:
        """验证未指定层数时按水深自动选择，以及显式指定固定层数时的覆盖行为。"""
        self.assertEqual(Point("A", 10).layer_count, 5)
        self.assertEqual(Point("A", 10, 6).layer_count, 6)
        for depth, expected_count in [(1, 1), (3, 2), (5, 3), (8, 5), (11, 6)]:
            result = process(records(), Point("A", depth), Rules())
            self.assertEqual(result["layer_count"], expected_count)
            self.assertEqual(result["layer_mode"], "standard")
            selected = next(
                row["selections"] for row in result["rows"] if row["selections"]
            )
            self.assertEqual(len(selected), expected_count)

    def test_explicit_files_without_input_directory_roundtrip(self) -> None:
        """验证仅包含显式文件列表且 `input_dir` 为空字符串时，配置保存与加载往返保持一致。"""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            batch = Batch(input_dir="", points=[Point("A", 10, files=["A.dat"])])
            save(batch, config_path)
            restored = load(config_path)
            self.assertEqual(restored.input_dir, "")
            self.assertIsNone(restored.points[0].layers)
            self.assertEqual(restored.points[0].layer_count, 5)


if __name__ == "__main__":
    unittest.main()
