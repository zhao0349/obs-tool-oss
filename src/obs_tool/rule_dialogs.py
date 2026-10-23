"""自定义分层规则新建对话框（`CreateRuleDialog`）与旧规则重命名迁移对话框（`MigrateNamesDialog`）模块。

设计说明：
1. `CreateRuleDialog`：自动生成下一个不重名的默认规则名（如 `自定义规则1`），内嵌 `LayerEditor` 供用户添加/移除层位比例，点击确定时通过 `rule_store.save_rules()` 原子写入本地规则库并发射 `saved` 信号；
2. `MigrateNamesDialog`：当加载旧版本规则库发现存在重名或与系统内置名称冲突时弹出，保留各规则原有的层位数组不变，仅引导用户修改冲突的规则名称。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .layer_editor import LayerEditor, position_label
from .models import validate_positions
from .rule_store import next_name, save_rules, validate_name
from .theme import chinese_buttons, fit_window, table_style


class CreateRuleDialog(QDialog):
    """创建并命名新自定义分层规则的模态对话框。"""

    saved = Signal(object)

    def __init__(
        self,
        parent: QWidget | None,
        rules: list[dict[str, Any]],
    ) -> None:
        """以当前已保存的规则列表 `rules` 作为重名校验基准，初始化规则名称输入框与层位编辑器。"""
        super().__init__(parent)
        self.rules = rules
        self.setWindowTitle("创建自定义规则")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        fit_window(self, 650, 510)

        root_layout = QVBoxLayout(self)
        root_layout.addWidget(QLabel("规则名称"))
        self.name = QLineEdit(next_name(rules))
        root_layout.addWidget(self.name)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setWordWrap(True)
        root_layout.addWidget(self.error)
        self.name.textChanged.connect(self.error.clear)

        self.editor = LayerEditor()
        self.editor.confirm_button.hide()
        root_layout.addWidget(self.editor)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        root_layout.addWidget(self.preview)

        self.editor.list.model().rowsInserted.connect(self.update_preview)
        self.editor.list.model().rowsRemoved.connect(self.update_preview)
        self.update_preview()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        chinese_buttons(self.buttons, "确定")
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        root_layout.addWidget(self.buttons)

    def update_preview(self, *_args: Any) -> None:
        """每当层位列表增删行时，实时刷新底部的层数与层位比例汇总预览。"""
        current_positions = self.editor.positions
        self.preview.setText(
            f"共 {len(current_positions)} 层 · "
            + "、".join(position_label(val) for val in current_positions)
        )

    def save(self) -> None:
        """校验规则名称唯一性与层位列表合法性，原子保存至规则库后发射 `saved` 信号并关闭窗口。"""
        try:
            clean_name = validate_name(self.name.text(), self.rules)
            current_positions = self.editor.positions
            validate_positions(current_positions)
            new_rule = dict(name=clean_name, positions=current_positions)
            save_rules([*self.rules, new_rule])
            self.saved.emit(new_rule)
            self.accept()
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
            self.name.setFocus()


class MigrateNamesDialog(QDialog):
    """修正历史规则库中重名或与系统内置名称冲突的规则名称迁移对话框（保留原有层位设置不变）。"""

    saved = Signal(object)

    def __init__(
        self,
        parent: QWidget | None,
        rules: list[dict[str, Any]],
    ) -> None:
        """以表格形式展示所有待迁移的自定义规则，第 1 列（名称）允许编辑，第 2 列（层位）锁定只读。"""
        super().__init__(parent)
        self.rules = deepcopy(rules)
        self.setWindowTitle("调整旧规则名称")
        fit_window(self, 680, 430)

        root_layout = QVBoxLayout(self)
        hint_label = QLabel(
            "部分规则名称重复，或与系统规则同名，请修改名称。修改名称不会改变层位设置。"
        )
        hint_label.setWordWrap(True)
        root_layout.addWidget(hint_label)

        self.table = QTableWidget(len(rules), 2)
        table_style(self.table)
        self.table.setHorizontalHeaderLabels(["名称（可修改）", "层位"])
        for row_idx, rule_dict in enumerate(rules):
            self.table.setItem(
                row_idx, 0, QTableWidgetItem(rule_dict["name"])
            )
            positions_item = QTableWidgetItem(
                "、".join(
                    position_label(pos) for pos in rule_dict["positions"]
                )
            )
            positions_item.setFlags(
                positions_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            self.table.setItem(row_idx, 1, positions_item)
        self.table.horizontalHeader().setStretchLastSection(True)
        root_layout.addWidget(self.table)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setWordWrap(True)
        root_layout.addWidget(self.error)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        chinese_buttons(button_box, "保存名称")
        button_box.accepted.connect(self.save)
        button_box.rejected.connect(self.reject)
        root_layout.addWidget(button_box)

    def save(self) -> None:
        """逐行校验修改后的规则名称无冲突且不为空，原子保存至规则库后发射 `saved` 信号。"""
        try:
            migrated_rules: list[dict[str, Any]] = []
            for row_idx, original_rule in enumerate(self.rules):
                new_name = validate_name(
                    self.table.item(row_idx, 0).text(), migrated_rules
                )
                migrated_rules.append(
                    dict(name=new_name, positions=original_rule["positions"])
                )
            save_rules(migrated_rules)
            self.saved.emit(migrated_rules)
            self.accept()
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
