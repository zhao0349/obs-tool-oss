"""主窗口“数据与设置”及“处理结果”两页桌面布局装配模块（`build_pages`）。

设计说明：
1. 本模块仅负责构建 Qt 控件层级、设置列宽与占位文案，并将所有信号连接到宿主 `Window` 的业务槽函数；
2. 测点草稿、后台任务生命周期与历史结果快照均由 `desktop.Window` 统一管理；
3. 为保持主界面简洁，低频操作（移除勾选测点、清空当前列表、恢复默认布局、查看当前测点水深详情）统一收纳至“更多”下拉菜单（`QToolButton` + `QMenu`），同时保留隐藏的 `remove_button` / `clear_button` 供自动化回归测试直接触发。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME
from .check_header import CheckHeader
from .result_view import ResultView
from .theme import fit_window, table_style

if TYPE_CHECKING:
    # 仅供 IDE 获取主窗口接口与类型补全，避免运行时循环导入。
    from .desktop import Window


def build_pages(window: Window) -> None:
    """构建主窗口顶栏、“数据与设置”页、“处理结果”页及底部状态栏，并绑定业务槽函数。"""
    root = QWidget()
    window.setCentralWidget(root)
    layout = QVBoxLayout(root)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(8)

    # 1. 顶部全局标题与工具入口栏（实用工具 / 帮助 / 高级设置）
    header = QHBoxLayout()
    layout.addLayout(header)
    title = QLabel(APP_NAME)
    title.setProperty("role", "title")
    header.addWidget(title, 1)
    window.tools_button = window.button(header, "实用工具", window.show_tools)
    window.help_button = window.button(header, "帮助", window.show_help)
    window.advanced_button = window.button(header, "高级设置", window.parameters)

    window.pages = QTabWidget()
    layout.addWidget(window.pages, 1)

    # 2. 第一页：“数据与设置”工作区
    data_page = QWidget()
    window.controls = data_page
    body = QVBoxLayout(data_page)
    body.setContentsMargins(12, 10, 12, 10)
    body.setSpacing(4)
    window.pages.addTab(data_page, "数据与设置")

    import_row = QHBoxLayout()
    body.addLayout(import_row)
    window.folder_button = window.button(import_row, "添加文件夹", window.scan)
    window.files_button = window.button(
        import_row, "添加 DAT 文件", window.add_dat_files
    )
    import_row.addStretch()
    window.new_button = window.button(import_row, "开始新一批", window.new_batch)

    window.input = QLabel("添加 DAT 文件或文件夹，开始本次处理")
    window.input.setProperty("role", "muted")
    body.addWidget(window.input)

    action_row = QHBoxLayout()
    body.addLayout(action_row)
    window.edit_button = window.button(
        action_row, "编辑当前测点", window.edit_point
    )
    window.bulk_button = window.button(action_row, "批量设置", window.bulk_edit)
    window.mapping_button = window.button(
        action_row, "设置数据文件", window.edit_files
    )
    window.select_all = window.button(
        action_row, "勾选全部", window.toggle_all_checks
    )
    action_row.addStretch()

    window.more = QToolButton()
    window.more.setText("更多")
    window.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    more_menu = QMenu(window.more)
    more_menu.addAction("移除勾选测点（不删除文件）", window.remove_points)
    more_menu.addAction("清空当前列表（不删除文件）", window.new_batch)
    more_menu.addAction("恢复默认布局", window.restore_layout)
    window.more.setMenu(more_menu)
    action_row.addWidget(window.more)
    window.remove_action = more_menu.actions()[0]
    window.check_action = more_menu.addAction(
        "查看当前测点水深详情", window.show_current_depth
    )

    # 历史开发与自动化测试接口保留，不在主操作区直接展示删除/清空按钮。
    window.remove_button = QPushButton(window)
    window.remove_button.hide()
    window.remove_button.clicked.connect(window.remove_points)
    window.clear_button = QPushButton(window)
    window.clear_button.hide()
    window.clear_button.clicked.connect(window.clear_inputs)

    window.empty = QLabel(
        "添加 DAT 数据后，可核对水深、采样层位和时间范围。\n原始文件始终保留，输出写入独立目录。"
    )
    window.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.empty.setProperty("role", "muted")
    body.addWidget(window.empty, 1)

    window.table = QTableWidget(0, 7)
    table_style(window.table)
    window.table.setHorizontalHeaderLabels(
        [
            "",
            "测点编号",
            "水深 H（米）",
            "水深来源",
            "分层规则与层位",
            "时间范围",
            "数据文件",
        ]
    )
    window.check_header = CheckHeader(window.table)
    window.table.setHorizontalHeader(window.check_header.header)
    window.check_header.toggled.connect(window.set_all_checks)
    window.table.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection
    )
    for col, width in enumerate([42, 135, 140, 150, 300, 200, 140]):
        window.table.setColumnWidth(col, width)
    window.table.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.Interactive
    )
    window.table.itemChanged.connect(window.point_changed)
    window.table.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
    window.table.cellDoubleClicked.connect(window.edit_double_clicked)
    window.table.itemSelectionChanged.connect(window.update_selection)
    body.addWidget(window.table, 1)
    window.table.hide()

    foot_row = QHBoxLayout()
    body.addLayout(foot_row)
    window.selection_summary = QLabel("已勾选 0 个测点 · 先勾选要修改的测点")
    foot_row.addWidget(window.selection_summary, 1)
    window.check_button = window.button(
        foot_row, "查看水深估算详情", window.show_depth_reasons
    )

    output_row = QHBoxLayout()
    body.addLayout(output_row)
    output_row.addWidget(QLabel("CSV 保存位置"))
    window.output = QLineEdit(window.batch.output_dir)
    window.output.setReadOnly(True)
    output_row.addWidget(window.output, 1)
    window.output_button = window.button(output_row, "更改", window.choose_output)

    run_row = QHBoxLayout()
    body.addLayout(run_row)
    window.ready = QLabel("请先添加数据")
    window.ready.setWordWrap(True)
    run_row.addWidget(window.ready, 1)
    window.start = window.button(run_row, "开始处理并导出 CSV", window.execute)
    window.start.setProperty("primary", True)

    # 3. 第二页：“处理结果”核查页
    result_page = QWidget()
    results_layout = QVBoxLayout(result_page)
    results_layout.setContentsMargins(12, 12, 12, 12)
    results_layout.setSpacing(4)
    window.pages.addTab(result_page, "处理结果")

    result_header = QHBoxLayout()
    results_layout.addLayout(result_header)
    window.run_summary = QLabel("尚无运行结果")
    window.run_summary.setWordWrap(True)
    result_header.addWidget(window.run_summary, 1)
    window.point_choice = QComboBox()
    window.point_choice.setPlaceholderText("选择结果测点")
    window.point_choice.setMaximumWidth(220)
    window.point_choice.setMinimumContentsLength(10)
    window.point_choice.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    window.point_choice.currentIndexChanged.connect(window.show_point)
    result_header.addWidget(window.point_choice)
    window.open_button = window.button(
        result_header, "打开结果文件夹", window.open_results
    )
    window.open_button.setEnabled(False)

    window.result_view = ResultView()
    results_layout.addWidget(window.result_view, 1)
    window.preview = window.result_view.table
    window.curve = window.result_view.curve
    window.details = window.result_view.details

    bottom_row = QHBoxLayout()
    results_layout.addLayout(bottom_row)
    window.log_toggle = QPushButton("展开运行日志")
    window.log_toggle.setCheckable(True)
    bottom_row.addWidget(window.log_toggle)
    bottom_row.addStretch()
    window.retry = window.button(
        bottom_row, "仅重试失败测点", window.retry_failed
    )
    window.retry.setEnabled(False)
    window.retry.hide()

    window.log = QTextEdit()
    window.log.setReadOnly(True)
    window.log.setMaximumHeight(100)
    results_layout.addWidget(window.log)
    window.log.hide()
    window.log_toggle.toggled.connect(window.toggle_log)

    # 4. 底部全局任务状态、进度条与取消按钮栏
    status_row = QHBoxLayout()
    layout.addLayout(status_row)
    window.summary = QLabel("无数据")
    status_row.addWidget(window.summary, 1)
    window.progress = QProgressBar()
    window.progress.setMaximumWidth(220)
    status_row.addWidget(window.progress)
    window.progress.hide()
    window.cancel = window.button(status_row, "取消任务", window.request_cancel)
    window.cancel.setEnabled(False)
    window.cancel.hide()
    window.progress.setValue(0)
    fit_window(window, 1200, 780)
