import unittest
from copy import deepcopy
from dataclasses import replace, asdict
import json, subprocess, sys, os
from pathlib import Path
import tempfile
from obs_tool.models import Point, Batch, validate_codes
from obs_tool.config import save, load
from obs_tool.exporters import preflight_exports, export_point
from obs_tool.result_projection import ReviewProjection


def fixture():
    obs = dict(
        file="C:/data/<&>/cast.dat",
        line=128,
        depth=2.4,
        timestamp="2026-01-01T00:00:00",
        temperature=20.0,
        salinity=30.0,
        turbidity=2.0,
    )
    selections = [
        dict(
            label=label, target_depth=target, observation=deepcopy(obs), deviation_m=0.0
        )
        for label, target in [("表层", None), ("0.6H", 3.6), ("底层", None)]
    ]
    rows = [
        dict(
            target=t,
            segment=0,
            status=[] if i == 0 else ["segment_reused"],
            offset_seconds=0,
            selections=deepcopy(selections),
        )
        for i, t in enumerate(
            ["2025-12-31T23:00:00", "2026-01-01T00:00:00", "2026-01-01T01:00:00"]
        )
    ]
    return dict(
        rows=rows,
        segments=[dict(start="2025-12-31T23:58:00", end="2026-01-01T00:02:00")],
        layer_plan=[dict(label=s["label"]) for s in selections],
    )


class ReviewTests(unittest.TestCase):
    def test_case_guards_and_preserved_spelling(self):
        a = Point("S1", 6)
        b = Point("s1", 6)
        with self.assertRaisesRegex(ValueError, "S1 与 s1"):
            Batch(points=[a, b]).validate()
        with self.assertRaisesRegex(ValueError, "文件名冲突"):
            preflight_exports([a, b])
        validate_codes(["S1", "S2"])
        self.assertEqual(a.code, "S1")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "good.json"
            save(Batch(points=[a]), p)
            self.assertEqual(load(p).points[0].code, "S1")
            p = Path(d) / "bad.json"
            p.write_text(
                json.dumps(
                    asdict(Batch(points=[a, b], output_dir=str(Path(d) / "out")))
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "编号冲突"):
                load(p)

    def test_validate_codes_edge_cases(self):
        # Empty sequences and iterators
        validate_codes([])
        validate_codes(iter([]))
        validate_codes(x for x in [])

        # Unique codes with varied case and special chars
        validate_codes(["S1", "S2", "s3", "测点_A", "CODE-01"])

        # Generator expressions
        validate_codes(c for c in ["P1", "P2", "P3"])

        # Duplicate exact matches
        with self.assertRaisesRegex(ValueError, "测点编号冲突：A 与 A"):
            validate_codes(["A", "A"])

        # Duplicate case-insensitive matches
        with self.assertRaisesRegex(ValueError, "测点编号冲突：Point1 与 POINT1"):
            validate_codes(["Point1", "POINT1"])

        # Duplicate in generator
        with self.assertRaisesRegex(ValueError, "测点编号冲突：code_a 与 CODE_A"):
            validate_codes(x for x in ["code_a", "CODE_A"])

        # Conflict in non-adjacent positions
        with self.assertRaisesRegex(ValueError, "测点编号冲突：s2 与 S2"):
            validate_codes(["S1", "s2", "S3", "S2"])

        # Chinese characters exact duplicate
        with self.assertRaisesRegex(ValueError, "测点编号冲突：测点1 与 测点1"):
            validate_codes(["测点1", "测点1"])

    def test_export_refuses_existing_case_variant_before_writing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s1#_output.csv"
            p.write_text("保留")
            with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
                export_point(d, {}, Point("S1", 6), Batch())
            self.assertEqual(p.read_text(), "保留")
            self.assertEqual(len(list(Path(d).iterdir())), 1)

    def test_full_projection_and_no_mutation(self):
        result = fixture()
        before = deepcopy(result)
        p = ReviewProjection(result)
        self.assertEqual(p.shared_segment(0), [0, 1, 2])
        self.assertEqual(p.shared_layers(0), {0, 1, 2})
        self.assertIn("segment_reused", p.status(0))
        self.assertIn("cross_layer_reused", p.status(0))
        self.assertEqual(result, before)
        separate = fixture()
        separate["rows"] = separate["rows"][:1]
        self.assertEqual(ReviewProjection(separate).shared_segment(0), [0])

    def test_observation_identity_not_depth_time_or_basename(self):
        result = fixture()
        s = result["rows"][0]["selections"]
        s[1]["observation"]["file"] = "C:/other/cast.dat"
        s[2]["observation"]["line"] = 129
        self.assertEqual(ReviewProjection(result).shared_layers(0), set())

    def test_source_dates_real_depth_history_and_escaping(self):
        html = ReviewProjection(fixture()).details_html(0, 6)
        for text in [
            "2025-12-31 23:00:00",
            "2026-01-01 01:00:00",
            "2025-12-31 23:58:00",
            "2026-01-01 00:02:00",
            "表层、0.6H、底层",
            "128",
            "2.400",
            "0.400H",
            "H＝6.000",
            "&lt;&amp;&gt;",
        ]:
            self.assertIn(text, html)
        self.assertNotIn("C:/data/<&>", html)

    def test_multiple_problems_and_legacy_fields(self):
        result = fixture()
        result["rows"][0]["status"] += ["missing_layer", "depth_out_of_tolerance"]
        p = ReviewProjection(result)
        html = p.details_html(0)
        for text in ["片段复用", "跨层复用", "缺层", "深度偏差超限", "本次水深未记录"]:
            self.assertIn(text, html)
        result["segments"] = []
        for row in result["rows"]:
            row["status"].append("cross_layer_reused")
            for s in row["selections"]:
                s["observation"].pop("line")
        html = ReviewProjection(result).details_html(0)
        self.assertIn("起止时间未记录", html)
        self.assertIn("无法可靠还原关联层位", html)
        self.assertIn("原始行号：未记录", html)


if __name__ == "__main__":
    unittest.main()
