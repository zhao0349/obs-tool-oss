"""曲线绘制组件兼容导出模块（实际画布实现位于 `curve_canvas.py`）。

保留本模块是为了兼容主窗口、历史插件及自动化测试通过 `obs_tool.curve_view.Curve` 或 `obs_tool.curve_view.dt` 访问曲线画布与时间戳转换函数。
"""

from __future__ import annotations

from .curve_canvas import Curve as Curve
from .curve_context import moment as dt

__all__ = ["Curve", "dt"]
