"""测点处理结果只读核查视图模块（`ResultView`）。

职责与交互设计：
1. **纯视图过滤与排序**：顶部的状态筛选（全部记录 / 缺测记录 / 需核查记录）、指标筛选（全部指标 / 深度 / 温度 / 盐度 / 浊度）、目标时间搜索与列头点击数值排序均只作用于界面表格，绝不修改底层已保存的业务结果；
2. **上下分栏联动核查**：点击结果表任意整点行时，下部辅助抽屉（`曲线`、`各层采样`、`来源详情` 三个标签页）实时同步展示该整点的深度-时间剖面曲线、各水层偏差明细及原始 `.dat` 文件行号追溯信息；
3. **小窗口自适应独立详情窗**：当主窗口高度小于 650px 时，点击“展开曲线与采样详情”会自动弹出独立对话框承载下部核查面板，避免挤扁结果主表。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .gui_components import STATUS, Curve
from .result_projection import ReviewProjection, time_text
from .theme import table_style

if TYPE_CHECKING:
    from .models import Point

# 属于“需人工核查”的状态码集合（片段复用、跨层复用、深度超差）
CHECK: set[str] = {
    "segment_reused",
    "cross_layer_reused",
    "depth_out_of_tolerance",
}
# 属于“缺测”的状态码集合（缺少匹配片段、单层缺测）
MISSING: set[str] = {"missing_segment", "missing_layer"}

# 各类异常状态码的详细中文解释文案
REASONS: dict[str, str] = {
    "missing_segment": "在设定的时间范围内，未找到可用于该整点的观测片段。",
    "missing_layer": "片段内缺少可选观测，空值保留为空。",
    "segment_reused": "同一片段被多个整点使用，请核对采样时间。",
    "cross_layer_reused": "不同层位使用了同一观测，请核对。",
    "depth_out_of_tolerance": "偏差超过本次设置容差，仍保留原值供核查。",
}


def status_text(row: dict[str, Any]) -> str:
    """将整点行的状态码列表转换为表格“状态”列展示的中文摘要（忽略 `tolerance_unset`，无异常时显示 `'有采样值'`）。"""
    active_codes = [
        code for code in row["status"] if code != "tolerance_unset"
    ]
    return "；".join(STATUS.get(code, code) for code in active_codes) or "有采样值"


class CopyTable(QTableWidget):
    """支持 `Ctrl+C` / 标准复制快捷键导出选中行制表符文本的核查表格控件。"""

    copyRequested = Signal()

    def keyPressEvent(self, event) -> None:
        """拦截系统标准复制快捷键并发射 `copyRequested` 信号，其余按键保持默认行为。"""
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copyRequested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class ReviewDelegate(QStyledItemDelegate):
    """自定义表格单元格绘制代理：在选中高亮行中依然保持异常警告文字的醒目红色前景。"""

    def initStyleOption(self, option, index) -> None:
        """若单元格设置了自定义前景色（如 `#B42318` 警告红），在选中态下同步覆盖 `HighlightedText` 颜色。"""
        super().initStyleOption(option, index)
        foreground_brush = index.data(Qt.ItemDataRole.ForegroundRole)
        if foreground_brush is not None:
            option.palette.setColor(
                QPalette.ColorRole.HighlightedText, foreground_brush.color()
            )


class NumberItem(QTableWidgetItem):
    """支持按真实浮点数值（而非字典序字符串）点击表头排序的数值单元格项。"""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        """读取存储在 `UserRole + 1` 中的原始浮点数进行比较；缺测空值（`None`）统一排在数值之前。"""
        left_val = self.data(Qt.ItemDataRole.UserRole + 1)
        right_val = other.data(Qt.ItemDataRole.UserRole + 1)
        return (float("-inf") if left_val is None else left_val) < (
            float("-inf") if right_val is None else right_val
        )


class ResultView(QWidget):
    """测点处理结果主核查视图：组合筛选栏、整点结果主表、深度-时间曲线画布、各层采样表与来源详情面板。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化结果主表、下部可折叠辅助核查抽屉（`QSplitter`）及后台曲线快照加载器（`ContextLoader`）。"""
        super().__init__(parent)
        self.result: dict[str, Any] | None = None
        self.identities: dict[int, dict[str, Any]] = {}
        self.user_split_state = None
        self.detail_dialog: QDialog | None = None
        self.snapshot: Point | None = None
        self.projection = ReviewProjection(None)
        self.detail_reset: QPushButton | None = None
        self.curve_context: dict[str, Any] | None = None
        self.curve_note: str = ""
        self.curve_path: Path | None = None

        from .curve_loader import ContextLoader

        self.context_loader = ContextLoader(self)
        self.context_loader.ready.connect(self.context_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.context = QLabel("尚无运行结果")
        self.context.setWordWrap(True)
        layout.addWidget(self.context)

        filter_bar = QHBoxLayout()
        layout.addLayout(filter_bar)

        self.filter = QComboBox()
        self.filter.addItems(["全部记录", "缺测记录", "需核查记录"])
        self.filter.setAccessibleName("按记录状态筛选")

        self.metric = QComboBox()
        self.metric.addItems(["全部指标", "深度", "温度", "盐度", "浊度"])
        self.metric.setAccessibleName("选择显示指标")

        self.search = QLineEdit()
        self.search.setPlaceholderText("查找目标时间，如 2026-01-01 09")
        self.search.setAccessibleName("查找目标时间")

        filter_bar.addWidget(self.filter)
        filter_bar.addWidget(self.metric)
        filter_bar.addWidget(self.search, 1)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        layout.addWidget(self.splitter, 1)

        self.table = CopyTable()
        table_style(self.table)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(140)
        self.table.setItemDelegate(ReviewDelegate(self.table))
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.ExtendedSelection
        )
        self.splitter.addWidget(self.table)

        self.aux = QTabWidget()
        self.splitter.addWidget(self.aux)

        self.curve = Curve()
        self.aux.addTab(self.curve, "曲线")
        self.aux.setMinimumHeight(0)
        self.aux.currentChanged.connect(self.update_detail_controls)

        self.samples = QTableWidget()
        table_style(self.samples)
        self.samples.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.aux.addTab(self.samples, "各层采样")
        self.samples.setItemDelegate(ReviewDelegate(self.samples))

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.aux.addTab(self.details, "来源详情")
        self.aux.hide()

        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(self.remember_splitter)

        self.count = QLabel("共 0 行")
        self.count.setWordWrap(True)
        layout.addWidget(self.count)

        footer_bar = QHBoxLayout()
        layout.addLayout(footer_bar)
        footer_bar.addStretch(1)

        self.guide = QLabel("展开详情以核查具体问题 →")
        self.guide.setProperty("role", "error")
        self.guide.setWordWrap(True)
        self.guide.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        footer_bar.addWidget(self.guide)
        self.guide.hide()

        self.toggle = QPushButton("展开曲线与采样详情")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self.toggle_aux)
        footer_bar.addWidget(self.toggle)

        self.full = QPushButton("恢复全图")
        self.full.clicked.connect(self.curve.reset_zoom)
        footer_bar.addWidget(self.full)
        self.full.hide()

        self.toggle.setFixedWidth(
            max(
                self.toggle.fontMetrics().horizontalAdvance(btn_label)
                for btn_label in ("展开曲线与采样详情", "收起曲线与采样详情")
            )
            + 34
        )
        self.full.setFixedWidth(
            self.full.fontMetrics().horizontalAdvance("恢复全图") + 34
        )
        for footer_ctrl in (self.guide, self.full):
            size_policy = footer_ctrl.sizePolicy()
            size_policy.setRetainSizeWhenHidden(True)
            footer_ctrl.setSizePolicy(size_policy)

        self.filter.currentIndexChanged.connect(self.render)
        self.metric.currentIndexChanged.connect(self.render)
        self.search.textChanged.connect(self.render)
        self.table.currentCellChanged.connect(self.select_row)
        self.table.itemSelectionChanged.connect(self.update_guide)
        self.table.copyRequested.connect(self.copy_selection)

    def toggle_aux(self, checked: bool) -> None:
        """展开或收起下部曲线与采样详情面板；当顶层窗口高度不足 650px 时自动改为弹出独立详情窗口。"""
        if checked and self.window().height() < 650:
            self.toggle.blockSignals(True)
            self.toggle.setChecked(False)
            self.toggle.blockSignals(False)
            self.open_detail_window()
            return
        self.aux.setMinimumHeight(220 if checked else 0)
        self.aux.setVisible(checked)
        self.toggle.setText(
            "收起曲线与采样详情" if checked else "展开曲线与采样详情"
        )
        if checked:
            if self.user_split_state:
                self.splitter.restoreState(self.user_split_state)
            else:
                self.splitter.setSizes([max(140, self.height() - 240), 220])
            self.correct_splitter()
        self.update_guide()
        self.update_detail_controls()
        if not checked:
            self.release_layout()

    def open_detail_window(self) -> None:
        """在小屏幕或低高度窗口下，将 `self.aux` 详情标签页临时挂载到独立对话框中展示。"""
        if self.detail_dialog and self.detail_dialog.isVisible():
            self.detail_dialog.raise_()
            return
        self.detail_dialog = QDialog(self.window())
        self.detail_dialog.setWindowTitle("曲线与采样详情")
        from .theme import fit_window

        fit_window(self.detail_dialog, 820, 500)
        dialog_layout = QVBoxLayout(self.detail_dialog)
        dialog_layout.addWidget(self.aux)
        self.aux.setMinimumHeight(220)
        self.aux.show()

        bottom_row = QHBoxLayout()
        dialog_layout.addLayout(bottom_row)
        bottom_row.addStretch(1)

        reset_zoom_btn = QPushButton("恢复全图")
        reset_zoom_btn.setFixedWidth(self.full.width())
        reset_zoom_btn.clicked.connect(self.curve.reset_zoom)
        bottom_row.addWidget(reset_zoom_btn)
        self.detail_reset = reset_zoom_btn
        btn_policy = reset_zoom_btn.sizePolicy()
        btn_policy.setRetainSizeWhenHidden(True)
        reset_zoom_btn.setSizePolicy(btn_policy)

        close_btn = QPushButton("关闭")
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.detail_dialog.accept)
        bottom_row.addWidget(close_btn)

        self.detail_dialog.finished.connect(self.return_aux)
        self.detail_dialog.open()
        self.update_guide()
        self.update_detail_controls()

    def return_aux(self, *_args: Any) -> None:
        """当独立详情对话框关闭时，将 `self.aux` 重新挂回主界面 `QSplitter` 底部并折叠隐藏。"""
        self.aux.hide()
        self.aux.setMinimumHeight(0)
        self.splitter.addWidget(self.aux)
        self.aux.hide()
        closing_dialog = self.detail_dialog
        self.detail_dialog = None
        self.detail_reset = None
        if closing_dialog:
            closing_dialog.deleteLater()
        self.toggle.setChecked(False)
        self.full.hide()
        self.toggle.setText("展开曲线与采样详情")
        self.update_guide()
        self.release_layout()

    def release_layout(self) -> None:
        """沿着父控件链逐级刷新布局几何缓存，消除折叠详情抽屉后的残余高度占用。"""
        current_widget: QWidget | None = self
        while current_widget is not None:
            if current_widget.layout():
                current_widget.layout().invalidate()
                current_widget.layout().activate()
            current_widget.updateGeometry()
            current_widget = current_widget.parentWidget()

    def resizeEvent(self, event) -> None:
        """窗口宽度变化时动态限制底部红色核查引导文字的最大宽度，防止挤压右侧展开按钮。"""
        super().resizeEvent(event)
        if hasattr(self, "full"):
            preferred_width = (
                self.guide.fontMetrics().horizontalAdvance(self.guide.text())
                + 8
            )
            available_width = (
                self.width() - self.toggle.width() - self.full.width() - 20
            )
            self.guide.setFixedWidth(
                max(90, min(preferred_width, available_width))
            )

    def remember_splitter(self, *_args: Any) -> None:
        """保存用户手动拖拽调整后的上下分隔条位置状态。"""
        if self.aux.isVisible() and not self.detail_dialog:
            self.correct_splitter()
            self.user_split_state = self.splitter.saveState()

    def correct_splitter(self) -> None:
        """确保上下分隔区域满足最小可读高度（主表至少 140px，详情抽屉至少 220px）。"""
        pane_sizes = self.splitter.sizes()
        if len(pane_sizes) == 2 and (
            pane_sizes[0] < 140 or pane_sizes[1] < 220
        ):
            self.splitter.setSizes([max(140, sum(pane_sizes) - 220), 220])

    def update_detail_controls(self, *_args: Any) -> None:
        """仅当当前激活的子标签页为“曲线”且面板已展开时，显示对应的“恢复全图”按钮。"""
        is_curve_tab = self.aux.currentWidget() is self.curve
        is_separate_dialog = bool(
            self.detail_dialog and self.detail_dialog.isVisible()
        )
        self.full.setVisible(
            is_curve_tab and self.toggle.isChecked() and not is_separate_dialog
        )
        if self.detail_reset:
            self.detail_reset.setVisible(is_curve_tab and is_separate_dialog)

    def context_ready(
        self, data: dict[str, Any] | None, note: str
    ) -> None:
        """接收 `ContextLoader` 后台加载完成的完整曲线快照并刷新曲线画布。"""
        self.curve_context = data
        self.curve_note = note
        self.curve.set_context(data, note)

    def show_result(
        self,
        result: dict[str, Any] | None,
        context: str,
        snapshot: Point | None = None,
        curve_path: Path | None = None,
    ) -> None:
        """绑定新的测点处理结果字典 `result` 与配置快照 `snapshot`，渲染表格并触发异步曲线上下文加载。"""
        is_different_target = (
            result is not self.result or curve_path != self.curve_path
        )
        self.result = result
        self.snapshot = deepcopy(snapshot)
        self.projection = ReviewProjection(result)
        self.context.setText(context)
        if is_different_target:
            self.curve_path = curve_path
            self.curve_context = None
            self.curve_note = "正在加载曲线…"
        self.render()
        if is_different_target:
            self.context_loader.load(
                curve_path, getattr(snapshot, "point_id", None)
            )

    def update_guide(self) -> None:
        """当当前选中的整点行存在缺测或需核查项且详情面板尚未展开时，显示底部红色“展开详情以核查具体问题 →”引导提示。"""
        current_item = self.table.item(self.table.currentRow(), 0)
        row_identity = (
            current_item.data(Qt.ItemDataRole.UserRole)
            if current_item
            else None
        )
        is_row_selected = any(
            model_idx.row() == self.table.currentRow()
            for model_idx in self.table.selectedIndexes()
        )
        is_detail_opened = self.toggle.isChecked() or bool(
            self.detail_dialog and self.detail_dialog.isVisible()
        )
        self.guide.setVisible(
            bool(
                is_row_selected
                and row_identity in self.identities
                and (CHECK | MISSING).intersection(
                    self.projection.status(row_identity)
                )
                and not is_detail_opened
            )
        )

    def clear(self) -> None:
        """清空当前展示的所有表格、详情、曲线与筛选状态，并立即取消未完成的后台曲线加载子进程。"""
        self.context_loader.cancel()
        self.curve_context = None
        self.curve_path = None
        self.curve_note = ""
        if self.detail_dialog and self.detail_dialog.isVisible():
            self.detail_dialog.reject()
        self.result = None
        self.snapshot = None
        self.projection = ReviewProjection(None)
        self.identities = {}
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.samples.setRowCount(0)
        self.samples.setColumnCount(0)

        for filter_ctrl in (self.filter, self.metric, self.search):
            filter_ctrl.blockSignals(True)
        self.filter.setCurrentIndex(0)
        self.metric.setCurrentIndex(0)
        self.search.clear()
        for filter_ctrl in (self.filter, self.metric, self.search):
            filter_ctrl.blockSignals(False)

        self.details.clear()
        self.curve.set_record(None, None)
        self.count.setText("共 0 行")
        self.count.setToolTip("")
        self.context.setText("尚无运行结果")
        self.toggle.setChecked(False)
        self.guide.hide()

    def render(self, *_args: Any) -> None:
        """根据顶部状态筛选、指标选择与目标时间搜索词，重新投影生成整点结果核查主表。"""
        current_first_item = self.table.item(self.table.currentRow(), 0)
        previous_row_id = (
            current_first_item.data(Qt.ItemDataRole.UserRole)
            if current_first_item
            else None
        )
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.identities = {}
        if not self.result:
            self.update_guide()
            return

        active_metrics = [
            ("depth", "深度 (m)"),
            ("temperature", "温度"),
            ("salinity", "盐度"),
            ("turbidity", "浊度"),
        ]
        if self.metric.currentIndex():
            active_metrics = [active_metrics[self.metric.currentIndex() - 1]]

        layer_labels = [
            layer_item["label"] for layer_item in self.result["layer_plan"]
        ]
        column_headers = ["目标时间", "状态"] + [
            f"{layer_label} {metric_name}"
            for layer_label in layer_labels
            for _metric_key, metric_name in active_metrics
        ]
        self.table.setColumnCount(len(column_headers))
        self.table.setHorizontalHeaderLabels(column_headers)

        visible_rows: list[tuple[int, dict[str, Any]]] = []
        search_needle = self.search.text().strip().replace("T", " ")
        for row_identity, row_dict in enumerate(self.result["rows"]):
            row_status_codes = self.projection.status(row_identity)
            if self.filter.currentIndex() == 1 and not MISSING.intersection(
                row_status_codes
            ):
                continue
            if self.filter.currentIndex() == 2 and not CHECK.intersection(
                row_status_codes
            ):
                continue
            if search_needle not in row_dict["target"].replace("T", " "):
                continue
            visible_rows.append((row_identity, row_dict))
            self.identities[row_identity] = row_dict

        for row_identity, row_dict in visible_rows:
            table_row_idx = self.table.rowCount()
            self.table.insertRow(table_row_idx)
            obs_by_layer = {
                sel["label"]: sel["observation"]
                for sel in row_dict["selections"]
            }
            row_status_codes = self.projection.status(row_identity)
            text_columns = [
                time_text(row_dict["target"]),
                status_text({"status": row_status_codes}),
            ]
            numeric_values: list[float | None] = []
            for layer_label in layer_labels:
                obs_record = obs_by_layer.get(layer_label)
                numeric_values.extend(
                    obs_record.get(metric_key) if obs_record else None
                    for metric_key, _metric_name in active_metrics
                )

            all_cell_strings = text_columns + [
                f"{num_val:.3f}" if num_val is not None else ""
                for num_val in numeric_values
            ]
            for col_idx, cell_str in enumerate(all_cell_strings):
                cell_item = (
                    QTableWidgetItem(cell_str)
                    if col_idx < 2
                    else NumberItem(cell_str)
                )
                cell_item.setData(Qt.ItemDataRole.UserRole, row_identity)
                if col_idx >= 2:
                    cell_item.setData(
                        Qt.ItemDataRole.UserRole + 1,
                        numeric_values[col_idx - 2],
                    )
                    cell_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                if (
                    col_idx == 0
                    and len(self.projection.shared_segment(row_identity)) > 1
                ):
                    cell_item.setForeground(QColor("#B42318"))
                if col_idx == 1 and (MISSING | CHECK).intersection(
                    row_status_codes
                ):
                    cell_item.setIcon(
                        self.style().standardIcon(
                            QStyle.StandardPixmap.SP_MessageBoxWarning
                        )
                    )
                self.table.setItem(table_row_idx, col_idx, cell_item)

        self.table.setSortingEnabled(True)
        self.table.setColumnWidth(0, 190)
        self.table.setColumnWidth(1, 150)
        for col_idx in range(2, len(column_headers)):
            self.table.setColumnWidth(col_idx, 120)

        restored_row_idx = next(
            (
                idx
                for idx in range(self.table.rowCount())
                if self.table.item(idx, 0).data(Qt.ItemDataRole.UserRole)
                == previous_row_id
            ),
            0,
        )
        if self.table.rowCount():
            self.table.setCurrentCell(restored_row_idx, 0)
            self.select_row(restored_row_idx, 0, 0, 0)
        else:
            self.details.setPlainText("没有符合筛选条件的记录")
            self.curve.row = None
            self.curve.update()
            self.samples.setRowCount(0)
        self.update_count()
        self.update_guide()

    def update_count(self) -> None:
        """刷新表格底部状态栏的总行数、当前筛选后显示行数及选中整点行的健康状态摘要。"""
        total_rows = len(self.result["rows"]) if self.result else 0
        current_item = self.table.item(self.table.currentRow(), 0)
        selected_time_part = " · " + current_item.text() if current_item else ""
        selected_row_dict = (
            self.identities.get(current_item.data(Qt.ItemDataRole.UserRole))
            if current_item
            else None
        )
        row_status_codes = (
            self.projection.status(current_item.data(Qt.ItemDataRole.UserRole))
            if selected_row_dict
            else []
        )
        health_summary_part = (
            " · "
            + (
                "缺测"
                if MISSING.intersection(row_status_codes)
                else (
                    "需核查"
                    if CHECK.intersection(row_status_codes)
                    else "有采样值"
                )
            )
            if selected_row_dict
            else ""
        )
        self.count.setText(
            f"共 {total_rows} 行 · 显示 {self.table.rowCount()} 行"
            + selected_time_part
            + health_summary_part
        )
        self.count.setToolTip(
            status_text({"status": row_status_codes})
            if selected_row_dict
            else ""
        )

    def select_row(
        self,
        row: int,
        _col: int,
        _previous_row: int,
        _previous_col: int,
    ) -> None:
        """当用户在结果主表中切换当前行时，联动刷新下部深度-时间曲线、10 列各层采样明细表与来源详情 HTML。"""
        first_col_item = self.table.item(row, 0)
        if not self.result or first_col_item is None:
            return
        row_identity = first_col_item.data(Qt.ItemDataRole.UserRole)
        row_data = self.identities.get(row_identity)
        if row_data is None:
            return

        self.table.scrollToItem(first_col_item)
        projected_row = {
            **row_data,
            "status": self.projection.status(row_identity),
        }
        self.curve.set_record(
            self.result, projected_row, self.curve_context, self.curve_note
        )
        reused_layer_indices = self.projection.shared_layers(row_identity)
        self.details.setHtml(
            self.projection.details_html(
                row_identity, getattr(self.snapshot, "depth", None)
            )
        )

        self.samples.setColumnCount(10)
        self.samples.setHorizontalHeaderLabels(
            [
                "层位",
                "目标深度 (m)",
                "实际深度 (m)",
                "偏差 (m)",
                "实际时间",
                "来源文件",
                "行号",
                "温度",
                "盐度",
                "浊度",
            ]
        )
        self.samples.setRowCount(len(row_data["selections"]))
        for layer_idx, selection_dict in enumerate(row_data["selections"]):
            obs_record = selection_dict.get("observation")
            sample_row_values = [
                selection_dict.get("label", "未记录"),
                selection_dict.get("target_depth"),
                obs_record.get("depth") if obs_record else None,
                selection_dict.get("deviation_m"),
                time_text(obs_record.get("timestamp")) if obs_record else "缺测",
                Path(obs_record.get("file") or "").name if obs_record else "",
                obs_record.get("line", "未记录") if obs_record else "",
                *[
                    obs_record.get(metric_key) if obs_record else None
                    for metric_key in ("temperature", "salinity", "turbidity")
                ],
            ]
            for col_idx, cell_val in enumerate(sample_row_values):
                cell_item = QTableWidgetItem(
                    f"{cell_val:.3f}"
                    if col_idx in (1, 2, 3, 7, 8, 9) and cell_val is not None
                    else str(cell_val)
                    if cell_val is not None
                    else ""
                )
                if obs_record:
                    cell_item.setToolTip(
                        str(obs_record.get("file", "来源未记录"))
                    )
                if col_idx in (1, 2, 3, 6, 7, 8, 9):
                    cell_item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter
                    )
                if layer_idx in reused_layer_indices and col_idx in (
                    0,
                    2,
                    7,
                    8,
                    9,
                ):
                    cell_item.setForeground(QColor("#B42318"))
                    cell_item.setToolTip(
                        cell_item.toolTip()
                        + "\n跨层复用：多个层位使用同一原始观测"
                    )
                if layer_idx in reused_layer_indices and col_idx == 0:
                    cell_item.setText(cell_item.text() + " · 跨层复用")
                self.samples.setItem(layer_idx, col_idx, cell_item)

        self.samples.resizeColumnsToContents()
        self.update_count()
        self.update_guide()

    def copy_selection(self) -> None:
        """将当前选中行的所有单元格拼接为制表符分隔文本（TSV）并写入系统剪贴板，方便直接粘贴至 Excel。"""
        from .diagnostics import action

        action("结果表：复制选中行")
        selected_rows = sorted(
            {model_idx.row() for model_idx in self.table.selectedIndexes()}
        )
        QApplication.clipboard().setText(
            "\n".join(
                "\t".join(
                    self.table.item(row_idx, col_idx).text()
                    for col_idx in range(self.table.columnCount())
                )
                for row_idx in selected_rows
            )
        )
