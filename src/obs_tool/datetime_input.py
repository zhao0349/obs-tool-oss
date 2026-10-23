"""统一的日期时间输入控件、三级自绘日历浮层（日/月/年）与 24 小时制时分秒滚轮选择器模块。

交互设计亮点：
1. `Flyout`：自适应屏幕可用区域的弹出式无边框浮层基类，当下方空间不足时自动向上弹出；
2. `CalendarGrid` / `CalendarPopup`：支持“日期 -> 月份 -> 年份”三级钻取浏览、键盘方向键与 PageUp/PageDown 翻页；
3. `TimeColumn` / `TimePopup`：24 小时制时、分、秒三列滚轮/点击/键盘选择器；在整点模式（`hour_only=True`，如时间校正工具的目标整点输入）下锁定分、秒为 `00`；
4. `DateTimeInput`：组合日期文本框、日历弹出按钮、时间文本框与时钟弹出按钮，在整点模式下严格拒绝粘贴或输入非整点分秒（绝不静默截断）。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QDate,
    QDateTime,
    QEvent,
    QPoint,
    QRegularExpression,
    QSize,
    Qt,
    QTime,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import ASSETS, TOKENS


class Flyout(QFrame):
    """日期选择器与时间选择器共用的屏幕边界自适应无边框弹出浮层基类。"""

    def __init__(self, parent: QWidget | None) -> None:
        """创建隶属于父控件的 Popup 无边框圆角白底浮层。"""
        super().__init__(
            parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setObjectName("dateTimeFlyout")
        self.setStyleSheet(
            "#dateTimeFlyout {background:white;border:1px solid #D1D1D1;border-radius:8px;}"
        )

    def show_at(self, anchor: QWidget) -> None:
        """根据锚点控件 `anchor` 的屏幕坐标及当前显示器可用区域，自动选择在下方或上方弹出浮层。"""
        self.adjustSize()
        screen_area = anchor.screen().availableGeometry()
        self.resize(
            min(self.width(), screen_area.width() - 16),
            min(self.height(), screen_area.height() - 16),
        )
        pos_below = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        pos_above = anchor.mapToGlobal(QPoint(0, -self.height() - 4))
        target_y = (
            pos_below.y()
            if pos_below.y() + self.height() <= screen_area.bottom() + 1
            else pos_above.y()
        )
        self.move(
            max(
                screen_area.left() + 8,
                min(pos_below.x(), screen_area.right() - self.width() - 7),
            ),
            max(
                screen_area.top() + 8,
                min(target_y, screen_area.bottom() - self.height() - 7),
            ),
        )
        self.show()

    def keyPressEvent(self, event) -> None:
        """按下 `Escape` 键时关闭弹出浮层；其余按键交由 Qt 默认事件处理。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


