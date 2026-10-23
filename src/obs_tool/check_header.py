"""原生水平表头及首列内嵌真实三态复选框（`QCheckBox`）组件模块。

设计说明：
1. 将真实 `QCheckBox` 挂载在 `QHeaderView.viewport()` 上，而非仅靠自绘图形，从而完整保留 Qt 无障碍可访问性（AccessibleName）与原生键鼠交互；
2. 监听表头列宽调整（`sectionResized`）与几何布局变化（`geometriesChanged`）信号，始终将复选框居中定位于第 0 列表头内；
3. 提供 `state` 属性用于程序静默同步三态状态（未选 / 部分勾选 / 全选），更新时临时阻断信号，避免误触发用户点击回调。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QHeaderView, QWidget


class CheckHeader:
    """将真实三态复选框嵌入表格水平表头首列，兼顾 Qt 无障碍访问与全选/取消全选交互。"""

    def __init__(self, parent: QWidget | None) -> None:
        """创建水平表头与真实三态复选框，并绑定表头尺寸变化后的自动重定位。"""
        self.header = QHeaderView(Qt.Orientation.Horizontal, parent)
        self.checkbox = QCheckBox(self.header.viewport())
        self.checkbox.setTristate(True)
        self.checkbox.setAccessibleName("勾选全部测点或取消全部勾选")
        self.checkbox.setToolTip("勾选全部测点 / 取消全部勾选")
        self.toggled = self.checkbox.clicked
        self.header.sectionResized.connect(self.position)
        self.header.geometriesChanged.connect(self.position)
        self.position()

    @property
    def state(self) -> Qt.CheckState:
        """读取或静默设置首列三态复选框的勾选状态；程序设置时不触发用户点击信号。"""
        return self.checkbox.checkState()

    @state.setter
    def state(self, value: Qt.CheckState) -> None:
        """静默设置三态勾选状态，避免触发用户交互信号。"""
        was_blocked = self.checkbox.blockSignals(True)
        self.checkbox.setCheckState(value)
        self.checkbox.blockSignals(was_blocked)

    def viewport(self) -> QWidget:
        """返回表头视口控件，供主窗口请求重绘。"""
        return self.header.viewport()

    def position(self, *_args) -> None:
        """在表头列宽或高度变化后，将 22x22 复选框重新居中放置于第 0 列表头区域。"""
        self.checkbox.resize(22, 22)
        self.checkbox.move(
            self.header.sectionViewportPosition(0)
            + max(0, (self.header.sectionSize(0) - 22) // 2),
            max(0, (self.header.height() - 22) // 2),
        )
        self.checkbox.show()
