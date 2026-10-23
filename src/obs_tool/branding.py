"""已确认的应用多尺寸图标加载与稳定 Windows 应用用户模型标识（AppUserModelID）绑定模块。

设计说明：
1. 路径基于 `__file__` 绝对解析，不依赖进程启动时的当前工作目录（CWD）；
2. 在 Windows 平台调用 `shell32.SetCurrentProcessExplicitAppUserModelID` 绑定稳定的 `APP_ID`，确保任务栏图标分组与快捷方式一致；
3. 按 `ICON_SIZES` 依次挂载 16px～256px 多尺寸 PNG 图标，并使用 `obs-branding-ready` 属性避免重复初始化。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon

from . import APP_NAME

APP_ID = "zhao0349.OBSDataTool"
ICON_PATH = (
    Path(__file__).resolve().parent / "assets" / "branding" / "obs-tool-256.png"
)
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def configure(app) -> QIcon:
    """设置 Qt 应用显示名称、Windows 任务栏 AppUserModelID 与多分辨率图标，并返回图标对象供主窗口复用。"""
    app.setApplicationDisplayName(APP_NAME)
    if app.property("obs-branding-ready"):
        return app.windowIcon()

    if sys.platform == "win32":
        import ctypes

        shell32_dll = ctypes.WinDLL("shell32")
        set_app_id = shell32_dll.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        hresult = set_app_id(APP_ID)
        if hresult < 0:
            logging.warning(
                "设置Windows应用标识失败 HRESULT=0x%08X", hresult & 0xFFFFFFFF
            )

    icon = QIcon()
    for size in ICON_SIZES:
        icon.addFile(
            str(ICON_PATH.with_name(f"obs-tool-{size}.png")), QSize(size, size)
        )
    if icon.isNull():
        logging.error("无法加载应用图标：%s", ICON_PATH)
    else:
        app.setWindowIcon(icon)
    app.setProperty("obs-branding-ready", True)
    return icon
