import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from dat_samples import HEADER
from obs_tool.readers import read_files
from obs_tool.depth import estimate_depth
from obs_tool.correction_actions import identify_actions, short_returns
from obs_tool.time_correction import analyse, save


def fixture(depths, hours=(0,), scale=1, offsets=None):
    offsets = offsets or [i * 20 for i in range(len(depths))]
    lines = []
    for hour in hours:
        for seconds, depth in zip(offsets, depths):
            t = datetime(1970, 1, 1) + timedelta(hours=hour, seconds=seconds)
            lines.append(f"{t:%H:%M:%S %m/%d/%Y} {depth*scale:g} 2 20 1 30 6\n")
    return HEADER + "".join(lines)


class CompleteActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "合成.dat"

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, text):
        self.path.write_text(text)
        return read_files([self.path], allow_early=True, preserve_order=True)[0]

    def plan(self, end="03:00:00"):
        return analyse(
            self.path,
            dict(mode="casts", start="2026-01-01T00:00:00", end="2026-01-01T" + end),
        )

    def test_fragment_chain_is_reproduced_then_grouped_without_hour_drift(self):
        shape = [0, 2, 3, 2, 1, 2, 4, 5, 4, 2, 0]
        for scale in (0.1, 1, 10):
            with self.subTest(scale=scale):
                rows = self.rows(fixture(shape, range(4), scale))
                original = self.path.read_bytes()
                old = estimate_depth(rows)
                self.assertEqual(
                    len(old["peaks"]), 8
                )  # 每小时两片，旧max(1,round(...))机械推进8小时。
                result = self.plan()
                self.assertTrue(result["valid"], result.get("reason"))
                self.assertEqual(
                    [a["line"] for a in result["actions"]],
                    [
                        len(HEADER.splitlines()) + hour * len(shape) + 8
                        for hour in range(4)
                    ],
                )
                self.assertEqual(
                    [a["candidate_ids"] for a in result["actions"]],
                    [[1, 2], [3, 4], [5, 6], [7, 8]],
                )
                self.assertEqual([r["action"] for r in result["matches"]], [1, 2, 3, 4])
                self.assertEqual(
                    [r["residual_seconds"] for r in result["matches"]], [0, 0, 0, 0]
                )
                self.assertEqual(estimate_depth(rows), old)  # 普通估深用途不变。
                self.assertEqual(self.path.read_bytes(), original)
                if scale == 1:
                    saved = save(result)
                    self.assertEqual(Path(saved["backup"]).read_bytes(), original)
                    corrected = read_files([self.path], preserve_order=True)[0]
                    self.assertEqual(
                        [r["depth"] for r in corrected], [r["depth"] for r in rows]
                    )
                    self.assertEqual(
                        [
                            (b["timestamp"] - a["timestamp"]).total_seconds()
                            for a, b in zip(rows, corrected)
                        ],
                        [result["delta_seconds"]] * len(rows),
                    )

    def test_independent_surface_returns_gaps_and_ambiguous_retractions(self):
        separate = [0, 2, 4, 5, 4, 2, 0, 0, 0, 2, 4, 5, 4, 2, 0]
        result = identify_actions(self.rows(fixture(separate)))
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual([a["candidate_ids"] for a in result["actions"]], [[1], [2]])
        self.assertFalse(self.plan("01:00:00")["valid"])
        shape = [0, 2, 3, 2, 1, 2, 4, 5, 4, 2, 0]
        offsets = [i * 20 + (360 if i >= 5 else 0) for i in range(len(shape))]
        self.assertEqual(
            len(
                identify_actions(self.rows(fixture(shape, offsets=offsets)))["actions"]
            ),
            2,
        )
        ambiguous = [0, 2, 3, 2, 1, 1, 1, 2, 4, 5, 4, 2, 0]
        self.rows(fixture(ambiguous))
        plan = self.plan("01:00:00")
        self.assertFalse(plan["valid"])
        self.assertTrue(plan["recognition"]["uncertainties"])
        self.assertEqual(plan["matches"][0]["action"], 1)  # 冲突不得覆盖首锚点。
        self.assertIn("分界不确定", " ".join(plan["matches"][0]["reasons"]))
        with self.assertRaises(ValueError):
            save(plan)

    def test_noise_nonuniform_sampling_tied_peak_and_missing_targets(self):
        for shape in (
            [0, 2, 4, 5, 4, 2, 0],
            [0, 2, 3, 2, 1, 2, 4, 3, 1, 3, 4, 5, 4, 2, 0],
            [0, 1, 2, 1.7, 2, 1.7, 2, 4, 5, 4, 2, 0],
        ):
            result = identify_actions(self.rows(fixture(shape)))
            self.assertEqual(len(result["actions"]), 1)
            self.assertEqual(result["actions"][0]["depth"], 5)
        shape = [0, 2, 3, 2, 1, 2, 4, 5, 5, 4, 2, 0]
        offsets = [0, 6, 17, 29, 41, 57, 84, 105, 130, 165, 180, 200]
        rows = self.rows(fixture(shape, (0, 2, 3), offsets=offsets))
        plan = self.plan("04:00:00")
        self.assertTrue(plan["needs_allocation"])
        plan = analyse(self.path, {**plan["parameters"], "head": 0, "tail": 1})
        self.assertTrue(plan["valid"])
        self.assertEqual(plan["middle_missing"], 1)
        self.assertEqual([r["action"] for r in plan["matches"]], [1, None, 2, 3, None])
        self.assertEqual(
            plan["actions"][0]["line"], len(HEADER.splitlines()) + 8
        )  # 等深取较早105秒。
        noisy = [0, 2, 4, 100, 4, 5, 4, 2, 0]
        result = identify_actions(self.rows(fixture(noisy)))
        self.assertEqual(result["actions"][0]["depth"], 5)
        with self.assertRaises(InterruptedError):
            identify_actions(rows, lambda: True)

    def test_local_envelopes_and_short_two_sample_returns(self):
        # 独立构造不同幅度的两次过程，不使用现场数据的时刻或数值。
        shape = [0, 0, 0, 2, 3, 3, 0.4, 0.6, 3, 4, 5, 5, 3, 0, 0, 0]
        text = fixture(shape, (0,), scale=0.2) + fixture(
            shape, (1,), scale=4
        ).removeprefix(HEADER)
        result = identify_actions(self.rows(text))
        self.assertEqual(
            [a["candidate_ids"] for a in result["actions"]], [[1, 2], [3, 4]]
        )
        self.assertEqual([a["depth"] for a in result["actions"]], [1, 20])
        self.assertFalse(result["uncertainties"])
        # 同样两条浅值若停留很久则仍为歧义，不借采样条数强合并。
        offsets = [i * 20 + (180 if i >= 7 else 0) for i in range(len(shape))]
        result = identify_actions(self.rows(fixture(shape, offsets=offsets)))
        self.assertEqual(len(result["actions"]), 2)
        self.assertTrue(result["uncertainties"])

    def test_single_shallow_return_requires_actual_duration_evidence(self):
        shape = [0, 2, 3, 2, 1, 2, 4, 5, 4, 2, 0]
        cases = [
            ([i * 20 for i in range(len(shape))], True),
            ([0, 20, 40, 60, 80, 360, 380, 400, 420, 440, 460], False),
            ([0, 20, 40, 60, 340, 360, 380, 400, 420, 440, 460], False),
            ([0, 20, 40, 60, 210, 360, 380, 400, 420, 440, 460], False),
        ]
        for offsets, valid in cases:
            with self.subTest(offsets=offsets):
                self.rows(fixture(shape, range(4), offsets=offsets))
                original = self.path.read_bytes()
                plan = self.plan()
                self.assertEqual(plan["valid"], valid)
                self.assertEqual(bool(plan["recognition"]["uncertainties"]), not valid)
                self.assertEqual(len(plan["actions"]), 4 if valid else 8)
                if valid:
                    self.assertEqual(
                        [a["candidate_ids"] for a in plan["actions"]],
                        [[1, 2], [3, 4], [5, 6], [7, 8]],
                    )
                else:
                    with self.assertRaises(ValueError):
                        save(plan)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_short_returns_duration_boundary_and_missing_side_evidence(self):
        def rows(seconds, depths):
            return [
                dict(timestamp=datetime(1970, 1, 1) + timedelta(seconds=s), depth=d)
                for s, d in zip(seconds, depths)
            ]

        for shallow in (1, 2):
            depths = [2, 2, 2] + [1] * shallow + [2, 2, 2]
            lows = list(range(3, 3 + shallow))
            for duration, expected in ((60, True), (61, False)):
                seconds = (
                    [0, 20, 40]
                    + [40 + duration * (i + 1) / (shallow + 1) for i in range(shallow)]
                    + [40 + duration, 60 + duration, 80 + duration]
                )
                with self.subTest(shallow=shallow, duration=duration):
                    self.assertEqual(
                        short_returns(rows(seconds, depths), lows, 2), expected
                    )
            # A return needs normal positive sampling intervals on both sides.
            regular = rows(range(0, 20 * len(depths), 20), depths)
            self.assertFalse(short_returns(regular[2:], [i - 2 for i in lows], 2))
            self.assertFalse(short_returns(regular[:-2], lows, 2))
            for row in regular[:3]:
                row["timestamp"] = regular[0]["timestamp"]
            self.assertFalse(short_returns(regular, lows, 2))
