"""参考水深推算依据展示及批处理前人工逐点核实确认对话框模块（`DepthReviewDialog`）。

两种工作模式：
1. **只读详情模式（`continuation=False`）**：用户在工作区点击“水深详情”时打开，展示各测点的水深数值、来源（人工填写 / 自动估算 / 尚未确定）、需要核实的原因以及底层统计指标（有效下放剖面数、峰值四分位距、99% 分位深度、分段阈值）。
2. **处理前拦截确认模式（`continuation=True`）**：当用户点击“开始处理”而部分测点水深处于“待确认”或“估算置信度较低（需核对）”时自动弹出，要求用户逐行核对正数水深并勾选“确认”，全部确认后直接无缝接续启动后台批处理。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import chinese_buttons, fit_window, table_style

if TYPE_CHECKING:
    from .models import Point


class DepthReviewDialog(QDialog):
    """参考水深推算依据展示与批处理前人工确认对话框。"""

    def __init__(
        self,
        parent: QWidget | None,
        points: list[Point],
        continuation: bool = False,
    ) -> None:
        """构建测点水深核查表格；当 `continuation=True` 时启用水深数值编辑框与右侧“确认”复选框。"""
        super().__init__(parent)
        self.points = points
        self.updated: list[Point] | None = None
        self.setWindowTitle("水深填写与确认" if continuation else "水深详情")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        fit_window(self, 860, 490)

        root_layout = QVBoxLayout(self)
        self.notice = QLabel(
            "以下测点的水深需要人工核实或填写。请逐行核对正数水深并勾选右侧“确认”，点击“确认并继续处理”即可接续完成本次任务；点击“取消”将停止本次任务。"
            if continuation
            else "以下为所选测点的参考水深推算依据及需核实原因（如需修改水深，请返回主界面使用“编辑当前测点”或“批量设置”）。"
        )
        self.notice.setWordWrap(True)
        root_layout.addWidget(self.notice)

        self.table = QTableWidget(len(points), 5)
        table_style(self.table)
        self.table.setHorizontalHeaderLabels(
            ["测点", "水深（米）", "来源", "需要确认的原因", "确认采用"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.inputs: list[QDoubleSpinBox] = []
        self.checks: list[QCheckBox] = []

        for row_idx, point in enumerate(points):
            estimate_info = point.depth_estimate or {}
            review_reason = estimate_info.get("reason") or (
                "该测点有效下放剖面较少或各次峰值深度波动较大，请结合现场记录核实水深是否合适。"
                if estimate_info.get("confidence") == "需核对"
                else (
                    "自动推算水深较为稳定，仍可结合现场测深记录复核。"
                    if point.depth_source == "estimated"
                    else (
                        "当前采用人工填写的实测水深。"
                        if point.depth is not None
                        else "尚未推算，将在开始处理时自动估算。"
                    )
                )
            )
            if point.depth is None and estimate_info.get("reason"):
                review_reason = (
                    "未能从观测数据中可靠推算水深，请填写现场实测水深。"
                )

            for col_idx, cell_text in [
                (0, point.code),
                (
                    2,
                    {"manual": "人工填写", "estimated": "自动估算"}.get(
                        point.depth_source, "尚未确定"
                    ),
                ),
                (3, review_reason),
            ]:
                table_item = QTableWidgetItem(cell_text)
                table_item.setToolTip(cell_text)
                self.table.setItem(row_idx, col_idx, table_item)

            depth_spin = QDoubleSpinBox()
            depth_spin.setDecimals(6)
            depth_spin.setRange(0, 1_000_000)
            depth_spin.setSpecialValueText("请填写")
            depth_spin.setValue(point.depth or 0)
            depth_spin.setEnabled(continuation)
            self.inputs.append(depth_spin)
            self.table.setCellWidget(row_idx, 1, depth_spin)

            confirm_check = QCheckBox("确认")
            confirm_check.setEnabled(continuation)
            self.checks.append(confirm_check)
            self.table.setCellWidget(row_idx, 4, confirm_check)

        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 125)
        self.table.setColumnWidth(2, 105)
        self.table.setColumnWidth(3, 370)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.resizeRowsToContents()
        root_layout.addWidget(self.table)

        self.error = QLabel()
        self.error.setProperty("role", "error")
        self.error.setWordWrap(True)
        root_layout.addWidget(self.error)

        self.technical = QLabel()
        self.technical.setWordWrap(True)
        self.technical.hide()
        root_layout.addWidget(self.technical)

        tech_button = QPushButton("查看技术信息")
        tech_button.clicked.connect(self.show_technical)
        root_layout.addWidget(tech_button)

        button_box = (
            QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            if continuation
            else QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        )
        if continuation:
            chinese_buttons(button_box, "确认并继续处理")
            button_box.accepted.connect(self.validate_accept)
            button_box.rejected.connect(self.reject)
        else:
            button_box.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
            button_box.rejected.connect(self.reject)
        root_layout.addWidget(button_box)

    def show_technical(self) -> None:
        """展开或收起底层水深推算统计指标（有效下放剖面次数、峰值四分位距 IQR、99% 分位深度、分段阈值）。"""
        detail_lines: list[str] = []
        for point in self.points:
            estimate_info = point.depth_estimate or {}
            reason_desc = (
                estimate_info.get("reason")
                or "基于有效下放剖面深度峰值中位数推算"
            )
            cast_count = estimate_info.get("casts", "无")
            peak_iqr = estimate_info.get("peak_iqr_m", "无")
            envelope_depth = estimate_info.get("envelope_m", "无")
            threshold_depth = estimate_info.get("threshold_m", "无")
            detail_lines.append(
                f"{point.code}：{reason_desc}；有效下放剖面 {cast_count} 次；"
                f"峰值四分位距（波动幅度）{peak_iqr} 米；99%分位深度 {envelope_depth} 米；"
                f"分段阈值 {threshold_depth} 米"
            )
        self.technical.setText("\n".join(detail_lines))
        self.technical.setVisible(not self.technical.isVisible())

    def validate_accept(self) -> None:
        """校验每一行是否均已填写正数水深并勾选“确认”，通过后生成带 `confirmed=True` 标记的测点副本列表。"""
        updated_points: list[Point] = []
        for point, depth_spin, confirm_check in zip(
            self.points, self.inputs, self.checks
        ):
            if depth_spin.value() <= 0 or not confirm_check.isChecked():
                self.error.setText(
                    f"{point.code}：请填写正数水深，并勾选确认采用"
                )
                return
            is_depth_modified = depth_spin.value() != point.depth
            updated_estimate = dict(point.depth_estimate or {})
            updated_estimate["confirmed"] = True
            updated_points.append(
                replace(
                    point,
                    depth=depth_spin.value() if is_depth_modified else point.depth,
                    depth_source=(
                        "manual" if is_depth_modified else point.depth_source
                    ),
                    depth_estimate=updated_estimate,
                )
            )
        self.updated = updated_points
        self.accept()
