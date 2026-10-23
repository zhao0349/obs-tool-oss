"""用真实生产函数的合成输出核对类型契约，不从类型声明反向构造期望字典。"""

from datetime import datetime
import os
from pathlib import Path
import tempfile
import types
from typing import Literal, NotRequired, get_args, get_origin, get_type_hints, is_typeddict
import unittest
from unittest.mock import patch

from obs_tool import data_types as contracts
from obs_tool.correction_actions import identify_actions
from obs_tool.curve_context import prepare_context, save_context
from obs_tool.depth import estimate_depth
from obs_tool.models import Batch, Point, Rules
from obs_tool.processing import process
from obs_tool.readers import read_files
from obs_tool.runner import run
from obs_tool.time_correction import analyse, save
from test_core import records
from test_correction_actions import fixture


class DataContractTests(unittest.TestCase):
    """覆盖成功、提前拒绝、取消及 JSON 时间文本等不同返回形态。"""

    def assert_contract(self, value, annotation):
        """递归核对声明字段与实际值；NotRequired 表示键可缺失，不代表值可为 None。"""
        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if origin is NotRequired:
            self.assert_contract(value, arguments[0])
        elif is_typeddict(annotation):
            self.assertIsInstance(value, dict)
            fields = get_type_hints(annotation, include_extras=True)
            required = {
                name
                for name, field_type in fields.items()
                if get_origin(field_type) is not NotRequired and annotation.__total__
            }
            self.assertLessEqual(required, value.keys())
            self.assertLessEqual(value.keys(), fields.keys())
            for name, item in value.items():
                self.assert_contract(item, fields[name])
        elif origin is list:
            self.assertIsInstance(value, list)
            for item in value:
                self.assert_contract(item, arguments[0])
        elif origin is dict:
            self.assertIsInstance(value, dict)
            for key, item in value.items():
                self.assert_contract(key, arguments[0])
                self.assert_contract(item, arguments[1])
        elif origin is tuple:
            self.assertIsInstance(value, tuple)
            self.assertEqual(len(value), len(arguments))
            for item, item_type in zip(value, arguments):
                self.assert_contract(item, item_type)
        elif origin is Literal:
            self.assertIn(value, arguments)
        elif origin is types.UnionType:
            for member in arguments:
                try:
                    self.assert_contract(value, member)
                    return
                except AssertionError:
                    pass
            self.fail(f"{value!r} does not match {annotation}")
        elif annotation is float:
            self.assertIn(type(value), (int, float))
        else:
            self.assertIsInstance(value, annotation)

    def test_processing_depth_and_serialized_curve(self):
        observations = records()
        point = Point("A", 5)
        result = process(observations, point, Rules())
        self.assert_contract(result, contracts.PointResult)
        self.assert_contract(estimate_depth(observations), contracts.DepthEstimate)
        self.assert_contract(estimate_depth([]), contracts.DepthEstimate)
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "curve.json"
            save_context(snapshot, observations, point, [], 5, lambda: False)
            context = prepare_context(snapshot)
            self.assert_contract(context, contracts.CurveContext)
            self.assertIsInstance(observations[0]["timestamp"], datetime)
            self.assertIsInstance(context["runs"][0][0]["timestamp"], str)

    def test_correction_plans_recognition_and_save(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.dat"
            source.write_text(
                fixture([0, 2, 3, 2, 1, 2, 4, 5, 4, 2, 0], range(4)),
                encoding="utf-8",
            )
            observations, _ = read_files(
                [source], preserve_order=True, allow_early=True
            )
            self.assert_contract(
                identify_actions(observations), contracts.ActionRecognition
            )
            self.assert_contract(identify_actions([]), contracts.ActionRecognition)
            for parameters in (
                dict(mode="inspect"),
                dict(
                    mode="casts",
                    start="2026-01-01T00:00:00",
                    end="2026-01-01T05:00:00",
                ),
                dict(
                    mode="casts",
                    start="2026-01-01T00:00:00",
                    end="2026-01-01T03:00:00",
                ),
                dict(
                    mode="known",
                    instrument="1970-01-01T00:00:00",
                    correct="2026-01-01T00:00:00",
                ),
            ):
                self.assert_contract(
                    analyse(source, parameters), contracts.CorrectionPlan
                )
            plan = analyse(source, parameters)
            self.assertTrue(plan["valid"])
            self.assert_contract(
                save(plan, Path(directory) / "corrected.dat"),
                contracts.CorrectionSaveResult,
            )
            self.assert_contract(save(plan), contracts.CorrectionSaveResult)

    def test_uncertainty_is_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ambiguous.dat"
            source.write_text(
                fixture([0, 2, 4, 5, 4, 1, 1, 1, 4, 5, 4, 2, 0]),
                encoding="utf-8",
            )
            observations, _ = read_files([source], allow_early=True)
            recognition = identify_actions(observations)
            self.assertTrue(recognition["uncertainties"])
            self.assert_contract(recognition, contracts.ActionRecognition)

    def test_batch_success_failure_and_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input" / "A.dat"
            source.parent.mkdir()
            source.write_text(
                fixture([0, 2, 4, 5, 4, 2, 0]).replace("01/01/1970", "01/01/2026"),
                encoding="utf-8",
            )
            batch = Batch(
                points=[Point("A", 5, files=[str(source)]), Point("B", 5)],
                output_dir=str(root / "output"),
            )
            with patch.dict(os.environ, OBS_RECORDS_DIR=str(root / "records")):
                _, report, results = run(batch)
                self.assertEqual(report["status"], "completed_with_errors")
                self.assert_contract(report, contracts.BatchRunReport)
                self.assert_contract(results["A"], contracts.PointResult)
                _, cancelled_report, _ = run(batch, cancelled=lambda: True)
                self.assertEqual(cancelled_report["status"], "cancelled")
                self.assert_contract(cancelled_report, contracts.BatchRunReport)


if __name__ == "__main__":
    unittest.main()
