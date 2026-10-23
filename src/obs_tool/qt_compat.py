"""已锁定 PySide6 / Qt 6.8.3 与 Nuitka 编译态信号槽连接的底层兼容补丁模块。

背景说明：
    当使用 Nuitka 将程序编译为原生机器码并配合 `shiboken6`（`cp39-abi3-win_amd64` Limited API）运行时，
    Shiboken 默认对编译态方法（compiled methods）的包装器会在临时 `QObject` 销毁时产生悬挂回调风险。
    `configure()` 在编译态下关闭 `SignalInstance.connect` 内部的 `_wrap_compiled_methods` 标志，
    从而保留 Qt 原生对 `@Slot` 参数计数与接收者生命周期的追踪，同时避免触发临时对象销毁探针。
"""

from __future__ import annotations


def configure() -> None:
    """仅在 Nuitka 编译态（`__compiled__` 存在）下关闭 Shiboken 的编译方法包装，保留原生 Qt 槽生命周期追踪。"""
    if "__compiled__" not in globals():
        return
    from PySide6.QtCore import SignalInstance

    # 锁定的 shiboken6 wheel 为 cp39-abi3-win_amd64（Limited API）。
    # 关闭 _wrap_compiled_methods 后，Qt 仍保留对 @Slot 参数签名和 QObject 接收者销毁的自动断开追踪，
    # 且 Nuitka 的 _protected 机制会自行持有编译方法引用，无需构造额外临时 QObject 探针。
    connect_globals = getattr(SignalInstance.connect, "__globals__", {})
    if "_wrap_compiled_methods" in connect_globals:
        connect_globals["_wrap_compiled_methods"] = False
