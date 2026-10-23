import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from dat_samples import HEADER
from obs_tool.time_correction import analyse, save


def casts(indices, start=datetime(1970, 1, 1), seconds=0):
    rows = []
    for i in indices:
        for j, depth in enumerate((0, 2, 4, 5, 4, 2, 0)):
            t = start + timedelta(hours=i, minutes=j, seconds=seconds * i)
            rows.append(f"{t:%H:%M:%S %m/%d/%Y} {depth} 2 20 1 30 6\r\n")
    return HEADER.replace("\n", "\r\n") + "".join(rows)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "原始 数据.dat"

    def tearDown(self):
        self.temp.cleanup()

    def known(self, wrong, correct):
        return analyse(
            self.source, dict(mode="known", instrument=wrong, correct=correct)
        )

    def fuzzy(self, indices, n=26, **parameters):
        self.source.write_bytes(casts(indices).encode())
        return analyse(
            self.source,
            dict(
                mode="casts",
                start="2026-01-01T00:00:00",
                end=(datetime(2026, 1, 1) + timedelta(hours=n - 1)).isoformat(),
                **parameters,
            ),
        )

    def test_known_positive_negative_zero_cross_year_and_exact_bytes(self):
        for shift in (3601, -3601, 0):
            with self.subTest(shift=shift):
                original = (
                    b"\xef\xbb\xbf" + casts([0], datetime(2024, 2, 29, 23)).encode()
                )
                self.source.write_bytes(original)
                wrong = datetime(2024, 2, 29, 23)
                plan = self.known(wrong, wrong + timedelta(seconds=shift))
                self.assertTrue(plan["valid"])
                self.assertEqual(self.source.read_bytes(), original)
                result = save(plan)
                self.assertEqual(Path(result["backup"]).read_bytes(), original)
                updated = self.source.read_bytes()
                old_lines = original.splitlines(keepends=True)
                new_lines = updated.splitlines(keepends=True)
                self.assertEqual(len(old_lines), len(new_lines))
                for old, new in zip(old_lines, new_lines):
                    self.assertEqual(old[19:], new[19:])
                with self.assertRaises(ValueError) if shift else self.subTest():
                    if shift:
                        save(plan)  # 不能对已校正源重试叠加。
        self.assertEqual(
            (self.root / "原始 数据.dat.bak").read_bytes(),
            b"\xef\xbb\xbf" + casts([0], datetime(2024, 2, 29, 23)).encode(),
        )

    def test_existing_backups_save_as_and_changed_source(self):
        self.source.write_text(casts([0]))
        original = self.source.read_bytes()
        bak = self.source.with_name(self.source.name + ".bak")
        bak.write_bytes(b"older")
        bak1 = self.source.with_name(self.source.name + ".bak.1")
        bak1.write_bytes(b"older1")
        plan = self.known("1970-01-01T00:00:00", "2026-01-01T00:00:00")
        result = save(plan)
        self.assertTrue(result["backup"].endswith(".bak.2"))
        self.assertEqual(bak.read_bytes(), b"older")
        self.assertEqual(bak1.read_bytes(), b"older1")
        self.source.write_bytes(original)
        target = self.root / "另存.dat"
        save(plan, target)
        self.assertEqual(self.source.read_bytes(), original)
        with self.assertRaises(ValueError):
            save(plan, target)
        self.source.write_bytes(original + b"\n")
        with self.assertRaises(ValueError):
            save(plan)

    def test_failures_preserve_source_and_cleanup(self):
        self.source.write_text(casts([0]))
        original = self.source.read_bytes()
        plan = self.known("1970-01-01T00:00:00", "2026-01-01T00:00:00")
        with patch(
            "obs_tool.time_correction.os.replace",
            side_effect=PermissionError("occupied"),
        ):
            with self.assertRaises(PermissionError):
                save(plan)
        self.assertEqual(self.source.read_bytes(), original)
        from types import SimpleNamespace

        with patch(
            "obs_tool.time_correction.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaisesRegex(OSError, "空间不足"):
                save(plan)
        with patch(
            "obs_tool.time_correction._copy",
            side_effect=OSError("disk full during backup"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                save(plan)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.source.with_name(self.source.name + ".bak.1").exists())
        self.assertEqual(
            self.source.with_name(self.source.name + ".bak").read_bytes(), original
        )
        self.assertFalse(list(self.root.glob(".obs-time-*")))
        self.assertFalse(list(self.root.glob("*.obs-time-lock")))
        with self.assertRaises(InterruptedError):
            save(plan, cancelled=lambda: True)
        self.assertEqual(self.source.read_bytes(), original)
        from obs_tool import time_correction

        real = time_correction.fingerprint

        def broken_read(path, *args, **kwargs):
            if Path(path) == self.source and self.source.read_bytes() != original:
                raise OSError("write verification failed")
            return real(path, *args, **kwargs)

        with patch("obs_tool.time_correction.fingerprint", side_effect=broken_read):
            with self.assertRaisesRegex(OSError, "已恢复原文件"):
                save(plan)
        self.assertEqual(self.source.read_bytes(), original)

    def test_reset_and_unparseable_time_refuse_write(self):
        self.source.write_text(casts([1, 0]))
        plan = self.known("1970-01-01T00:00:00", "2026-01-01T00:00:00")
        self.assertFalse(plan["valid"])
        self.assertTrue(plan["curve"])
        with self.assertRaises(ValueError):
            save(plan)
        self.source.write_text(casts([0]).replace("00:03:00", "99:03:00"))
        self.assertFalse(
            self.known("1970-01-01T00:00:00", "2026-01-01T00:00:00")["valid"]
        )

    def test_full_and_endpoint_allocations_and_middle_missing(self):
        p = self.fuzzy(range(26))
        self.assertTrue(p["valid"])
        self.assertEqual(p["problem_count"], 0)
        for head, tail in ((2, 0), (0, 2), (1, 1)):
            p = self.fuzzy(range(24), head=head, tail=tail)
            self.assertTrue(p["valid"])
            self.assertEqual(p["problem_count"], 2)
            self.assertEqual(p["matches"][head]["action"], 1)
            (
                self.assertIsNone(p["matches"][0]["action"])
                if head
                else self.assertEqual(p["matches"][0]["action"], 1)
            )
        p = self.fuzzy(range(24))
        self.assertFalse(p["valid"])
        self.assertTrue(p["needs_allocation"])
        p = self.fuzzy(range(24), assume_no_head=True)
        self.assertTrue(p["valid"])
        self.assertIn("不记得", p["assumption"])
        p = self.fuzzy([i for i in range(25) if i != 10], head=0, tail=1)
        self.assertEqual(p["middle_missing"], 1)
        self.assertEqual(p["end_missing_total"], 1)
        self.assertTrue(p["valid"])
        self.assertIsNone(p["matches"][10]["action"])
        p = self.fuzzy([i for i in range(24) if i != 10], head=1, tail=1)
        self.assertTrue(p["valid"])
        self.assertEqual(p["matches"][1]["action"], 1)
        self.assertIsNone(p["matches"][11]["action"])
        self.assertIsNone(p["matches"][-1]["action"])

    def test_quality_half_odd_boundary_extra_actions_and_tolerance(self):
        p = self.fuzzy(range(13), head=0, tail=13)
        self.assertFalse(p["valid"])
        self.assertEqual(p["problem_count"], 13)
        p = self.fuzzy(range(14), head=0, tail=12)
        self.assertTrue(p["valid"])
        self.assertTrue(self.fuzzy(range(3), n=5, head=0, tail=2)["valid"])
        self.assertFalse(self.fuzzy(range(2), n=5, head=0, tail=3)["valid"])
        self.assertFalse(self.fuzzy(range(27))["valid"])
        self.source.write_text(casts(range(3), seconds=15 * 60))
        params = dict(
            mode="casts", start="2026-01-01T00:00:00", end="2026-01-01T02:00:00"
        )
        p = analyse(self.source, params)
        self.assertEqual(p["problem_count"], 1)
        self.assertEqual(
            p["matches"][2]["residual_seconds"], 1800
        )  # 不逐段拉伸；仍按问题比例判定。
        self.source.write_text(casts(range(2), seconds=15 * 60))
        p = analyse(self.source, {**params, "end": "2026-01-01T01:00:00"})
        self.assertTrue(p["valid"])
        self.source.write_text(casts(range(2), seconds=15 * 60 + 1))
        self.assertFalse(
            analyse(self.source, {**params, "end": "2026-01-01T01:00:00"})["valid"]
        )
