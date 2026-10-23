"""主窗口与后台任务状态机协调模块（`Window`、`main`）。

核心架构与状态隔离原则：
1. **三层批次快照分离**：
   - `self.points` / `self.batch`：用户在“数据与设置”页实时编辑的当前草稿；
   - `self.pending_batch`：点击“开始处理”后进入水深后台估算与人工确认门禁时的准备期冻结快照；
   - `self.active_batch` 与 `self.entries[point_id]`：正式启动后台批处理及保存历史运行结果时的运行时配置快照。
   历史结果条目（`self.entries`）始终按其运行当次的测点水深、层位方案和容差配置解释，绝不因用户事后修改草稿而发生错位。
2. **稳定测点身份（`point_id`）**：
   - 表格勾选集合（`self.checked_ids`）、单点编辑定位、水深估算回填及结果映射均以不可变的 `Point.point_id` 为主键，允许用户随时重命名测点编号 `code`。
3. **非阻塞异步协同与安全退出**：
   - 水深推算（`AnalysisWorker`）、批处理（`Worker`）、原生文件选择（`FilePicker`）与实用工具（`ToolsDialog`）均通过信号驱动；
   - 关闭主窗口时（`closeEvent`）若存在运行中的子进程，先请求协作取消并挂载 `try_close` 回调，待全部后台进程安全退出后再落盘窗口几何布局并关闭。
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from html import escape
import logging
import math
import os
from pathlib import Path
import sys

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStyle,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from . import APP_NAME, __version__
from .analysis_worker import AnalysisWorker
from .bulk_dialog import BulkDialog
from .depth_review import DepthReviewDialog
from .dialogs import AdvancedDialog, FileMappingDialog, FilePicker, PointDialog
from .gui_components import Worker
from .models import Batch, Point, validate_codes
from .workspace_view import build_pages

STATE = {
    "completed": "完成",
    "completed_with_errors": "部分失败",
    "failed": "失败",
    "cancelled": "已取消",
    "not_started": "未开始",
}


class Window(QMainWindow):
    """协调测点草稿编辑、水深自动估算与人工确认、后台批处理及结果核查的主窗口控制器。

    状态说明：
    - `self.points` 是当前工作区草稿；
    - `self.pending_batch` 是水深准备与确认阶段的批次快照；
    - `self.active_batch` 是正在执行批处理的运行快照；
    - `self.entries` 保存各测点已完成或失败的历史运行快照，始终按当次运行配置展示，不随草稿修改而漂移。
    """

    def __init__(self) -> None:
        """初始化默认批次配置、后台控制器引用与本地 `QSettings`，构建两页桌面布局并恢复历史窗口尺寸。"""
        from .branding import configure

        app_icon = configure(QApplication.instance())
        super().__init__()
        self.setWindowIcon(app_icon)
        self.setWindowTitle(APP_NAME + " " + __version__)
        if os.environ.get("OBS_SETTINGS_DIR"):
            Path(os.environ["OBS_SETTINGS_DIR"]).mkdir(
                parents=True, exist_ok=True
            )
            self.settings = QSettings(
                str(Path(os.environ["OBS_SETTINGS_DIR"]) / "ui.ini"),
                QSettings.Format.IniFormat,
            )
        else:
            self.settings = QSettings("OBS工具", "桌面工具")

        self.batch = Batch(output_dir=str(Path.home() / "OBS处理结果"))
        self.checked_ids: set[str] = set()
        self.points: list[Point] = []
        self.results = {}
        self.report = {}
        self.worker: Worker | None = None
        self.entries = {}
        self.active_batch: Batch | None = None
        self.stale = False
        self.task_state = "无数据"
        self.dialog = None
        self.file_picker = None
        self.mapping_dialog = None
        self.point_dialog = None
        self.pending_folder = ""
        self.folder_action = ""
        self.edit_row = -1
        self.analysis_worker: AnalysisWorker | None = None
        self.pending_batch: Batch | None = None
        self.analysis_pending: set[str] = set()
        self.closing_requested = False

        from .theme import apply_theme, fit_window

        apply_theme(QApplication.instance())
        build_pages(self)
        saved_geometry = self.settings.value("geometry-redesign")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
        fit_window(self, self.width(), self.height())
        saved_splitter = self.settings.value("result-splitter")
        if saved_splitter:
            self.result_view.user_split_state = saved_splitter
        self.update_selection()
        self.refresh_ready()

    @property
    def checked_codes(self) -> set[str]:
        """在测点稳定内部身份（`point_id`）与当前可编辑测点编号（`code`）之间映射勾选集合。"""
        return {
            point.code
            for point in self.points
            if point.point_id in self.checked_ids
        }

    @checked_codes.setter
    def checked_codes(self, codes) -> None:
        """按给定的测点编号集合更新 `checked_ids` 稳定身份集合。"""
        self.checked_ids = {
            point.point_id for point in self.points if point.code in codes
        }

    def checked_rows(self) -> list[int]:
        """返回 `self.points` 中当前已勾选测点的下标列表（按测点列表顺序排列）。"""
        return [
            idx
            for idx, point in enumerate(self.points)
            if point.point_id in self.checked_ids
        ]

    def table_point_index(self, row: int) -> int:
        """通过表格单元格 `UserRole` 中绑定的稳定 `point_id` 查找对应的 `self.points` 下标。"""
        cell_item = self.table.item(row, 1)
        identity = (
            cell_item.data(Qt.ItemDataRole.UserRole) if cell_item else None
        )
        return next(
            (
                idx
                for idx, point in enumerate(self.points)
                if point.point_id == identity
            ),
            -1,
        )

    def set_all_checks(self, checked: bool) -> None:
        """全选或取消全选所有测点身份并刷新工作区表格。"""
        self.checked_ids = (
            {point.point_id for point in self.points} if checked else set()
        )
        self.populate()

    def toggle_all_checks(self) -> None:
        """根据当前已勾选测点数量切换全选或取消全选。"""
        self.set_all_checks(len(self.checked_ids) != len(self.points))

    def update_selection(self) -> None:
        """根据当前勾选集合与高亮选中行，更新工具栏按钮可用性、勾选摘要及表头三态复选框。"""
        checked_count = len(self.checked_rows())
        has_current_row = self.table.currentRow() >= 0
        self.selection_summary.setText(
            f"已勾选 {checked_count} 个测点"
            + (" · 先勾选要修改的测点" if not checked_count else "")
        )
        self.select_all.setText(
            "取消全选"
            if self.points and checked_count == len(self.points)
            else "勾选全部"
        )
        self.edit_button.setEnabled(has_current_row)
        self.mapping_button.setEnabled(has_current_row)
        self.bulk_button.setEnabled(checked_count > 0)
        self.remove_button.setEnabled(checked_count > 0)
        self.remove_action.setEnabled(checked_count > 0)
        self.check_header.state = (
            Qt.CheckState.Unchecked
            if not checked_count
            else (
                Qt.CheckState.Checked
                if checked_count == len(self.points)
                else Qt.CheckState.PartiallyChecked
            )
        )
        self.check_header.viewport().update()
        self.select_all.setEnabled(bool(self.points))
        self.check_button.setEnabled(bool(self.depth_issues()))
        self.check_action.setEnabled(has_current_row)

    def refresh_ready(self) -> None:
        """实时校验任务状态与表格输入字段，推导“开始处理并导出 CSV”按钮可用性并展示阻止原因。"""
        is_busy = bool(self.worker and self.worker.isRunning())
        is_analysing = (
            bool(self.analysis_worker and self.analysis_worker.isRunning())
            or self.task_state == "识别中"
        )
        blocking_reason = ""
        if is_busy:
            blocking_reason = "处理中；请等待或安全取消"
        elif is_analysing:
            blocking_reason = "正在估算水深，完成后可处理"
        elif not self.points:
            blocking_reason = "请先添加 DAT 文件或文件夹"
        else:
            collected_codes: list[str] = []
            for row in range(self.table.rowCount()):
                point = self.points[self.table_point_index(row)]
                error_column = 2
                try:
                    code_text = self.table.item(row, 1).text().strip()
                    collected_codes.append(code_text)
                    try:
                        depth_text = self.table.item(row, 2).text().strip()
                        depth_value = float(depth_text) if depth_text else None
                    except ValueError:
                        raise ValueError("请输入大于0的水深（米）。")
                    if depth_value is not None and (
                        not math.isfinite(depth_value) or depth_value <= 0
                    ):
                        raise ValueError("请输入大于0的水深（米）。")
                    error_column = 1
                    replace(
                        point, code=code_text, depth=depth_value
                    ).validate(require_depth=False)
                    error_column = 6
                    if not point.files:
                        raise ValueError("未关联 DAT 文件")
                except (ValueError, AttributeError) as exc:
                    blocking_reason = f"{point.code}：{exc}"
                    self.flag_field(row, error_column, str(exc))
                    break
            if not blocking_reason and len(
                {code.casefold() for code in collected_codes}
            ) != len(collected_codes):
                try:
                    validate_codes(collected_codes)
                except ValueError as exc:
                    blocking_reason = str(exc)
                for row, code_text in enumerate(collected_codes):
                    if (
                        sum(
                            candidate.casefold() == code_text.casefold()
                            for candidate in collected_codes
                        )
                        > 1
                    ):
                        self.flag_field(row, 1, blocking_reason)
            if not blocking_reason and not self.output.text():
                blocking_reason = "请选择 CSV 保存位置"

        self.start.setEnabled(not blocking_reason)
        self.ready.setText(
            blocking_reason
            or f"将处理当前列表的全部 {len(self.points)} 个测点；未填水深将在处理时估算"
        )
        self.new_button.setEnabled(not (is_busy or is_analysing))
        self.more.setEnabled(not (is_busy or is_analysing))
        self.advanced_button.setEnabled(not is_busy)
        review_count = len(self.depth_issues())
        self.check_button.setVisible(review_count > 0)
        self.check_button.setEnabled(review_count > 0)
        self.check_button.setText(
            f"{review_count} 个测点的水深需要确认 · 查看详情"
        )
        self.start.setEnabled(
            not blocking_reason and self.task_state != "等待水深确认"
        )

    def flag_field(self, row: int, col: int, message: str) -> None:
        """临时阻断表格信号，用浅红错误底色与悬浮提示标记校验未通过的单元格。"""
        from .theme import ERROR_BACKGROUND

        was_blocked = self.table.blockSignals(True)
        cell_item = self.table.item(row, col)
        if cell_item:
            cell_item.setBackground(QColor(ERROR_BACKGROUND))
            cell_item.setToolTip(message)
        self.table.blockSignals(was_blocked)

    @staticmethod
    def point_signature(point: Point) -> dict:
        """提取决定采样结果的核心测点配置签名（忽略水深来源标签与规则显示名），用于判断历史结果是否已过时。"""
        signature = asdict(point)
        for non_semantic_key in (
            "depth_source",
            "depth_estimate",
            "layer_rule_name",
        ):
            signature.pop(non_semantic_key, None)
        return signature

    def entry_changed(self, entry: dict) -> bool:
        """比较当前工作区草稿与历史条目的运行时快照，判断该测点的配置或关联文件是否已发生变化。"""
        original_point = entry["point"]
        current_point = next(
            (
                point
                for point in self.points
                if point.point_id == original_point.point_id
            ),
            None,
        )
        batch_snapshot = entry["batch"]
        return (
            entry.get("input_changed", False)
            or current_point is None
            or self.point_signature(current_point)
            != self.point_signature(original_point)
            or self.batch.rules != batch_snapshot.rules
            or self.batch.encoding != batch_snapshot.encoding
            or self.batch.complete_rows_only
            != batch_snapshot.complete_rows_only
        )

    def new_batch(self) -> None:
        """在空闲状态下请求开始新一批处理；若当前已有测点或结果，先弹出确认对话框防止误清空。"""
        if (self.worker and self.worker.isRunning()) or (
            self.analysis_worker and self.analysis_worker.isRunning()
        ):
            self.summary.setText("任务进行中，请先安全取消再开始新一批")
            return
        if not self.points and not self.entries:
            self.clear_inputs()
            return
        self.confirm_new = QMessageBox(
            QMessageBox.Icon.Question,
            "开始新一批",
            "仅清空当前列表、预览和日志；原始文件和历史输出保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        self.confirm_new.button(QMessageBox.StandardButton.Yes).setText(
            "开始新一批"
        )
        self.confirm_new.button(QMessageBox.StandardButton.No).setText(
            "保留当前列表"
        )
        self.confirm_new.setDefaultButton(QMessageBox.StandardButton.No)
        self.confirm_new.buttonClicked.connect(self.new_batch_confirmed)
        self.confirm_new.open()

    @Slot(object)
    def new_batch_confirmed(self, button) -> None:
        """仅当用户在确认框中点击“开始新一批”时执行清空。"""
        if (
            self.confirm_new.standardButton(button)
            == QMessageBox.StandardButton.Yes
        ):
            self.clear_inputs()

    def show_help(self) -> None:
        """以非阻塞方式打开内置使用帮助与关于窗口。"""
        from .help_dialog import HelpDialog

        self.help_dialog = HelpDialog(self)
        self.help_dialog.open()

    def depth_issues(self) -> list[Point]:
        """筛选出当前已有自动水深估算记录、但因可信度较低或存在同秒冲突仍需人工确认的测点列表。"""
        from .depth_workflow import requires_confirmation

        return [
            point
            for point in self.points
            if point.depth_estimate and requires_confirmation(point)
        ]

    def show_depth_reasons(self) -> None:
        """以只读模式打开待确认测点的水深推算依据详情窗口。"""
        from .depth_review import DepthReviewDialog

        self.depth_message = DepthReviewDialog(self, self.depth_issues())
        self.depth_message.open()

    def show_current_depth(self) -> None:
        """以只读模式打开当前选中测点的水深推算依据详情窗口。"""
        point_idx = self.table_point_index(self.table.currentRow())
        if point_idx < 0:
            return
        from .depth_review import DepthReviewDialog

        target_point = self.points[point_idx]
        self.depth_message = DepthReviewDialog(self, [target_point])
        self.depth_message.setWindowTitle(target_point.code + "：水深详情")
        self.depth_message.open()

    def restore_layout(self) -> None:
        """恢复表格默认列宽、收起详情区与运行日志，并清除持久化的分隔条位置。"""
        if (
            self.result_view.detail_dialog
            and self.result_view.detail_dialog.isVisible()
        ):
            self.result_view.detail_dialog.reject()
        self.result_view.user_split_state = None
        self.settings.remove("result-splitter")
        self.result_view.toggle.setChecked(False)
        self.result_view.splitter.setSizes([500, 180])
        self.log_toggle.setChecked(False)
        for col_idx, col_width in enumerate(
            [42, 135, 140, 150, 300, 200, 140]
        ):
            self.table.setColumnWidth(col_idx, col_width)

    def toggle_log(self, checked: bool) -> None:
        """展开或收起底部运行日志；当主窗口高度小于 650px 时自动改为在独立弹窗中展示日志。"""
        if checked and self.height() < 650:
            self.log_toggle.blockSignals(True)
            self.log_toggle.setChecked(False)
            self.log_toggle.blockSignals(False)
            self.log_dialog = QDialog(self)
            self.log_dialog.setWindowTitle("本次运行日志")
            dialog_layout = QVBoxLayout(self.log_dialog)
            log_viewer = QTextEdit()
            log_viewer.setReadOnly(True)
            log_viewer.setPlainText(self.log.toPlainText())
            dialog_layout.addWidget(log_viewer)
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(self.log_dialog.accept)
            dialog_layout.addWidget(close_btn)
            from .theme import fit_window

            fit_window(self.log_dialog, 650, 420)
            self.log_dialog.open()
            return
        self.log.setVisible(checked)
        self.log_toggle.setText("收起运行日志" if checked else "展开运行日志")

    def resizeEvent(self, event) -> None:
        """响应式窗口高度适配：小高度屏幕下自动折叠内嵌详情与日志面板，保证核心表格可见。"""
        super().resizeEvent(event)
        if hasattr(self, "result_view") and self.height() < 650:
            self.result_view.toggle.setChecked(False)
            self.log_toggle.setChecked(False)
        if hasattr(self, "result_view"):
            self.result_view.toggle.setEnabled(True)
            self.result_view.toggle.setToolTip(
                "窗口较小时在独立窗口查看曲线和采样详情"
                if self.height() < 650
                else "展开曲线、各层采样与来源详情"
            )
        if hasattr(self, "selection_summary"):
            self.selection_summary.setVisible(self.height() >= 600)
            self.check_button.setVisible(bool(self.depth_issues()))
            self.update_selection()

    def button(self, row, text: str, slot) -> QPushButton:
        """创建按钮并同时绑定无隐私操作轨迹日志（`trace_action`）与目标业务槽函数。"""
        btn = QPushButton(text)
        btn.clicked.connect(self.trace_action)
        btn.clicked.connect(slot)
        row.addWidget(btn)
        return btn

    @Slot()
    def trace_action(self) -> None:
        """在诊断日志中记录用户点击的按钮名称（不包含任何文件路径或业务数据）。"""
        from .diagnostics import action

        sender_btn = self.sender()
        action(sender_btn.text() if sender_btn else "未知按钮")

    @staticmethod
    def user_error(message) -> str:
        """将底层异常文本映射为面向用户的友好中文提示。"""
        from .user_messages import error_text

        return error_text(message)

    def notify(self, title: str, message: str) -> None:
        """统一通知出口：常规校验提示显示在底部状态栏，系统/失败/异常级错误弹出非阻塞警告框。"""
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        logging.warning("%s: %s", title, message)
        if not any(
            keyword in title for keyword in ("系统", "失败", "异常")
        ):
            self.summary.setText(title + "：" + self.user_error(message))
            return
        self.message = QMessageBox(
            QMessageBox.Icon.Warning,
            title,
            self.user_error(message),
            QMessageBox.StandardButton.Ok,
            self,
        )
        self.message.setDetailedText(str(message))
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWindowModality(Qt.WindowModality.WindowModal)
        self.message.open()

    def populate(self) -> None:
        """根据 `self.points` 重新填充工作区测点表格，按 `point_id` 恢复当前焦点行与复选状态。"""
        current_item = self.table.item(self.table.currentRow(), 1)
        focused_point_id = (
            current_item.data(Qt.ItemDataRole.UserRole)
            if current_item
            else None
        )
        self.checked_ids.intersection_update(
            point.point_id for point in self.points
        )
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.points))

        for row, point in enumerate(self.points):
            try:
                rule_title = (
                    point.layer_rule_name
                    or f"自定义规则（{point.layer_count}层）"
                    if point.layer_positions is not None
                    else (
                        f"系统规则：{point.layer_count}层"
                        if point.layers is not None
                        else f"按水深自动选择：系统规则{point.layer_count}层"
                    )
                )
                scheme_display = (
                    rule_title
                    + " · "
                    + "、".join(layer["label"] for layer in point.layer_plan)
                )
            except ValueError:
                scheme_display = "根据水深自动选择"

            source_display = {
                "manual": "人工设置",
                "estimated": "自动估算",
                "pending": "处理时自动估算",
                "unavailable": "需填写或确认",
            }.get(point.depth_source, "未识别")
            if (
                (point.depth_estimate or {}).get("confidence") == "需核对"
                and point.depth_source == "estimated"
            ):
                source_display += (
                    " · 需要确认"
                    if not (point.depth_estimate or {}).get("confirmed")
                    else " · 已确认"
                )

            time_range_display = (
                (
                    point.start.replace("T", " ")
                    if point.start
                    else "观测起点"
                )
                + " → "
                + (point.end.replace("T", " ") if point.end else "观测终点")
            )

            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(
                Qt.CheckState.Checked
                if point.point_id in self.checked_ids
                else Qt.CheckState.Unchecked
            )
            check_item.setData(Qt.ItemDataRole.UserRole, point.point_id)
            self.table.setItem(row, 0, check_item)

            row_values = [
                point.code,
                str(point.depth) if point.depth is not None else "",
                source_display,
                scheme_display,
                time_range_display,
                f"{len(point.files)} 个文件 · "
                + (Path(point.files[0]).name if point.files else "未选择"),
            ]
            for col, cell_text in enumerate(row_values, 1):
                cell_item = QTableWidgetItem(cell_text)
                cell_item.setData(Qt.ItemDataRole.UserRole, point.point_id)
                cell_item.setToolTip(cell_text)
                if col not in (1, 2):
                    cell_item.setFlags(
                        cell_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                    )
                if col == 6:
                    cell_item.setToolTip("\n".join(point.files))
                if col == 3 and (
                    "确认" in source_display or source_display == "未识别"
                ):
                    cell_item.setIcon(
                        self.style().standardIcon(
                            QStyle.StandardPixmap.SP_MessageBoxWarning
                        )
                    )
                self.table.setItem(row, col, cell_item)

        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        if focused_point_id:
            for row_idx in range(self.table.rowCount()):
                if (
                    self.table.item(row_idx, 1).data(Qt.ItemDataRole.UserRole)
                    == focused_point_id
                ):
                    self.table.setCurrentCell(row_idx, 1)
                    break
        self.table.blockSignals(False)
        self.table.setVisible(bool(self.points))
        self.empty.setVisible(not self.points)

        source_folders = {
            str(Path(file_path).parent)
            for point in self.points
            for file_path in point.files
        }
        self.input_summary = (
            f"{len(self.points)} 个测点 · {sum(len(p.files) for p in self.points)} 个文件 · 来自 {len(source_folders)} 个文件夹"
            if self.points
            else ""
        )
        self.input.setText(self.input_summary)
        self.input.setToolTip("\n".join(sorted(source_folders)))
        self.update_selection()
        self.refresh_ready()

    def sync_points(self) -> None:
        """读取并验证表格内联编辑的测点编号与水深列，仅在整表全部通过校验后原子替换 `self.points`。"""
        updated_points = list(self.points)
        for row in range(self.table.rowCount()):
            point_idx = self.table_point_index(row)
            if point_idx < 0:
                continue
            point = self.points[point_idx]
            code_text = self.table.item(row, 1).text().strip()
            depth_text = self.table.item(row, 2).text().strip()
            try:
                parsed_depth = float(depth_text) if depth_text else None
            except ValueError:
                raise ValueError(f"{code_text}：水深请输入数值（米）")
            updated_points[point_idx] = replace(
                point,
                code=code_text,
                depth=parsed_depth,
                depth_source=(
                    ("manual" if parsed_depth is not None else "pending")
                    if parsed_depth != point.depth
                    else point.depth_source
                ),
                depth_estimate=(
                    None
                    if parsed_depth != point.depth
                    else point.depth_estimate
                ),
            )
        validate_codes(point.code for point in updated_points)
        for point in updated_points:
            point.validate(require_depth=False)
        self.points = updated_points

    def collect(self) -> Batch:
        """同步表格内联编辑结果，生成深拷贝的 `Batch` 配置并执行处理前完整性校验。"""
        self.sync_points()
        batch = replace(
            self.batch,
            input_dir="",
            output_dir=self.output.text(),
            points=[
                replace(point, files=list(point.files))
                for point in self.points
            ],
        )
        batch.validate(require_depth=False)
        if any(not point.files for point in batch.points):
            raise ValueError("每个测点都需要选择至少一个DAT文件")
        return batch

    @Slot(QTableWidgetItem)
    def point_changed(self, item: QTableWidgetItem) -> None:
        """响应表格单元格变更：第 0 列更新勾选身份，第 1/2 列执行内联编号/水深校验并标记错误单元格。"""
        if item.column() == 0:
            point_id = item.data(Qt.ItemDataRole.UserRole)
            if item.checkState() == Qt.CheckState.Checked:
                self.checked_ids.add(point_id)
            else:
                self.checked_ids.discard(point_id)
            self.update_selection()
            return
        if item.column() not in (1, 2):
            return
        try:
            self.sync_points()
            self.populate()
            self.reset_results()
        except ValueError as exc:
            self.table.item(item.row(), 3).setText("输入有误")
            self.table.item(item.row(), 3).setToolTip(str(exc))
            self.flag_field(item.row(), item.column(), str(exc))
            self.ready.setText(str(exc))
            self.start.setEnabled(False)

    @Slot(int, int)
    def edit_double_clicked(self, _row: int, col: int) -> None:
        """双击除复选框外的任意测点单元格时，打开该测点的配置编辑窗口。"""
        if col > 0:
            self.edit_point()

    def reset_results(self) -> None:
        """检查工作区草稿是否已偏离历史运行快照，更新过时标记（`self.stale`）但完整保留历史结果。"""
        if self.entries:
            has_changed = any(
                self.entry_changed(entry) for entry in self.entries.values()
            )
            if has_changed or has_changed != self.stale:
                self.stale = has_changed
                self.show_point()
        self.refresh_ready()

    @Slot()
    def clear_inputs(self) -> None:
        """清空当前工作区测点、结果预览与运行日志（不删除任何原始 DAT 文件或已导出的历史 CSV）。"""
        if self.analysis_worker and self.analysis_worker.isRunning():
            self.analysis_worker.cancel.set()
        self.checked_ids.clear()
        self.points = []
        self.batch = replace(self.batch, points=[])
        self.results = {}
        self.report = {}
        self.entries = {}
        self.stale = False
        self.point_choice.clear()
        self.result_view.clear()
        self.log.clear()
        self.progress.setValue(0)
        self.progress.hide()
        self.cancel.hide()
        self.point_choice.setToolTip("")
        self.run_summary.setText("尚无运行结果")
        self.retry.setEnabled(False)
        self.retry.hide()
        self.open_button.setEnabled(False)
        self.task_state = "无数据"
        self.input.clear()
        self.populate()
        self.pages.setCurrentIndex(0)
        self.summary.setText("当前列表已清空；原始文件与历史输出保留")

    @Slot()
    def remove_points(self) -> None:
        """从当前工作区列表中移除已勾选的测点（不删除磁盘文件）。"""
        checked_row_set = set(self.checked_rows())
        self.points = [
            point
            for row, point in enumerate(self.points)
            if row not in checked_row_set
        ]
        self.populate()
        self.reset_results()

    def pick_directory(
        self, action: str, title: str, folder: str = ""
    ) -> None:
        """启动隔离的原生文件夹选择器子进程，并记录本次选择的业务用途（`'input'` 或 `'output'`）。"""
        if self.file_picker and self.file_picker.isVisible():
            return
        self.folder_action = action
        self.file_picker = FilePicker(
            self,
            title,
            folder,
            directory=True,
            dat_directory=(action == "input"),
        )
        self.file_picker.filesSelected.connect(self.directory_selected)
        self.file_picker.error.connect(self.picker_error)
        self.file_picker.finished.connect(self.try_close)
        self.file_picker.open()

    @Slot(str)
    def picker_error(self, message: str) -> None:
        """展示原生文件/文件夹选择器的错误提示。"""
        self.notify("系统文件选择器", message)

    @Slot()
    def scan(self) -> None:
        """打开包含原始 DAT 文件的文件夹选择流程。"""
        self.pick_directory(
            "input", "选择包含原始DAT的文件夹", self.pending_folder
        )

    @Slot()
    def choose_output(self) -> None:
        """打开 CSV 结果保存目录的选择流程。"""
        self.pick_directory("output", "选择CSV保存文件夹", self.output.text())

    @Slot(list)
    def directory_selected(self, paths: list[str]) -> None:
        """根据 `self.folder_action` 处理选定的 CSV 输出目录或文件夹内扫描出的 DAT 文件列表。"""
        if not paths or self.closing_requested:
            return
        try:
            selected_folder = Path(paths[0]).resolve()
            if self.folder_action == "output":
                from .runner import protect_output

                protect_output(
                    selected_folder,
                    [
                        Path(file_path).parent
                        for point in self.points
                        for file_path in point.files
                    ],
                )
                self.output.setText(str(selected_folder))
                self.refresh_ready()
                return
            dat_files = paths
            if not dat_files:
                self.notify("没有DAT数据", "所选文件夹中未找到DAT文件。")
                return
            self.pending_folder = str(Path(dat_files[0]).parent)
            self.add_files(paths=[str(file_path) for file_path in dat_files])
        except Exception as exc:
            self.notify("无法选择文件夹", str(exc))

    @Slot()
    def add_dat_files(self) -> None:
        """打开支持多选 `.dat` 文件的隔离原生文件选择器。"""
        if self.file_picker and self.file_picker.isVisible():
            return
        self.file_picker = FilePicker(
            self, "选择一个或多个原始DAT文件", self.pending_folder
        )
        self.file_picker.filesSelected.connect(self.dat_files_selected)
        self.file_picker.error.connect(self.picker_error)
        self.file_picker.finished.connect(self.try_close)
        self.file_picker.open()

    @Slot(list)
    def dat_files_selected(self, paths: list[str]) -> None:
        """接收多选 DAT 文件列表并调用 `add_files` 导入，异常时在主界面友好提示。"""
        if self.closing_requested:
            return
        try:
            self.add_files(paths)
        except Exception as exc:
            self.notify("无法添加DAT", str(exc))

    def add_files(self, paths: list[str]) -> None:
        """先原子校验整批追加文件的数量/体积预算与编号大小写冲突，通过后再按 `#` 前缀归并到测点列表。"""
        self.sync_points()
        # 先检查整次追加与累计预算，避免中途报错留下半成品列表
        from .input_policy import LIMITS, check_files, enforce

        validated_paths = check_files(
            paths,
            max_files=LIMITS.selection_entries,
            max_bytes=LIMITS.batch_bytes,
        )
        check_files(
            [file_path for point in self.points for file_path in point.files]
            + list(validated_paths),
            max_files=LIMITS.batch_files,
            max_bytes=LIMITS.batch_bytes,
        )

        prepared_paths: list[str] = []
        known_codes_by_case = {
            point.code.casefold(): point.code for point in self.points
        }
        for raw_path in validated_paths:
            resolved_path = Path(raw_path).resolve()
            if (
                resolved_path.suffix.lower() != ".dat"
                or not resolved_path.is_file()
            ):
                raise ValueError("只能选择存在的原始DAT文件")
            inferred_code = resolved_path.stem.split("#")[0]
            Point(inferred_code, None).validate(require_depth=False)
            if (
                inferred_code.casefold() in known_codes_by_case
                and known_codes_by_case[inferred_code.casefold()]
                != inferred_code
            ):
                validate_codes(
                    [
                        known_codes_by_case[inferred_code.casefold()],
                        inferred_code,
                    ]
                )
            known_codes_by_case[inferred_code.casefold()] = inferred_code
            prepared_paths.append(str(resolved_path))

        file_counts_per_code: Counter[str] = Counter()
        for existing_point in self.points:
            file_counts_per_code[existing_point.code] = len(
                existing_point.files
            )
        existing_all_files = {
            file_path for point in self.points for file_path in point.files
        }
        for resolved_str in prepared_paths:
            inferred_code = Path(resolved_str).stem.split("#")[0]
            if resolved_str not in existing_all_files:
                file_counts_per_code[inferred_code] += 1
            enforce(
                file_counts_per_code[inferred_code],
                LIMITS.point_files,
                f"测点{inferred_code}文件数",
            )

        files_by_point = {
            point.code: list(point.files) for point in self.points
        }
        for resolved_str in prepared_paths:
            files_by_point.setdefault(
                Path(resolved_str).stem.split("#")[0], []
            ).append(resolved_str)
        for point_files in files_by_point.values():
            check_files(
                point_files,
                max_files=LIMITS.point_files,
                max_bytes=LIMITS.point_bytes,
            )

        grouped_points = {point.code: point for point in self.points}
        known_resolved_files = {
            str(Path(file_path).resolve())
            for point in self.points
            for file_path in point.files
        }
        added_count = 0
        merged_into_existing: dict[str, list[str]] = {}
        existing_codes = set(grouped_points)

        for resolved_str in prepared_paths:
            resolved_path = Path(resolved_str).resolve()
            if (
                resolved_path.suffix.lower() != ".dat"
                or not resolved_path.is_file()
            ):
                raise ValueError("只能选择存在的原始DAT文件")
            if str(resolved_path) in known_resolved_files:
                continue
            inferred_code = resolved_path.stem.split("#")[0]
            if inferred_code not in grouped_points:
                new_point = Point(
                    inferred_code, None, files=[], depth_source="pending"
                )
                grouped_points[inferred_code] = new_point
                self.points.append(new_point)
            elif inferred_code in existing_codes:
                merged_into_existing.setdefault(inferred_code, []).append(
                    resolved_path.name
                )
            grouped_points[inferred_code].files.append(str(resolved_path))
            known_resolved_files.add(str(resolved_path))
            added_count += 1
            if grouped_points[inferred_code].depth_source != "manual":
                grouped_points[inferred_code].depth = None
                grouped_points[inferred_code].depth_source = "pending"
            else:
                grouped_points[inferred_code].depth_estimate = None
            grouped_points[inferred_code].depth_estimate = None

        self.populate()
        self.reset_results()
        self.summary.setText(
            f"已选择 {len(self.points)} 个测点，请核对水深与文件归属"
        )
        self.log.append(
            f"追加 {added_count} 个文件；按编号合并："
            + (
                "；".join(
                    code + "：" + "、".join(names)
                    for code, names in merged_into_existing.items()
                )
                or "无"
            )
        )
        self.summary.setText(
            f"已添加 {added_count} 个文件，其中 {sum(len(files) for files in merged_into_existing.values())} 个归入已有测点。"
        )

    def begin_analysis(self) -> None:
        """为 `self.pending_batch` 中尚未填写水深的测点启动后台 `AnalysisWorker` 子进程，并暂时锁定工作区编辑。"""
        pending_points = [
            point
            for point in self.pending_batch.points
            if point.depth is None
        ]
        self.analysis_result = None
        self.analysis_worker = AnalysisWorker(
            replace(self.pending_batch, points=pending_points)
        )
        self.analysis_worker.analysed.connect(self.analysis_done)
        self.analysis_worker.error.connect(self.analysis_error)
        self.analysis_worker.progress.connect(self.analysis_progress)
        self.analysis_worker.finished.connect(self.analysis_finished)
        self.analysis_worker.finished.connect(self.try_close)
        self.task_state = "识别中"
        self.controls.setEnabled(False)
        self.advanced_button.setEnabled(False)
        self.cancel.show()
        self.cancel.setEnabled(True)
        self.progress.show()
        self.progress.setRange(0, len(pending_points))
        self.progress.setValue(0)
        self.summary.setText("正在估算未填写的水深，完成后继续处理…")
        self.analysis_worker.start()

    def analysis_done(self, data: dict) -> None:
        """暂存后台水深估算子进程返回的响应字典，待进程 `finished` 后统一推进状态。"""
        self.analysis_result = data

    def analysis_progress(self, index: int, total: int, point: dict) -> None:
        """将后台水深估算的逐点进度更新到状态栏与进度条。"""
        self.progress.setValue(index)
        self.summary.setText(
            f"正在估算水深 {index}/{total}：{point['point']}"
        )

    def analysis_error(self, message: str) -> None:
        """记录后台水深估算错误，并将本次响应标记为已取消以触发恢复流程。"""
        self.analysis_result = dict(cancelled=True, error=message)
        self.log.append(message)

    def analysis_finished(self) -> None:
        """水深估算子进程退出后应用结果；若发生异常则统一转入 `preparation_error` 恢复界面。"""
        if self.pending_batch is None:
            return
        try:
            self.apply_analysis_result()
        except Exception as exc:
            self.preparation_error(exc)

    def apply_analysis_result(self) -> None:
        """检查用户是否已取消或请求关闭窗口；正常完成时将估算水深合入 `self.pending_batch` 并进入确认门禁。"""
        result_payload = self.analysis_result or dict(cancelled=True)
        if (
            result_payload.get("cancelled")
            or self.task_state == "取消中"
            or self.closing_requested
        ):
            self.cancel_preparation()
            if result_payload.get("error"):
                self.task_state = "估算失败"
                self.summary.setText(
                    "水深估算未完成，请检查数据或重试；详细原因见运行日志"
                )
            return
        estimates_by_code = result_payload.get("estimates", {})
        updated_points: list[Point] = []
        for point in self.pending_batch.points:
            if point.depth is None:
                estimate_info = estimates_by_code.get(
                    point.code, dict(value=None, reason="未取得估算结果")
                )
                point = replace(
                    point,
                    depth=estimate_info.get("value"),
                    depth_source=(
                        "estimated"
                        if estimate_info.get("value") is not None
                        else "unavailable"
                    ),
                    depth_estimate=estimate_info,
                )
            updated_points.append(point)
        self.pending_batch = replace(self.pending_batch, points=updated_points)
        self.prepare_or_confirm()

    def prepare_or_confirm(self) -> None:
        """推进水深准备阶段，捕获任何回调异常以保证主界面可恢复。"""
        try:
            self.update_preparation()
        except Exception as exc:
            self.preparation_error(exc)

    def update_preparation(self) -> None:
        """按 `point_id` 将水深估算结果同步回工作区列表；若存在需人工确认的测点则弹出确认框，否则立即启动批处理。"""
        from .depth_workflow import requires_confirmation

        updates_by_id = {
            point.point_id: point for point in self.pending_batch.points
        }
        self.points = [
            updates_by_id.get(point.point_id, point) for point in self.points
        ]
        self.populate()
        self.reset_results()

        points_needing_review = [
            point
            for point in self.pending_batch.points
            if requires_confirmation(point)
        ]
        if points_needing_review:
            from .depth_review import DepthReviewDialog

            self.task_state = "等待水深确认"
            self.depth_review = DepthReviewDialog(
                self, points_needing_review, True
            )
            self.depth_review.accepted.connect(self.depth_confirmed)
            self.depth_review.rejected.connect(self.cancel_preparation)
            self.depth_review.open()
            self.progress.hide()
            self.summary.setText(
                f"{len(points_needing_review)} 个测点需要填写或确认水深，确认后继续本次处理"
            )
            return
        self.launch_processing(self.pending_batch)

    def depth_confirmed(self) -> None:
        """将用户在“水深填写与确认”弹窗中确认的水深合入 `self.pending_batch`，继续启动批处理。"""
        if self.pending_batch is None:
            return
        try:
            confirmed_by_id = {
                point.point_id: point for point in self.depth_review.updated
            }
            self.pending_batch = replace(
                self.pending_batch,
                points=[
                    confirmed_by_id.get(point.point_id, point)
                    for point in self.pending_batch.points
                ],
            )
            self.prepare_or_confirm()
        except Exception as exc:
            self.preparation_error(exc)

    def preparation_error(self, exc: Exception) -> None:
        """记录水深准备阶段的异常日志并安全取消当前准备会话，恢复主界面控件可交互状态。"""
        logging.exception("水深准备失败")
        self.log.append(str(exc))
        self.cancel_preparation()
        if self.pending_batch is None:
            self.task_state = "准备失败"
        self.summary.setText(
            "未能开始处理，请检查设置后重试：" + self.user_error(str(exc))
        )

    def cancel_preparation(self) -> None:
        """取消水深准备或确认流程：若后台子进程仍在运行则先请求取消，否则立即恢复主界面编辑状态。"""
        if any(
            worker and worker.isRunning()
            for worker in (self.analysis_worker, self.worker)
        ):
            self.request_cancel()
            return
        self.pending_batch = None
        self.task_state = "已取消"
        self.summary.setText(
            "本次处理已取消，任务未完成；列表和上次结果保留"
        )
        self.controls.setEnabled(True)
        self.advanced_button.setEnabled(True)
        self.cancel.hide()
        self.cancel.setEnabled(False)
        self.progress.hide()
        self.refresh_ready()

    @Slot()
    def bulk_edit(self) -> None:
        """同步表格内联编辑后，打开已勾选测点的批量设置对话框。"""
        try:
            self.sync_points()
            checked_indices = self.checked_rows()
            if not checked_indices:
                raise ValueError("请先勾选需要批量修改的测点")
            self.bulk_dialog = BulkDialog(
                self, self.points, checked_indices
            )
            self.bulk_dialog.accepted.connect(self.bulk_applied)
            self.bulk_dialog.open()
        except Exception as exc:
            self.notify("无法批量编辑", str(exc))

    @Slot()
    def bulk_applied(self) -> None:
        """应用批量设置对话框通过校验后的测点列表，刷新表格并更新历史结果过时提示。"""
        self.points = self.bulk_dialog.updated
        self.populate()
        self.reset_results()

    def selected_row(self) -> int:
        """返回当前高亮选中行对应的 `self.points` 索引；未选中时抛出可读提示异常。"""
        point_idx = self.table_point_index(self.table.currentRow())
        if point_idx < 0:
            raise ValueError("请先点击一个测点")
        return point_idx

    @Slot()
    def edit_point(self) -> None:
        """记录当前选中测点的稳定身份 `point_id`，并打开单测点编辑对话框。"""
        try:
            self.sync_points()
            self.edit_row = self.selected_row()
            self.edit_point_id = self.points[self.edit_row].point_id
            self.point_dialog = PointDialog(
                self,
                self.points[self.edit_row],
                {
                    point.code
                    for idx, point in enumerate(self.points)
                    if idx != self.edit_row
                },
            )
            self.point_dialog.accepted.connect(self.point_edited)
            self.point_dialog.open()
        except Exception as exc:
            self.notify("无法编辑测点", str(exc))

    @Slot()
    def point_edited(self) -> None:
        """按稳定身份 `edit_point_id` 定位原测点，校验编号无冲突后提交单点编辑结果。"""
        try:
            self.edit_row = next(
                idx
                for idx, point in enumerate(self.points)
                if point.point_id == self.edit_point_id
            )
            updated_point = self.point_dialog.updated
            updated_point.validate(require_depth=False)
            validate_codes(
                [
                    point.code
                    for idx, point in enumerate(self.points)
                    if idx != self.edit_row
                ]
                + [updated_point.code]
            )
            self.points[self.edit_row] = updated_point
            self.populate()
            self.reset_results()
        except Exception as exc:
            self.notify("测点设置无效", str(exc))

    @Slot()
    def edit_files(self) -> None:
        """打开当前选中测点的数据文件关联管理窗口。"""
        try:
            self.edit_row = self.selected_row()
            self.edit_point_id = self.points[self.edit_row].point_id
            target_point = self.points[self.edit_row]
            self.mapping_dialog = FileMappingDialog(
                self, target_point.code, target_point.files
            )
            self.mapping_dialog.accepted.connect(self.files_edited)
            self.mapping_dialog.open()
        except Exception as exc:
            self.notify("无法调整文件", str(exc))

    @Slot()
    def files_edited(self) -> None:
        """提交测点关联文件变更：若文件清单发生变化，保留人工水深而将自动推算水深重置为待重新估算。"""
        self.edit_row = next(
            idx
            for idx, point in enumerate(self.points)
            if point.point_id == self.edit_point_id
        )
        point = self.points[self.edit_row]
        new_files = self.mapping_dialog.files
        self.points[self.edit_row] = replace(
            point,
            files=new_files,
            depth=(
                None
                if point.depth_source != "manual" and new_files != point.files
                else point.depth
            ),
            depth_source=(
                "pending"
                if point.depth_source != "manual" and new_files != point.files
                else point.depth_source
            ),
            depth_estimate=(
                point.depth_estimate if new_files == point.files else None
            ),
        )
        self.populate()
        self.reset_results()

    @Slot()
    def parameters(self) -> None:
        """同步表格内联编辑后，打开高级设置窗口（自定义分层规则、筛选参数与 CSV 选项）。"""
        try:
            self.sync_points()
            self.dialog = AdvancedDialog(self, self.batch, self.points)
            self.dialog.accepted.connect(self.settings_applied)
            self.dialog.open()
        except Exception as exc:
            self.notify("无法打开高级设置", str(exc))

    @Slot()
    def settings_applied(self) -> None:
        """应用高级设置窗口提交的筛选参数与 CSV 选项，并按配置差异更新历史结果的过时提示。"""
        before_snapshot = (
            self.batch.rules,
            self.batch.encoding,
            self.batch.complete_rows_only,
            [self.point_signature(point) for point in self.points],
        )
        self.batch = replace(
            self.batch,
            rules=self.dialog.rules,
            encoding=self.dialog.encoding,
            complete_rows_only=self.dialog.complete_rows_only,
        )
        after_snapshot = (
            self.batch.rules,
            self.batch.encoding,
            self.batch.complete_rows_only,
            [self.point_signature(point) for point in self.points],
        )
        self.populate()
        self.reset_results()
        self.summary.setText(
            "设置已修改，重新处理后生效；当前显示上次结果"
            if before_snapshot != after_snapshot and self.entries
            else (
                "高级设置已应用"
                if before_snapshot != after_snapshot
                else "设置未变化；现有结果保留"
            )
        )

    @Slot()
    def execute(self) -> None:
        """启动当前列表中全部测点的完整水深准备与批处理导出流程。"""
        self.start_run(False)

    @Slot()
    def retry_failed(self) -> None:
        """仅针对上次运行失败且仍在当前列表中的测点发起重试处理。"""
        self.start_run(True)

    def start_run(self, retry: bool) -> None:
        """检查互斥后台任务、校验输出目录安全边界并冻结本次批次配置，随后进入水深推算或确认流程。"""
        if self.pending_batch is not None or any(
            worker and worker.isRunning()
            for worker in (self.analysis_worker, self.worker)
        ):
            return
        try:
            tools_dlg = getattr(self, "tools_dialog", None)
            if tools_dlg and tools_dlg.busy():
                raise ValueError("请先结束实用工具中的任务。")
            batch = self.collect()
            if retry:
                failed_point_ids = {
                    entry["point"].point_id
                    for entry in self.entries.values()
                    if entry["status"] == "failed"
                }
                batch = replace(
                    batch,
                    points=[
                        point
                        for point in batch.points
                        if point.point_id in failed_point_ids
                    ],
                )
                batch.validate(require_depth=False)
            from .runner import protect_output

            protect_output(
                batch.output_dir,
                [
                    Path(file_path).parent
                    for point in batch.points
                    for file_path in point.files
                ],
            )
            self.batch = batch
            self.pending_batch = deepcopy(batch)
            self.is_retry = retry
            self.controls.setEnabled(False)
            self.advanced_button.setEnabled(False)
            self.cancel.show()
            self.cancel.setEnabled(True)
            if any(point.depth is None for point in batch.points):
                self.begin_analysis()
            else:
                self.prepare_or_confirm()
        except Exception as exc:
            if self.pending_batch is not None:
                self.preparation_error(exc)
            else:
                self.notify("无法开始处理", str(exc))

    def launch_processing(self, batch: Batch) -> None:
        """在水深准备与人工确认完成后，冻结 `self.active_batch` 并启动后台批处理 `Worker` 子进程。

        注：存在输入文件损坏的测点在水深准备阶段不会弹窗要求人工填水深（其 `depth` 仍可能为 `None`），
        由后台批处理进程在逐测点隔离校验时统一报告 `failed`。
        """
        batch.validate(require_depth=False)
        self.batch = batch
        self.active_batch = deepcopy(batch)
        self.worker = Worker(batch)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.finished)
        self.worker.finished.connect(self.try_close)
        self.controls.setEnabled(False)
        self.start.setEnabled(False)
        self.retry.setEnabled(False)
        self.cancel.show()
        self.cancel.setEnabled(True)
        self.progress.show()
        self.progress.setRange(0, len(batch.points))
        self.progress.setValue(0)
        self.task_state = "处理中"
        self.advanced_button.setEnabled(False)
        self.summary.setText("处理中：正在筛选数据…")
        self.worker.start()
        self.pending_batch = None

    @Slot()
    def request_cancel(self) -> None:
        """向当前活动的水深确认弹窗、水深估算子进程或批处理子进程发出协作取消请求。"""
        if self.task_state == "等待水深确认":
            self.depth_review.reject()
            return
        self.task_state = "取消中"
        self.cancel.setEnabled(False)
        if self.analysis_worker and self.analysis_worker.isRunning():
            self.analysis_worker.cancel.set()
            self.summary.setText("正在停止，请稍候…")
            return
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set()
            self.summary.setText("正在停止，请稍候…")
            return
        self.cancel_preparation()

    @Slot(int, int, object)
    def on_progress(self, index: int, total: int, point: dict) -> None:
        """更新后台批处理的逐测点进度条、运行日志与底部状态栏摘要。"""
        self.progress.setValue(index)
        self.log.append(
            escape(
                f"{point['point']}：{STATE.get(point['status'], point['status'])}"
                + ("；" + str(point["error"]) if point.get("error") else "")
            )
        )
        self.summary.setText(
            ("取消中；" if self.task_state == "取消中" else "处理中；")
            + f"已处理 {index}/{total} 个测点"
        )

    @Slot(object)
    def on_done(self, value: tuple) -> None:
        """接收后台批处理结果，按 `point_id` 更新历史运行快照并自动切换至“处理结果”页。"""
        self.directory, self.report, new_results = value
        if not self.is_retry:
            self.results = {}
            self.entries = {}
        batch_snapshot = deepcopy(self.active_batch)
        for point_state in self.report["points"]:
            point = next(
                candidate
                for candidate in batch_snapshot.points
                if candidate.code == point_state["point"]
            )
            identity = point.point_id
            self.results.pop(identity, None)
            if point.code in new_results:
                self.results[identity] = new_results[point.code]
            self.entries[identity] = dict(
                status=point_state["status"],
                error=point_state.get("error", ""),
                directory=self.directory,
                point=point,
                batch=batch_snapshot,
                started=self.report["started"],
            )

        completed_count = sum(
            item["status"] == "completed" for item in self.report["points"]
        )
        failed_count = sum(
            item["status"] == "failed" for item in self.report["points"]
        )
        csv_hint = (
            "已完成测点的CSV已保存"
            if completed_count
            else "未生成完成测点的CSV"
        )
        self.summary.setText(
            csv_hint + "；请在结果页查看缺测和需要核查的记录"
        )

        self.point_choice.blockSignals(True)
        self.point_choice.clear()
        from .result_projection import ReviewProjection
        from .result_view import CHECK, MISSING

        code_occurrences = Counter(
            entry["point"].code for entry in self.entries.values()
        )
        projections = {
            identity: ReviewProjection(result)
            for identity, result in self.results.items()
        }
        for identity, entry in self.entries.items():
            result = self.results.get(identity, {})
            projection = projections.get(identity, ReviewProjection(None))
            needs_quality_review = any(
                (CHECK | MISSING).intersection(projection.status(row_idx))
                for row_idx in range(len(result.get("rows", [])))
            )
            status_label = STATE.get(entry["status"], entry["status"]) + (
                " · 需核查"
                if needs_quality_review and entry["status"] == "completed"
                else ""
            )
            status_icon = (
                QStyle.StandardPixmap.SP_MessageBoxCritical
                if entry["status"] == "failed"
                else (
                    QStyle.StandardPixmap.SP_MessageBoxWarning
                    if needs_quality_review
                    else (
                        QStyle.StandardPixmap.SP_DialogApplyButton
                        if entry["status"] == "completed"
                        else QStyle.StandardPixmap.SP_MessageBoxInformation
                    )
                )
            )
            code = entry["point"].code
            run_kind = (
                "本次结果"
                if entry["directory"] == self.directory
                else "上次结果"
            )
            started_stamp = datetime.fromisoformat(entry["started"]).strftime(
                "%m-%d %H:%M:%S"
            )
            disambiguation_suffix = (
                f" · {run_kind} {started_stamp}"
                if code_occurrences[code] > 1
                else ""
            )
            self.point_choice.addItem(
                self.style().standardIcon(status_icon),
                code + " · " + status_label + disambiguation_suffix,
                identity,
            )
            self.point_choice.setItemData(
                self.point_choice.count() - 1,
                f"{code} · {run_kind}\n运行时间：{entry['started'].replace('T', ' ')}\n结果目录：{entry['directory']}",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.point_choice.blockSignals(False)
        if self.entries:
            self.point_choice.setCurrentIndex(0)
        self.open_button.setEnabled(True)
        self.retry.setEnabled(bool(failed_count))
        self.log.append("结果目录：" + str(self.directory))

        missing_hours = sum(
            bool(MISSING.intersection(row["status"]))
            for result in self.results.values()
            for row in result["rows"]
        )
        review_hours = sum(
            bool(CHECK.intersection(projection.status(row_idx)))
            for projection in projections.values()
            for row_idx in range(len(projection.rows))
        )
        started_display = datetime.fromisoformat(
            self.report["started"]
        ).strftime("%m月%d日 %H:%M")
        total_completed_entries = sum(
            entry["status"] == "completed" for entry in self.entries.values()
        )
        total_failed_entries = sum(
            entry["status"] == "failed" for entry in self.entries.values()
        )
        self.run_summary.setText(
            f"本次处理：{started_display}\n完成 {total_completed_entries} 个测点 · 失败 {total_failed_entries} · 缺测 {missing_hours} 整点 · 需核查 {review_hours} 整点"
        )
        self.task_state = STATE.get(
            self.report["status"], self.report["status"]
        )
        self.stale = False
        self.show_point()
        self.pages.setCurrentIndex(1)

    @Slot(str)
    def on_error(self, message: str) -> None:
        """记录并弹窗展示后台批处理子进程级错误，保留上次已完成的结果。"""
        self.task_state = "失败"
        logging.error("处理失败：%s", message)
        self.summary.setText("处理失败；上次运行结果保留")
        self.notify("处理失败", message)

    @Slot()
    def finished(self) -> None:
        """后台批处理子进程退出后恢复工作区控件、隐藏进度条并按需展示“仅重试失败测点”按钮。"""
        self.pending_batch = None
        self.controls.setEnabled(True)
        self.cancel.setEnabled(False)
        self.cancel.hide()
        self.progress.hide()
        self.advanced_button.setEnabled(True)
        has_failed = any(
            entry["status"] == "failed" for entry in self.entries.values()
        )
        self.retry.setEnabled(has_failed)
        self.retry.setVisible(has_failed)
        self.refresh_ready()

    @Slot()
    def show_point(self, *_args) -> None:
        """在“处理结果”页按所选历史条目自身的运行快照展示采样表格与过程线。"""
        identity = self.point_choice.currentData()
        entry = self.entries.get(identity)
        if entry is None:
            self.result_view.clear()
            self.open_button.setEnabled(False)
            return
        self.directory = entry["directory"]
        self.open_button.setEnabled(True)
        point = entry["point"]
        code = point.code
        source_text = {"manual": "人工设置", "estimated": "自动估算"}.get(
            point.depth_source, "尚未确定"
        )
        stale_prefix = (
            "设置已修改，重新处理后生效；当前显示上次结果 · "
            if self.entry_changed(entry)
            else ""
        )
        tolerance_text = (
            "未检查深度偏差是否超限"
            if entry["batch"].rules.tolerance_m is None
            and entry["batch"].rules.tolerance_ratio is None
            else "按本次容差判定"
        )
        depth_text = (
            f"{point.depth:.3f} m" if point.depth is not None else "未记录"
        )
        context_banner = (
            f"{stale_prefix}{code} · H={depth_text} / {source_text} · {tolerance_text}"
        )
        self.point_choice.setToolTip(
            code + " · " + STATE.get(entry["status"], entry["status"])
        )
        if entry["status"] != "completed":
            self.result_view.clear()
            self.result_view.context.setText(
                context_banner
                + "\n"
                + STATE.get(entry["status"], entry["status"])
                + "："
                + self.user_error(entry["error"])
            )
            self.result_view.details.setPlainText("详细信息：" + entry["error"])
            return
        from .record_store import record_directory

        self.result_view.show_result(
            self.results.get(identity),
            context_banner,
            point,
            record_directory(entry["directory"]) / (point.code + "-curve.json"),
        )

    @Slot()
    def open_results(self) -> None:
        """调用系统文件资源管理器打开当前选中结果测点对应的 CSV 输出文件夹。"""
        identity = self.point_choice.currentData()
        entry = self.entries.get(identity)
        if entry:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(entry["directory"]))
            )

    def show_tools(self) -> None:
        """在主界面无活动批处理或水深估算任务时，创建或激活“实用工具（时间校正）”窗口。"""
        if (self.worker and self.worker.isRunning()) or (
            self.analysis_worker and self.analysis_worker.isRunning()
        ):
            self.notify("请稍候", "本批处理结束后再打开实用工具。")
            return
        from .tools_dialog import ToolsDialog

        if not getattr(self, "tools_dialog", None):
            self.tools_dialog = ToolsDialog(self)
        self.tools_dialog.show()
        self.tools_dialog.raise_()

    def corrected_file(self, filename: str | Path) -> None:
        """接收时间校正向导的原位保存完成通知：将关联该文件的测点标记为输入已变更并重置自动水深。"""
        corrected_path = Path(filename).resolve()
        affected_codes: list[str] = []
        for idx, point in enumerate(self.points):
            if any(
                Path(file_path).resolve() == corrected_path
                for file_path in point.files
            ):
                point = replace(
                    point,
                    depth=(
                        point.depth if point.depth_source == "manual" else None
                    ),
                    depth_source=(
                        "manual"
                        if point.depth_source == "manual"
                        else "pending"
                    ),
                    depth_estimate=None,
                )
                affected_codes.append(point.code)
            self.points[idx] = point

        if affected_codes:
            for entry in self.entries.values():
                if any(
                    Path(file_path).resolve() == corrected_path
                    for file_path in entry["point"].files
                ):
                    entry["input_changed"] = True
            self.result_view.context_loader.cancel()
            self.populate()
            self.reset_results()
            self.summary.setText(
                "关联文件记录时间已校准，请重新点击“开始处理并导出 CSV”更新结果："
                + "、".join(affected_codes)
                + "；当前显示的仍是上次结果。"
            )

    def closeEvent(self, event) -> None:
        """主窗口关闭事件：先协调实用工具、文件选择器及后台工作子进程安全退出，再保存窗口布局并关闭。"""
        self.closing_requested = True
        tools_dlg = getattr(self, "tools_dialog", None)
        if tools_dlg and (
            tools_dlg.busy()
            or (tools_dlg.picker and tools_dlg.picker.isVisible())
        ):
            if tools_dlg.worker and not tools_dlg.worker.ended:
                tools_dlg.worker.finished.connect(self.try_close)
            if tools_dlg.picker and tools_dlg.picker.isVisible():
                tools_dlg.picker.finished.connect(self.try_close)
            tools_dlg.close()
            event.ignore()
            return
        if tools_dlg:
            tools_dlg.close()
        if self.file_picker and self.file_picker.isVisible():
            self.file_picker.reject()
            event.ignore()
            return
        if (self.analysis_worker and self.analysis_worker.isRunning()) or (
            self.worker and self.worker.isRunning()
        ):
            self.request_cancel()
            event.ignore()
            return

        self.settings.setValue("geometry-redesign", self.saveGeometry())
        if self.result_view.user_split_state:
            self.settings.setValue(
                "result-splitter", self.result_view.user_split_state
            )
        else:
            self.settings.remove("result-splitter")
        self.result_view.context_loader.cancel()
        super().closeEvent(event)

    @Slot()
    def try_close(self) -> None:
        """当用户已请求关闭窗口且所有异步子进程均已结束时，在下一事件循环安全关闭主窗口。"""
        if (
            self.closing_requested
            and not (self.file_picker and self.file_picker.isVisible())
            and not (self.analysis_worker and self.analysis_worker.isRunning())
            and not (self.worker and self.worker.isRunning())
        ):
            QTimer.singleShot(0, self.close)


def main() -> int:
    """初始化故障诊断日志、创建 Qt 应用与主窗口、安装全局异常钩子并进入事件循环。"""
    from .diagnostics import install

    log_directory = install()
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Microsoft YaHei UI", 10))
    from PySide6 import __version__ as qt_binding_version

    logging.info("GUI PySide6=%s", qt_binding_version)
    window = Window()

    def exception_hook(exc_type, exc_value, exc_tb) -> None:
        """记录未捕获的主线程界面异常，并通过主窗口向用户展示故障日志保存目录。"""
        logging.error("界面异常", exc_info=(exc_type, exc_value, exc_tb))
        window.notify(
            "操作未完成", f"{exc_value}\n详细记录已保存到：{log_directory}"
        )

    sys.excepthook = exception_hook
    window.show()
    return app.exec()
