import json
import tempfile
import unittest
from pathlib import Path

from obs_tool.config import load, save
from obs_tool.input_policy import LIMITS, CapacityError
from obs_tool.models import Batch, Point, Rules


class ConfigTests(unittest.TestCase):
    def test_save_and_load_roundtrip_with_relative_paths(self):
        """Test that saving a batch and loading it back restores attributes and resolves relative paths."""
        with tempfile.TemporaryDirectory() as d:
            dir_path = Path(d)
            json_path = dir_path / "batch.json"

            in_dir = dir_path / "input"
            out_dir = dir_path / "output"
            in_dir.mkdir()
            out_dir.mkdir()
            sample_file = in_dir / "A#.dat"
            sample_file.touch()

            point = Point(code="A", depth=10.0, files=["input/A#.dat"])
            batch = Batch(
                schema_version=1,
                points=[point],
                input_dir="input",
                output_dir="output",
                rules=Rules(),
            )

            save(batch, json_path)
            self.assertTrue(json_path.exists())

            loaded_batch = load(json_path)

            self.assertEqual(loaded_batch.schema_version, 1)
            self.assertEqual(len(loaded_batch.points), 1)
            self.assertEqual(loaded_batch.points[0].code, "A")
            self.assertEqual(loaded_batch.points[0].depth, 10.0)

            # Check path resolutions
            self.assertEqual(loaded_batch.input_dir, str(in_dir.resolve()))
            self.assertEqual(loaded_batch.output_dir, str(out_dir.resolve()))
            self.assertEqual(loaded_batch.points[0].files, [str(sample_file.resolve())])

    def test_load_legacy_export_field_conversion(self):
        """Test that legacy_export field in JSON is mapped to complete_rows_only."""
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "legacy_batch.json"
            data = {
                "schema_version": 1,
                "legacy_export": True,
                "points": [{"code": "A", "depth": 10.0, "files": ["A.dat"]}],
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")

            batch = load(json_path)
            self.assertTrue(batch.complete_rows_only)

    def test_load_default_depth_source_when_depth_is_none(self):
        """Test that depth_source is set to 'pending' if depth is None and depth_source is missing."""
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "batch.json"
            data = {
                "schema_version": 1,
                "points": [
                    {"code": "A", "depth": None, "files": ["A.dat"]},
                    {
                        "code": "B",
                        "depth": None,
                        "depth_source": "manual",
                        "files": ["B.dat"],
                    },
                ],
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")

            batch = load(json_path)
            self.assertEqual(batch.points[0].depth_source, "pending")
            self.assertEqual(batch.points[1].depth_source, "manual")

    def test_load_invalid_structure(self):
        """Test that non-dict or missing points list raises ValueError."""
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "invalid.json"

            # Non-dict JSON root
            json_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("任务记录结构无效", str(ctx.exception))

            # Missing or invalid points list
            json_path.write_text(
                json.dumps({"schema_version": 1, "points": "not_a_list"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("任务记录结构无效", str(ctx.exception))

    def test_load_invalid_schema_version(self):
        """Test that unsupported schema version raises ValueError."""
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "invalid_schema.json"

            # schema_version missing
            data = {"points": [{"code": "A", "depth": 10.0, "files": ["A.dat"]}]}
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("未知批次版本", str(ctx.exception))

            # schema_version != 1
            data["schema_version"] = 2
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("未知批次版本", str(ctx.exception))

    def test_load_invalid_point_files_structure(self):
        """Test invalid point files structures (not a list, non-string items, or exceeding path length)."""
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "invalid_files.json"

            # Point item is not a dict
            data = {"schema_version": 1, "points": ["not_a_dict"]}
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("任务必须明确列出每个测点的输入文件", str(ctx.exception))

            # Point files missing
            data = {"schema_version": 1, "points": [{"code": "A"}]}
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("任务必须明确列出每个测点的输入文件", str(ctx.exception))

            # Point files is not a list
            data = {
                "schema_version": 1,
                "points": [{"code": "A", "depth": 10.0, "files": "A.dat"}],
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("任务必须明确列出每个测点的输入文件", str(ctx.exception))

            # Point files contains non-string item
            data = {
                "schema_version": 1,
                "points": [{"code": "A", "depth": 10.0, "files": [123]}],
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("输入文件路径格式无效", str(ctx.exception))

            # Point files contains string exceeding max length (32760 chars)
            long_path = "a" * 32761
            data = {
                "schema_version": 1,
                "points": [{"code": "A", "depth": 10.0, "files": [long_path]}],
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load(json_path)
            self.assertIn("输入文件路径格式无效", str(ctx.exception))

    def test_load_limit_enforcements(self):
        """Test capacity limit enforcements for batch points count and point files count."""
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "limit_test.json"

            # Exceeding batch_files limit
            excess_points = [
                {"code": f"P{i}", "depth": 10.0, "files": ["f.dat"]}
                for i in range(LIMITS.batch_files + 1)
            ]
            data = {"schema_version": 1, "points": excess_points}
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(CapacityError) as ctx:
                load(json_path)
            self.assertIn("任务测点数", str(ctx.exception))

            # Exceeding point_files limit
            excess_files = [f"f{i}.dat" for i in range(LIMITS.point_files + 1)]
            data = {
                "schema_version": 1,
                "points": [{"code": "A", "depth": 10.0, "files": excess_files}],
            }
            json_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(CapacityError) as ctx:
                load(json_path)
            self.assertIn("测点文件条目数", str(ctx.exception))

    def test_save_validations(self):
        """Test save method validation errors for invalid file extension and invalid batch contents."""
        with tempfile.TemporaryDirectory() as d:
            dir_path = Path(d)
            batch = Batch(
                schema_version=1, points=[Point(code="A", depth=10.0, files=["A.dat"])]
            )

            # Non-json extension
            txt_path = dir_path / "batch.txt"
            with self.assertRaises(ValueError) as ctx:
                save(batch, txt_path)
            self.assertIn("批次须保存为独立目录内的JSON文件", str(ctx.exception))

            # Upper case .JSON extension should pass extension check
            json_upper = dir_path / "batch.JSON"
            save(batch, json_upper)
            self.assertTrue(json_upper.exists())

            # Invalid batch validation during save
            invalid_batch = Batch(
                schema_version=1,
                points=[Point(code="INVALID_CODE!", depth=10.0, files=["A.dat"])],
            )
            with self.assertRaises(ValueError):
                save(invalid_batch, dir_path / "batch2.json")


if __name__ == "__main__":
    unittest.main()
