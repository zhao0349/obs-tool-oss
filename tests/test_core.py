from dat_samples import HEADER
import csv
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import replace
from obs_tool.models import Point, Rules, Batch
from obs_tool.readers import read_files
from obs_tool.processing import process
from obs_tool.algorithm import (
    build_segments,
    choose_segment,
    select_surface_record,
    select_depth_matches,
)
from obs_tool.config import load, save
from obs_tool.runner import run, protect_output
from obs_tool.exporters import atomic_write


def records(
    depths=(0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 0, 0, 0),
    start=datetime(2025, 12, 31, 23, 55),
):
    return [
        dict(
            timestamp=start + timedelta(seconds=i * 20),
            depth=d,
            temperature=20.0,
            salinity=30.0,
            turbidity=2.0,
            conductivity=1.0,
            file="synthetic.dat",
            line=i + 1,
        )
        for i, d in enumerate(depths)
    ]


class CoreTests(unittest.TestCase):
    def test_parser_quality_sources_and_merge(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.dat"
            b = Path(d) / "b.dat"
            good = "00:00:00 01/01/2025 1 2 3 4 5 6\n"
            a.write_text(
                HEADER
                + good
                + "bad-time 01/01/2025 1 2 3 4 5 6\n00:00:01 01/01/2025 nan 2 3 4 5 6\n00:00:02 01/01/1970 1 2 3 4 5 6\n00:00:03 01/32/2025 1 2 3 4 5 6\n",
                encoding="utf-8",
            )
            b.write_text(
                HEADER + good + "00:00:00 01/01/2025 2 2 3 4 5 6\n", encoding="utf-8"
            )
            r, di = read_files([b, a])
            self.assertEqual(len(r), 2)
            self.assertEqual(r[0]["line"], 9)
            for reason in [
                "duplicate",
                "timestamp_conflict",
                "nonfinite",
                "invalid_year",
            ]:
                self.assertIn(reason, [x["reason"] for x in di])
            self.assertTrue(any(x["reason"].startswith("bad_data") for x in di))
            empty = Path(d) / "empty.dat"
            empty.touch()
            with self.assertRaises(ValueError):
                read_files([empty])

    def test_finite_windows(self):
        r = records()
        for start, end, count in [
            (None, None, 1),
            ("2025-12-31T23:00:00", None, 1),
            (None, "2026-01-01T01:00:00", 3),
            ("2025-12-31T23:30:00", "2026-01-01T01:00:00", 2),
            ("2026-01-01T03:00:00", None, 0),
        ]:
            result = process(r, Point("A", 5, start=start, end=end), Rules())
            self.assertEqual(len(result["rows"]), count)

    def test_midnight_target_and_source(self):
        result = process(
            records(),
            Point("A", 5, start="2026-01-01T00:00:00", end="2026-01-01T00:00:00"),
            Rules(),
        )
        row = result["rows"][0]
        self.assertEqual(row["target"], "2026-01-01T00:00:00")
        self.assertEqual(row["selections"][0]["observation"]["timestamp"].year, 2025)

    def test_segments_no_duplicate_or_unsorted(self):
        r = records()
        segs = build_segments(r, 5)
        self.assertTrue(segs)
        for seg in segs:
            identities = [(o["file"], o["line"]) for o in seg["records"]]
            self.assertEqual(len(identities), len(set(identities)))
            self.assertEqual(
                [o["timestamp"] for o in seg["records"]],
                sorted(o["timestamp"] for o in seg["records"]),
            )
        r2 = records(start=datetime(2026, 1, 1, 2))
        self.assertEqual(len(build_segments(r + r2, 5)), 2)

    def test_no_segment_and_layers(self):
        self.assertIn(
            "missing_segment",
            process(records([0] * 10), Point("A", 5), Rules())["rows"][0]["status"],
        )
        for layers in (3, 5, 6):
            row = process(
                records(),
                Point(
                    "A",
                    5,
                    layers,
                    start="2026-01-01T00:00:00",
                    end="2026-01-01T00:00:00",
                ),
                Rules(),
            )["rows"][0]
            self.assertEqual(len(row["selections"]), layers)
            self.assertIn("tolerance_unset", row["status"])
        self.assertEqual(len(select_depth_matches(records([2]), [0.2, 0.6, 0.8], 5)), 1)

    def test_matching_tie_limit_reuse(self):
        t = datetime(2025, 1, 1, 1)
        early = {"apex": t - timedelta(hours=1)}
        late = {"apex": t + timedelta(hours=1)}
        self.assertIs(choose_segment([late, early], t)[0], early)
        self.assertIsNone(choose_segment([early], t + timedelta(seconds=1))[0])
        row = process(
            records(),
            Point("A", 5, start="2025-12-31T23:00:00", end="2026-01-01T00:00:00"),
            Rules(),
        )["rows"][1]
        self.assertIn("segment_reused", row["status"])

    def test_surface_and_tolerance(self):
        r = records([0.5, 0.51, 1])
        self.assertEqual(
            select_surface_record(r, r[0]["timestamp"], r[0]["timestamp"])["depth"],
            0.51,
        )
        r = records()
        row = process(r, Point("A", 6), Rules(tolerance_m=0))["rows"][0]
        self.assertIn("depth_out_of_tolerance", row["status"])

    def test_configuration_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "batch.json"
            b = Batch(points=[Point("A", 5)], input_dir="data", output_dir="out")
            save(b, path)
            restored = load(path)
            self.assertEqual(restored.points, b.points)
            self.assertEqual(restored.input_dir, str(Path(d) / "data"))
            data = json.loads(path.read_text())
            data["schema_version"] = 2
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load(path)
        for p in [
            Point("A", float("nan")),
            Point("../A", 5),
            Point("A", 5, 4),
            Point("CON", 5),
        ]:
            with self.assertRaises(ValueError):
                p.validate()
        for rules in [
            Rules(depth_weight=float("inf")),
            Rules(stop_count=0),
            Rules(stop_count=1.5),
        ]:
            with self.assertRaises(ValueError):
                rules.validate()

    def test_atomic_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "valid.csv"
            p.write_text("old")

            def broken(f):
                f.write("partial")
                raise OSError("test")

            with self.assertRaises(OSError):
                atomic_write(p, broken)
            self.assertEqual(p.read_text(), "old")
            self.assertEqual(list(Path(d).glob("*.tmp")), [])

    def test_isolation_cancel_retry_export(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            inp = root / "输入"
            inp.mkdir()
            f = inp / "A#.dat"
            f.write_text(
                HEADER
                + "\n".join(
                    f"{r['timestamp']:%H:%M:%S %m/%d/%Y} {r['depth']} 2 20 1 30 6"
                    for r in records()
                ),
                encoding="utf-8",
            )
            b = Batch(
                input_dir=str(inp),
                output_dir=str(root / "输出"),
                points=[
                    Point(
                        "A",
                        5,
                        files=[str(f)],
                        start="2025-12-31T23:00:00",
                        end="2026-01-01T02:00:00",
                    ),
                    Point("B", 5),
                ],
            )
            directory, report, _ = run(b)
            self.assertEqual(report["status"], "completed_with_errors")
            self.assertEqual(
                [p["status"] for p in report["points"]], ["completed", "failed"]
            )
            with (directory / "A#_output.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(v == "" for v in rows[-1][2:]))
            self.assertEqual(rows[2][2], "1.000")
            directory2, report, _ = run(b, cancelled=lambda: True)
            self.assertNotEqual(directory, directory2)
            self.assertEqual(report["status"], "cancelled")
            self.assertTrue(all(p["status"] == "not_started" for p in report["points"]))
            with self.assertRaises(ValueError):
                run(replace(b, output_dir=str(inp / "csv")))


if __name__ == "__main__":
    unittest.main()
