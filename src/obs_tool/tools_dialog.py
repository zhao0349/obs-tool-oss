"""实用工具箱与单文件 DAT 时间校正四步向导对话框模块（`ToolsDialog`、`CorrectionCurve`）。

核心安全与防误触机制：
1. **预览与最终写入许可严格分离**：
   - 第 1 步（选择文件）、第 2 步（设置校正方式）、第 3 步（核对结果与过程线预览）全程均为纯只读分析，绝不改动磁盘上的原始 `.dat` 文件；
   - 仅当进入第 4 步（确认修改）且经过防双击冷却延时（`doubleClickInterval() + 80ms`）激活 `self.save_armed = True` 后，“确认修改”按钮才真正具备触发写入的资格；
   - 全窗口所有按钮显式禁用 `default` / `autoDefault`，并在 `keyPressEvent` 中拦截除第 0 步入口焦点外的回车键，杜绝按回车键越过最终写入确认。
2. **会话代次号（`self.generation`）隔离**：任何参数改动、重新选文件或取消任务均递增 `self.generation`，丢弃后台子进程迟到的旧响应。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .correction_worker import CorrectionWorker
from .curve_view import Curve
from .datetime_input import DateTimeInput
from .native_picker import FilePicker
from .theme import ASSETS, fit_window, table_style


def plain_label(text: str = "") -> QLabel:
    """构造强制以 `PlainText` 纯文本模式渲染且自动换行的 `QLabel`，防止文件路径或外部文本中的 `<...>` 被当作 HTML 解析。"""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class CorrectionCurve(Curve):
    """在标准深度-时间核查曲线画布上叠加时间校正目标整点竖向参考线的预览组件。"""

    def __init__(self) -> None:
        """初始化曲线画布与空的校正目标整点列表 `self.targets`。"""
        super().__init__()
        self.targets: list[dict[str, Any]] = []

    def reset_zoom(self) -> None:
        """恢复同时完整覆盖平移后观测序列与所有目标整点（前后各留 30 分钟余量）的全图视野。"""
        super().reset_zoom()
        if self.browse:
            target_datetimes = [
                datetime.fromisoformat(match_row["target"])
                for match_row in self.targets
            ]
            if target_datetimes:
                self.browse = (
                    min(
                        self.browse[0],
                        min(target_datetimes) - timedelta(minutes=30),
                    ),
                    max(
                        self.browse[1],
                        max(target_datetimes) + timedelta(minutes=30),
                    ),
                )
            self.default_view = self.view = self.browse
            self.sync_scroll()
            self.update()

    def paintEvent(self, event) -> None:
        """先调用基类绘制深度-时间折线与候选/动作标记，再叠加所有目标整点的竖向虚线（异常整点标红，正常整点标灰）。"""
        if self.result is None:
            painter = QPainter(self)
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "暂无可绘制的曲线，原因或下一步操作见上方说明。",
            )
            return
        super().paintEvent(event)
        if not self.view:
            return
        left_time, right_time = self.view
        view_span_sec = max(1.0, (right_time - left_time).total_seconds())
        plot_width = max(1, self.width() - 100)
        painter = QPainter(self)
        for match_entry in self.targets:
            target_time = datetime.fromisoformat(match_entry["target"])
            if left_time <= target_time <= right_time:
                target_x = (
                    65.0
                    + (target_time - left_time).total_seconds()
                    / view_span_sec
                    * plot_width
                )
                painter.setPen(
                    QPen(
                        QColor(
                            "#B42318" if match_entry["reasons"] else "#888888"
                        ),
                        1,
                        Qt.PenStyle.DashLine,
                    )
                )
                painter.drawLine(
                    QPointF(target_x, 64),
                    QPointF(
                        target_x,
                        max(65, self.height() - 60 - self.scroll.height()),
                    ),
                )


class ToolsDialog(QDialog):
    """“实用工具”模态向导对话框：承载单文件 `.dat` 时间校正的完整五页式会话流程。"""

    TITLES = [
        "实用工具",
        "选择文件",
        "设置校正方式",
        "核对结果",
        "确认修改",
        "校正完成",
    ]

    def __init__(self, parent: QWidget | None) -> None:
        """初始化向导页面栈、状态标签、进度条及底部导航栏，并全局禁用按钮隐式回车默认触发。"""
        super().__init__(parent)
        self.setWindowTitle("实用工具")
        fit_window(self, 960, 780)

        self.worker: CorrectionWorker | None = None
        self.picker: FilePicker | None = None
        self.generation: int = 0
        self.page_generation: int = 0
        self.closing: bool = False
        self.updating: bool = False
        self.step: int = 0
        self.path: str | None = None
        self.plan: dict[str, Any] | None = None
        self.inspection: dict[str, Any] | None = None
        self.target: str | None = None
        self.head_confirmed: bool = False
        self.assume_no_head: bool = False
        self.save_armed: bool = False
        self.cancelled_task: bool = False
        self.origin_step: int = 1
        self.reselecting: bool = False

        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(12)

        self.heading = plain_label("实用工具")
        self.heading.setProperty("role", "title")
        root_layout.addWidget(self.heading)

        self.pages = QStackedWidget()
        root_layout.addWidget(self.pages, 1)
        self.build_home()
        self.build_file()
        self.build_settings()
        self.build_review()
        self.build_confirm()
        self.build_complete()

        self.status = plain_label()
        root_layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("正在处理时间校正任务")
        root_layout.addWidget(self.progress)

        footer_layout = QHBoxLayout()
        footer_layout.addStretch(1)
        self.previous = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.next_button.setProperty("primary", True)
        self.cancel_button = QPushButton("取消")
        for nav_btn in (self.previous, self.next_button, self.cancel_button):
            nav_btn.setAutoDefault(False)
            nav_btn.setDefault(False)
            nav_btn.setMinimumWidth(116)
            footer_layout.addWidget(nav_btn)

        self.previous.clicked.connect(self.go_back)
        self.next_button.clicked.connect(self.go_next)
        self.cancel_button.clicked.connect(self.cancel_or_leave)
        root_layout.addLayout(footer_layout)

        # 整个向导窗口严格禁用隐式默认按钮，确保按 Enter 键绝不会越过最终保存确认
        for child_btn in self.findChildren(QPushButton):
            child_btn.setAutoDefault(False)
            child_btn.setDefault(False)
        self.set_step(0)

    def page(self, scroll: bool = False) -> QVBoxLayout:
        """向 `self.pages` 添加一页向导容器（可选外包无边框 `QScrollArea`），返回内部垂直布局。"""
        page_widget = QWidget()
        page_layout = QVBoxLayout(page_widget)
        page_layout.setContentsMargins(4, 4, 4, 4)
        self.pages.addWidget(page_widget)
        if not scroll:
            return page_layout
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_content = QWidget()
        body_layout = QVBoxLayout(scroll_content)
        body_layout.setContentsMargins(4, 4, 12, 4)
        scroll_area.setWidget(scroll_content)
        page_layout.addWidget(scroll_area)
        return body_layout

    def build_home(self) -> None:
        """构建第 0 页：实用工具箱入口卡片页（当前包含“时间校正”大按钮）。"""
        body = self.page()
        self.tool_entry = QPushButton()
        self.tool_entry.setAccessibleName("时间校正：校准DAT文件中的记录时间")
        self.tool_entry.setMinimumHeight(108)

        card_row = QHBoxLayout(self.tool_entry)
        card_row.setContentsMargins(20, 16, 20, 16)
        clock_icon_label = QLabel()
        clock_icon_label.setPixmap(
            QIcon(str(ASSETS / "clock.svg")).pixmap(36, 36)
        )
        card_row.addWidget(clock_icon_label)

        text_col = QVBoxLayout()
        title_label = plain_label("时间校正")
        title_font = title_label.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 2)
        title_label.setFont(title_font)

        desc_label = plain_label(
            "校准单份 DAT 原始文件中的记录时间，支持过程线预览与原文件自动备份。"
        )
        desc_label.setProperty("role", "muted")
        text_col.addWidget(title_label)
        text_col.addWidget(desc_label)
        card_row.addLayout(text_col, 1)

        for inner_widget in (clock_icon_label, title_label, desc_label):
            inner_widget.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
        self.tool_entry.clicked.connect(self.start_session)
        body.addWidget(self.tool_entry)
        body.addStretch(1)

    def build_file(self) -> None:
        """构建第 1 步：待校准 `.dat` 文件选择与只读预检信息展示页。"""
        body = self.page(True)
        body.addWidget(
            plain_label(
                "请选择一份需要校准时间的 DAT 原始文件。前 3 步均为只读预览，不会修改原始数据。"
            )
        )
        self.choose = QPushButton("选择DAT文件")
        self.choose.clicked.connect(self.pick_file)
        body.addWidget(self.choose)

        self.file = plain_label("尚未选择文件")
        self.file.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.file)

        self.file_info = plain_label()
        body.addWidget(self.file_info)
        body.addStretch(1)

    def build_settings(self) -> None:
        """构建第 2 步：校正模式选择（已知时间对应关系 / 按抛测规律匹配）与时间参数输入页。"""
        body = self.page(True)
        self.setting_file = plain_label()
        body.addWidget(self.setting_file)

        self.mode = QComboBox()
        self.mode.addItems(["已知时间对应关系", "按抛测规律匹配"])
        body.addWidget(self.mode)

        self.forms = QStackedWidget()
        self.forms.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        body.addWidget(self.forms)

        known_page, casts_page = QWidget(), QWidget()
        self.forms.addWidget(known_page)
        self.forms.addWidget(casts_page)
        known_form, casts_form = QFormLayout(known_page), QFormLayout(casts_page)

        self.instrument, self.correct = DateTimeInput(), DateTimeInput()
        self.start, self.end = DateTimeInput(hour_only=True), DateTimeInput(
            hour_only=True
        )
        known_form.addRow("仪器记录时间", self.instrument)
        known_form.addRow("实际正确时间", self.correct)
        known_form.addRow(
            plain_label(
                "填写同一采样时刻对应的仪器记录时间与实际正确时间，软件将按计算出的固定时差整体平移整份文件。"
            )
        )
        casts_form.addRow("测验开始整点", self.start)
        casts_form.addRow("测验结束整点", self.end)
        casts_form.addRow(
            plain_label(
                "请填写测验起止整点（仅需选择日期与 0—23 小时，分、秒固定为 00）。软件将整理有效抛测动作并以每次最深观测时刻对齐目标整点（相邻抛测间隔偏差与整点残差均不超过 15 分钟）。"
            )
        )

        self.mode.currentIndexChanged.connect(self.forms.setCurrentIndex)
        self.mode.currentIndexChanged.connect(self.invalidate)
        self.mode.currentIndexChanged.connect(
            lambda *_: QTimer.singleShot(0, self.fit_form)
        )
        for dt_field in (self.instrument, self.correct, self.start, self.end):
            dt_field.date_edit.textChanged.connect(self.invalidate)
            dt_field.time_edit.textChanged.connect(self.invalidate)
        body.addStretch(1)

    def build_review(self) -> None:
        """构建第 3 步：校正方案摘要、首尾缺测次数分配、深度-时间过程线预览及目标整点匹配明细页。"""
        body = self.page(True)
        self.review_area = self.pages.widget(3).findChild(QScrollArea)
        self.review_text = plain_label()
        body.addWidget(self.review_text)

        self.allocation = QWidget()
        alloc_form = QFormLayout(self.allocation)
        self.head, self.tail = QSpinBox(), QSpinBox()
        self.head.setRange(0, 2048)
        self.tail.setRange(0, 2048)
        alloc_form.addRow("开头缺测次数", self.head)
        alloc_form.addRow("结尾缺测次数", self.tail)

        self.allocate_button = QPushButton("确认首尾缺测并重新预览")
        self.unknown_button = QPushButton("不记得，按开头无缺测处理")
        alloc_form.addRow(self.allocate_button)
        alloc_form.addRow(self.unknown_button)
        self.allocate_button.clicked.connect(self.allocate)
        self.unknown_button.clicked.connect(self.assume)
        self.head.valueChanged.connect(self.invalidate_allocation)
        self.tail.valueChanged.connect(self.invalidate_allocation)
        body.addWidget(self.allocation)

        self.allocation_summary = QWidget()
        summary_row = QHBoxLayout(self.allocation_summary)
        self.allocation_text = plain_label()
        summary_row.addWidget(self.allocation_text, 1)
        self.edit_allocation = QPushButton("修改缺测分配")
        self.edit_allocation.clicked.connect(self.reopen_allocation)
        summary_row.addWidget(self.edit_allocation)
        body.addWidget(self.allocation_summary)

        curve_toolbar = QHBoxLayout()
        curve_toolbar.addWidget(plain_label("深度时间预览"), 1)
        self.reset_curve = QPushButton("恢复全图")
        curve_toolbar.addWidget(self.reset_curve)
        body.addLayout(curve_toolbar)

        self.curve = CorrectionCurve()
        self.curve.setMinimumHeight(280)
        body.addWidget(self.curve, 1)
        self.reset_curve.clicked.connect(self.curve.reset_zoom)

        self.details_button = QPushButton("展开对应明细")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self.toggle_details)
        body.addWidget(self.details_button)

        self.matches = QTableWidget(0, 4)
        self.matches.setHorizontalHeaderLabels(
            ["目标整点", "动作/代表时刻", "残差（秒）", "状态"]
        )
        self.matches.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.matches.setMinimumHeight(224)
        table_style(self.matches)
        self.matches.horizontalHeader().setStretchLastSection(True)
        self.matches.itemSelectionChanged.connect(self.target_selected)
        body.addWidget(self.matches)

        self.recognition_text = plain_label()
        self.recognition_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.recognition_text)
        self.recognition_text.hide()
        self.matches.hide()

    def build_confirm(self) -> None:
        """构建第 4 步：最终写回/另存确认页（展示醒目红色提示、`.bak` 备份说明及失败后另存入口）。"""
        body = self.page(True)
        self.final_warning = plain_label("将修改原始 DAT 文件")
        self.final_warning.setProperty("role", "error")
        warn_font = self.final_warning.font()
        warn_font.setBold(True)
        self.final_warning.setFont(warn_font)
        body.addWidget(self.final_warning)

        self.final_text = plain_label()
        self.final_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.final_text)

        self.save_as = QPushButton("选择保存位置后重试")
        self.save_as.clicked.connect(self.pick_save)
        body.addWidget(self.save_as)
        body.addStretch(1)

    def build_complete(self) -> None:
        """构建第 5 页：时间校正文件保存完成页（展示输出路径与 `.bak` 备份文件路径）。"""
        body = self.page(True)
        self.complete_text = plain_label()
        self.complete_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.addWidget(self.complete_text)
        body.addStretch(1)

    def fit_form(self) -> None:
        """将第 2 步校正模式参数容器的高度动态贴合当前激活子表单的 `sizeHint`。"""
        self.forms.setFixedHeight(
            self.forms.currentWidget().sizeHint().height()
        )

    def busy(self) -> bool:
        """返回后台时间校正子进程（`self.worker`）当前是否仍在运行中。"""
        return self.worker is not None and not self.worker.ended

    def picking(self) -> bool:
        """返回 Windows 原生文件选择器子进程（`self.picker`）当前是否仍处于打开状态。"""
        return bool(self.picker and self.picker.isVisible())

    def refresh_navigation(self) -> None:
        """根据当前所在的向导步骤、后台任务忙碌状态及方案有效性，统一刷新底部按钮文字与可用状态。"""
        is_active = self.busy() or self.picking()
        self.previous.setVisible(self.step != 0)
        self.previous.setText(
            "返回工具列表" if self.step == 5 else "上一步"
        )
        self.previous.setEnabled(not is_active and self.step > 1)

        self.next_button.setVisible(self.step not in (0, 5))
        self.next_button.setText("确认修改" if self.step == 4 else "下一步")
        can_advance = (
            (self.step == 1 and self.inspection is not None)
            or self.step == 2
            or (self.step == 3 and bool(self.plan and self.plan.get("valid")))
            or (
                self.step == 4
                and self.save_armed
                and bool(self.plan and self.plan.get("valid"))
            )
        )
        self.next_button.setEnabled(not is_active and bool(can_advance))

        self.cancel_button.setText(
            "关闭"
            if self.step in (0, 5)
            else "取消任务"
            if self.busy()
            else "取消"
        )
        # 在保存提交阶段禁止点击取消，防止破坏正在原子替换的文件
        self.cancel_button.setEnabled(
            not (self.busy() and self.worker.saving)
        )
        for interactive_widget in (
            self.choose,
            self.mode,
            self.forms,
            self.allocation,
            self.edit_allocation,
            self.save_as,
            self.tool_entry,
        ):
            interactive_widget.setEnabled(not is_active)
        self.progress.setVisible(self.busy())

    def set_step(self, step: int) -> None:
        """切换到指定向导步骤页 `step`，并在进入第 4 步（确认修改）时启动防双击延时解锁定时器。"""
        self.page_generation += 1
        self.step = step
        self.pages.setCurrentIndex(step)
        self.heading.setText(
            f"第{step}步，共4步 · {self.TITLES[step]}"
            if 1 <= step <= 4
            else self.TITLES[step]
        )
        self.save_armed = False
        if step == 4:
            self.update_final_text()
            self.next_button.clearFocus()
            self.previous.setFocus()
            session_token = self.generation
            current_page_token = self.page_generation
            # 延时 doubleClickInterval + 80ms 后才解锁“确认修改”按钮，防止从第 3 步切页时的连击误触保存
            QTimer.singleShot(
                QApplication.doubleClickInterval() + 80,
                self,
                lambda: self.arm_save(session_token, current_page_token),
            )
        self.refresh_navigation()
        if step == 2:
            QTimer.singleShot(0, self.fit_form)

    def arm_save(self, token: int, page_token: int) -> None:
        """当防误触冷却期结束且会话代次、页面代次均未改变时，解锁第 4 步的“确认修改”保存按钮。"""
        if (
            token == self.generation
            and page_token == self.page_generation
            and self.step == 4
            and not self.closing
            and not self.busy()
        ):
            self.save_armed = True
            self.refresh_navigation()

    def start_session(self) -> None:
        """重置所有草稿状态与预览控件，开启一次全新的单文件时间校正向导会话（进入第 1 步）。"""
        if self.busy():
            return
        self.generation += 1
        self.updating = True
        self.path = self.plan = self.inspection = self.target = None
        self.head_confirmed = self.assume_no_head = False
        self.reselecting = False
        self.mode.setCurrentIndex(0)
        for dt_field in (self.instrument, self.correct, self.start, self.end):
            dt_field.setDateTime(QDateTime())
        self.head.setValue(0)
        self.tail.setValue(0)
        self.updating = False

        self.file.setText("尚未选择文件")
        self.file_info.clear()
        self.status.clear()
        self.curve.set_record(None, None)
        self.curve.targets = []
        self.details_button.setChecked(False)
        self.allocation.hide()
        self.allocation_summary.hide()
        self.save_as.hide()
        self.review_area.verticalScrollBar().setValue(0)
        self.set_step(1)

    def invalidate(self, *_args: Any) -> None:
        """当用户在第 2 步修改校正方式或时间输入时，立即作废已生成的旧分析方案并退回第 2 步等待重新分析。"""
        if self.updating:
            return
        self.generation += 1
        self.plan = self.target = None
        self.head_confirmed = self.assume_no_head = self.save_armed = False
        self.curve.set_record(None, None)
        self.curve.targets = []
        self.status.setText(
            "校正参数已更改，请点击“下一步”重新分析。"
            if self.inspection
            else ""
        )
        if self.step >= 3:
            self.set_step(2)
        self.refresh_navigation()

    def invalidate_allocation(self, *_args: Any) -> None:
        """当用户在第 3 步调整开头/结尾缺测次数微调框时，作废当前方案并提示点击“确认首尾缺测并重新预览”。"""
        if self.updating:
            return
        self.generation += 1
        self.head_confirmed = self.assume_no_head = self.save_armed = False
        self.plan = self.target = None
        self.curve.set_record(None, None)
        self.curve.targets = []
        self.status.setText("首尾缺测分配已更改，请点击确认按钮重新预览。")
        self.refresh_navigation()

    def go_back(self) -> None:
        """点击“上一步 / 返回工具列表”按钮时的步骤回退逻辑。"""
        if self.busy() or self.picking():
            return
        if self.step == 5:
            self.leave_session()
        elif self.step > 1:
            self.status.clear()
            self.set_step(self.step - 1)

    def go_next(self) -> None:
        """点击“下一步 / 确认修改”按钮时的前推逻辑（在第 2 步若尚无方案则先启动后台只读分析）。"""
        if self.busy() or self.picking():
            return
        if self.step == 1 and self.inspection:
            self.setting_file.setText("当前文件：" + Path(self.path).name)
            self.set_step(2)
        elif self.step == 2:
            if self.plan is not None:
                self.set_step(3)
            else:
                self.analyse()
        elif self.step == 3 and self.plan and self.plan.get("valid"):
            self.status.clear()
            self.set_step(4)
        elif self.step == 4:
            self.save_confirmed()

    def pick_file(self) -> None:
        """在第 1 步弹出 Windows 原生文件对话框以选择待校准的 `.dat` 文件。"""
        if self.busy() or self.picking():
            return
        self.open_picker(False)

    def open_picker(self, save: bool) -> None:
        """启动隔离的 `FilePicker` 子进程（`save=False` 为选择原文件，`save=True` 为选择另存新路径）。"""
        session_token = self.generation
        self.picker = picker = FilePicker(
            self,
            "另存校正后DAT" if save else "选择要校正的一份DAT",
            str(Path(self.path).parent) if self.path else "",
            save=save,
        )
        if save and self.path:
            picker.initial_names = [Path(self.path).stem + "-校正.dat"]

        def _on_paths_selected(selected_paths: list[str]) -> None:
            """仅当选择器实例与会话代次均匹配且窗口未关闭时，分发至原文件预检或另存路径回调。"""
            if (
                picker is self.picker
                and session_token == self.generation
                and not self.closing
            ):
                (self.save_target_selected if save else self.file_selected)(
                    selected_paths
                )

        picker.filesSelected.connect(_on_paths_selected)
        picker.error.connect(
            lambda err_msg: (
                self.status.setText(err_msg)
                if not self.closing and session_token == self.generation
                else None
            )
        )
        picker.finished.connect(self.picker_finished)
        picker.open()
        self.refresh_navigation()

    def picker_finished(self) -> None:
        """当原生文件选择器关闭后恢复向导导航按钮，若此前用户请求了关闭窗口则继续完成关闭。"""
        if self.step == 4 and not self.closing:
            self.set_step(4)
        self.refresh_navigation()
        if self.closing:
            QTimer.singleShot(0, self.close)

    def file_selected(self, paths: list[str]) -> None:
        """接收用户选定的单份 `.dat` 文件路径，保留同一会话内已填的目标时间草稿并启动后台 `inspect` 预检。"""
        if self.closing or self.busy():
            return
        if len(paths) != 1:
            self.status.setText("每次仅支持校准一份 DAT 文件，请重新选择。")
            return
        self.reselecting = self.path is not None
        self.generation += 1
        self.plan = self.inspection = self.target = None
        self.head_confirmed = self.assume_no_head = self.save_armed = False
        self.updating = True
        self.instrument.setDateTime(QDateTime())
        self.head.setValue(0)
        self.tail.setValue(0)
        self.updating = False

        self.curve.targets = []
        self.curve.set_record(None, None)
        self.file_info.clear()
        self.allocation.hide()
        self.allocation_summary.hide()
        self.save_as.hide()
        self.matches.setRowCount(0)
        self.details_button.setChecked(False)
        self.set_step(1)

        self.path = str(Path(paths[0]).resolve())
        self.file.setText(self.path)
        self.file.setToolTip(self.path)
        self.launch(
            dict(
                operation="analyse",
                path=self.path,
                parameters=dict(mode="inspect"),
            )
        )

    def parameters(self) -> dict[str, Any]:
        """读取并校验当前选中的校正方式（`known` 或 `casts`）及对应的时间/缺测分配参数字典。"""
        if self.mode.currentIndex() == 0:
            return dict(
                mode="known",
                instrument=self.instrument.value("仪器记录"),
                correct=self.correct.value("实际正确"),
            )
        return dict(
            mode="casts",
            start=self.start.value("测验开始"),
            end=self.end.value("测验结束"),
            head=self.head.value() if self.head_confirmed else None,
            tail=self.tail.value() if self.head_confirmed else None,
            assume_no_head=self.assume_no_head,
        )

    def analyse(self) -> None:
        """收集第 2/3 步的校正参数并启动后台只读预览分析任务。"""
        if self.busy() or not self.inspection:
            return
        try:
            params_dict = self.parameters()
            self.plan = None
            self.save_armed = False
            self.launch(
                dict(
                    operation="analyse",
                    path=self.path,
                    parameters=params_dict,
                )
            )
        except ValueError as exc:
            self.status.setText(str(exc))

    def allocate(self) -> None:
        """记录用户显式确认的开头/结尾缺测次数分配，并重新发起预览分析。"""
        self.head_confirmed = True
        self.assume_no_head = False
        self.analyse()

    def assume(self) -> None:
        """记录用户点击“不记得，按开头无缺测处理”的显式假设，并重新发起预览分析。"""
        self.head_confirmed = False
        self.assume_no_head = True
        self.analyse()

    def reopen_allocation(self) -> None:
        """在已确认首尾缺测分配后重新展开微调框供用户修改，同时撤销既有确认状态。"""
        self.allocation_summary.hide()
        self.allocation.show()
        self.invalidate_allocation()

    def launch(self, request: dict[str, Any]) -> None:
        """启动互斥的 `CorrectionWorker` 后台子进程，通过 worker 实例引用与 `self.generation` 过滤过期回调。"""
        parent_win = self.parentWidget()
        if self.busy() or any(
            getattr(parent_win, attr_name, None)
            and getattr(parent_win, attr_name).isRunning()
            for attr_name in ("worker", "analysis_worker")
        ):
            self.status.setText("请等待当前任务结束后重试。")
            return

        self.origin_step = self.step
        self.cancelled_task = False
        session_token = self.generation
        worker = self.worker = CorrectionWorker(request, self)

        def _on_completed(result_data: dict[str, Any]) -> None:
            """仅接收当前活动 worker 的完成信号；保存任务必须完成收尾，分析任务还须核对会话代次。"""
            if worker is not self.worker:
                return
            if worker.saving:
                self.saved(result_data)
            elif session_token == self.generation and not self.closing:
                self.analysed(result_data)

        def _on_failed(error_msg: str) -> None:
            """将后台任务失败原因展示在当前向导页。"""
            if (
                worker is self.worker
                and session_token == self.generation
                and not self.closing
            ):
                self.failed(error_msg)

        worker.completed.connect(_on_completed)
        worker.failed.connect(_on_failed)
        worker.finished.connect(lambda: self.task_finished(worker))
        self.status.setText(
            "正在写入并校验文件，请稍候…"
            if worker.saving
            else "正在后台分析文件，可随时取消…"
        )
        self.refresh_navigation()
        worker.start()

    def analysed(self, plan: dict[str, Any]) -> None:
        """接收后台只读分析结果：若为 `inspect` 模式则填充第 1 步文件摘要，否则渲染第 3 步过程线与匹配表。"""
        if plan["parameters"]["mode"] == "inspect":
            self.inspection = plan
            self.updating = True
            self.instrument.setDateTime(
                QDateTime.fromString(plan["old_start"], "yyyy-MM-ddTHH:mm:ss")
            )
            self.updating = False
            self.file_info.setText(
                f"有效观测记录：{plan['records']} 条\n"
                f"原始记录时段：{plan['old_start'].replace('T', ' ')} → {plan['old_end'].replace('T', ' ')}\n"
                f"已识别传感器通道：{'、'.join(plan['columns']['sensors'])}"
            )
            self.status.setText(
                "文件校验通过。已保留此前填写的正确时间与测验起止草稿，并已重新读取当前文件的仪器参考时间，请核对这些设置是否适用于当前文件。"
                if self.reselecting
                else "文件校验通过，请点击“下一步”设置校正方式。"
            )
            return

        self.plan = plan
        end_missing_total = plan.get("end_missing_total", 0)
        needs_allocation = bool(plan.get("needs_allocation"))
        self.allocation.setVisible(needs_allocation)
        self.allocation_summary.setVisible(
            plan["parameters"]["mode"] == "casts"
            and end_missing_total > 0
            and not needs_allocation
        )
        if needs_allocation:
            self.updating = True
            self.head.setValue(0)
            self.tail.setValue(max(0, end_missing_total))
            self.updating = False

        self.allocation_text.setText(
            f"开头缺测 {plan.get('head_missing', 0)} 次、结尾缺测 {plan.get('tail_missing', 0)} 次；{plan.get('assumption', '')}"
        )

        shift_delta = timedelta(seconds=plan.get("delta_seconds", 0))
        self.curve.targets = plan["matches"]
        shifted_curve_rows = [
            dict(
                timestamp=(
                    datetime.fromisoformat(pt["timestamp"]) + shift_delta
                ).isoformat(),
                depth=pt["depth"],
            )
            for pt in plan["curve"]
        ]
        if shifted_curve_rows:
            curve_context = dict(
                start=shifted_curve_rows[0]["timestamp"],
                end=shifted_curve_rows[-1]["timestamp"],
                runs=[shifted_curve_rows],
                gap_seconds=300,
            )
            initial_target = (
                plan["matches"][0]["target"]
                if plan["matches"]
                else shifted_curve_rows[0]["timestamp"]
            )
            action_selections = [
                dict(
                    label=f"动作{idx + 1}",
                    observation={
                        **action_item,
                        "timestamp": (
                            datetime.fromisoformat(action_item["timestamp"])
                            + shift_delta
                        ).isoformat(),
                    },
                )
                for idx, action_item in enumerate(plan.get("actions", []))
            ]
            candidate_selections = [
                dict(
                    label=f"候选{cand['id']}",
                    display_kind="candidate",
                    observation={
                        **cand,
                        "timestamp": (
                            datetime.fromisoformat(cand["timestamp"])
                            + shift_delta
                        ).isoformat(),
                    },
                )
                for cand in plan.get("recognition", {}).get("candidates", [])
            ]
            self.curve.set_record(
                dict(segments=[]),
                dict(
                    target=initial_target,
                    segment=None,
                    selections=[*candidate_selections, *action_selections],
                ),
                curve_context,
                "时间校正预览 · 滚轮缩放、底部滚动条平移；空心圆（○）为候选片段，实心方块（■）为整理后的抛测动作最深时刻",
            )
        else:
            self.curve.set_record(None, None)

        self.matches.setRowCount(len(plan["matches"]))
        for row_idx, match_row in enumerate(plan["matches"]):
            for col_idx, cell_val in enumerate(
                (
                    match_row["target"],
                    f"{match_row['action'] or '—'} · {match_row['representative'] or '—'}",
                    (
                        match_row["residual_seconds"]
                        if match_row["residual_seconds"] is not None
                        else "—"
                    ),
                    "、".join(match_row["reasons"]) or "匹配",
                )
            ):
                self.matches.setItem(
                    row_idx, col_idx, QTableWidgetItem(str(cell_val))
                )
        self.matches.resizeColumnsToContents()

        recognition_info = plan.get("recognition", {})
        candidate_fragments = recognition_info.get("candidates", [])
        detail_lines: list[str] = []
        for action_idx, action_item in enumerate(plan.get("actions", [])):
            member_ids = action_item.get("candidate_ids", [])
            merge_note_str = (
                ("；合并依据：" + "；".join(action_item["notes"]))
                if action_item.get("notes")
                else ""
            )
            detail_lines.append(
                f"动作 {action_idx + 1}（含候选 {','.join(map(str, member_ids)) or '—'}）："
                f"最深原始时刻 {action_item['timestamp'].replace('T', ' ')}，"
                f"深度 {action_item['depth']:g} m（来源：{action_item['file']} 第 {action_item['line']} 行）{merge_note_str}。"
            )
        detail_lines.extend(
            f"候选 {cand['id']}：原始时段 {cand['start'].replace('T', ' ')} → {cand['end'].replace('T', ' ')}，"
            f"峰值深度 {cand['depth']:g} m（来源：{cand['file']} 第 {cand['line']} 行）。"
            for cand in candidate_fragments
        )
        detail_lines.extend(recognition_info.get("uncertainties", []))
        self.recognition_text.setText("\n".join(detail_lines))

        self.details_button.setVisible(bool(plan["matches"]))
        self.toggle_details(self.details_button.isChecked())
        self.review_text.setText(self.summary_text(plan))
        self.status.setText(
            plan.get(
                "reason",
                "请核对校正后的时间范围与过程线预览，确认无误后点击“下一步”。",
            )
        )
        self.set_step(3)

    def summary_text(self, plan: dict[str, Any]) -> str:
        """根据分析方案字典 `plan` 生成包含原始时段、平移后时段、平移秒数及缺测分配说明的中文摘要。"""
        old_start_str = plan["old_start"].replace("T", " ")
        old_end_str = plan["old_end"].replace("T", " ")
        shifted_range_line = (
            f"校正后范围：{plan['new_start'].replace('T', ' ')} → {plan['new_end'].replace('T', ' ')}（整体平移 {plan['delta_seconds']:+d} 秒）\n"
            if "new_start" in plan
            else ""
        )
        if plan["parameters"]["mode"] == "casts":
            assumption_suffix = (
                f"（{plan['assumption']}）" if plan.get("assumption") else ""
            )
            mode_detail = (
                f"中间时段缺测 {plan.get('middle_missing', 0)} 次；首尾缺测分配尚待确认。"
                if plan.get("needs_allocation")
                else f"中间时段缺测 {plan.get('middle_missing', 0)} 次；开头缺测 {plan.get('head_missing', 0)} 次、结尾缺测 {plan.get('tail_missing', 0)} 次{assumption_suffix}。"
            )
        else:
            mode_detail = "整份文件有效观测按固定时差统一平移，采样间隔与传感器数值保持不变。"
        return (
            f"文件：{Path(plan['path']).name} · {self.mode.itemText(0 if plan['parameters']['mode'] == 'known' else 1)}\n"
            f"原始记录范围：{old_start_str} → {old_end_str}\n"
            + shifted_range_line
            + mode_detail
        )

    def toggle_details(self, expanded: bool) -> None:
        """展开或收起第 3 步底部的整点匹配明细表与候选抛测动作识别日志。"""
        self.details_button.setText(
            "收起对应明细" if expanded else "展开对应明细"
        )
        self.matches.setVisible(expanded and self.matches.rowCount() > 0)
        self.recognition_text.setVisible(
            expanded and bool(self.recognition_text.text())
        )
        if expanded:
            QTimer.singleShot(
                0,
                self,
                lambda: (
                    self.review_area.ensureWidgetVisible(self.matches, 0, 8)
                    if self.matches.isVisible()
                    else None
                ),
            )

    def target_selected(self) -> None:
        """当用户在第 3 步匹配明细表中点击某一行目标整点时，将上方过程线视野中心同步平移至该整点。"""
        selected_idx = self.matches.currentRow()
        if (
            self.plan
            and self.curve.row is not None
            and 0 <= selected_idx < len(self.plan["matches"])
        ):
            self.curve.row["target"] = self.plan["matches"][selected_idx][
                "target"
            ]
            self.curve.locate_target()

    def update_final_text(self) -> None:
        """根据当前方案及是否设置了另存路径 `self.target`，刷新第 4 步最终确认页的提示文案。"""
        plan = self.plan
        if not plan:
            return
        self.final_warning.setText(
            "将另存校正后的 DAT，原文件保持不变"
            if self.target
            else "将修改原始 DAT 文件"
        )
        self.final_text.setText(
            f"原文件：{plan['path']}\n{self.summary_text(plan)}\n"
            + (
                f"另存目标：{self.target}\n请核对新文件路径，确认无误后点击“确认修改”保存。"
                if self.target
                else "保存前将先在同目录下创建完整原始备份（.bak 文件，重名自动递增编号），校验无误后再更新原文件。\n请点击“确认修改”开始保存。"
            )
        )

    def save_confirmed(self) -> None:
        """仅在第 4 步且 `self.save_armed` 已解锁时启动后台文件保存任务，并立即撤销再次触发资格。"""
        if (
            self.step != 4
            or not self.save_armed
            or self.busy()
            or self.picking()
            or not self.plan
            or not self.plan.get("valid")
        ):
            return
        self.save_armed = False
        self.launch(
            dict(operation="save", plan=self.plan, target=self.target)
        )

    def pick_save(self) -> None:
        """在第 4 步保存失败后，弹出原生另存为对话框以指定新的 `.dat` 输出路径。"""
        if self.step != 4 or self.busy() or self.picking():
            return
        self.save_armed = False
        self.open_picker(True)

    def save_target_selected(self, paths: list[str]) -> None:
        """校验用户选定的另存路径（必须是不存在的 `.dat` 新文件名），通过后更新第 4 步确认文案。"""
        if (
            self.closing
            or self.step != 4
            or len(paths) != 1
            or not self.plan
        ):
            return
        candidate_path = Path(paths[0]).resolve()
        if candidate_path.exists() or candidate_path.suffix.lower() != ".dat":
            self.status.setText(
                "该目标文件已存在或不是 .dat 格式，请指定一个尚未使用的新文件名。"
            )
            return
        self.target = str(candidate_path)
        self.status.setText(
            "另存目标路径已更新，请核对后点击“确认修改”。"
        )
        self.set_step(4)

    def saved(self, result: dict[str, Any]) -> None:
        """文件保存成功回调：若为原位覆盖修改则通知主窗口联动更新测点水深状态，随后进入第 5 页完成页。"""
        if result["inplace"]:
            parent_win = self.parentWidget()
            if hasattr(parent_win, "corrected_file"):
                parent_win.corrected_file(result["path"])
        self.plan = None
        if self.closing:
            return
        self.complete_text.setText(
            (
                "原文件校正完成。\n输出文件："
                + result["path"]
                + "\n完整原始备份："
                + str(result["backup"])
                if result["inplace"]
                else "另存成功，原文件保持不变。\n输出文件："
                + result["path"]
                + "\n本次另存未改动原文件，不额外创建 .bak 备份。"
            )
            + "\n如需使用校正后的数据导出成果表，请返回主界面重新点击“开始处理并导出 CSV”。"
        )
        self.status.clear()
        self.set_step(5)

    def failed(self, message: str) -> None:
        """后台任务失败回调：若为保存失败则在第 4 步展示“选择保存位置后重试”按钮，若为分析失败则退回发起步骤。"""
        if self.worker and self.worker.saving:
            self.save_as.show()
            self.status.setText(
                "保存失败："
                + message
                + "。请核对后重试，或点击“选择保存位置后重试”另存为新文件。"
            )
            # 重新进入第 4 步并走防误触延时解锁，要求用户明确再次点击确认
            self.set_step(4)
        else:
            self.plan = None
            if self.origin_step == 1:
                self.inspection = None
            self.curve.set_record(None, None)
            self.set_step(self.origin_step)
            self.status.setText("无法完成分析：" + message)

    def task_finished(self, worker: CorrectionWorker) -> None:
        """当活动 `CorrectionWorker` 彻底结束时，恢复导航按钮状态或继续执行延迟的窗口关闭。"""
        if worker is not self.worker:
            return
        if self.cancelled_task:
            self.cancelled_task = False
            self.set_step(self.origin_step)
            self.status.setText("已取消分析，输入已保留；没有修改文件。")
        self.refresh_navigation()
        if self.closing:
            QTimer.singleShot(0, self.close)

    def cancel_task(self) -> None:
        """向正在运行的只读分析子进程发送协作取消请求（保存提交期间不允许取消）。"""
        if self.busy() and not self.worker.saving:
            self.cancelled_task = True
            self.generation += 1
            self.worker.cancel()
            self.status.setText("正在取消分析，请稍候…")

    def leave_session(self) -> None:
        """在无活动后台任务与文件选择器时，清空当前校正会话并返回第 0 页工具列表入口。"""
        if self.busy() or self.picking():
            return
        self.generation += 1
        self.path = self.plan = self.inspection = self.target = None
        self.save_armed = False
        self.status.clear()
        self.set_step(0)
        self.tool_entry.setFocus()

    def cancel_or_leave(self) -> None:
        """底部“取消 / 取消任务 / 关闭”按钮的统一路由处理。"""
        if self.busy():
            self.cancel_task()
        elif self.step in (0, 5):
            self.close()
        elif self.picking():
            self.picker.reject()
        else:
            self.leave_session()

    def keyPressEvent(self, event) -> None:
        """仅允许在第 0 页工具入口按钮聚焦时按回车启动会话；其余所有步骤一律吞掉回车键，严防误触发保存。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.step == 0 and self.tool_entry.hasFocus():
                self.start_session()
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        """拦截 `Escape` 键或对话框标准 `reject()`：在向导步骤中优先退回工具入口或取消当前分析任务。"""
        if self.closing:
            super().reject()
        elif self.step not in (0, 5):
            self.cancel_or_leave()
        else:
            self.close()

    def showEvent(self, event) -> None:
        """当对话框被重新打开且后台已无残留任务时，清除旧的关闭标记并回到第 0 页工具入口。"""
        if self.closing and not self.busy() and not self.picking():
            self.closing = False
            self.leave_session()
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        """窗口关闭保护：若后台有分析任务或文件选择器正在运行则先请求取消并暂缓关闭；若正在保存提交则等待完成后再关闭。"""
        self.closing = True
        if self.busy():
            if not self.worker.saving:
                self.cancel_task()
            self.status.setText(
                "正在安全结束任务，保存提交期间请稍候…"
            )
            event.ignore()
            return
        if self.picking():
            self.picker.reject()
            event.ignore()
            return
        self.generation += 1
        self.plan = None
        super().closeEvent(event)
