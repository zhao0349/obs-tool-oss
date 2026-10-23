import json
import os
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from obs_tool.input_policy import (
    LIMITS,
    CapacityError,
    bounded_json,
    dat_in_folder,
    check_files,
)
from obs_tool.readers import read_files, DATFormatError
from obs_tool.runner import run
from obs_tool.models import Batch, Point

SENSORS = [
    ("Depth", "4"),
    ("OBS-1 (NTU)", "7"),
    ("Temperature (deg. C)", "20"),
    ("Salinity (PSU)", "30"),
]


def text_file(sensors=SENSORS, times=("00:00:00",), date="01/01/2026"):
    return (
        "OBS3A #0000 Firmware Version: 6.20\nSensor Data Columns\n"
        + "".join(f" {i+1} {name} Mean\n" for i, (name, value) in enumerate(sensors))
        + "".join(
            f"{t} {date} " + " ".join(v for n, v in sensors) + "\n" for t in times
        )
    )


class DATInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, content):
        p = self.root / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_each_file_maps_independently_optional_and_unknown_columns(self):
        a = self.file("a.dat", text_file())
        b = self.file(
            "b.dat",
            text_file(
                [
                    ("Extra sensor", "garbage"),
                    *reversed(SENSORS),
                    ("Battery (V)", "nan"),
                    ("Conductivity(mS/cm)", "bad"),
                ],
                times=("00:01:00",),
            ),
        )
        rows, di = read_files([a, b])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(
                [row[k] for k in ("depth", "turbidity", "temperature", "salinity")],
                [4, 7, 20, 30],
            )
            self.assertNotIn("conductivity", row)
        self.assertEqual(di.files[1]["mapping"][0], None)

    def test_instrument_trailers_keep_observations_and_csv(self):
        from dat_samples import HEADER

        samples = (
            (
                "16:11:31 09/22/2026 0.04 0.5 26.26 0.00 0.01 3.4\n"
                "16:11:39 09/22/2026 0.04 0.5 26.25 0.00 0.01 3.4\n"
                "16:11:47 09/22/2026 0.04 0.4 26.25 0.00 0.01 3.4\n"
                "16:11:55 09/22/2026 0.00 0.4 26.25 0.00 0.01 3.4\n",
                "00:00:00 01/01/1970 0.00 0.0 0.00 0.00 0.00 0.0\n 345780  Bytes read from file.\nOBS>",
            ),
            (
                "10:01:18 09/17/2026 0.00 2.0 24.51 1.17 0.58 3.8\n"
                "10:01:26 09/17/2026 0.00 1.8 24.52 1.05 0.52 3.8\n"
                "10:01:34 09/17/2026 0.00 2.0 24.49 1.07 0.53 3.8\n"
                "10:01:42 09/17/2026 0.00 2.0 24.47 1.05 0.52 3.8\n"
                "10:01:50 09/17/2026 0.00 2.0 24.48 1.05 0.52 3.8\n",
                "1 23:59:59 12/31/1969 0.00 0.0 0.00 0.00 0.00 0.0\nFile ended without EOF.\n 320310  Bytes read from file.\nOBS>",
            ),
        )
        inp = self.root / "input"
        inp.mkdir()
        for index, (body, trailer) in enumerate(samples):
            with self.subTest(index=index):
                p = inp / f"{index}.dat"
                p.write_text(HEADER + body)
                batch = Batch(
                    points=[Point("A", 5, files=[str(p)])],
                    output_dir=str(self.root / "out"),
                )
                before, _, _ = run(batch)
                original = (before / "A#_output.csv").read_bytes()
                p.write_text(HEADER + body + trailer)
                rows, diag = read_files([p])
                self.assertEqual(len(rows), len(body.splitlines()))
                self.assertEqual(diag.counts["instrument_end_record"], 1)
                self.assertEqual(diag.counts["instrument_notice"], 2 + index)
                self.assertEqual(
                    rows[-1]["line"], len(HEADER.splitlines()) + len(body.splitlines())
                )
                after, report, _ = run(batch)
                self.assertEqual(report["status"], "completed")
                self.assertEqual((after / "A#_output.csv").read_bytes(), original)
                from obs_tool.time_correction import analyse, save

                plan = analyse(
                    p,
                    dict(
                        mode="known",
                        instrument="2026-09-17T10:00:00",
                        correct="2026-09-17T11:00:00",
                    ),
                )
                self.assertTrue(plan["valid"])
                self.assertEqual(plan["records"], len(rows))
                source = p.read_bytes()
                result = save(plan)
                self.assertEqual(Path(result["backup"]).read_bytes(), source)
                self.assertTrue(
                    p.read_bytes().endswith(
                        source[source.index(trailer.splitlines()[0].encode()) :]
                    )
                )

    def test_end_marker_is_contextual_not_a_general_early_year_filter(self):
        for preserve in (False, True):
            p = self.file(
                "early.dat",
                text_file(times=("00:00:00", "00:00:01"), date="01/01/1970"),
            )
            rows, _ = read_files([p], allow_early=True, preserve_order=preserve)
            self.assertEqual(len(rows), 2)  # 非零的早年数据不被当作结束记录。
        zero = text_file(
            sensors=[(name, "0") for name, _ in SENSORS], date="01/01/1970"
        )
        p.write_text(zero + "00:00:01 01/01/1970 1 2 20 30\n")
        rows, _ = read_files([p], allow_early=True, preserve_order=True)
        self.assertEqual(len(rows), 2)  # 内部全零观测仍保留。
        p.write_text(text_file() + "00:00:00 01/01/1970 0 0 0 0\n\n")
        self.assertEqual(len(read_files([p], allow_early=True)[0]), 1)
        for tail in (
            "unexpected prose\n",
            "00:00:01 01/01/2026 1 2 3\n",
            "OBS>\nMZ\x00binary",
        ):
            p.write_text(text_file() + tail)
            with self.assertRaises(ValueError):
                read_files([p])

    def test_bad_structures_and_missing_each_required_field(self):
        bad = [
            "not OBS\n",
            b"\x89PNG\x00data",
            text_file().replace(" Mean", " Min Max", 1),
            text_file(SENSORS + [SENSORS[0]]),
            text_file().replace("OBS-1 (NTU)", "OBS-1 (FTU)"),
            text_file().replace("4 7 20 30", "4 7 20"),
        ]
        bad.extend(
            text_file([v for i, v in enumerate(SENSORS) if i != j]) for j in range(4)
        )
        for index, content in enumerate(bad):
            p = self.root / f"bad{index}.dat"
            p.write_bytes(content if isinstance(content, bytes) else content.encode())
            with self.subTest(index=index), self.assertRaises(ValueError):
                read_files([p])

    def test_file_count_line_and_record_limits_are_injectable(self):
        p = self.file("a.dat", text_file(times=("00:00:00", "00:00:01", "00:00:02")))
        for count in (2, 3, 4):
            with self.subTest(count=count):
                if count < 3:
                    with self.assertRaises(CapacityError):
                        read_files([p], limits=replace(LIMITS, file_records=count))
                else:
                    self.assertEqual(
                        len(
                            read_files([p], limits=replace(LIMITS, file_records=count))[
                                0
                            ]
                        ),
                        3,
                    )
        with self.assertRaises(CapacityError):
            read_files([p], limits=replace(LIMITS, header_lines=2))
        with self.assertRaises(CapacityError):
            read_files([p], limits=replace(LIMITS, line_bytes=16))
        self.assertEqual(len(read_files([p, p])[0]), 3)
        with self.assertRaises(CapacityError):
            read_files([p, p, p], limits=replace(LIMITS, selection_entries=2))
        with self.assertRaises(InterruptedError):
            read_files([p], lambda: True)

    def test_terabyte_metadata_refused_without_content_read(self):
        p = self.file("huge.dat", text_file())
        status = list(p.stat())
        status[6] = 1024**4
        with (
            patch(
                "obs_tool.input_policy.file_status", return_value=os.stat_result(status)
            ),
            patch.object(Path, "open", side_effect=AssertionError("must not read")),
        ):
            with self.assertRaises(CapacityError):
                read_files([p])

    def test_binary_with_forged_header_and_unbounded_garbage_refused(self):
        p = self.root / "binary.dat"
        p.write_bytes(text_file().encode() + b"\x00" * 12)
        with self.assertRaises(DATFormatError):
            read_files([p])
        p.write_text(text_file() + ("bad bad bad bad bad bad\n" * 10))
        with self.assertRaises(DATFormatError):
            read_files([p])
        p.write_text(text_file().replace("4 7 20 30", "4 7 nan 30"))
        with self.assertRaises(DATFormatError):
            read_files([p])

    def test_check_files_policy_enforcement(self):
        f1 = self.file("f1.dat", "content1")
        f2 = self.file("f2.dat", "content22")

        # Happy path with mixed str and Path
        checked = check_files([str(f1), f2])
        self.assertEqual(checked, [f1.resolve(), f2.resolve()])

        # Deduplication of repeated file path
        checked_dup = check_files([f1, str(f1), f2])
        self.assertEqual(checked_dup, [f1.resolve(), f2.resolve()])

        # Empty files list
        with self.assertRaises(ValueError) as cm:
            check_files([])
        self.assertIn("请至少选择一个 DAT 数据文件", str(cm.exception))

        # Non-regular file (directory)
        dir_path = self.root / "a_dir"
        dir_path.mkdir()
        with self.assertRaises(ValueError) as cm:
            check_files([dir_path])
        self.assertIn("不是可读取的常规文件", str(cm.exception))

        # Path length exceeded
        with patch.object(Path, "resolve", return_value=Path("/" + "a" * 32761)):
            with self.assertRaises(ValueError) as cm:
                check_files([f1])
            self.assertIn("文件路径过长", str(cm.exception))

        # Exceed max submitted file entries (before deduplication)
        with self.assertRaises(CapacityError) as cm:
            check_files([f1, f2], max_files=1)
        self.assertIn("提交的文件条目数超过 1", str(cm.exception))

        # Exceed single file byte limit
        with self.assertRaises(CapacityError) as cm:
            check_files([f1], limits=replace(LIMITS, file_bytes=3))
        self.assertIn("字节数超过 3", str(cm.exception))

        # Exceed total max_bytes limit
        with self.assertRaises(CapacityError) as cm:
            check_files([f1, f2], max_bytes=10)
        self.assertIn("输入总字节数超过 10", str(cm.exception))

        # Exceed result file count limit via limits.point_files
        with self.assertRaises(CapacityError) as cm:
            check_files([f1, f2], limits=replace(LIMITS, point_files=1))
        self.assertIn("文件数超过 1", str(cm.exception))

    def test_check_files_deduplication_hardlink(self):
        f1 = self.file("f1.dat", "content1")
        f2 = self.file("f2.dat", "content22")
        link = self.root / "f1_link.dat"
        if not hasattr(os, "link"):
            raise unittest.SkipTest("os.link is not supported on this platform")
        try:
            os.link(f1, link)
        except OSError as e:
            raise unittest.SkipTest(f"Hard links not supported on this filesystem: {e}")

        checked_dup = check_files([f1, link, f2])
        self.assertEqual(checked_dup, [f1.resolve(), f2.resolve()])

    def test_bounded_json_and_enumeration(self):
        p = self.file("record.json", "[[[[0]]]]")
        with self.assertRaises(CapacityError):
            bounded_json(p, limits=replace(LIMITS, json_depth=3))
        with self.assertRaises(CapacityError):
            bounded_json(p, max_bytes=3)
        for i in range(4):
            self.file(str(i) + ".txt", "a")
        with self.assertRaises(CapacityError):
            dat_in_folder(self.root, replace(LIMITS, directory_entries=3))

    def test_whole_point_fails_but_other_points_export(self):
        inp = self.root / "input"
        inp.mkdir()
        good = inp / "a.dat"
        good.write_text(text_file())
        bad = inp / "bad.dat"
        bad.write_bytes(b"MZ\x00fake")
        batch = Batch(
            points=[
                Point("A", 4, files=[str(good), str(bad)]),
                Point("B", 4, files=[str(good)]),
            ],
            output_dir=str(self.root / "out"),
        )
        directory, report, result = run(batch)
        self.assertEqual(report["status"], "completed_with_errors")
        self.assertFalse((directory / "A#_output.csv").exists())
        self.assertTrue((directory / "B#_output.csv").exists())
        self.assertIn("bad.dat", report["points"][0]["error"])

    def test_timespan_and_same_second_are_not_capacity_rules(self):
        p = self.file(
            "times.dat",
            text_file()
            + text_file().splitlines()[-1].replace("01/01/2026", "01/05/2026")
            + "\n",
        )
        self.assertEqual(len(read_files([p])[0]), 2)
        p.write_text(
            text_file()
            + text_file().splitlines()[-1].replace("4 7 20 30", "5 7 20 30")
            + "\n"
        )
        rows, diag = read_files([p])
        self.assertEqual(len(rows), 2)
        self.assertEqual(diag.counts["timestamp_conflict"], 1)

    def test_blank_header_and_shared_failed_input_budget(self):
        from obs_tool.input_policy import Budget

        p = self.file("blank.dat", "\n" * 8 + text_file())
        with self.assertRaises(CapacityError):
            read_files([p], limits=replace(LIMITS, header_lines=7))
        limits = replace(LIMITS, batch_records=2)
        total = Budget(limits)
        p.write_text(text_file(times=("00:00:00", "00:00:01")))
        read_files([p], limits=limits, budget=Budget(limits, parent=total))
        with self.assertRaises(CapacityError):
            read_files([p], limits=limits, budget=Budget(limits, parent=total))

    def test_reordered_columns_keep_csv_and_business_diagnostics(self):
        import json
        from obs_tool.ipc_reader import validate_result
        from obs_tool.exporters import atomic_json

        (self.root / "input").mkdir()
        a = self.file("input/a.dat", text_file(times=("00:00:00", "00:01:00")))
        b = self.file(
            "input/b.dat",
            text_file(list(reversed(SENSORS)), times=("00:00:00", "00:01:00")),
        )
        outcomes = []
        for i, source in enumerate((a, b)):
            directory, report, result = run(
                Batch(
                    points=[Point("A", 4, files=[str(source)])],
                    output_dir=str(self.root / f"out{i}"),
                )
            )
            outcomes.append(((directory / "A#_output.csv").read_bytes(), result))
            record = self.root / "result.json"
            atomic_json(
                record, dict(directory=str(directory), report=report, results=result)
            )
            data = json.loads(record.read_text("utf-8"))
            validate_result(data, {"A"})
            data["report"]["points"][0]["point"] = "forged"
            with self.assertRaises(ValueError):
                validate_result(data, {"A"})
        self.assertEqual(outcomes[0][0], outcomes[1][0])
        first, second = outcomes[0][1], outcomes[1][1]
        self.assertEqual(
            [r["status"] for r in first["A"]["rows"]],
            [r["status"] for r in second["A"]["rows"]],
        )
        self.assertEqual(first["A"]["parse_counts"], second["A"]["parse_counts"])
