"""Qt Widgets 全局视觉主题（Fluent 浅色样式表）、数值框防误滚轮策略与多显示器窗口尺寸适配模块。

设计说明：
1. `TOKENS` 与 `QSS`：定义全软件统一的浅色高对比度色彩令牌、按钮主次层级、复选框/下拉箭头 SVG 图标及表格选中态样式；
2. `InputPolicy`：全局事件过滤器，统一隐藏 `QSpinBox` / `QDoubleSpinBox` 的微调上下小箭头并拦截鼠标滚轮事件，防止用户在滚动长表单时无意间篡改水深或算法参数；
3. `fit_window()`：按当前屏幕可用工作区（`availableGeometry`）约束窗口宽高（全部使用 Qt 逻辑像素单位），并在外接显示器断开导致历史坐标越界时自动居中拉回主屏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QDialogButtonBox,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
    QWidget,
)

# 表格异常行（如待填写水深或处理失败）的浅粉红警示背景色
ERROR_BACKGROUND: str = "#FFF1F2"

# 全局设计色彩与圆角令牌
TOKENS: dict[str, Any] = dict(
    background="#F3F3F3",
    surface="#FFFFFF",
    text="#242424",
    muted="#616161",
    border="#D1D1D1",
    accent="#005FB8",
    selection="#E5F1FB",
    radius=4,
)
ASSETS: Path = (Path(__file__).parent / "assets").resolve()
ARROW: str = (ASSETS / "chevron.svg").as_posix()

QSS: str = """
QWidget { color: @text; }
QMainWindow, QDialog { background: @background; }
QLabel[role="title"] { font-size: 17pt; font-weight: 600; }
QLabel[role="muted"] { color: @muted; }
QLabel[role="error"] { color: #B42318; }
QLabel[role="warning"] { color: #895500; }
QPushButton, QToolButton { padding: 5px 12px; border: 1px solid @border; border-radius: 4px; background: @surface; }
QPushButton:hover, QToolButton:hover { background: #F9F9F9; border-color: #989898; }
QPushButton:pressed, QToolButton:pressed { background: #EDEDED; }
QPushButton:checked, QToolButton:checked { background: @selection; border-color: @accent; }
QPushButton[primary="true"] { background: @accent; color: white; border-color: @accent; }
QPushButton[primary="true"]:hover { background: #0056A6; }
QPushButton[primary="true"]:pressed { background: #004C94; }
QPushButton:disabled, QToolButton:disabled { background: #F0F0F0; color: #8A8A8A; border-color: #DFDFDF; }
QPushButton:focus, QToolButton:focus { border-color: @accent; }
QLineEdit, QComboBox, QAbstractSpinBox { padding: 5px 8px; border: 1px solid @border; border-bottom-color: #898989; border-radius: 4px; background: @surface; min-height: 18px; }
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover { border-color: #989898; }
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus { border-color: @accent; border-bottom: 1px solid @accent; background: #F8FCFF; }
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled { background: #F0F0F0; color: #8A8A8A; border-color: #DFDFDF; }
QComboBox { padding-right: 32px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 28px; border: 0; background: transparent; }
QComboBox::down-arrow, QToolButton::menu-indicator { image: url(@arrow); width: 12px; height: 12px; }
QComboBox::down-arrow:disabled { image: url(@arrow); }
QToolButton { padding-right: 30px; }
QToolButton::menu-indicator { subcontrol-origin: padding; subcontrol-position: right center; }
QComboBox QAbstractItemView { background: white; color: @text; border: 1px solid @border; selection-background-color: @selection; selection-color: @text; outline: 0; padding: 4px; }
QComboBox QAbstractItemView::item { min-height: 28px; padding: 3px 8px; }
QTableWidget, QListWidget, QTextEdit { background: white; border: 1px solid @border; gridline-color: #EEEEEE; selection-background-color: @selection; selection-color: @text; }
QTableWidget::item:selected { background: @selection; }
QListWidget::item:selected { background: @selection; color: @text; }
QTableWidget::item:focus, QListWidget::item:focus { border: 1px solid @accent; }
QHeaderView::section { background: #F9F9F9; padding: 5px; border: 0; border-bottom: 1px solid @border; font-weight: 600; }
QHeaderView, QTableCornerButton::section { background: #F9F9F9; border: 0; }
QTabWidget::pane { border: 1px solid @border; background: white; }
QTabBar::tab { background: transparent; border: 0; border-bottom: 2px solid transparent; padding: 7px 16px; margin-right: 4px; }
QTabBar::tab:hover { background: #EAEAEA; }
QTabBar::tab:selected { background: white; color: @accent; border-bottom-color: @accent; }
QTabBar::tab:disabled { color: #8A8A8A; }
QTabBar::tab:focus { color: @accent; }
QGroupBox { border: 0; margin-top: 22px; padding-top: 4px; }
QGroupBox::title { subcontrol-origin: margin; left: 0; top: 2px; font-weight: 600; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: #F5F5F5; width: 12px; margin: 0; }
QScrollBar:horizontal { background: #F5F5F5; height: 12px; margin: 0; }
QScrollBar::handle { background: #B5B5B5; border-radius: 4px; min-width: 28px; min-height: 28px; }
QScrollBar::handle:hover { background: #858585; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMenu { background: white; border: 1px solid @border; padding: 4px; }
QMenu::item { padding: 7px 28px 7px 12px; border: 1px solid transparent; border-radius: 3px; }
QMenu::item:selected { background: @selection; border-color: @accent; }
QMenu::item:disabled { color: #8A8A8A; }
QMenu::separator { height: 1px; background: @border; margin: 4px 8px; }
QCheckBox { spacing: 7px; }
QCheckBox:disabled { color: #8A8A8A; }
QCheckBox:focus { color: @accent; }
QCheckBox::indicator, QTableWidget::indicator { width: 17px; height: 17px; border: 1px solid #898989; border-radius: 3px; background: white; }
QCheckBox::indicator:hover, QTableWidget::indicator:hover { border-color: @accent; background: #F3F8FD; }
QCheckBox::indicator:checked, QTableWidget::indicator:checked { image: url(@check); border-color: @accent; background: @accent; }
QCheckBox::indicator:indeterminate, QTableWidget::indicator:indeterminate { image: url(@partial); border-color: @accent; background: @accent; }
QCheckBox::indicator:disabled, QTableWidget::indicator:disabled { border-color: #C6C6C6; background: #EDEDED; }
QCheckBox::indicator:checked:disabled, QTableWidget::indicator:checked:disabled { image: url(@check); background: #8A8A8A; }
QCheckBox::indicator:focus { border-color: @accent; }
QRadioButton { spacing: 7px; }
QRadioButton:disabled { color: #8A8A8A; }
QRadioButton:focus { color: @accent; }
QRadioButton::indicator { width: 17px; height: 17px; border: 1px solid #898989; border-radius: 9px; background: white; }
QRadioButton::indicator:hover { border-color: @accent; background: #F3F8FD; }
QRadioButton::indicator:checked { border-color: @accent; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 white, stop:0.4 white, stop:0.45 @accent, stop:1 @accent); }
QRadioButton::indicator:disabled { border-color: #C6C6C6; background: #EDEDED; }
QRadioButton::indicator:checked:disabled { border-color: #8A8A8A; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 #EDEDED, stop:0.4 #EDEDED, stop:0.45 #8A8A8A, stop:1 #8A8A8A); }
QSplitter::handle { background: #E5E5E5; }
QSplitter::handle:hover { background: @accent; }
QProgressBar { border: 1px solid @border; border-radius: 4px; text-align: center; background: white; }
QProgressBar::chunk { background: @accent; }
QToolTip { background: white; color: @text; border: 1px solid @border; padding: 5px; }
"""
for token_key, token_val in {
    **TOKENS,
    "arrow": ARROW,
    "check": (ASSETS / "check.svg").as_posix(),
    "partial": (ASSETS / "partial.svg").as_posix(),
}.items():
    QSS = QSS.replace("@" + token_key, str(token_val))


class InputPolicy(QObject):
    """应用级数值输入框交互策略：隐藏 `QSpinBox` / `QDoubleSpinBox` 步进按钮并屏蔽鼠标滚轮误触。"""

    def eventFilter(self, watched_obj: QObject, event: QEvent) -> bool:
        """对所有数值微调框统一隐藏右侧微调箭头、限制最大宽度，并在滚轮滚动时将事件交由外层滚动区域处理。"""
        if isinstance(watched_obj, (QSpinBox, QDoubleSpinBox)):
            if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
                watched_obj.setButtonSymbols(
                    QAbstractSpinBox.ButtonSymbols.NoButtons
                )
                watched_obj.setMaximumWidth(200)
            if event.type() == QEvent.Type.Wheel:
                event.ignore()
                return True
        return False


def apply_theme(app: QApplication) -> None:
    """为 `QApplication` 应用 Fusion 引擎、微软雅黑 UI 字体、Fluent 浅色调色板与 QSS，并安装全局输入过滤器。"""
    app.setStyle("Fusion")
    if not hasattr(app, "input_policy"):
        app.input_policy = InputPolicy(app)
        app.installEventFilter(app.input_policy)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app_palette = app.palette()
    for color_role, hex_color in [
        (QPalette.ColorRole.Window, TOKENS["background"]),
        (QPalette.ColorRole.Base, "#FFFFFF"),
        (QPalette.ColorRole.AlternateBase, "#F9FAFC"),
        (QPalette.ColorRole.Button, "#FFFFFF"),
        (QPalette.ColorRole.ButtonText, TOKENS["text"]),
        (QPalette.ColorRole.WindowText, TOKENS["text"]),
        (QPalette.ColorRole.Text, TOKENS["text"]),
        (QPalette.ColorRole.Highlight, TOKENS["selection"]),
        (QPalette.ColorRole.HighlightedText, TOKENS["text"]),
    ]:
        app_palette.setColor(color_role, QColor(hex_color))
    app.setPalette(app_palette)
    app.setStyleSheet(QSS)


def fit_window(window: QWidget, width: int, height: int) -> None:
    """按当前显示器可用工作区裁剪窗口逻辑尺寸，并在窗口坐标落入已断开显示器时自动居中回当前屏幕。"""
    target_screen = (
        window.parentWidget().screen()
        if window.parentWidget()
        else QApplication.primaryScreen()
    )
    available_area = target_screen.availableGeometry()
    window.resize(
        min(width, max(360, available_area.width() - 32)),
        min(height, max(300, available_area.height() - 48)),
    )
    # 若恢复了已拔除副屏的几何坐标，则将窗口重新居中放置到当前可用屏幕内
    window_frame = window.frameGeometry()
    if not any(
        scr.availableGeometry().contains(window_frame)
        for scr in QApplication.screens()
    ):
        window.move(
            available_area.x()
            + max(0, (available_area.width() - window.width()) // 2),
            available_area.y()
            + max(0, (available_area.height() - window.height() - 40) // 2),
        )


def chinese_buttons(
    buttons: QDialogButtonBox, accept: str = "应用"
) -> None:
    """将标准 `QDialogButtonBox` 中的 `Ok` 与 `Cancel` 按钮统一替换为中文标签（默认为 `'应用'` 与 `'取消'`）。"""
    for std_role, label_text in [
        (QDialogButtonBox.StandardButton.Ok, accept),
        (QDialogButtonBox.StandardButton.Cancel, "取消"),
    ]:
        btn_widget = buttons.button(std_role)
        if btn_widget:
            btn_widget.setText(label_text)


def table_style(table: QTableWidget) -> None:
    """为 `QTableWidget` 统一配置 30px 默认行高、隔行变色、不自动换行、省略号居中及整行选中行为。"""
    table.verticalHeader().setDefaultSectionSize(30)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    table.setSelectionBehavior(table.SelectionBehavior.SelectRows)