class CalendarGrid(QWidget):
    """自绘三级日历网格控件，维护日期（`days`）、月份（`months`）与年份（`years`）选择层级。"""

    chosen = Signal(QDate)
    browseChanged = Signal()

    def __init__(self, date: QDate, parent: QWidget | None) -> None:
        """以给定初始日期 `date` 初始化选中态与浏览月份，并开启鼠标悬停追踪与键盘强焦点。"""
        super().__init__(parent)
        self.selected = date
        self.focus_date = date if date.isValid() else QDate.currentDate()
        self.month = QDate(self.focus_date.year(), self.focus_date.month(), 1)
        self.mode = "days"
        self.hover = -1
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("日历，方向键选择，Enter确认，PageUp或PageDown翻月")
        self.setMinimumSize(280, 252)

    def sizeHint(self) -> QSize:
        """返回日历网格的推荐逻辑尺寸。"""
        return QSize(336, 294)

    def cells(self) -> list[QDate]:
        """生成当前浏览层级需要展示的日历单元格日期列表（日期视图 42 格、月份视图 12 格、年份视图 12 格）。"""
        if self.mode == "days":
            first_grid_day = self.month.addDays(1 - self.month.dayOfWeek())
            return [first_grid_day.addDays(offset) for offset in range(42)]
        if self.mode == "months":
            return [QDate(self.month.year(), month_num, 1) for month_num in range(1, 13)]
        start_year = max(1, ((self.month.year() - 1) // 12) * 12 + 1)
        return [
            QDate(year_num, 1, 1)
            for year_num in range(start_year, min(10000, start_year + 12))
        ]

    def geometry_values(self) -> tuple[int, float, float]:
        """计算日历网格绘制与鼠标命中检测共用的 `(列数, 单元格宽, 单元格高)`。"""
        columns, rows = (7, 7) if self.mode == "days" else (3, 4)
        return columns, self.width() / columns, self.height() / rows

    def paintEvent(self, event) -> None:
        """绘制星期表头、各日期/月份/年份单元格、悬停高亮、当前选中圆圈、今日描边及键盘焦点虚线框。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        columns, cell_w, row_h = self.geometry_values()
        is_days_mode = self.mode == "days"

        if is_days_mode:
            painter.setPen(QColor(TOKENS["muted"]))
            for col_idx, weekday_label in enumerate("一二三四五六日"):
                painter.drawText(
                    int(col_idx * cell_w),
                    0,
                    int(cell_w),
                    int(row_h),
                    Qt.AlignmentFlag.AlignCenter,
                    weekday_label,
                )

        for cell_idx, cell_date in enumerate(self.cells()):
            center_x = (cell_idx % columns + 0.5) * cell_w
            center_y = (cell_idx // columns + 0.5 + (1 if is_days_mode else 0)) * row_h
            circle_radius = min(cell_w, row_h) * 0.40
            is_selected = is_days_mode and cell_date == self.selected

            if is_selected or cell_idx == self.hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(
                    QColor(TOKENS["accent"] if is_selected else "#EFF5FA")
                )
                if is_days_mode:
                    painter.drawEllipse(
                        QPoint(int(center_x), int(center_y)),
                        int(circle_radius),
                        int(circle_radius),
                    )
                else:
                    painter.drawRoundedRect(
                        int(cell_idx % columns * cell_w + 3),
                        int(cell_idx // columns * row_h + 3),
                        int(cell_w - 6),
                        int(row_h - 6),
                        5,
                        5,
                    )

            if is_days_mode and cell_date == QDate.currentDate():
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(
                    QPen(QColor("white" if is_selected else TOKENS["accent"]), 1.5)
                )
                today_radius = int(circle_radius - 3 if is_selected else circle_radius)
                painter.drawEllipse(
                    QPoint(int(center_x), int(center_y)),
                    today_radius,
                    today_radius,
                )

            if (
                is_days_mode
                and self.hasFocus()
                and cell_date == self.focus_date
                and not is_selected
            ):
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(
                    QPen(QColor(TOKENS["accent"]), 1, Qt.PenStyle.DotLine)
                )
                painter.drawEllipse(
                    QPoint(int(center_x), int(center_y)),
                    int(circle_radius),
                    int(circle_radius),
                )

            painter.setPen(
                QColor(
                    "white"
                    if is_selected
                    else (
                        TOKENS["text"]
                        if not is_days_mode or cell_date.month() == self.month.month()
                        else "#919191"
                    )
                )
            )
            cell_text = (
                str(cell_date.day())
                if is_days_mode
                else (
                    f"{cell_date.month()}月"
                    if self.mode == "months"
                    else str(cell_date.year())
                )
            )
            painter.drawText(
                int(cell_idx % columns * cell_w),
                int((cell_idx // columns + (1 if is_days_mode else 0)) * row_h),
                int(cell_w),
                int(row_h),
                Qt.AlignmentFlag.AlignCenter,
                cell_text,
            )

    def index_at(self, pos) -> int:
        """将鼠标局部坐标转换为当前网格中的单元格索引；若落在表头或越界则返回 `-1`。"""
        columns, cell_w, row_h = self.geometry_values()
        grid_row = int(pos.y() / row_h) - (1 if self.mode == "days" else 0)
        cell_index = grid_row * columns + int(pos.x() / cell_w)
        return (
            cell_index
            if grid_row >= 0 and 0 <= cell_index < len(self.cells())
            else -1
        )

    def mouseMoveEvent(self, event) -> None:
        """更新鼠标悬停的单元格索引并请求重绘。"""
        self.hover = self.index_at(event.position())
        self.update()

    def leaveEvent(self, event) -> None:
        """鼠标移出日历网格时清除悬停高亮。"""
        self.hover = -1
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        """鼠标左键释放时选中命中的有效日历单元格。"""
        if event.button() == Qt.MouseButton.LeftButton:
            cell_index = self.index_at(event.position())
            if cell_index >= 0:
                self.select(self.cells()[cell_index])

    def select(self, date: QDate) -> None:
        """响应单元格选择：在 `days` 模式下发射 `chosen` 信号提交日期，在年/月模式下进入下一级视图。"""
        if not date.isValid() or not (1 <= date.year() <= 9999):
            return
        if self.mode == "days":
            self.chosen.emit(date)
        else:
            self.month = QDate(
                date.year(),
                date.month() if self.mode == "months" else self.month.month(),
                1,
            )
            self.focus_date = QDate(
                self.month.year(),
                self.month.month(),
                min(self.focus_date.day(), self.month.daysInMonth()),
            )
            self.mode = "days" if self.mode == "months" else "months"
            self.hover = -1
            self.browseChanged.emit()
            self.update()

    def move_page(self, direction: int) -> None:
        """按当前浏览层级翻页（日期视图按月翻页，月份视图按年翻页，年份视图按 12 年翻页）。"""
        target_month = (
            self.month.addMonths(direction)
            if self.mode == "days"
            else self.month.addYears(
                direction * (1 if self.mode == "months" else 12)
            )
        )
        if target_month.isValid() and 1 <= target_month.year() <= 9999:
            self.month = target_month
            self.focus_date = QDate(
                target_month.year(),
                target_month.month(),
                min(self.focus_date.day(), target_month.daysInMonth()),
            )
        self.hover = -1
        self.browseChanged.emit()
        self.update()

    def keyPressEvent(self, event) -> None:
        """支持键盘 PageUp/PageDown 翻页、方向键移动焦点以及回车键确认选择。"""
        key = event.key()
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.move_page(-1 if key == Qt.Key.Key_PageUp else 1)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.select(
                self.focus_date
                if self.mode == "days"
                else self.cells()[max(0, self.hover)]
            )
            return
        navigation_steps = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: -7 if self.mode == "days" else -3,
            Qt.Key.Key_Down: 7 if self.mode == "days" else 3,
        }
        if key in navigation_steps:
            if self.mode == "days":
                candidate_date = self.focus_date.addDays(navigation_steps[key])
                if 1 <= candidate_date.year() <= 9999:
                    self.focus_date = candidate_date
                    self.month = QDate(
                        candidate_date.year(), candidate_date.month(), 1
                    )
            else:
                self.hover = max(
                    0,
                    min(
                        len(self.cells()) - 1,
                        max(0, self.hover) + navigation_steps[key],
                    ),
                )
            self.browseChanged.emit()
            self.update()
            return
        super().keyPressEvent(event)


class CalendarPopup(Flyout):
    """组合顶部年月导航栏与 `CalendarGrid` 日历网格的弹出式日期选择浮层。"""

    chosen = Signal(QDate)

    def __init__(self, parent: QWidget | None, date: QDate) -> None:
        """构建日历浮层头部（层级切换标题按钮与左/右翻页按钮）及下部网格。"""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        header_layout = QHBoxLayout()
        layout.addLayout(header_layout)

        self.title = QPushButton()
        self.title.setAccessibleName("选择年份和月份")
        self.title.clicked.connect(self.choose_level)
        header_layout.addWidget(self.title, 1)
        self.title.setStyleSheet(
            "QPushButton {border:0;text-align:left;font-weight:600;} "
            "QPushButton:hover {background:#EFF5FA;} "
            "QPushButton:focus {border:1px solid #005FB8;}"
        )

        for btn_symbol, page_step, a11y_name in [
            ("‹", -1, "上一页"),
            ("›", 1, "下一页"),
        ]:
            nav_button = QPushButton(btn_symbol)
            nav_button.setFixedWidth(38)
            nav_button.setAccessibleName(a11y_name)
            nav_button.setToolTip(a11y_name)
            nav_button.setStyleSheet(
                "QPushButton {border:0;font-size:16pt;} "
                "QPushButton:hover {background:#EFF5FA;} "
                "QPushButton:focus {border:1px solid #005FB8;}"
            )
            nav_button.clicked.connect(
                lambda _checked=False, step=page_step: self.grid.move_page(step)
            )
            header_layout.addWidget(nav_button)

        self.grid = CalendarGrid(date, self)
        layout.addWidget(self.grid)
        self.grid.browseChanged.connect(self.update_title)
        self.grid.chosen.connect(self.commit)
        self.update_title()

    def update_title(self) -> None:
        """根据日历网格当前所处的浏览层级（日/月/年）刷新顶部标题文字。"""
        current_month = self.grid.month
        self.title.setText(
            f"{current_month.year()}年{current_month.month()}月"
            if self.grid.mode == "days"
            else (
                f"{current_month.year()}年"
                if self.grid.mode == "months"
                else f"{self.grid.cells()[0].year()} — {self.grid.cells()[-1].year()}年"
            )
        )

    def choose_level(self) -> None:
        """点击顶部标题时按 `days -> months -> years -> days` 循环切换日历浏览层级。"""
        self.grid.mode = (
            "months"
            if self.grid.mode == "days"
            else "years"
            if self.grid.mode == "months"
            else "days"
        )
        self.grid.hover = -1
        self.update_title()
        self.grid.setFocus()
        self.grid.update()

    def commit(self, date: QDate) -> None:
        """发射选定日期信号并立即关闭日历浮层。"""
        self.chosen.emit(date)
        self.close()


class TimeColumn(QWidget):
    """自绘单列滚轮数字选择器（用于“时”、“分”或“秒”列），支持鼠标滚轮、点击与键盘上下键。"""

    def __init__(
        self, value: int, maximum: int, name: str, parent: QWidget | None
    ) -> None:
        """初始化当前数值 `value`、上限 `maximum`（如 23 或 59）及无障碍名称。"""
        super().__init__(parent)
        self.value = value
        self.maximum = maximum
        self.wheel_delta = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(name)
        self.setMinimumWidth(76)

    def sizeHint(self) -> QSize:
        """返回单列时间滚轮的推荐尺寸。"""
        return QSize(88, 220)

    def paintEvent(self, event) -> None:
        """绘制居中高亮选中行及其上下各 2 行的邻近数值，并在聚焦时绘制外框。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row_height = self.height() / 5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(TOKENS["accent"]))
        painter.drawRoundedRect(
            1, int(row_height * 2), self.width() - 2, int(row_height), 4, 4
        )
        for offset in range(-2, 3):
            candidate_val = self.value + offset
            if not (0 <= candidate_val <= self.maximum):
                continue
            painter.setPen(QColor("white" if offset == 0 else TOKENS["muted"]))
            painter.drawText(
                0,
                int((offset + 2) * row_height),
                self.width(),
                int(row_height),
                Qt.AlignmentFlag.AlignCenter,
                f"{candidate_val:02d}",
            )
        if self.hasFocus():
            painter.setPen(QPen(QColor(TOKENS["accent"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 4, 4)

    def step(self, amount: int) -> None:
        """在 `[0, self.maximum]` 闭区间内步进调整当前选中值并重绘。"""
        self.value = max(0, min(self.maximum, self.value + amount))
        self.update()

    def wheelEvent(self, event) -> None:
        """累积高精度触控板/滚轮增量，每满 120 单位步进一格。"""
        self.wheel_delta += event.angleDelta().y()
        steps = int(self.wheel_delta / 120)
        self.wheel_delta -= steps * 120
        self.step(-steps)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        """点击上方或下方邻近行时直接按偏移量步进，并获取键盘焦点。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.step(int(event.position().y() / (self.height() / 5)) - 2)
            self.setFocus()

    def keyPressEvent(self, event) -> None:
        """响应键盘上下键（±1）、PageUp/PageDown（±5）及 Home/End 首尾跳转。"""
        key = event.key()
        step_map = {
            Qt.Key.Key_Up: -1,
            Qt.Key.Key_Down: 1,
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_PageUp: -5,
            Qt.Key.Key_PageDown: 5,
        }
        if key in step_map:
            self.step(step_map[key])
            return
        if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.value = 0 if key == Qt.Key.Key_Home else self.maximum
            self.update()
            return
        super().keyPressEvent(event)


class TimePopup(Flyout):
    """组合“时、分、秒”三列 `TimeColumn` 与确定/取消按钮的弹出式时间选择浮层。"""

    chosen = Signal(QTime)

    def __init__(
        self, parent: QWidget | None, time: QTime, hour_only: bool = False
    ) -> None:
        """以给定时间初始化时、分、秒三列；当 `hour_only=True` 时将分、秒列固定为 `00` 且禁用。"""
        super().__init__(parent)
        initial_time = time if time.isValid() else QTime(0, 0, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        labels_layout = QHBoxLayout()
        columns_layout = QHBoxLayout()
        layout.addLayout(labels_layout)
        layout.addLayout(columns_layout)

        self.columns: list[TimeColumn] = []
        for col_name, col_val, col_max in [
            ("时", initial_time.hour(), 23),
            ("分", initial_time.minute(), 59),
            ("秒", initial_time.second(), 59),
        ]:
            col_label = QLabel(col_name)
            col_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            labels_layout.addWidget(col_label)
            is_fixed_zero = hour_only and col_name != "时"
            time_col = TimeColumn(
                0 if is_fixed_zero else col_val,
                0 if is_fixed_zero else col_max,
                col_name,
                self,
            )
            if is_fixed_zero:
                time_col.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                time_col.setEnabled(False)
            columns_layout.addWidget(time_col)
            self.columns.append(time_col)

        buttons_layout = QHBoxLayout()
        layout.addLayout(buttons_layout)
        self.confirm = QPushButton("确定")
        self.confirm.setProperty("primary", True)
        self.confirm.clicked.connect(self.commit)
        self.cancel = QPushButton("取消")
        self.cancel.clicked.connect(self.close)
        buttons_layout.addWidget(self.confirm)
        buttons_layout.addWidget(self.cancel)

    def commit(self) -> None:
        """将当前三列数值组装为 `QTime` 发射 `chosen` 信号并关闭浮层。"""
        self.chosen.emit(QTime(*(col.value for col in self.columns)))
        self.close()

    def keyPressEvent(self, event) -> None:
        """按下回车键时直接确认提交时间选择。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
        else:
            super().keyPressEvent(event)


class DateTimeInput(QWidget):
    """统一的日期与秒级时间组合输入控件（支持常规秒级模式与 `hour_only=True` 整点模式）。"""

    def __init__(
        self, parent: QWidget | None = None, *, hour_only: bool = False
    ) -> None:
        """构建日期输入框、日历图标按钮、时间输入框及内部时钟图标按钮。"""
        super().__init__(parent)
        self.hour_only = hour_only
        self.popup: Flyout | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.date_edit = QLineEdit()
        self.date_edit.setPlaceholderText("选择日期")
        self.date_edit.setAccessibleName("日期，格式为年-月-日")
        self.date_edit.setMinimumWidth(115)

        self.time_edit = QLineEdit()
        self.time_edit.setPlaceholderText("选择时间")
        self.time_edit.setAccessibleName("时间，24小时制时:分:秒")
        self.time_edit.setMinimumWidth(87)

        time_container_widget: QWidget = self.time_edit
        if hour_only:
            self.time_edit.setValidator(
                QRegularExpressionValidator(
                    QRegularExpression(r"(?:[01]?\d|2[0-3])"), self.time_edit
                )
            )
            self.time_edit.setPlaceholderText("选择小时")
            self.time_edit.setAccessibleName("小时，0至23，分钟和秒固定为00")
            self.time_edit.setToolTip(
                "仅需输入小时（0—23），分、秒固定为 00；不支持粘贴非整点时间。"
            )
            self.time_edit.setMinimumWidth(54)
            time_container_widget = QWidget()
            hour_layout = QHBoxLayout(time_container_widget)
            hour_layout.setContentsMargins(0, 0, 0, 0)
            hour_layout.setSpacing(4)
            hour_layout.addWidget(self.time_edit, 1)
            self.hour_suffix = QLabel(":00:00")
            self.hour_suffix.setAccessibleName("分钟00，秒00，不可编辑")
            hour_layout.addWidget(self.hour_suffix)

        self.date_button = QPushButton()
        self.date_button.setIcon(QIcon(str(ASSETS / "calendar.svg")))
        self.date_button.setAccessibleName("选择日期")
        self.date_button.setToolTip("选择日期")

        self.time_button = QPushButton()
        self.time_button.setIcon(QIcon(str(ASSETS / "clock.svg")))
        self.time_button.setAccessibleName("选择时间")
        self.time_button.setToolTip("选择时间")

        for icon_btn in (self.date_button, self.time_button):
            icon_btn.setFixedSize(34, 34)
        for child_widget in (
            self.date_edit,
            self.date_button,
            time_container_widget,
            self.time_button,
        ):
            layout.addWidget(child_widget)

        self.date_button.clicked.connect(self.open_calendar)
        self.time_button.clicked.connect(self.open_time)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setDateTime(self, value: QDateTime) -> None:
        """将 `QDateTime` 写入日期与时间文本框；在 `hour_only` 整点模式下若含非零分/秒则显式报错拒绝。"""
        if (
            self.hour_only
            and value.isValid()
            and (
                value.time().minute()
                or value.time().second()
                or value.time().msec()
            )
        ):
            raise ValueError(
                "此处仅支持设置整点时刻（分、秒须为 00），请直接选择小时。"
            )
        self.date_edit.setText(
            value.date().toString("yyyy-MM-dd") if value.isValid() else ""
        )
        self.time_edit.setText(
            value.time().toString("HH" if self.hour_only else "HH:mm:ss")
            if value.isValid()
            else ""
        )

    def input_time(self) -> QTime:
        """解析时间输入框草稿文本；若格式不合法则返回无效 `QTime()`，交由提交校验统一报错。"""
        raw_text = self.time_edit.text().strip()
        if self.hour_only:
            parsed_time = (
                QTime(int(raw_text), 0, 0)
                if raw_text.isascii()
                and raw_text.isdigit()
                and len(raw_text) <= 2
                and 0 <= int(raw_text) <= 23
                else QTime()
            )
        else:
            parsed_time = QTime.fromString(raw_text, "HH:mm:ss")
        return parsed_time

    def dateTime(self) -> QDateTime:
        """合并日期草稿与时间草稿并返回 `QDateTime` 对象。"""
        return QDateTime(
            QDate.fromString(self.date_edit.text().strip(), "yyyy-MM-dd"),
            self.input_time(),
        )

    def value(self, label: str) -> str:
        """校验当前输入并返回无时区的 ISO 秒精度字符串（`YYYY-MM-DDTHH:MM:SS`），无效时抛出带字段名的 `ValueError`。"""
        dt_val = self.dateTime()
        if not dt_val.isValid():
            raise ValueError(f"请选择有效的{label}日期和时间")
        return dt_val.toString("yyyy-MM-ddTHH:mm:ss")

    def prepare_popup(
        self, popup: Flyout, anchor: QWidget, focus_target: QWidget
    ) -> None:
        """关闭已打开的旧浮层，在顶层窗口安装隐藏/关闭事件过滤器，并在锚点处弹出新浮层。"""
        if self.popup:
            self.popup.close()
            self.popup.deleteLater()
        self.popup = popup
        self.window().installEventFilter(self)
        popup.show_at(anchor)
        focus_target.setFocus()

    def open_calendar(self) -> None:
        """弹出 `CalendarPopup` 日历浮层，并在用户选中日期后写回 `self.date_edit`。"""
        calendar_popup = CalendarPopup(
            self, QDate.fromString(self.date_edit.text().strip(), "yyyy-MM-dd")
        )
        calendar_popup.chosen.connect(
            lambda chosen_date: self.date_edit.setText(
                chosen_date.toString("yyyy-MM-dd")
            )
        )
        self.prepare_popup(calendar_popup, self.date_edit, calendar_popup.grid)

    def open_time(self) -> None:
        """弹出 `TimePopup` 时分秒滚轮浮层，并在用户点击确定后写回 `self.time_edit`。"""
        time_popup = TimePopup(self, self.input_time(), self.hour_only)
        time_popup.chosen.connect(
            lambda chosen_time: self.time_edit.setText(
                chosen_time.toString("HH" if self.hour_only else "HH:mm:ss")
            )
        )
        self.prepare_popup(time_popup, self.time_edit, time_popup.columns[0])

    def eventFilter(self, watched_obj, event) -> bool:
        """当宿主顶层窗口隐藏或关闭时自动关闭当前打开的日期/时间浮层，防止遗留孤立悬浮窗。"""
        if event.type() in (QEvent.Type.Hide, QEvent.Type.Close) and self.popup:
            self.popup.close()
        return super().eventFilter(watched_obj, event)
