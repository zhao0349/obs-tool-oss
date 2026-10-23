"""OBS 桌面图形界面公共导出入口模块。

说明：
    主界面支持用户直接拖拽或选择 `.dat` 原始观测文件进行批处理，无需手动编写 JSON 配置文件。
    本模块显式重导出 `Window`、`main`、`Worker` 与 `Curve`，确保外部启动脚本与自动化测试拥有稳定的导入路径。
"""

from __future__ import annotations

from .desktop import Window as Window, main as main
from .gui_components import Curve as Curve, Worker as Worker

__all__ = ["Window", "main", "Worker", "Curve"]
