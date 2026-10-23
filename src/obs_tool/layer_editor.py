"""相对层位标签格式化与自定义分层规则草稿编辑组件模块（`LayerEditor`）。

业务约定：
1. 层位比例 `0` 表示“表层”，`1` 表示“底层”，其余 `(0, 1)` 之间的数值表示为 `0.2H`、`0.6H` 等水深比例；
2. 用户逐个输入层位比例并添加到草稿列表时，组件自动按从浅到深（升序）排列，并拦截重复层位或超过 64 层的输入；
3. 点击“确定规则”时调用 `models.validate_positions()` 校验并发射 `ruleConfirmed` 信号，由外层对话框决定最终规则名称与持久化保存。
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .models import validate_positions


def position_label(value: float) -> str:
    """将相对层位比例数值（`0 ~ 1`）转换为可读中文标签（`0 -> 表层`，`1 -> 底层`，其余 -> `0.2H`）。"""
    return "表层" if value == 0 else "底层" if value == 1 else f"{value:g}H"


def rule_name(values: list[float]) -> str:
    """根据层位比例列表生成规则内容摘要描述（例如 `'3层：0.2H、0.6H、0.8H'`）。"""
    return f"{len(values)}层：" + "、".join(position_label(val) for val in values)


class LayerEditor(QWidget):
    """自定义分层规则层位比例列表草稿编辑器控件。"""

    ruleConfirmed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        """构建层位比例数值框、添加/移除/清空按钮、升序层位列表及确认规则按钮。"""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            QLabel("添加自定义规则：逐个输入层位，列表自动按从浅到深排列。")
        )

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("层位"))
        self.ratio = QDoubleSpinBox()
        self.ratio.setDecimals(3)
        self.ratio.setRange(0, 1)
        self.ratio.setSingleStep(0.1)
        self.ratio.setSuffix(" H")
        self.ratio.setValue(0.2)
        input_row.addWidget(self.ratio)

        self.add_button = QPushButton("添加到列表")
        self.add_button.clicked.connect(self.add_position)
        input_row.addWidget(self.add_button)

        self.remove_button = QPushButton("移除选中层位")
        self.remove_button.clicked.connect(self.remove_position)
        input_row.addWidget(self.remove_button)
        layout.addLayout(input_row)

        self.clear_button = QPushButton("清空层位")
        self.clear_button.clicked.connect(self.clear_draft)
        layout.addWidget(self.clear_button)

        self.warning = QLabel()
        self.warning.setProperty("role", "warning")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        usage_hint = QLabel(
            "输入 0 选择表层，输入 1 选择底层；其他数值表示水深比例，例如 0.2 表示 0.2H。"
        )
        usage_hint.setWordWrap(True)
        layout.addWidget(usage_hint)

        self.list = QListWidget()
        self.list.setMaximumHeight(110)
        layout.addWidget(self.list)

        self.message = QLabel()
        layout.addWidget(self.message)

        self.confirm_button = QPushButton("确定规则")
        self.confirm_button.clicked.connect(self.confirm_rule)
        layout.addWidget(self.confirm_button)

        self.ratio.valueChanged.connect(self.update_warning)
        self.update_warning(self.ratio.value())

    @property
    def positions(self) -> list[float]:
        """返回当前草稿列表中已添加的所有层位比例浮点数（按从浅到深升序排列）。"""
        return [
            self.list.item(row_idx).data(Qt.ItemDataRole.UserRole)
            for row_idx in range(self.list.count())
        ]

    @Slot(float)
    def update_warning(self, _value: float) -> None:
        """当层位比例输入值变化时清除旧提示信息（有效范围 `[0, 1]` 已由 `QDoubleSpinBox` 强约束）。"""
        self.warning.clear()

    def populate(self, values: Iterable[float]) -> None:
        """将层位比例集合按升序填入 `QListWidget`，并将原始浮点比例绑定在 `UserRole` 中。"""
        self.list.clear()
        for ratio_val in sorted(values):
            list_item = QListWidgetItem(position_label(ratio_val))
            list_item.setData(Qt.ItemDataRole.UserRole, ratio_val)
            self.list.addItem(list_item)

    @Slot()
    def add_position(self) -> None:
        """将当前输入框中的层位比例（保留 3 位小数）加入草稿列表，拦截重复项与超过 64 层的添加。"""
        candidate_val = round(self.ratio.value(), 3)
        current_values = self.positions
        if candidate_val in current_values:
            self.message.setText("该层位已在列表中")
            return
        if len(current_values) >= 64:
            self.message.setText("一个规则最多64个层位")
            return
        self.populate([*current_values, candidate_val])
        self.message.clear()

    @Slot()
    def remove_position(self) -> None:
        """从当前草稿列表中移除用户选中的层位条目。"""
        for selected_item in self.list.selectedItems():
            self.list.takeItem(self.list.row(selected_item))
        self.message.clear()

    def clear_draft(self) -> None:
        """清空当前编辑中的层位草稿列表（不触碰已持久化的规则库）。"""
        self.populate([])
        self.message.setText("层位已清空")

    @Slot()
    def confirm_rule(self) -> None:
        """调用 `validate_positions` 校验当前层位草稿，通过后发射 `ruleConfirmed` 信号。"""
        try:
            current_values = self.positions
            validate_positions(current_values)
            rule_payload = dict(
                name=rule_name(current_values), positions=current_values
            )
            self.message.setText(f"已确定 {len(current_values)} 层")
            self.ruleConfirmed.emit(rule_payload)
        except ValueError as exc:
            self.message.setText(str(exc))
