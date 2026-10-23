"""多测点批量草稿编辑对话框模块（`BulkDialog`）。

交互与安全设计：
1. 复用单测点编辑器的水深编辑控件（`DepthEditor`）与分层方案选择器（`LayerChoice`）；
2. **显式勾选才修改**：水深、分层规则、开始时间、结束时间均默认处于“不更改”状态，仅当用户明确勾选启用某项时才纳入批量变更字典；
3. **原子校验提交**：点击确定时调用 `editing.apply_bulk()` 先在内存副本上对所有选中测点执行完整校验，任何一点不合法均在对话框底部展示错误提示，绝不产生“改了一半”的脏状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from PySide6.QtCore import QDateTime, Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .datetime_input import DateTimeInput
from .editing import apply_bulk
from .point_editors import DepthEditor, LayerChoice
from .theme import chinese_buttons, fit_window

if TYPE_CHECKING:
    from .models import Point


class BulkDialog(QDialog):
    """多选测点批量编辑对话框：仅提交用户明确勾选的修改项，经全量校验通过后发布 `self.updated`。"""

    def __init__(
        self,
        parent: QWidget | None,
        points: list[Point],
        rows: Sequence[int],
    ) -> None:
        """基于 `points` 中 `rows` 指定的选中测点初始化批量编辑草稿界面。"""
        super().__init__(parent)
        self.setWindowTitle("批量编辑测点")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.points = points
        self.rows = list(rows)
        self.updated: list[Point] | None = None
        fit_window(self, 760, 600)

        root_layout = QVBoxLayout(self)
        root_layout.addWidget(
            QLabel(
                f"当前已勾选 {len(rows)} 个测点。仅对下方明确勾选启用的项目进行统一修改，未启用项目保留各测点原有设置。"
            )
        )

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        form_container = QWidget()
        form_layout = QFormLayout(form_container)
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        scroll_area.setWidget(form_container)
        root_layout.addWidget(scroll_area, 1)

        first_selected_point = points[self.rows[0]]
        self.depth_editor = DepthEditor(first_selected_point.depth)
        self.change_depth = self.depth_editor.change
        self.depth = self.depth_editor.value
        form_layout.addRow(self.depth_editor)

        self.change_layers = QCheckBox("修改分层规则")
        self.layer_choice = LayerChoice([points[idx] for idx in self.rows])
        self.layer_choice.setEnabled(False)
        self.change_layers.toggled.connect(self.layer_choice.setEnabled)
        form_layout.addRow(self.change_layers, self.layer_choice)
        self.depth_editor.changed.connect(self.update_layer_depths)

        self.start_mode = QComboBox()
        self.end_mode = QComboBox()
        self.start = DateTimeInput()
        self.end = DateTimeInput()

        for mode_combo, datetime_widget, current_iso_val in [
            (self.start_mode, self.start, first_selected_point.start),
            (self.end_mode, self.end, first_selected_point.end),
        ]:
            mode_combo.addItems(["不更改", "设置时间", "不限定"])
            suggested_iso = current_iso_val or (
                first_selected_point.depth_estimate or {}
            ).get(
                "observed_end"
                if datetime_widget is self.end
                else "observed_start"
            )
            if suggested_iso:
                datetime_widget.setDateTime(
                    QDateTime.fromString(
                        suggested_iso.replace("T", " "), "yyyy-MM-dd HH:mm:ss"
                    )
                )

        form_layout.addRow("开始时间操作", self.start_mode)
        form_layout.addRow("开始日期时间", self.start)
        form_layout.addRow("结束时间操作", self.end_mode)
        form_layout.addRow("结束日期时间", self.end)
        self.start.setEnabled(False)
        self.end.setEnabled(False)
        self.start_mode.currentIndexChanged.connect(self.update_enabled)
        self.end_mode.currentIndexChanged.connect(self.update_enabled)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setWordWrap(True)
        root_layout.addWidget(self.error)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        chinese_buttons(button_box)
        button_box.accepted.connect(self.validate_accept)
        button_box.rejected.connect(self.reject)
        root_layout.addWidget(button_box)

    def showEvent(self, event) -> None:
        """每次以非系统自发方式重新打开窗口时，清除上一次提交结果并重置水深编辑会话状态。"""
        if not event.spontaneous():
            self.updated = None
            self.depth_editor.reset_session()
        super().showEvent(event)

    def done(self, result: int) -> None:
        """关闭批量编辑对话框时重置水深编辑器的会话标记。"""
        self.depth_editor.reset_session()
        super().done(result)

    def update_layer_depths(self) -> None:
        """当水深草稿数值或开关变化时，实时刷新下方分层方案预览中的理论层深（不直接修改原测点）。"""
        self.layer_choice.set_depths(
            [
                self.depth_editor.draft_depth(self.points[row_idx].depth)
                for row_idx in self.rows
            ]
        )

    @Slot()
    def update_enabled(self) -> None:
        """仅当时间操作下拉框选为“设置时间”（索引 1）时启用对应的日期时间输入控件。"""
        self.start.setEnabled(self.start_mode.currentIndex() == 1)
        self.end.setEnabled(self.end_mode.currentIndex() == 1)

    @Slot()
    def validate_accept(self) -> None:
        """汇总所有明确勾选的修改项并调用 `apply_bulk` 原子验证，通过后保存至 `self.updated` 并关闭对话框。"""
        candidate_changes = self.depth_editor.changes()
        if self.change_layers.isChecked():
            candidate_changes.update(self.layer_choice.changes())
        try:
            for field_key, mode_combo, datetime_widget, field_label in [
                ("start", self.start_mode, self.start, "开始"),
                ("end", self.end_mode, self.end, "结束"),
            ]:
                if mode_combo.currentIndex() == 1:
                    candidate_changes[field_key] = datetime_widget.value(field_label)
                elif mode_combo.currentIndex() == 2:
                    candidate_changes[field_key] = None
            self.updated = apply_bulk(self.points, self.rows, candidate_changes)
            self.accept()
        except ValueError as exc:
            self.error.setText(str(exc))
