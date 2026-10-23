"""Windows COM 原生文件对话框（`IFileOpenDialog` / `IFileSaveDialog`）底层调用与窗口控制模块。

技术说明：
1. 本模块仅在 `--native-picker` 独立子进程的 STA（单线程套间）主线程中调用，浏览目录期间绝不预读 `.dat` 文件内容；
2. 直接通过 `ctypes` 绑定 `ole32` 与 `shell32` 的 COM 虚函数表（vtable），无需额外安装 `pywin32` 等重量级依赖；
3. 提供 `dialog_window()` 与 `command_dialog()`：通过 `CreateToolhelp32Snapshot` 遍历进程树定位子进程创建的标准对话框窗口（类名 `#32770`），供主界面取消关闭及自动化测试驱动使用。
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os
from typing import Sequence
from uuid import UUID


class GUID(c.Structure):
    """与 Windows COM `GUID` 16 字节内存布局严格对应的 `ctypes` 结构体。"""

    _fields_ = [
        ("Data1", c.c_uint32),
        ("Data2", c.c_uint16),
        ("Data3", c.c_uint16),
        ("Data4", c.c_ubyte * 8),
    ]


def guid(value: str) -> GUID:
    """将标准文本格式的 UUID/GUID 解析为 COM 接口调用所需的 `GUID` 结构体。"""
    return GUID.from_buffer_copy(UUID(value).bytes_le)


def method(com_obj: c.c_void_p, vtable_index: int, *argtypes: type):
    """从 COM 对象指针的虚函数表（vtable）中取出指定槽位的方法，并绑定 `stdcall` 调用签名。"""
    vtable_ptr = c.cast(com_obj, c.POINTER(c.POINTER(c.c_void_p))).contents
    return c.WINFUNCTYPE(c.c_int32, c.c_void_p, *argtypes)(vtable_ptr[vtable_index])


def check(hresult: int) -> None:
    """检查 COM 调用返回的 `HRESULT`，若为负数（失败）则抛出包含十六进制错误码的 `OSError`。"""
    if hresult < 0:
        raise OSError(f"Windows文件选择失败：0x{hresult & 0xffffffff:08X}")


def release(com_obj: c.c_void_p) -> None:
    """调用 `IUnknown::Release`（vtable 槽位 2）释放 COM 接口指针。"""
    if com_obj:
        method(com_obj, 2)(com_obj)


def choose(
    title: str,
    folder: str = "",
    directory: bool = False,
    owner: int = 0,
    initial_names: Sequence[str] | None = None,
    save: bool = False,
) -> list[str]:
    """弹出 Windows 原生打开/保存文件或选择文件夹对话框，返回用户选定的文件系统路径列表。

    Args:
        title: 对话框窗口标题。
        folder: 初始打开的目录路径（若存在则自动定位）。
        directory: 是否为选择文件夹模式（`FOS_PICKFOLDERS`）。
        owner: 父窗口句柄（`HWND`），用于模态挂靠。
        initial_names: 预填入文件名输入框的初始名称列表。
        save: 是否使用保存对话框（`IFileSaveDialog`）；为 `False` 时使用打开对话框（`IFileOpenDialog`）。

    Returns:
        选定的绝对路径字符串列表；若用户点击取消（`ERROR_CANCELLED = 0x800704C7`），返回空列表 `[]`。
    """
    if os.name != "nt":
        raise OSError("系统文件选择器仅支持Windows")

    ole32_dll = c.WinDLL("ole32")
    shell32_dll = c.WinDLL("shell32")
    ole32_dll.CoInitializeEx.argtypes = [c.c_void_p, c.c_uint32]
    ole32_dll.CoInitializeEx.restype = c.c_int32
    ole32_dll.CoCreateInstance.argtypes = [
        c.POINTER(GUID),
        c.c_void_p,
        c.c_uint32,
        c.POINTER(GUID),
        c.POINTER(c.c_void_p),
    ]
    ole32_dll.CoCreateInstance.restype = c.c_int32
    ole32_dll.CoTaskMemFree.argtypes = [c.c_void_p]
    ole32_dll.CoUninitialize.argtypes = []
    shell32_dll.SHCreateItemFromParsingName.argtypes = [
        w.LPCWSTR,
        c.c_void_p,
        c.POINTER(GUID),
        c.POINTER(c.c_void_p),
    ]
    shell32_dll.SHCreateItemFromParsingName.restype = c.c_int32

    # COINIT_APARTMENTTHREADED (0x2) | COINIT_DISABLE_OLE1DDE (0x4)
    check(ole32_dll.CoInitializeEx(None, 2 | 4))
    dialog_ptr = c.c_void_p()
    results_array_ptr = c.c_void_p()
    initial_folder_item = c.c_void_p()
    try:
        # CLSID_FileSaveDialog / CLSID_FileOpenDialog 与 IID_IFileSaveDialog / IID_IFileOpenDialog
        check(
            ole32_dll.CoCreateInstance(
                c.byref(
                    guid(
                        "C0B4E2F3-BA21-4773-8DBA-335EC946EB8B"
                        if save
                        else "DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7"
                    )
                ),
                None,
                1,  # CLSCTX_INPROC_SERVER
                c.byref(
                    guid(
                        "84BCCD23-5FDE-4CDB-AEA4-AF64B83D78AB"
                        if save
                        else "D57C7288-D4AD-4768-BE02-9D969532D960"
                    )
                ),
                c.byref(dialog_ptr),
            )
        )

        current_options = c.c_uint32()
        check(
            method(dialog_ptr, 10, c.POINTER(c.c_uint32))(
                dialog_ptr, c.byref(current_options)
            )
        )
        # 组合标志位：FOS_NOCHANGEDIR (0x8) | FOS_FORCEFILESYSTEM (0x40) | FOS_PATHMUSTEXIST (0x800) | FOS_DONTADDTORECENT (0x2000000)
        dialog_flags = current_options.value | 0x8 | 0x40 | 0x800 | 0x2000000
        # 保存模式启用 FOS_OVERWRITEPROMPT (0x2)；打开模式启用 FOS_FILEMUSTEXIST (0x1000) 及文件夹选择 (0x20) 或多选 (0x200)
        dialog_flags |= 0x2 if save else (0x1000 | (0x20 if directory else 0x200))
        if save:
            dialog_flags &= ~(0x1000 | 0x200 | 0x20)
        check(method(dialog_ptr, 9, c.c_uint32)(dialog_ptr, dialog_flags))
        check(method(dialog_ptr, 17, w.LPCWSTR)(dialog_ptr, title))
        if save:
            check(method(dialog_ptr, 22, w.LPCWSTR)(dialog_ptr, "dat"))
        check(
            method(dialog_ptr, 18, w.LPCWSTR)(
                dialog_ptr,
                "保存DAT" if save else "选择文件夹" if directory else "选择DAT",
            )
        )

        if not directory:

            class ComDlgFilterSpec(c.Structure):
                """对应 `COMDLG_FILTERSPEC` 结构体，定义文件扩展名过滤器名称与通配模式。"""

                _fields_ = [("name", w.LPCWSTR), ("spec", w.LPCWSTR)]

            file_filters = (ComDlgFilterSpec * 1)(
                ComDlgFilterSpec("OBS原始数据（DAT）", "*.dat")
            )
            check(
                method(dialog_ptr, 4, c.c_uint32, c.POINTER(ComDlgFilterSpec))(
                    dialog_ptr, 1, file_filters
                )
            )

        if folder:
            parse_hr = shell32_dll.SHCreateItemFromParsingName(
                str(folder),
                None,
                c.byref(guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")),  # IID_IShellItem
                c.byref(initial_folder_item),
            )
            if parse_hr >= 0:
                check(
                    method(dialog_ptr, 12, c.c_void_p)(dialog_ptr, initial_folder_item)
                )

        if initial_names:
            check(
                method(dialog_ptr, 15, w.LPCWSTR)(
                    dialog_ptr,
                    (
                        initial_names[0]
                        if save
                        else " ".join(f'"{name}"' for name in initial_names)
                    ),
                )
            )

        show_hr = method(dialog_ptr, 3, w.HWND)(dialog_ptr, owner or None)
        # 0x800704C7 为 HRESULT_FROM_WIN32(ERROR_CANCELLED)，表示用户关闭或取消了对话框
        if (show_hr & 0xFFFFFFFF) == 0x800704C7:
            return []
        check(show_hr)

        if save:
            shell_item = c.c_void_p()
            psz_name = c.c_void_p()
            try:
                check(
                    method(dialog_ptr, 20, c.POINTER(c.c_void_p))(
                        dialog_ptr, c.byref(shell_item)
                    )
                )
                # SIGDN_FILESYSPATH = 0x80058000
                check(
                    method(shell_item, 5, c.c_uint32, c.POINTER(c.c_void_p))(
                        shell_item, 0x80058000, c.byref(psz_name)
                    )
                )
                return [c.wstring_at(psz_name)]
            finally:
                if psz_name:
                    ole32_dll.CoTaskMemFree(psz_name)
                release(shell_item)

        check(
            method(dialog_ptr, 27, c.POINTER(c.c_void_p))(
                dialog_ptr, c.byref(results_array_ptr)
            )
        )
        item_count = c.c_uint32()
        check(
            method(results_array_ptr, 7, c.POINTER(c.c_uint32))(
                results_array_ptr, c.byref(item_count)
            )
        )
        from .input_policy import LIMITS, enforce

        enforce(item_count.value, LIMITS.selection_entries, "系统多选文件数")
        selected_paths: list[str] = []
        for item_index in range(item_count.value):
            shell_item = c.c_void_p()
            psz_name = c.c_void_p()
            try:
                check(
                    method(results_array_ptr, 8, c.c_uint32, c.POINTER(c.c_void_p))(
                        results_array_ptr, item_index, c.byref(shell_item)
                    )
                )
                check(
                    method(shell_item, 5, c.c_uint32, c.POINTER(c.c_void_p))(
                        shell_item, 0x80058000, c.byref(psz_name)
                    )
                )
                selected_paths.append(c.wstring_at(psz_name))
            finally:
                if psz_name:
                    ole32_dll.CoTaskMemFree(psz_name)
                release(shell_item)
        return selected_paths
    finally:
        release(results_array_ptr)
        release(initial_folder_item)
        release(dialog_ptr)
        ole32_dll.CoUninitialize()


def dialog_window(process_id: int) -> int | None:
    """定位指定选择器进程（或其派生子进程）创建的 Windows 标准对话框窗口句柄（`#32770`）。

    注意：在 Windows 虚拟环境（venv）下，`python.exe` 是启动器外壳，真正的解释器与对话框可能属于其子进程，
    因此需要通过 `CreateToolhelp32Snapshot` 向下收集最多 4 层子进程 PID。
    """
    if not process_id or process_id <= 0:
        return None
    user32_dll = c.WinDLL("user32")
    matched_hwnds: list[int] = []

    class ProcessEntry32W(c.Structure):
        """对应 Win32 `PROCESSENTRY32W` 结构体，用于遍历系统进程快照建立父子进程关系。"""

        _fields_ = [
            ("size", w.DWORD),
            ("usage", w.DWORD),
            ("pid", w.DWORD),
            ("heap", c.c_void_p),
            ("module", w.DWORD),
            ("threads", w.DWORD),
            ("parent", w.DWORD),
            ("priority", w.LONG),
            ("flags", w.DWORD),
            ("name", w.WCHAR * 260),
        ]

    kernel32_dll = c.WinDLL("kernel32")
    kernel32_dll.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel32_dll.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel32_dll.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry32W)]
    kernel32_dll.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry32W)]
    kernel32_dll.CloseHandle.argtypes = [w.HANDLE]

    allowed_pids = {process_id}
    parent_child_edges: list[tuple[int, int]] = []
    # TH32CS_SNAPPROCESS = 0x2
    snapshot_handle = kernel32_dll.CreateToolhelp32Snapshot(2, 0)
    if snapshot_handle and snapshot_handle != c.c_void_p(-1).value:
        try:
            proc_entry = ProcessEntry32W()
            proc_entry.size = c.sizeof(proc_entry)
            has_more = kernel32_dll.Process32FirstW(
                snapshot_handle, c.byref(proc_entry)
            )
            while has_more:
                parent_child_edges.append((proc_entry.pid, proc_entry.parent))
                has_more = kernel32_dll.Process32NextW(
                    snapshot_handle, c.byref(proc_entry)
                )
            for _ in range(4):
                allowed_pids.update(
                    child_pid
                    for child_pid, parent_pid in parent_child_edges
                    if parent_pid in allowed_pids
                )
        finally:
            kernel32_dll.CloseHandle(snapshot_handle)

    user32_dll.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user32_dll.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
    enum_callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def _visit_window(hwnd: int, _lparam: int) -> bool:
        """枚举顶层窗口，收集隶属于目标进程树且窗口类名为 `#32770` 的原生对话框句柄。"""
        window_pid = w.DWORD()
        user32_dll.GetWindowThreadProcessId(hwnd, c.byref(window_pid))
        class_name_buf = c.create_unicode_buffer(256)
        user32_dll.GetClassNameW(hwnd, class_name_buf, 256)
        if window_pid.value in allowed_pids and class_name_buf.value == "#32770":
            matched_hwnds.append(hwnd)
        return True

    enum_callback = enum_callback_type(_visit_window)
    user32_dll.EnumWindows.argtypes = [enum_callback_type, w.LPARAM]
    user32_dll.EnumWindows(enum_callback, 0)
    return matched_hwnds[0] if matched_hwnds else None


def command_dialog(process_id: int, accept: bool = False) -> bool:
    """向指定进程的原生文件对话框发送确认（`WM_COMMAND IDOK`）或关闭（`WM_CLOSE`）消息。"""
    hwnd = dialog_window(process_id)
    if not hwnd:
        return False
    user32_dll = c.WinDLL("user32")
    user32_dll.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    # 0x111 = WM_COMMAND (wParam=1 即 IDOK)；0x10 = WM_CLOSE
    return bool(
        user32_dll.PostMessageW(hwnd, 0x111 if accept else 0x10, 1 if accept else 0, 0)
    )
