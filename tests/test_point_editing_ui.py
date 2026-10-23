"""测点编辑的真实Qt控件、原子提交及入口集成回归。"""

import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QPoint, QDateTime
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from obs_tool.models import Point, Batch
from obs_tool.dialogs import PointDialog, AdvancedDialog
from obs_tool.bulk_dialog import BulkDialog
from obs_tool.desktop import Window
from obs_tool.rule_store import save_rules, load_rules
from obs_tool.theme import apply_theme


class PointEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OBS_SETTINGS_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close()
        self.app.processEvents()
        self.env.stop()
        self.tmp.cleanup()

    def show(self, dialog):
        dialog.show()
        self.app.processEvents()
        return dialog

    def click(self, button):
        QTest.mouseClick(
            button, Qt.MouseButton.LeftButton, pos=QPoint(8, button.height() // 2)
        )
        self.app.processEvents()

    def choose(self, choice, layers):
        index = next(
            i
            for i in range(choice.combo.count())
            if choice.combo.itemData(i)["layers"] == layers
            and choice.combo.itemData(i)["layer_positions"] is None
        )
        choice.combo.setCurrentIndex(index)

    def test_depth_session_table_in_both_dialogs(self):
        point = Point(
            "A", 2, depth_source="estimated", depth_estimate={"confidence": "高"}
        )
        for dialog in (PointDialog(None, point), BulkDialog(None, [point], [0])):
            with self.subTest(dialog=type(dialog).__name__):
                self.show(dialog)
                editor = dialog.depth_editor
                self.assertFalse(editor.change.isChecked())
                self.assertTrue(
                    all(
                        not w.isEnabled()
                        for w in (editor.manual, editor.automatic, editor.value)
                    )
                )
                self.assertFalse(
                    editor.manual.isChecked() or editor.automatic.isChecked()
                )
                self.click(editor.change)
                self.assertTrue(editor.manual.isChecked() and editor.value.isEnabled())
                editor.value.setValue(7)
                self.click(editor.automatic)
                self.assertFalse(editor.value.isEnabled())
                self.click(editor.change)
                self.assertFalse(
                    editor.manual.isChecked() or editor.automatic.isChecked()
                )
                self.assertFalse(
                    editor.manual.isEnabled() or editor.automatic.isEnabled()
                )
                self.click(editor.change)
                self.assertTrue(editor.automatic.isChecked())
                self.click(editor.manual)
                self.assertEqual(editor.value.value(), 7)
                self.click(editor.automatic)
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
                self.assertIsNone(editor.last_mode)
                self.show(dialog)
                self.assertFalse(editor.change.isChecked())
                self.click(editor.change)
                self.assertTrue(editor.manual.isChecked())
                dialog.close()

    def test_single_noop_legacy_rule_and_precision_preserved(self):
        point = Point(
            "A",
            2.12345678912,
            layers=3,
            layer_positions=[0.2, 0.8],
            layer_rule_name="旧规则",
            depth_source="estimated",
            depth_estimate={"confirmed": True},
        )
        original = deepcopy(point)
        dialog = self.show(PointDialog(None, point))
        dialog.change_depth.setChecked(True)
        dialog.depth.setValue(11)
        dialog.change_depth.setChecked(False)
        dialog.validate_accept()
        self.assertEqual(dialog.updated, original)
        self.assertEqual(point, original)
        self.assertEqual(dialog.updated.layer_rule_name, "旧规则")
        from obs_tool.processing import process
        from obs_tool.models import Rules
        from test_core import records

        self.assertEqual(
            process(records(), original, Rules()),
            process(records(), dialog.updated, Rules()),
        )

    def test_rule_choice_is_fixed_and_follows_draft_depth_only_for_recommendation(self):
        point = Point("A", 2)
        dialog = self.show(PointDialog(None, point))
        self.choose(dialog.layer_choice, 3)
        self.assertEqual(dialog.layer_choice.combo.currentData()["layers"], 3)
        dialog.change_depth.setChecked(True)
        dialog.depth.setValue(9)
        self.assertEqual(dialog.layer_choice.combo.currentData()["layers"], 3)
        self.assertIn("默认", dialog.layer_choice.combo.itemText(4))
        dialog.depth.setValue(2)
        self.choose(dialog.layer_choice, 2)  # 推荐项也明确保留固定意图。
        dialog.validate_accept()
        self.assertEqual((dialog.updated.depth, dialog.updated.layers), (2, 2))
        self.assertIsNotNone(dialog.updated.layers)

    def test_atomic_bulk_rules_depth_time_and_noncontiguous_selection(self):
        points = [Point("A", 2, layers=3), Point("B", 5), Point("C", 9, layers=6)]
        original = deepcopy(points)
        dialog = self.show(BulkDialog(None, points, [0, 2]))
        self.assertFalse(
            dialog.change_layers.isChecked() or dialog.layer_choice.isEnabled()
        )
        self.assertFalse(
            any(
                "默认" in dialog.layer_choice.combo.itemText(i)
                for i in range(dialog.layer_choice.combo.count())
            )
        )
        dialog.change_depth.setChecked(True)
        dialog.depth.setValue(6)
        dialog.change_layers.setChecked(True)
        self.choose(dialog.layer_choice, None)
        dialog.start_mode.setCurrentIndex(1)
        dialog.end_mode.setCurrentIndex(1)
        dialog.start.setDateTime(
            QDateTime.fromString("2026-01-02 00:00:01", "yyyy-MM-dd HH:mm:ss")
        )
        dialog.end.setDateTime(
            QDateTime.fromString("2026-01-01 00:00:01", "yyyy-MM-dd HH:mm:ss")
        )
        dialog.validate_accept()
        self.assertIsNone(dialog.updated)
        self.assertEqual(points, original)
        self.assertTrue(dialog.change_depth.isChecked())
        dialog.depth.setValue(0)
        dialog.end.setDateTime(
            QDateTime.fromString("2026-01-03 00:00:59", "yyyy-MM-dd HH:mm:ss")
        )
        dialog.validate_accept()
        self.assertIsNone(dialog.updated)
        dialog.depth.setValue(6)
        dialog.validate_accept()
        self.assertEqual(dialog.updated[1], original[1])
        self.assertEqual([p.depth for p in dialog.updated], [6, 5, 6])
        self.assertTrue(all(dialog.updated[i].layers is None for i in (0, 2)))
        self.assertEqual(dialog.updated[2].end, "2026-01-03T00:00:59")
        self.assertEqual(
            [p.point_id for p in dialog.updated], [p.point_id for p in points]
        )

    def test_bulk_restore_auto_uses_each_depth_and_auto_depth_ignores_manual_zero(self):
        points = [Point("A", 2, layers=3), Point("B", 9, layers=6)]
        dialog = self.show(BulkDialog(None, points, [0, 1]))
        dialog.change_layers.setChecked(True)
        self.choose(dialog.layer_choice, None)
        dialog.validate_accept()
        self.assertEqual([p.layer_count for p in dialog.updated], [2, 5])
        dialog = self.show(BulkDialog(None, points, [0, 1]))
        dialog.change_depth.setChecked(True)
        dialog.depth.setValue(0)
        dialog.depth_editor.automatic.setChecked(True)
        dialog.validate_accept()
        self.assertTrue(
            all(
                p.depth is None
                and p.depth_source == "pending"
                and p.depth_estimate is None
                for p in dialog.updated
            )
        )
        self.assertEqual([p.layers for p in dialog.updated], [3, 6])
        pending = self.show(PointDialog(None, Point("P", None)))
        self.assertFalse(
            any(
                "默认" in pending.layer_choice.combo.itemText(i)
                for i in range(pending.layer_choice.combo.count())
            )
        )
        pending.change_depth.setChecked(True)
        pending.depth.setValue(0)
        pending.validate_accept()
        self.assertIsNone(pending.updated)
        pending.depth_editor.automatic.setChecked(True)
        pending.validate_accept()
        self.assertIsNone(pending.updated.depth)

    def test_custom_rule_selection_and_single_invalid_draft_are_atomic(self):
        rule = {"name": "四层", "positions": [0.2, 0.4, 0.6, 0.8]}
        save_rules([rule])
        point = Point(
            "A", 2, depth_source="estimated", depth_estimate={"confidence": "高"}
        )
        original = deepcopy(point)
        dialog = self.show(PointDialog(None, point, {"B"}))
        dialog.layer_choice.combo.setCurrentIndex(
            dialog.layer_choice.combo.findText("四层")
        )
        dialog.change_depth.setChecked(True)
        dialog.depth.setValue(8)
        dialog.code.setText("B")
        dialog.validate_accept()
        self.assertIsNone(dialog.updated)
        self.assertEqual(point, original)
        self.assertTrue(dialog.change_depth.isChecked())
        dialog.code.setText("C")
        dialog.validate_accept()
        self.assertEqual((dialog.updated.code, dialog.updated.depth), ("C", 8))
        self.assertEqual(dialog.updated.layer_positions, rule["positions"])
        self.assertEqual(dialog.updated.layer_rule_name, "四层")
        self.assertEqual(dialog.updated.point_id, original.point_id)

    def test_window_entrypoints_update_main_table_and_preserve_cancel(self):
        window = self.show(Window())
        window.points = [Point("A", 2), Point("B", 7), Point("C", 9)]
        window.populate()
        window.table.setCurrentCell(0, 1)
        pos = window.table.visualItemRect(window.table.item(0, 1)).center()
        QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseDClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.app.processEvents()
        self.assertIsInstance(window.point_dialog, PointDialog)
        self.assertTrue(window.point_dialog.isVisible())
        self.choose(window.point_dialog.layer_choice, 3)
        window.point_dialog.validate_accept()
        self.assertEqual(window.points[0].layers, 3)
        self.assertIn("3层", window.table.item(0, 4).text())
        original = deepcopy(window.points)
        window.edit_button.click()
        self.choose(window.point_dialog.layer_choice, 6)
        window.point_dialog.reject()
        self.assertEqual(window.points, original)
        window.checked_ids = {window.points[0].point_id, window.points[2].point_id}
        window.populate()
        window.table.setCurrentCell(1, 1)  # 高亮行不决定批量对象。
        window.bulk_button.click()
        window.bulk_dialog.change_layers.setChecked(True)
        self.choose(window.bulk_dialog.layer_choice, 1)
        window.bulk_dialog.validate_accept()
        self.assertEqual([p.layers for p in window.points], [1, None, 1])
        self.assertIsNone(window.analysis_worker)

    def test_rule_management_protects_in_use_and_keeps_other_parameters(self):
        rule = {"name": "四层", "positions": [0.2, 0.4, 0.6, 0.8]}
        save_rules([rule])
        point = Point(
            "A", 2, layer_positions=rule["positions"], layer_rule_name=rule["name"]
        )
        batch = Batch(points=[point], encoding="gb18030", complete_rows_only=True)
        advanced = self.show(AdvancedDialog(None, batch, [point]))
        self.assertFalse(hasattr(advanced, "layer_table"))
        advanced.library.setCurrentCell(0, 0)
        advanced.delete_rule()
        self.assertIn("主界面", advanced.library_error.text())
        self.assertEqual(load_rules(), [rule])
        advanced.apply_settings()
        self.assertEqual(
            (advanced.rules, advanced.encoding, advanced.complete_rows_only),
            (batch.rules, "gb18030", True),
        )
        single = self.show(PointDialog(None, point))
        self.choose(single.layer_choice, None)
        single.validate_accept()
        advanced = self.show(AdvancedDialog(None, batch, [single.updated]))
        advanced.library.setCurrentCell(0, 0)
        advanced.delete_rule()
        self.assertEqual(load_rules(), [])
        self.assertEqual(point.layer_positions, rule["positions"])


if __name__ == "__main__":
    unittest.main()
