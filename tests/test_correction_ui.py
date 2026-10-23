import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import QDateTime, Qt, QProcess
from obs_tool.desktop import Window
from obs_tool.tools_dialog import ToolsDialog
from obs_tool.models import Point
from obs_tool.native_picker import FilePicker
from obs_tool.theme import apply_theme
from obs_tool.time_correction import analyse
from test_time_correction import casts


class CorrectionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "OBS_SETTINGS_DIR": str(self.root / "settings"),
                "OBS_JOB_DIR": str(self.root / "jobs"),
            },
        )
        self.env.start()
        self.source = self.root / "待校正.dat"
        self.source.write_text(casts([0, 1, 2]))
        self.window = Window()
        self.window.points = [
            Point(
                "A",
                5,
                files=[str(self.source)],
                depth_source="estimated",
                depth_estimate={"confirmed": True},
            )
        ]
        self.window.populate()
        self.tool = ToolsDialog(self.window)
        self.tool.show()
        self.app.processEvents()

    def tearDown(self):
        if self.tool.busy():
            self.tool.cancel_task()
            self.wait(lambda: not self.tool.busy())
        for widget in self.app.topLevelWidgets():
            widget.close()
        self.app.processEvents()
        self.env.stop()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            self.app.processEvents()
            QTest.qWait(20)
            self.assertLess(time.monotonic(), deadline, "校正任务未结束")

    def choose(self):
        self.tool.tool_entry.click()
        self.assertEqual(self.tool.step, 1)
        self.tool.file_selected([str(self.source)])
        self.wait(lambda: not self.tool.busy())
        self.assertIsNotNone(self.tool.inspection, self.tool.status.text())
        self.tool.next_button.click()
        self.assertEqual(self.tool.step, 2)

    def plan(self, fuzzy=False, end="2026-01-01T02:00:00"):
        self.choose()
        if fuzzy:
            self.tool.mode.setCurrentIndex(1)
            self.tool.start.setDateTime(
                QDateTime.fromString("2026-01-01T00:00:00", "yyyy-MM-ddTHH:mm:ss")
            )
            self.tool.end.setDateTime(QDateTime.fromString(end, "yyyy-MM-ddTHH:mm:ss"))
        else:
            self.tool.correct.setDateTime(
                QDateTime.fromString("2026-01-01T00:00:00", "yyyy-MM-ddTHH:mm:ss")
            )
        self.tool.next_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertEqual(self.tool.step, 3, self.tool.status.text())

    def confirm(self):
        self.tool.next_button.click()
        self.assertEqual(self.tool.step, 4)
        self.wait(lambda: self.tool.save_armed)

    def test_full_wizard_cache_final_cancel_single_save_and_cache_invalidation(self):
        original = self.source.read_bytes()
        self.plan()
        self.assertEqual(self.source.read_bytes(), original)
        cached = self.tool.plan
        self.tool.previous.click()
        self.assertEqual(self.tool.step, 2)
        with patch.object(self.tool, "launch") as launch:
            self.tool.next_button.click()
            launch.assert_not_called()
        self.assertIs(self.tool.plan, cached)
        self.tool.next_button.click()
        self.assertEqual(self.tool.step, 4)
        # 双击与残留Enter不能从核对页跨到写入。
        self.tool.next_button.click()
        QTest.keyClick(self.tool, Qt.Key.Key_Return)
        self.assertFalse(self.tool.busy())
        self.assertEqual(self.source.read_bytes(), original)
        self.wait(lambda: self.tool.save_armed)
        QTest.keyClick(self.tool, Qt.Key.Key_Return)
        self.assertFalse(self.tool.busy())
        self.tool.previous.click()
        self.assertEqual(self.tool.step, 3)
        self.confirm()
        self.tool.next_button.click()
        self.tool.next_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertEqual(self.tool.step, 5, self.tool.status.text())
        self.assertEqual(
            self.source.with_name(self.source.name + ".bak").read_bytes(), original
        )
        self.assertFalse(self.source.with_name(self.source.name + ".bak.1").exists())
        saved = self.source.read_bytes()
        self.tool.go_next()
        self.tool.save_confirmed()
        self.assertEqual(self.source.read_bytes(), saved)
        self.assertIsNone(self.window.points[0].depth)
        self.tool.previous.click()
        self.assertEqual(self.tool.step, 0)
        self.tool.tool_entry.click()
        self.assertIsNone(self.tool.path)
        self.assertIsNone(self.tool.plan)

    def test_save_failure_save_as_cancel_new_target_needs_confirmation(self):
        self.plan()
        self.confirm()
        original = self.source.read_bytes()
        # 实际后台发现源内容已变，必须停在第4步，不能伪造完成。
        self.source.write_bytes(original + b"\n")
        self.tool.next_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertEqual(self.tool.step, 4)
        self.assertTrue(self.tool.save_as.isVisible())
        self.source.write_bytes(original)
        with patch("obs_tool.tools_dialog.FilePicker") as factory:
            picker = factory.return_value
            picker.isVisible.return_value = False
            self.tool.pick_save()
            self.assertTrue(factory.call_args.kwargs["save"])
        self.tool.picker = None
        self.tool.picker_finished()
        self.assertEqual(self.tool.step, 4)
        self.assertIsNotNone(self.tool.plan)
        self.tool.save_target_selected([str(self.source)])
        self.assertIsNone(self.tool.target)
        target = self.root / "另存.dat"
        self.tool.save_target_selected([str(target)])
        self.assertFalse(target.exists())
        self.assertEqual(self.source.read_bytes(), original)
        self.tool.go_next()
        self.assertFalse(self.tool.busy())
        # 另存目标在确认前被占用：失败仍留在第4步，已有内容保留。
        target.write_bytes(b"keep existing target")
        self.wait(lambda: self.tool.save_armed)
        self.tool.next_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertEqual(self.tool.step, 4)
        self.assertEqual(target.read_bytes(), b"keep existing target")
        target = self.root / "另存重试.dat"
        self.tool.save_target_selected([str(target)])
        self.wait(lambda: self.tool.save_armed)
        self.tool.next_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertEqual(self.tool.step, 5)
        self.assertTrue(target.exists())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.bak*")))

    def test_fuzzy_allocation_details_and_full_save(self):
        self.plan(True, end="2026-01-01T03:00:00")
        self.assertTrue(self.tool.plan["needs_allocation"])
        self.assertFalse(self.tool.next_button.isEnabled())
        self.assertTrue(self.tool.allocation.isVisible())
        self.assertFalse(self.tool.matches.isVisible())
        self.tool.unknown_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertTrue(self.tool.plan["valid"])
        self.assertFalse(self.tool.allocation.isVisible())
        self.assertIn("不记得", self.tool.review_text.text())
        self.tool.details_button.click()
        self.assertTrue(self.tool.matches.isVisible())
        self.assertGreaterEqual(self.tool.matches.height(), 224)
        self.tool.matches.setCurrentCell(1, 0)
        self.assertEqual(
            self.tool.curve.row["target"], self.tool.plan["matches"][1]["target"]
        )
        self.tool.edit_allocation.click()
        self.assertFalse(self.tool.next_button.isEnabled())
        self.assertIsNone(self.tool.plan)
        self.tool.head.setValue(1)
        self.tool.tail.setValue(0)
        self.tool.allocate_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertTrue(self.tool.plan["valid"])
        self.assertEqual(self.tool.plan["head_missing"], 1)
        self.confirm()
        self.tool.next_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertEqual(self.tool.step, 5)

    def test_changed_parameters_file_and_quality_failure_never_write(self):
        self.plan()
        original = self.source.read_bytes()
        self.tool.previous.click()
        self.tool.correct.time_edit.setText("01:00:00")
        self.assertIsNone(self.tool.plan)
        self.assertIsNone(self.tool.curve.result)
        self.tool.cancel_button.click()
        self.assertEqual(self.tool.step, 0)
        self.assertEqual(self.source.read_bytes(), original)
        self.plan(True, end="2026-01-01T06:00:00")
        self.tool.unknown_button.click()
        self.wait(lambda: not self.tool.busy())
        self.assertFalse(self.tool.plan["valid"])
        self.assertIsNotNone(self.tool.curve.result)
        self.assertFalse(self.tool.next_button.isEnabled())
        self.assertTrue(self.tool.previous.isEnabled())
        self.tool.previous.click()
        self.tool.previous.click()
        self.assertEqual(self.tool.step, 1)
        other = self.root / "其他.dat"
        other.write_text(casts([0]))
        self.tool.file_selected([str(other)])
        self.wait(lambda: not self.tool.busy())
        self.assertIsNone(self.tool.plan)
        self.assertFalse(self.tool.correct.dateTime().isValid())
        self.assertEqual(self.source.read_bytes(), original)

    def test_target_union_and_selection_move_the_actual_view(self):
        from datetime import datetime, timedelta

        self.plan(True, end="2026-01-01T04:00:00")
        self.tool.unknown_button.click()
        self.wait(lambda: not self.tool.busy())
        curve = self.tool.curve
        end = datetime(2026, 1, 1, 4)
        self.assertTrue(self.tool.plan["valid"])
        self.assertGreaterEqual(curve.browse[1], end)
        curve.set_view(datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 15))
        span = curve.view[1] - curve.view[0]
        self.tool.matches.setCurrentCell(4, 0)
        self.assertLessEqual(curve.view[0], end)
        self.assertGreaterEqual(curve.view[1], end)
        self.assertEqual(curve.view[1] - curve.view[0], span)
        self.assertGreater(curve.scroll.maximum(), 0)
        self.assertGreater(curve.scroll.value(), 0)
        self.tool.reset_curve.click()
        self.assertEqual(curve.view, curve.browse)
        self.assertEqual(curve.scroll.maximum(), 0)
        self.tool.edit_allocation.click()
        self.tool.head.setValue(2)
        self.tool.tail.setValue(0)
        self.tool.allocate_button.click()
        self.wait(lambda: not self.tool.busy())
        curve.set_view(end - timedelta(minutes=15), end)
        self.tool.matches.setCurrentCell(0, 0)
        self.assertLessEqual(curve.view[0], datetime(2026, 1, 1))
        self.assertGreaterEqual(curve.view[1], datetime(2026, 1, 1))

    def test_file_reselection_preserves_drafts_but_revokes_file_analysis(self):
        self.plan(True)
        self.tool.correct.setDateTime(
            QDateTime.fromString("2026-10-18T12:34:56", "yyyy-MM-ddTHH:mm:ss")
        )
        self.tool.start.setDateTime(
            QDateTime.fromString("2026-10-18T02:00:00", "yyyy-MM-ddTHH:mm:ss")
        )
        self.tool.end.setDateTime(
            QDateTime.fromString("2026-10-18T04:00:00", "yyyy-MM-ddTHH:mm:ss")
        )
        draft = [
            f.value("草稿") for f in (self.tool.correct, self.tool.start, self.tool.end)
        ]
        other = self.root / "新文件.dat"
        other.write_text(casts([4, 5]))
        for path in (self.source, other):
            self.tool.target = str(self.root / "old-target.dat")
            self.tool.head_confirmed = True
            self.tool.save_armed = True
            self.tool.file_selected([str(path)])
            self.wait(lambda: not self.tool.busy())
            self.assertEqual(
                [
                    f.value("草稿")
                    for f in (self.tool.correct, self.tool.start, self.tool.end)
                ],
                draft,
            )
            self.assertIsNone(self.tool.plan)
            self.assertIsNone(self.tool.target)
            self.assertFalse(self.tool.head_confirmed)
            self.assertFalse(self.tool.save_armed)
            self.assertIn("草稿", self.tool.status.text())
        self.assertEqual(self.tool.instrument.dateTime().time().hour(), 4)
        self.tool.cancel_button.click()
        self.tool.tool_entry.click()
        self.assertFalse(self.tool.correct.dateTime().isValid())
        self.assertFalse(self.tool.start.dateTime().isValid())

    def test_hour_only_controls_keep_minutes_seconds_static_and_common_precision(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent

        self.choose()
        self.tool.mode.setCurrentIndex(1)
        field = self.tool.start
        field.setDateTime(
            QDateTime.fromString("2026-10-18T07:00:00", "yyyy-MM-ddTHH:mm:ss")
        )
        before = field.value("开始")
        with self.assertRaises(ValueError):
            field.setDateTime(
                QDateTime.fromString("2026-10-18T07:34:56", "yyyy-MM-ddTHH:mm:ss")
            )
        self.assertEqual(field.value("开始"), before)
        field.time_edit.selectAll()
        QTest.keyClicks(field.time_edit, "12:34:56")
        self.assertNotIn(":", field.time_edit.text())
        self.assertEqual(field.hour_suffix.text(), ":00:00")
        clipboard = self.app.clipboard()
        old = clipboard.text()
        try:
            clipboard.setText("12:34:56")
            field.time_edit.setText("07")
            field.time_edit.selectAll()
            QTest.keyClick(
                field.time_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
            )
            self.assertEqual(field.time_edit.text(), "07")
            clipboard.setText("23")
            field.time_edit.selectAll()
            QTest.keyClick(
                field.time_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier
            )
            self.assertTrue(field.value("开始").endswith("23:00:00"))
        finally:
            clipboard.setText(old)
        field.time_button.click()
        popup = field.popup
        for column in popup.columns[1:]:
            QTest.keyClick(column, Qt.Key.Key_Down)
            event = QWheelEvent(
                QPointF(10, 10),
                QPointF(10, 10),
                QPoint(),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            self.app.sendEvent(column, event)
            self.assertEqual(column.value, 0)
            self.assertEqual(column.maximum, 0)
        popup.cancel.click()
        self.assertTrue(field.value("开始").endswith("23:00:00"))
        field.time_button.click()
        field.popup.columns[0].step(-1)
        field.popup.confirm.click()
        self.assertTrue(field.value("开始").endswith("22:00:00"))
        self.tool.correct.setDateTime(
            QDateTime.fromString("2026-10-18T12:34:56", "yyyy-MM-ddTHH:mm:ss")
        )
        self.tool.mode.setCurrentIndex(0)
        self.tool.correct.time_button.click()
        self.assertEqual(
            [c.value for c in self.tool.correct.popup.columns], [12, 34, 56]
        )
        self.tool.correct.popup.cancel.click()
        self.tool.mode.setCurrentIndex(1)
        self.assertTrue(self.tool.correct.value("正确").endswith("12:34:56"))

    def test_cancel_and_late_callbacks_do_not_replace_session(self):
        self.choose()
        self.tool.correct.setDateTime(
            QDateTime.fromString("2026-01-01T00:00:00", "yyyy-MM-ddTHH:mm:ss")
        )
        from PySide6.QtCore import QObject, Signal

        class FakeWorker(QObject):
            completed = Signal(object)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, request, parent):
                super().__init__(parent)
                self.saving = False
                self.ended = False

            def start(self):
                pass

            def cancel(self):
                pass

        with patch("obs_tool.tools_dialog.CorrectionWorker", FakeWorker):
            self.tool.next_button.click()
            worker = self.tool.worker
            self.tool.cancel_button.click()
            worker.completed.emit(analyse(self.source, dict(mode="inspect")))
            worker.ended = True
            worker.finished.emit()
        self.assertEqual(self.tool.step, 2)
        self.assertIsNone(self.tool.plan)
        old_status = self.tool.status.text()
        self.tool.cancel_button.click()
        worker.completed.emit({"invalid": "late"})
        self.assertEqual(self.tool.step, 0)
        self.assertIsNone(self.tool.plan)
        # 首页全栏含图标空白，Enter/空格均为一个按钮的激活。
        self.tool.tool_entry.setFocus()
        QTest.keyClick(self.tool.tool_entry, Qt.Key.Key_Return)
        self.assertEqual(self.tool.step, 1)
        self.tool.cancel_button.click()
        self.tool.tool_entry.setFocus()
        QTest.keyClick(self.tool.tool_entry, Qt.Key.Key_Space)
        self.assertEqual(self.tool.step, 1)

    def test_picker_cancel_start_failure_and_close_during_task(self):
        self.choose()
        picker = FilePicker(self.tool, "另存DAT", save=True)
        events = []
        picker.finished.connect(lambda: events.append("finished"))
        picker.rejected.connect(lambda: events.append("cancelled"))
        from obs_tool.exporters import atomic_json

        atomic_json(picker.job / "result.json", {"paths": []})
        picker.process_finished(0)
        picker.process_finished(0)
        picker.end()
        self.assertEqual(events, ["cancelled", "finished"])
        picker = FilePicker(self.tool, "另存DAT", save=True)
        errors = []
        picker.error.connect(errors.append)
        with patch.object(
            picker.process, "error", return_value=QProcess.ProcessError.FailedToStart
        ):
            picker.process_error()
        self.assertTrue(picker.ended)
        self.assertEqual(len(errors), 1)
        self.tool.correct.setDateTime(
            QDateTime.fromString("2026-01-01T00:00:00", "yyyy-MM-ddTHH:mm:ss")
        )
        self.tool.next_button.click()
        self.tool.close()
        self.wait(lambda: not self.tool.busy())
        self.app.processEvents()
        self.assertFalse(self.tool.isVisible())
        self.assertIsNone(self.tool.plan)
        self.tool.show()
        self.assertEqual(self.tool.step, 0)
        original = self.source.read_bytes()
        self.plan()
        self.confirm()
        self.tool.next_button.click()
        self.tool.close()
        self.wait(lambda: not self.tool.busy())
        self.wait(lambda: not self.tool.isVisible())
        self.assertEqual(
            self.source.with_name(self.source.name + ".bak").read_bytes(), original
        )
        self.assertNotEqual(self.source.read_bytes(), original)
