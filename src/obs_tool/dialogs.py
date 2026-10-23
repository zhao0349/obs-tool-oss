"""主工作区通用表单对话框模块（包含高级设置 `AdvancedDialog`、单点文件关联 `FileMappingDialog` 与单点配置编辑 `PointDialog`）。

交互与生命周期原则：
1. 所有对话框均采用非阻塞的 `WindowModal` + `open()` 模式启动，并由父窗口（`Window`）持有实例引用，防止局部变量被垃圾回收；
2. 用户在对话框内的修改均先作用于内存草稿；仅当点击“确定 / 应用设置”并通过完整领域规则校验后，才向主窗口发布更新结果；
3. 自定义分层规则库的增删与重命名迁移在 `AdvancedDialog` 的“分层规则”标签页内即时持久化至磁盘规则库（`rule_store.py`），并拦截删除当前正被工作区测点引用的规则。
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QDateTime, Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .editing import apply_bulk
from .layer_editor import position_label
from .models import Rules
from .native_picker import FilePicker
from .point_editors import DepthEditor, LayerChoice
from .rule_dialogs import CreateRuleDialog, MigrateNamesDialog
from .rule_store import load_rules, name_conflicts, save_rules
from .theme import chinese_buttons, fit_window, table_style

if TYPE_CHECKING:
    from .models import Batch, Point


class AdvancedDialog(QDialog):
    """高级设置对话框：管理标准/自定义分层规则库、分段与表层筛选算法参数、深度容差及 CSV 导出格式。"""

    def __init__(
        self,
        parent: QWidget | None,
        batch: Batch,
        points: list[Point],
    ) -> None:
        """使用当前批次配置 `batch` 初始化三大设置标签页（分层规则、筛选参数、CSV导出），并加载本地自定义规则库。"""
        super().__init__(parent)
        self.setWindowTitle("高级设置")
        fit_window(self, 900, 700)
        self.setWindowModality(Qt.WindowModality.WindowModal)

        self.rules = batch.rules
        self.original_rules = deepcopy(batch.rules)
        self.initial_values: dict[str, float | int] = {}
        self.force_default_fields = False
        self.encoding = batch.encoding
        self.complete_rows_only = batch.complete_rows_only
        self.fields: dict[str, QSpinBox | QDoubleSpinBox] = {}

        root_layout = QVBoxLayout(self)
        tabs = QTabWidget()
        self.tabs = tabs
        root_layout.addWidget(tabs)

        # -------------------- 标签页 1：分层规则与自定义规则库 --------------------
        layers_page = QWidget()
        layers_layout = QVBoxLayout(layers_page)
        layers_scroll = QScrollArea()
        layers_scroll.setWidgetResizable(True)
        layers_scroll.setWidget(layers_page)
        tabs.addTab(layers_scroll, "分层规则")

        layers_layout.addWidget(
            QLabel(
                "在此可查看系统标准分层规则，或创建、删除自定义分层规则。为测点选用规则请返回主界面通过“编辑当前测点”或“批量设置”指定。"
            )
        )

        standard_table = QTableWidget(5, 3)
        standard_table.setHorizontalHeaderLabels(["水深 H（米）", "采样层数", "层位"])
        standard_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row_idx, std_row in enumerate(
            [
                ("H < 2", 1, "0.6H"),
                ("2 ≤ H < 5", 2, "0.2H、0.8H"),
                ("5 ≤ H < 8", 3, "0.2H、0.6H、0.8H"),
                ("8 ≤ H < 11", 5, "表层、0.2H、0.6H、0.8H、底层"),
                ("H ≥ 11", 6, "表层、0.2H、0.4H、0.6H、0.8H、底层"),
            ]
        ):
            for col_idx, cell_val in enumerate(std_row):
                standard_table.setItem(
                    row_idx, col_idx, QTableWidgetItem(str(cell_val))
                )
        standard_table.resizeColumnsToContents()
        standard_table.horizontalHeader().setStretchLastSection(True)
        standard_table.setMaximumHeight(210)
        layers_layout.addWidget(standard_table)

        self.saved_rules = load_rules()
        self.points = deepcopy(points)
        self.create_dialog: CreateRuleDialog | None = None
        self.migration_dialog: MigrateNamesDialog | None = None

        library_toolbar = QHBoxLayout()
        library_hint = QLabel(
            "自定义分层规则库（新建或删除后立即保存；正在被当前测点使用的规则不可直接删除）："
        )
        library_hint.setWordWrap(True)
        library_toolbar.addWidget(library_hint, 1)

        self.create_button = QPushButton("创建自定义规则")
        self.create_button.clicked.connect(self.create_rule)
        library_toolbar.addWidget(self.create_button)

        self.delete_button = QPushButton("删除自定义规则")
        self.delete_button.clicked.connect(self.delete_rule)
        library_toolbar.addWidget(self.delete_button)
        layers_layout.addLayout(library_toolbar)

        self.library = QTableWidget(0, 3)
        table_style(self.library)
        self.library.setHorizontalHeaderLabels(["名称", "层数", "层位"])
        self.library.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.library.setMaximumHeight(145)
        self.library.horizontalHeader().setStretchLastSection(True)
        self.library.itemSelectionChanged.connect(self.library_selection)
        layers_layout.addWidget(self.library)

        self.empty_rules = QLabel("暂无自定义规则")
        layers_layout.addWidget(self.empty_rules)

        self.library_error = QLabel()
        self.library_error.setProperty("role", "error")
        self.library_error.setWordWrap(True)
        layers_layout.addWidget(self.library_error)

        self.migrate_button = QPushButton("调整旧规则名称")
        self.migrate_button.clicked.connect(self.migrate_names)
        layers_layout.addWidget(self.migrate_button)
        self.refresh_library()

        # -------------------- 标签页 2：分段、整点匹配、表层评分与质量检查 --------------------
        processing_page = QWidget()
        processing_layout = QVBoxLayout(processing_page)
        processing_scroll = QScrollArea()
        processing_scroll.setWidgetResizable(True)
        processing_scroll.setWidget(processing_page)
        tabs.addTab(processing_scroll, "筛选参数")

        parameter_specs = [
            (
                "gap_minutes",
                "片段断档上限（分钟）",
                0.001,
                "相邻有效观测的时间间隔超过此值时，划分为不同的观测片段。",
            ),
            (
                "pre_minutes",
                "前缓冲（分钟）",
                0,
                "将观测片段正式开始前该时长内的观测一并纳入候选范围。",
            ),
            (
                "tail_minutes",
                "后缓冲上限（分钟）",
                0,
                "限制观测片段末尾已纳入的低深度观测保留时长（不会向后读取片段结束后的未来数据）。",
            ),
            (
                "stop_count",
                "结束所需连续低值（个）",
                1,
                "深度回落阶段连续出现达到该数量的低于分段阈值的观测时，结束当前观测片段。",
            ),
            (
                "max_offset_minutes",
                "整点与片段峰值的最大时间差（分钟）",
                0,
                "观测片段最深峰值时刻与目标整点的时间差不超过此值（含边界）时，方可匹配该整点。",
            ),
            (
                "min_depth",
                "最低参考深度（米）",
                0,
                "识别观测片段时的深度阈值下限（并非测点总水深 H）。",
            ),
            (
                "surface_min",
                "表层深度下限（米，严格大于）",
                0,
                "表层候选观测点的实测深度必须严格大于此值。",
            ),
            (
                "depth_weight",
                "表层评分：深度权重",
                0,
                "数值越大，表层选点时越优先选取深度较浅的观测记录。",
            ),
            (
                "hour_weight",
                "表层评分：距整点时间权重",
                0,
                "数值越大，表层选点时越优先选取采样时刻更接近目标整点的观测记录。",
            ),
            (
                "entry_weight",
                "表层评分：距片段起点权重",
                0,
                "数值越大，表层选点时越优先选取更靠近观测片段起点的观测记录。",
            ),
        ]
        group_forms: dict[str, QFormLayout] = {}
        group_category_map = {
            "gap_minutes": "片段识别",
            "pre_minutes": "片段识别",
            "tail_minutes": "片段识别",
            "stop_count": "片段识别",
            "min_depth": "片段识别",
            "max_offset_minutes": "整点匹配",
        }
        for param_key, param_label, min_val, tooltip_text in parameter_specs:
            group_title = group_category_map.get(param_key, "表层选择")
            if group_title not in group_forms:
                group_box = QGroupBox(group_title)
                group_form = QFormLayout(group_box)
                group_form.setRowWrapPolicy(
                    QFormLayout.RowWrapPolicy.DontWrapRows
                )
                group_form.setFieldGrowthPolicy(
                    QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
                )
                group_forms[group_title] = group_form
                processing_layout.addWidget(group_box)

            target_form = group_forms[group_title]
            spin_widget = (
                QSpinBox() if param_key == "stop_count" else QDoubleSpinBox()
            )
            if isinstance(spin_widget, QDoubleSpinBox):
                spin_widget.setDecimals(4)
            spin_widget.setRange(min_val, 1_000_000)
            spin_widget.setValue(getattr(batch.rules, param_key))
            spin_widget.setToolTip(tooltip_text)
            self.fields[param_key] = spin_widget
            self.initial_values[param_key] = spin_widget.value()

            label_widget = QLabel(param_label)
            label_widget.setWordWrap(True)
            label_widget.setMinimumWidth(280)
            label_widget.setMaximumWidth(320)
            label_widget.setToolTip(tooltip_text)
            target_form.addRow(label_widget, spin_widget)

        quality_group = QGroupBox("质量检查")
        quality_form = QFormLayout(quality_group)
        quality_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        quality_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )
        processing_layout.addWidget(quality_group)

        self.absolute_check = QCheckBox("启用绝对深度容差")
        self.absolute = QDoubleSpinBox()
        self.absolute.setRange(0, 1_000_000)
        self.absolute.setDecimals(4)
        self.absolute.setSuffix(" 米")

        self.relative_check = QCheckBox("启用相对深度容差")
        self.relative = QDoubleSpinBox()
        self.relative.setRange(0, 100_000)
        self.relative.setDecimals(3)
        self.relative.setSuffix(" %")

        self.absolute_check.setChecked(batch.rules.tolerance_m is not None)
        self.absolute.setValue(batch.rules.tolerance_m or 0)
        self.relative_check.setChecked(batch.rules.tolerance_ratio is not None)
        self.relative.setValue((batch.rules.tolerance_ratio or 0) * 100)

        self.initial_absolute = self.absolute.value()
        self.initial_relative = self.relative.value()
        self.absolute.setEnabled(self.absolute_check.isChecked())
        self.relative.setEnabled(self.relative_check.isChecked())
        self.absolute_check.toggled.connect(self.absolute.setEnabled)
        self.relative_check.toggled.connect(self.relative.setEnabled)
        quality_form.addRow(self.absolute_check, self.absolute)
        quality_form.addRow(self.relative_check, self.relative)

        tolerance_note = QLabel(
            "说明：相对深度容差按当前目标层深度的百分比计算（例如目标层深度为 2 米、设置 10% 容差时，允许偏差 0.2 米）。两种容差同时启用时采用更严格标准。超出允许偏差的采样值仍会导出至 CSV，并在结果页标记“深度超差”供复核；未启用时不检查深度偏差是否超限。"
        )
        tolerance_note.setWordWrap(True)
        quality_form.addRow(tolerance_note)

        # -------------------- 标签页 3：CSV 导出编码与完整行过滤 --------------------
        export_page = QWidget()
        export_form = QFormLayout(export_page)
        export_scroll = QScrollArea()
        export_scroll.setWidgetResizable(True)
        export_scroll.setWidget(export_page)
        tabs.addTab(export_scroll, "CSV导出")

        self.encoding_combo = QComboBox()
        for label_text, codec_name in [
            ("UTF-8（推荐，Excel可识别）", "utf-8-sig"),
            ("GB18030", "gb18030"),
            ("Windows ANSI", "mbcs"),
        ]:
            self.encoding_combo.addItem(label_text, codec_name)
        self.encoding_combo.setCurrentIndex(
            ["utf-8-sig", "gb18030", "mbcs"].index(batch.encoding)
        )

        self.legacy = QCheckBox("仅导出各层数据齐全的整点")
        self.legacy.setChecked(batch.complete_rows_only)
        self.legacy.setToolTip(
            "勾选后，缺少任一层采样值的整点将整行不写入 CSV（软件结果页仍保留完整记录供核查）。"
        )
        export_form.addRow("CSV编码", self.encoding_combo)
        export_form.addRow(self.legacy)
        export_form.addRow(
            QLabel(
                "说明：导出的各层深度、温度、盐度、浊度均直接取自选中的原始观测记录（保留三位小数，不做插值或平均；缺测项留空，不填为 0）。"
            )
        )

        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        root_layout.addWidget(self.error_label)

        bottom_bar = QHBoxLayout()
        reset_page_btn = QPushButton("恢复当前页默认")
        reset_page_btn.clicked.connect(self.reset_current_page)
        bottom_bar.addWidget(reset_page_btn)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText("应用设置")
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        button_box.accepted.connect(self.apply_settings)
        button_box.rejected.connect(self.reject)
        bottom_bar.addWidget(button_box)
        root_layout.addLayout(bottom_bar)

    def refresh_library(self) -> None:
        """将内存中的自定义分层规则列表渲染至规则库表格，并检测是否存在需要重命名迁移的旧规则冲突。"""
        self.library.setRowCount(len(self.saved_rules))
        for row_idx, rule_dict in enumerate(self.saved_rules):
            for col_idx, cell_str in enumerate(
                [
                    rule_dict["name"],
                    str(len(rule_dict["positions"])),
                    "、".join(
                        position_label(pos) for pos in rule_dict["positions"]
                    ),
                ]
            ):
                self.library.setItem(
                    row_idx, col_idx, QTableWidgetItem(cell_str)
                )
        self.empty_rules.setVisible(not self.saved_rules)
        self.library.setVisible(bool(self.saved_rules))
        self.library_selection()

        conflicting_indices = name_conflicts(self.saved_rules)
        self.migrate_button.setVisible(bool(conflicting_indices))
        self.create_button.setEnabled(not conflicting_indices)
        self.library_error.setText(
            "旧规则名称有冲突，请调整名称后继续；现有规则与层位已保留。"
            if conflicting_indices
            else ""
        )

    def library_selection(self) -> None:
        """根据自定义规则库表格当前是否有选中行，更新“删除自定义规则”按钮的可用状态。"""
        self.delete_button.setEnabled(self.library.currentRow() >= 0)

    def create_rule(self) -> None:
        """弹出 `CreateRuleDialog` 新建自定义分层规则窗口。"""
        if self.create_dialog and self.create_dialog.isVisible():
            return
        self.create_dialog = CreateRuleDialog(self, self.saved_rules)
        self.create_dialog.saved.connect(self.rule_saved)
        self.create_dialog.open()

    def rule_saved(self, rule: dict[str, Any]) -> None:
        """当新建规则保存成功后，追加至内存列表并刷新规则库表格与提示信息。"""
        self.saved_rules.append(rule)
        self.refresh_library()
        self.library_error.setText(
            "自定义规则已保存至规则库，可返回主界面在编辑测点或批量设置时选用。"
        )

    def delete_rule(self) -> None:
        """从自定义规则库删除选中的规则；若当前工作区有测点正在使用该规则则拦截并提示先切换。"""
        selected_row = self.library.currentRow()
        if selected_row < 0:
            return
        target_rule = self.saved_rules[selected_row]
        referencing_point_ids = {
            point.point_id
            for point in self.points
            if point.layer_rule_name == target_rule["name"]
            and point.layer_positions == target_rule["positions"]
        }
        if referencing_point_ids:
            self.library_error.setText(
                f"该规则当前正在被 {len(referencing_point_ids)} 个测点使用，请先回到主界面将这些测点切换为其他分层规则并保存，再重新打开高级设置删除此规则。"
            )
            return
        try:
            remaining_rules = [
                rule_item
                for idx, rule_item in enumerate(self.saved_rules)
                if idx != selected_row
            ]
            save_rules(remaining_rules)
            self.saved_rules = remaining_rules
            self.refresh_library()
        except (ValueError, OSError) as exc:
            self.library_error.setText(str(exc))

    def migrate_names(self) -> None:
        """弹出 `MigrateNamesDialog` 引导用户解决旧版本自定义规则的名称冲突。"""
        self.migration_dialog = MigrateNamesDialog(self, self.saved_rules)
        self.migration_dialog.saved.connect(self.names_migrated)
        self.migration_dialog.open()

    def names_migrated(self, migrated_rules: list[dict[str, Any]]) -> None:
        """规则名称冲突解决后，同步更新当前工作区中引用了对应旧规则且层位一致的测点规则名。"""
        previous_rules = self.saved_rules

        def _sync_point_rule_name(point: Point) -> Point:
            """仅当测点的旧规则名与层位数组均与迁移前条目完全一致时，同步替换为新规则名称。"""
            for old_rule, new_rule in zip(previous_rules, migrated_rules):
                if (
                    point.layer_rule_name == old_rule["name"]
                    and point.layer_positions == old_rule["positions"]
                ):
                    return replace(point, layer_rule_name=new_rule["name"])
            return point

        self.points = [_sync_point_rule_name(point) for point in self.points]
        parent_win = self.parentWidget()
        if hasattr(parent_win, "points"):
            parent_win.points = [
                _sync_point_rule_name(point) for point in parent_win.points
            ]
            parent_win.populate()
        self.saved_rules = migrated_rules
        self.refresh_library()

    @Slot()
    def reset_current_page(self) -> None:
        """仅将当前激活标签页中的参数控件恢复为程序出厂默认值（不自动关闭对话框）。"""
        if self.tabs.currentIndex() == 0:
            self.library_error.setText(
                "系统分层规则为内置规则；已保存的自定义规则保持不变。"
            )
        elif self.tabs.currentIndex() == 1:
            self.force_default_fields = True
            default_rules = Rules()
            for param_key, spin_widget in self.fields.items():
                spin_widget.setValue(getattr(default_rules, param_key))
            self.absolute_check.setChecked(False)
            self.relative_check.setChecked(False)
        else:
            self.encoding_combo.setCurrentIndex(0)
            self.legacy.setChecked(False)

    @Slot()
    def reset_defaults(self) -> None:
        """将所有标签页中的筛选参数与导出选项全部恢复为程序出厂默认值（仍需点击“应用设置”生效）。"""
        self.force_default_fields = True
        default_rules = Rules()
        for param_key, spin_widget in self.fields.items():
            spin_widget.setValue(getattr(default_rules, param_key))
        self.absolute_check.setChecked(False)
        self.relative_check.setChecked(False)
        self.encoding_combo.setCurrentIndex(0)
        self.legacy.setChecked(False)

    @Slot()
    def apply_settings(self) -> None:
        """读取并校验筛选参数与容差设置（对未触碰的控件保留原始完整浮点精度），通过后关闭对话框。"""
        from .diagnostics import action

        action("高级设置：应用设置")
        try:
            rule_fields = asdict(Rules())
            rule_fields.update(
                {
                    param_key: (
                        getattr(self.original_rules, param_key)
                        if not self.force_default_fields
                        and spin_widget.value() == self.initial_values[param_key]
                        else spin_widget.value()
                    )
                    for param_key, spin_widget in self.fields.items()
                }
            )
            abs_tolerance = (
                self.original_rules.tolerance_m
                if not self.force_default_fields
                and self.original_rules.tolerance_m is not None
                and self.absolute.value() == self.initial_absolute
                else self.absolute.value()
            )
            rel_tolerance = (
                self.original_rules.tolerance_ratio
                if not self.force_default_fields
                and self.original_rules.tolerance_ratio is not None
                and self.relative.value() == self.initial_relative
                else self.relative.value() / 100
            )
            rule_fields.update(
                tolerance_m=(
                    abs_tolerance if self.absolute_check.isChecked() else None
                ),
                tolerance_ratio=(
                    rel_tolerance if self.relative_check.isChecked() else None
                ),
            )
            validated_rules = Rules(**rule_fields)
            validated_rules.validate()
            self.rules = validated_rules
            self.encoding = self.encoding_combo.currentData()
            self.complete_rows_only = self.legacy.isChecked()
            self.accept()
        except (ValueError, TypeError, OSError) as exc:
            from .user_messages import error_text

            self.error_label.setText(error_text(exc))


class FileMappingDialog(QDialog):
    """单个测点的关联 `.dat` 数据文件清单编辑对话框（新增文件须通过 `check_files` 安全预算检查）。"""

    def __init__(
        self,
        parent: QWidget | None,
        code: str,
        files: list[str],
    ) -> None:
        """以当前测点编号 `code` 与关联文件路径列表 `files` 初始化文件清单草稿对话框。"""
        super().__init__(parent)
        self.setWindowTitle(f"{code}：设置数据文件")
        self.resize(700, 400)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.picker: FilePicker | None = None
        fit_window(self, 700, 400)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "同一测点可关联多个 DAT 原始文件。此处仅调整当前测点的文件关联清单，不会改动硬盘上的原始文件。"
            )
        )

        self.list = QListWidget()
        self.list.addItems(files)
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        layout.addWidget(self.list)

        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        action_row = QHBoxLayout()
        add_btn = QPushButton("添加DAT")
        add_btn.clicked.connect(self.add_files)
        action_row.addWidget(add_btn)

        remove_btn = QPushButton("移除选中文件")
        remove_btn.clicked.connect(self.remove_files)
        action_row.addWidget(remove_btn)
        layout.addLayout(action_row)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        chinese_buttons(button_box)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    @property
    def files(self) -> list[str]:
        """返回当前列表控件中保留的所有 `.dat` 文件路径字符串列表。"""
        return [
            self.list.item(row_idx).text()
            for row_idx in range(self.list.count())
        ]

    @Slot()
    def add_files(self) -> None:
        """启动隔离的 Windows 原生多选文件对话框以向当前测点追加 `.dat` 文件。"""
        if self.picker and self.picker.isVisible():
            return
        self.picker = FilePicker(self, "添加DAT文件")
        self.picker.filesSelected.connect(self.selected_files)
        self.picker.error.connect(self.picker_error)
        self.picker.open()

    @Slot(str)
    def picker_error(self, message: str) -> None:
        """展示原生文件选择器返回的错误提示。"""
        self.error_label.setText(message)

    @Slot(list)
    def selected_files(self, files: list[str]) -> None:
        """对合并后的文件集合执行 `check_files` 去重与大小/数量上限检查，通过后刷新列表。"""
        from .input_policy import check_files

        try:
            checked_paths = check_files([*self.files, *files])
            self.list.clear()
            self.list.addItems([str(path) for path in checked_paths])
        except (OSError, ValueError) as exc:
            self.error_label.setText(str(exc))

    @Slot()
    def remove_files(self) -> None:
        """从当前草稿列表中移除用户选中的文件路径条目（不删除磁盘文件）。"""
        for selected_item in self.list.selectedItems():
            self.list.takeItem(self.list.row(selected_item))


class PointDialog(QDialog):
    """单测点详细配置编辑对话框：支持修改测点编号、参考水深、分层方案与起止时间范围。"""

    def __init__(
        self,
        parent: QWidget | None,
        point: Point,
        used_codes: Iterable[str] | None = None,
    ) -> None:
        """以目标测点 `point` 初始化草稿表单，`used_codes` 传入其余测点的编号集合以拦截重名冲突。"""
        super().__init__(parent)
        self.setWindowTitle("编辑测点")
        self.original = point
        self.used_codes = set(used_codes or ())
        self.setWindowModality(Qt.WindowModality.WindowModal)
        fit_window(self, 760, 600)

        root_layout = QVBoxLayout(self)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        form_container = QWidget()
        form_layout = QFormLayout(form_container)
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        scroll_area.setWidget(form_container)
        root_layout.addWidget(scroll_area, 1)

        self.updated: Point | None = None
        self.code = QLineEdit(point.code)
        form_layout.addRow("测点编号", self.code)

        depth_source_label = {
            "manual": "人工填写",
            "estimated": "自动估算",
            "pending": "待估算",
        }.get(point.depth_source, "已有水深")
        current_depth_summary = (
            f"{point.depth:g} 米 · {depth_source_label}"
            if point.depth is not None
            else "待处理时自动估算"
        )
        form_layout.addRow("当前水深", QLabel(current_depth_summary))

        self.depth_editor = DepthEditor(point.depth)
        self.change_depth = self.depth_editor.change
        self.depth = self.depth_editor.value
        form_layout.addRow(self.depth_editor)

        self.layer_choice = LayerChoice([point])
        form_layout.addRow("分层规则", self.layer_choice)
        self.depth_editor.changed.connect(self.update_layer_depths)

        self.start_check = QCheckBox("限定开始时间")
        self.end_check = QCheckBox("限定结束时间")
        from .datetime_input import DateTimeInput

        self.start = DateTimeInput()
        self.end = DateTimeInput()
        for limit_checkbox, datetime_widget, configured_iso in [
            (self.start_check, self.start, point.start),
            (self.end_check, self.end, point.end),
        ]:
            suggested_iso = configured_iso or (point.depth_estimate or {}).get(
                "observed_end"
                if datetime_widget is self.end
                else "observed_start"
            )
            limit_checkbox.setChecked(bool(configured_iso))
            if suggested_iso:
                datetime_widget.setDateTime(
                    QDateTime.fromString(
                        suggested_iso.replace("T", " "), "yyyy-MM-dd HH:mm:ss"
                    )
                )
            datetime_widget.setEnabled(limit_checkbox.isChecked())
            limit_checkbox.toggled.connect(datetime_widget.setEnabled)

        form_layout.addRow(self.start_check, self.start)
        form_layout.addRow(self.end_check, self.end)

        estimate_info = point.depth_estimate or {}
        observed_span_text = (
            "观测范围："
            + str(estimate_info.get("observed_start") or "尚未获取").replace(
                "T", " "
            )
            + " → "
            + str(estimate_info.get("observed_end") or "尚未获取").replace(
                "T", " "
            )
            if estimate_info.get("observed_start")
            or estimate_info.get("observed_end")
            else "尚未获取观测时间，可手动选择日期和时间。"
        )
        time_context_label = QLabel(
            observed_span_text
            + "\n说明：起止时间用于限定本次需生成的目标整点范围（未限定时默认覆盖首尾有效观测所在整点；明确限定开始时间将向上取整点）。"
        )
        time_context_label.setWordWrap(True)
        form_layout.addRow(time_context_label)

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
        """关闭单点编辑对话框时重置水深编辑器的会话标记。"""
        self.depth_editor.reset_session()
        super().done(result)

    def update_layer_depths(self) -> None:
        """当水深草稿变化时，实时刷新下方分层规则预览中的层位信息。"""
        self.layer_choice.set_depths(
            [self.depth_editor.draft_depth(self.original.depth)]
        )

    def draft_changes(self) -> dict[str, Any]:
        """汇总当前单点编辑表单中的候选修改字段字典。"""
        changes = self.depth_editor.changes()
        # 未改动过的历史分层配置（包括已被从规则库删除但仍保存在测点快照中的层位）必须原样保留
        changes.update(self.layer_choice.changes())
        changes.update(
            code=self.code.text().strip(),
            start=(
                self.start.value("开始")
                if self.start_check.isChecked()
                else None
            ),
            end=(
                self.end.value("结束") if self.end_check.isChecked() else None
            ),
        )
        return changes

    @Slot()
    def validate_accept(self) -> None:
        """校验单点候选修改及测点编号唯一性，通过后保存至 `self.updated` 并关闭对话框。"""
        try:
            candidate_point = apply_bulk(
                [self.original], [0], self.draft_changes()
            )[0]
            from .models import validate_codes

            validate_codes([*self.used_codes, candidate_point.code])
            self.updated = candidate_point
            self.accept()
        except ValueError as exc:
            self.error.setText(str(exc))

    def values(self) -> tuple[str, float | None, str | None, str | None]:
        """返回当前表单对应的 `(code, depth, start, end)` 四元组（供测试与外部读取使用）。"""
        candidate_point = (
            self.updated
            or apply_bulk([self.original], [0], self.draft_changes())[0]
        )
        return (
            candidate_point.code,
            candidate_point.depth,
            candidate_point.start,
            candidate_point.end,
        )
