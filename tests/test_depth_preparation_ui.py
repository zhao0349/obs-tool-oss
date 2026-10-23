"""水深准备、确认接续和失败恢复的真实 Qt/子进程回归。"""

import os
import json
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from dat_samples import HEADER
from obs_tool.desktop import Window
from obs_tool.models import Point


class DepthPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                name: str(self.root / name)
                for name in (
                    "OBS_SETTINGS_DIR",
                    "OBS_JOB_DIR",
                    "OBS_RECORDS_DIR",
                    "OBS_LOG_DIR",
                )
            },
        )
        self.env.start()
        self.errors = []
        self.hook = patch.object(
            sys, "excepthook", lambda *error: self.errors.append(error)
        )
        self.hook.start()
        self.window = Window()
        self.window.output.setText(str(self.root / "out"))
        self.window.show()

    def tearDown(self):
        w = self.window
        if getattr(w, "depth_review", None):
            w.depth_review.reject()
        for worker in (w.analysis_worker, w.worker):
            if worker and worker.isRunning():
                worker.cancel.set()
                self.wait(lambda: not worker.isRunning())
        for widget in self.app.topLevelWidgets():
            widget.close()
        self.app.processEvents()
        self.hook.stop()
        self.env.stop()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def wait(self, condition):
        until = time.monotonic() + 15
        while not condition() and time.monotonic() < until:
            self.app.processEvents()
            QTest.qWait(10)
        self.app.processEvents()
        self.assertTrue(condition(), self.window.summary.text())

    def point(self, code, shape=(1,), hours=(0,), **kwargs):
        path = self.root / "in" / f"{code}.dat"
        path.parent.mkdir(exist_ok=True)
        lines = []
        for hour in hours:
            for i, depth in enumerate(shape):
                stamp = datetime(2026, 1, 1) + timedelta(hours=hour, seconds=i * 20)
                lines.append(f"{stamp:%H:%M:%S %m/%d/%Y} {depth} 2 20 1 30 6\n")
        path.write_text(HEADER + "".join(lines), encoding="utf-8")
        return Point(code, None, files=[str(path)], depth_source="pending", **kwargs)

    def start(self, points):
        w = self.window
        w.points = points
        w.populate()
        w.start_run(False)

    def review(self):
        self.wait(lambda: self.window.task_state == "等待水深确认")
        dialog = self.window.depth_review
        self.assertTrue(dialog.isVisible())
        self.assertFalse(self.window.controls.isEnabled())
        return dialog

    def idle(self):
        w = self.window
        self.assertFalse(
            any(
                worker and worker.isRunning()
                for worker in (w.analysis_worker, w.worker)
            )
        )
        self.assertTrue(w.controls.isEnabled())
        self.assertTrue(w.start.isEnabled())
        self.assertTrue(w.new_button.isEnabled())
        self.assertTrue(w.advanced_button.isEnabled())
        self.assertFalse(w.cancel.isVisible())

    def test_missing_depth_all_rule_modes_and_manual_continuation(self):
        for rules in ({}, {"layers": 3}, {"layer_positions": [0.2, 0.8]}):
            with self.subTest(rules=rules):
                p = self.point("A", **rules)
                self.start([p])
                dialog = self.review()
                self.assertEqual(dialog.inputs[0].value(), 0)
                self.assertTrue(dialog.inputs[0].isEnabled())
                dialog.validate_accept()
                self.assertTrue(dialog.isVisible())
                dialog.inputs[0].setValue(5)
                dialog.validate_accept()
                self.assertTrue(dialog.isVisible())
                self.assertEqual(dialog.inputs[0].value(), 5)
                dialog.checks[0].setChecked(True)
                with patch.object(
                    self.window,
                    "launch_processing",
                    wraps=self.window.launch_processing,
                ) as launch:
                    dialog.validate_accept()
                    self.assertEqual(launch.call_count, 1)
                self.wait(lambda: self.window.worker.ended)
                self.idle()
                current = self.window.points[0]
                self.assertEqual(
                    (current.point_id, current.files), (p.point_id, p.files)
                )
                self.assertEqual((current.depth, current.depth_source), (5, "manual"))
                self.assertTrue(current.depth_estimate["confirmed"])

    def test_mixed_batch_only_reviews_missing_and_uncertain(self):
        shape = [0, 1, 2, 3, 2, 1, 0]
        stable = self.point("stable", shape, (0, 1, 2))
        uncertain = self.point("uncertain", shape)
        missing = self.point("missing")
        manual = self.point("manual")
        manual.depth = 9
        manual.depth_source = "manual"
        self.start([stable, uncertain, missing, manual])
        dialog = self.review()
        self.assertEqual([p.code for p in dialog.points], ["uncertain", "missing"])
        self.assertGreater(dialog.inputs[0].value(), 0)
        self.assertEqual(dialog.inputs[1].value(), 0)
        dialog.inputs[1].setValue(4)
        for check in dialog.checks:
            check.setChecked(True)
        dialog.validate_accept()
        self.wait(lambda: self.window.worker.ended)
        self.idle()
        values = {p.code: p for p in self.window.points}
        self.assertEqual(
            (values["manual"].depth, values["manual"].depth_source), (9, "manual")
        )
        self.assertEqual(values["stable"].depth_source, "estimated")
        self.assertEqual(values["uncertain"].depth_source, "estimated")
        self.assertTrue(values["uncertain"].depth_estimate["confirmed"])
        self.assertEqual(values["missing"].depth_source, "manual")
        self.assertEqual(len(self.window.report["points"]), 4)

    def test_bad_required_file_does_not_prompt_or_block_other_points(self):
        bad = self.point("bad")
        broken = self.root / "in" / "bad-extra.dat"
        broken.write_text("invalid DAT")
        bad.files.append(str(broken))
        good = self.point("good")
        good.depth = 4
        good.depth_source = "manual"
        self.start([bad, good])
        self.wait(
            lambda: self.window.analysis_worker.ended
            and self.window.worker is not None
            and self.window.worker.ended
        )
        self.idle()
        self.assertFalse(getattr(self.window, "depth_review", None))
        statuses = {p["point"]: p["status"] for p in self.window.report["points"]}
        self.assertEqual(statuses, {"bad": "failed", "good": "completed"})
        self.assertNotIn(bad.point_id, self.window.results)

    def test_missing_required_file_is_isolated(self):
        bad = self.point("missing")
        Path(bad.files[0]).unlink()
        good = self.point("good")
        good.depth = 4
        good.depth_source = "manual"
        self.start([bad, good])
        self.wait(lambda: self.window.worker is not None and self.window.worker.ended)
        self.idle()
        self.assertFalse(getattr(self.window, "depth_review", None))
        self.assertEqual(
            {p["point"]: p["status"] for p in self.window.report["points"]},
            {"missing": "failed", "good": "completed"},
        )

    def test_read_and_resource_rejection_never_request_manual_override(self):
        from obs_tool.analysis_process import analyse_main
        from obs_tool.depth_workflow import requires_confirmation
        from dataclasses import replace

        p = self.point("bad")
        for error in (
            PermissionError("read denied"),
            ValueError("resource budget exceeded"),
        ):
            with self.subTest(error=error):
                job = self.root / "analysis"
                job.mkdir(exist_ok=True)
                (job / "request.json").write_text(
                    json.dumps({"points": [{"code": p.code, "files": p.files}]})
                )
                with patch("obs_tool.depth_workflow.analyse_files", side_effect=error):
                    analyse_main(job)
                info = json.loads((job / "result.json").read_text())["estimates"]["bad"]
                self.assertTrue(info["input_error"])
                self.assertIn(str(error), info["reason"])
                self.assertFalse(requires_confirmation(replace(p, depth_estimate=info)))

    def test_cancel_dialog_escape_close_and_retry_preserve_results(self):
        p = self.point("A")
        p.depth = 4
        p.depth_source = "manual"
        self.start([p])
        self.wait(lambda: self.window.worker.ended)
        history = deepcopy(
            (self.window.results, self.window.entries, self.window.report)
        )
        p.depth = None
        p.depth_source = "pending"
        for cancel in ("reject", "escape", "close", "button"):
            self.start([deepcopy(p)])
            dialog = self.review()
            if cancel == "escape":
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
            elif cancel == "button":
                self.window.request_cancel()
            else:
                getattr(dialog, cancel)()
            self.app.processEvents()
            self.idle()
            self.window.request_cancel()
            self.idle()
            self.assertEqual(
                (self.window.results, self.window.entries, self.window.report), history
            )
        self.window.new_batch()
        self.window.confirm_new.button(QMessageBox.StandardButton.Yes).click()
        self.app.processEvents()
        self.assertFalse(self.window.points)

    def test_analysis_cancellation_and_transition_exceptions_recover(self):
        p = self.point("A")
        self.start([p])
        self.window.request_cancel()
        self.wait(lambda: self.window.analysis_worker.ended)
        self.idle()
        for method in ("populate", "reset_results", "launch_processing"):
            with self.subTest(method=method):
                self.window.points = [deepcopy(p)]
                self.window.populate()
                if method == "launch_processing":
                    self.window.start_run(False)
                    dialog = self.review()
                    dialog.inputs[0].setValue(4)
                    dialog.checks[0].setChecked(True)
                    with patch.object(
                        self.window,
                        method,
                        side_effect=RuntimeError("transition failure"),
                    ):
                        dialog.validate_accept()
                else:
                    self.window.start_run(False)
                    with patch.object(
                        self.window,
                        method,
                        side_effect=RuntimeError("transition failure"),
                    ):
                        self.wait(lambda: self.window.analysis_worker.ended)
                self.idle()
                self.assertIn("transition failure", self.window.log.toPlainText())

    def test_worker_creation_start_errors_and_duplicate_start_recover(self):
        from obs_tool.analysis_worker import AnalysisWorker

        p = self.point("A")
        with patch(
            "obs_tool.desktop.AnalysisWorker",
            side_effect=OSError("analysis creation failure"),
        ):
            self.start([p])
        self.idle()
        self.start([p])
        dialog = self.review()
        with patch.object(self.window, "begin_analysis") as duplicate:
            self.window.start_run(False)
            duplicate.assert_not_called()
        dialog.reject()
        self.idle()
        p.depth = 4
        p.depth_source = "manual"
        with patch(
            "obs_tool.desktop.Worker", side_effect=OSError("worker creation failure")
        ):
            self.start([p])
        self.idle()
        with patch(
            "obs_tool.gui_components.Worker.start",
            side_effect=OSError("worker start failure"),
        ):
            self.start([p])
        self.idle()
        from obs_tool.gui_components import Worker

        original_start = Worker.start

        def started_then_failed(worker):
            original_start(worker)
            raise OSError("failure after process start")

        with patch.object(Worker, "start", started_then_failed):
            self.start([p])
            self.assertFalse(self.window.controls.isEnabled())
            self.assertEqual(self.window.task_state, "取消中")
            self.wait(lambda: self.window.worker.ended)
        self.idle()
        self.assertIsNone(self.window.pending_batch)
        p.depth = None
        p.depth_source = "pending"

        def failed_start(worker):
            worker.process.start(str(self.root / "missing-python.exe"), [])

        with patch.object(AnalysisWorker, "start", failed_start):
            self.start([p])
            self.wait(lambda: self.window.analysis_worker.ended)
        self.idle()


if __name__ == "__main__":
    unittest.main()
