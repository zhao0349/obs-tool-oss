"""自定义分层方案导出、规则库版本校验、多测点原子批量修改与智能水深抗毛刺估算测试模块。"""

import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from obs_tool.algorithm import custom_layer_plan
from obs_tool.depth import estimate_depth
from obs_tool.editing import apply_bulk
from obs_tool.exporters import export_point
from obs_tool.models import Batch, Point, Rules, validate_positions
from obs_tool.processing import process
from obs_tool.rule_store import load_rules, save_rules
from test_core import records


class NewTests(unittest.TestCase):
    """验证 4 层自定义分层 CSV 导出、非法层位拦截、规则库持久化、批量编辑不变性及水深毛刺过滤。"""

    def test_four_layers_and_endpoints_export(self) -> None:
        """验证 4 层自定义方案（`0.2H / 0.4H / 0.6H / 0.8H`）生成 18 列 CSV，且 `0` 与 `1` 端点映射为表层与底层。"""
        point = Point("A", 5, layer_positions=[0.2, 0.4, 0.6, 0.8])
        result = process(records(), point, Rules())
        self.assertEqual(result["layer_count"], 4)
        self.assertEqual(
            [sel["target_depth"] for sel in result["rows"][0]["selections"]],
            [1, 2, 3, 4],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            export_point(temp_dir, result, point, Batch(points=[point]))
            with (Path(temp_dir) / "A#_output.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows[1]), 18)
            self.assertIn("0.4H 温度", rows[1])
        self.assertEqual(
            [layer["kind"] for layer in custom_layer_plan([0, 0.05, 0.95, 1])],
            ["surface", "relative", "relative", "bottom"],
        )

    def test_invalid_custom_rules(self) -> None:
        """验证空列表、逆序、重复、越界、NaN 及布尔值均被 `validate_positions` 拒绝。"""
        for invalid_values in [
            [],
            [0.4, 0.2],
            [0.2, 0.2],
            [-0.1],
            [1.1],
            [float("nan")],
            [True],
        ]:
            with self.assertRaises(ValueError):
                validate_positions(invalid_values)

    def test_rules_external_roundtrip_and_unknown_version(self) -> None:
        """验证自定义规则库文件的保存与读取往返一致，且遇到未知 `schema_version` 时抛出 `ValueError`。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            rules_path = Path(temp_dir) / "rules.json"
            rules = [dict(name="4层", positions=[0.2, 0.4, 0.6, 0.8])]
            save_rules(rules, rules_path)
            self.assertEqual(load_rules(rules_path), rules)
            rules_path.write_text(
                json.dumps(dict(schema_version=2, rules=rules)), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                load_rules(rules_path)

    def test_bulk_atomic_unchanged_fields(self) -> None:
        """验证 `apply_bulk` 仅修改目标索引的指定字段，不污染原列表且在校验失败时保持原子性。"""
        points = [
            Point("A", 5, start="2025-01-01T00:00:00", layer_positions=[0.2, 0.4]),
            Point("B", 7),
            Point("C", 10),
        ]
        edited = apply_bulk(points, [0, 2], dict(depth=8))
        self.assertEqual([pt.depth for pt in edited], [8, 7, 8])
        self.assertEqual(edited[0].start, points[0].start)
        self.assertEqual(edited[0].layer_positions, [0.2, 0.4])
        self.assertEqual(points[0].depth, 5)
        cleared = apply_bulk(edited, [0], dict(start=None))
        self.assertIsNone(cleared[0].start)
        with self.assertRaises(ValueError):
            apply_bulk(points, [0, 2], dict(start="2025-02-01", end="2025-01-01"))
        self.assertEqual(points[2].depth, 10)

    def test_depth_spike_multiple_profiles_and_invalid(self) -> None:
        """验证单点瞬态水深尖峰（500m）被去毛刺逻辑剔除，多剖面峰值中位数正确估算参考水深。"""
        obs_records = []
        for hour_idx in range(5):
            cast = records(
                [0, 1, 3, 4, 5, 4, 3, 1, 0],
                datetime(2025, 1, 1) + timedelta(hours=hour_idx),
            )
            for obs in cast:
                obs["line"] += hour_idx * 100
            obs_records += cast
        obs_records.insert(
            2,
            dict(
                obs_records[1],
                depth=500,
                line=999,
                timestamp=obs_records[1]["timestamp"] + timedelta(seconds=1),
            ),
        )
        estimate = estimate_depth(obs_records)
        self.assertEqual(estimate["value"], 5)
        self.assertEqual(estimate["casts"], 5)
        self.assertNotIn(500, [peak["depth"] for peak in estimate["peaks"]])
        self.assertIsNone(estimate_depth([])["value"])
        self.assertIsNone(
            estimate_depth(records([-1, float("nan"), float("inf")]))["value"]
        )


if __name__ == "__main__":
    unittest.main()
