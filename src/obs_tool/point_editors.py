"""单测点编辑与多选批量编辑共用的水深草稿控件（`DepthEditor`）及分层方案选择器（`LayerChoice`）模块。

核心设计原则：
1. **水深原始精度保护（`DepthEditor`）**：未勾选“修改水深”或勾选后未改动初始显示值时，完整保留测点原有的高精度浮点数，不因 `QDoubleSpinBox` 的显示小数位数而发生截断；
2. **规则身份与默认推荐文字解耦（`LayerChoice`）**：当用户在上方修改水深草稿导致推荐层数变化时，仅更新下拉框中“系统规则：N层（默认）”的提示文本与底部层位预览，绝不强行改变用户当前选中的分层规则项。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .layer_editor import rule_name
from .models import standard_layer_count
from .rule_store import load_rules

if TYPE_CHECKING:
    from .models import Point


class DepthEditor(QWidget):
    """单测点与批量编辑共用的水深草稿编辑控件：支持“不修改”、“手动填写”与“处理时自动估算”三种状态。"""

    changed = Signal()

    def __init__(
        self, depth: float | None, parent: QWidget | None = None
    ) -> None:
        """保留原始水深 `depth` 及其十进制小数位数，初始化默认关闭的“修改水深”复选框与单选按钮组。"""
        super().__init__(parent)
        self.original = depth
        self.last_mode: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.change = QCheckBox("修改水深")
        layout.addWidget(self.change)

        manual_row = QHBoxLayout()
        manual_row.setContentsMargins(24, 0, 0, 0)
        self.manual = QRadioButton("手动填写")
        self.automatic = QRadioButton("处理时自动估算")

        self.group = QButtonGroup(self)
        self.group.addButton(self.manual)
        self.group.addButton(self.automatic)

        self.value = QDoubleSpinBox()
        decimal_exponent = Decimal(str(depth or 0)).as_tuple().exponent
        self.value.setDecimals(
            max(3, min(9, -decimal_exponent))
            if isinstance(decimal_exponent, int)
            else 3
        )
        self.value.setRange(0, 1_000_000)
        self.value.setSuffix(" 米")
        self.value.setSpecialValueText("请填写")
        self.value.setValue(depth if depth is not None else 0)
        self.initial_display = self.value.value()

        manual_row.addWidget(self.manual)
        manual_row.addWidget(self.value)
        manual_row.addStretch()
        layout.addLayout(manual_row)

        auto_row = QHBoxLayout()
        auto_row.setContentsMargins(24, 0, 0, 0)
        auto_row.addWidget(self.automatic)
        auto_row.addStretch()
        layout.addLayout(auto_row)

        self.change.toggled.connect(self.toggle_change)
        self.group.buttonToggled.connect(self.toggle_mode)
        self.value.valueChanged.connect(self.changed)
        self.reset_session()

    def clear_selection(self) -> None:
        """临时关闭单选按钮组的互斥约束，以便同时取消“手动填写”与“自动估算”的选中状态。"""
        self.group.setExclusive(False)
        self.manual.setChecked(False)
        self.automatic.setChecked(False)
        self.group.setExclusive(True)

    def reset_session(self) -> None:
        """重置“修改水深”开关与上次模式记忆，恢复进入对话框时的未修改初始状态。"""
        self.change.setChecked(False)
        self.last_mode = None
        self.clear_selection()
        self.manual.setEnabled(False)
        self.automatic.setEnabled(False)
        self.value.setEnabled(False)

    def toggle_change(self, enabled: bool) -> None:
        """切换“修改水深”启用状态；重新勾选时自动恢复同一次会话内上次选择的手动/自动模式。"""
        if not enabled:
            if self.group.checkedButton():
                self.last_mode = (
                    "automatic" if self.automatic.isChecked() else "manual"
                )
            self.clear_selection()
        self.manual.setEnabled(enabled)
        self.automatic.setEnabled(enabled)
        if enabled:
            (
                self.automatic
                if self.last_mode == "automatic"
                else self.manual
            ).setChecked(True)
        self.value.setEnabled(enabled and self.manual.isChecked())
        self.changed.emit()

    def toggle_mode(self, button: QRadioButton, checked: bool) -> None:
        """记录当前选中的手动或自动估算模式，并同步水深数值输入框的可用状态。"""
        if checked:
            self.last_mode = (
                "automatic" if button is self.automatic else "manual"
            )
        self.value.setEnabled(
            self.change.isChecked() and self.manual.isChecked()
        )
        self.changed.emit()

    def draft_depth(self, original: float | None) -> float | None:
        """计算当前草稿水深值：未勾选修改时返回 `original`，选择自动估算时返回 `None`，手动填写时返回输入值。"""
        if not self.change.isChecked():
            return original
        if self.automatic.isChecked():
            return None
        return (
            self.original
            if self.value.value() == self.initial_display
            and self.original is not None
            else self.value.value()
        )

    def changes(self) -> dict[str, float | None]:
        """导出待提交的字段变更字典（未勾选“修改水深”时返回空字典 `{}`）。"""
        return (
            {"depth": self.draft_depth(self.original)}
            if self.change.isChecked()
            else {}
        )


def layer_data(point: Point) -> dict[str, Any]:
    """深拷贝提取测点的三项分层规则标识字段（`layers`、`layer_positions`、`layer_rule_name`），防止共享可变列表。"""
    return dict(
        layers=point.layers,
        layer_positions=deepcopy(point.layer_positions),
        layer_rule_name=point.layer_rule_name,
    )


class LayerChoice(QWidget):
    """分层方案下拉选择与层位实时预览控件（支持系统自动分层、固定标准层数、规则库自定义规则及测点内嵌快照）。"""

    def __init__(
        self, points: list[Point], parent: QWidget | None = None
    ) -> None:
        """载入内置标准规则、本地自定义规则库及目标测点当前使用的规则快照，并默认选中首个测点的方案。"""
        super().__init__(parent)
        self.points = points
        self.depths: list[float | None] = [point.depth for point in points]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.combo = QComboBox()
        self.combo.addItem(
            "根据水深自动选择",
            dict(layers=None, layer_positions=None, layer_rule_name=None),
        )
        for std_count in (1, 2, 3, 5, 6):
            self.combo.addItem(
                f"系统规则：{std_count}层",
                dict(
                    layers=std_count,
                    layer_positions=None,
                    layer_rule_name=None,
                ),
            )
        for saved_rule in load_rules():
            self.combo.addItem(
                saved_rule["name"],
                dict(
                    layers=None,
                    layer_positions=deepcopy(saved_rule["positions"]),
                    layer_rule_name=saved_rule["name"],
                ),
            )
        for point in points:
            point_layer_spec = layer_data(point)
            if self.combo.findData(point_layer_spec) < 0:
                self.combo.addItem(
                    point.layer_rule_name
                    or (
                        rule_name(point.layer_positions)
                        if point.layer_positions is not None
                        else f"系统规则：{point.layers}层"
                    ),
                    point_layer_spec,
                )

        self.combo.setCurrentIndex(
            self.combo.findData(layer_data(points[0]))
        )
        layout.addWidget(self.combo)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setProperty("role", "muted")
        layout.addWidget(self.preview)

        self.combo.currentIndexChanged.connect(self.update_preview)
        self.set_depths(self.depths)

    def set_depths(self, depths: list[float | None]) -> None:
        """更新各测点的预览水深并刷新“系统规则：N层（默认）”后缀提示，不改变当前选中的规则项。"""
        self.depths = depths
        recommended_counts = {
            standard_layer_count(depth_val)
            if depth_val is not None and depth_val > 0
            else None
            for depth_val in depths
        }
        common_default_count = (
            next(iter(recommended_counts))
            if len(recommended_counts) == 1
            else None
        )
        for combo_idx, std_count in enumerate((1, 2, 3, 5, 6), 1):
            self.combo.setItemText(
                combo_idx,
                f"系统规则：{std_count}层"
                + ("（默认）" if std_count == common_default_count else ""),
            )
        self.update_preview()

    def changes(self) -> dict[str, Any]:
        """返回当前下拉框选中方案的深拷贝字段字典（`layers`、`layer_positions`、`layer_rule_name`）。"""
        return deepcopy(self.combo.currentData())

    def update_preview(self) -> None:
        """根据当前选中的分层方案与预览水深，实时生成底部灰字层位说明。"""
        selected_spec = self.changes()
        preview_texts: list[str] = []
        for point, preview_depth in zip(self.points, self.depths):
            if (
                selected_spec["layer_positions"] is None
                and selected_spec["layers"] is None
                and (preview_depth is None or preview_depth <= 0)
            ):
                plan_desc = "待水深确定后按规范选择层位"
            else:
                plan_desc = "、".join(
                    layer_item["label"]
                    for layer_item in replace(
                        point, depth=preview_depth, **selected_spec
                    ).layer_plan
                )
            if plan_desc not in preview_texts:
                preview_texts.append(plan_desc)

        self.preview.setText(
            "层位：" + preview_texts[0]
            if len(preview_texts) == 1
            else "自动分层：各测点按自己的水深确定层位。"
        )
