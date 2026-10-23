"""面向终端用户的错误提示友好化映射模块（`user_messages.py`）。

设计目标：
    在 GUI 界面边界将底层操作系统异常（如 `PermissionError`、`WinError 5`、`FileNotFoundError`）
    或领域字段校验错误转换为清晰、可操作的中文排障指引，同时由调用方在运行日志中保留原始技术堆栈。
"""

from __future__ import annotations


def error_text(message: object) -> str:
    """将已知的技术异常消息转换为用户可直接采取行动的中文说明；对未知业务错误则原样保留。

    Args:
        message: 异常对象或原始错误字符串。

    Returns:
        转换后的用户可读中文错误提示字符串。
    """
    raw_text = str(message)
    if "文件选择" in raw_text:
        return "无法打开文件选择窗口，请重试。"

    parameter_labels = {
        "min_depth": "最低参考深度",
        "gap_minutes": "片段断档上限",
        "pre_minutes": "前缓冲",
        "tail_minutes": "后缓冲上限",
        "stop_count": "结束所需连续低值数量",
        "max_offset_minutes": "整点与片段峰值的最大时间差",
        "surface_min": "表层深度下限",
        "depth_weight": "表层深度权重",
        "hour_weight": "距整点时间权重",
        "entry_weight": "距片段起点权重",
        "tolerance_m": "绝对深度容差",
        "tolerance_ratio": "相对深度容差",
    }
    for field_key, chinese_name in parameter_labels.items():
        if raw_text.startswith(field_key + " "):
            return f"{chinese_name}：请输入不小于 0 的有效数值。"

    if "内部身份" in raw_text:
        return "测点配置无法读取，请移除该测点后重新添加数据文件；详细原因见运行日志。"
    if "布尔值" in raw_text:
        return "导出设置无效，请在“高级设置”中重新选择导出选项。"
    if "首版仅支持" in raw_text:
        return "当前处理参数不受支持，请在“高级设置”中恢复默认参数后重试。"

    if any(
        err_token in raw_text
        for err_token in ("Permission denied", "WinError 5", "Access is denied")
    ):
        if ".dat" in raw_text.lower():
            return "无法读取 DAT 数据文件，请检查文件是否被其他程序占用或是否具备读取权限。"
        if ".csv" in raw_text.lower():
            return "无法保存 CSV 文件，请先关闭正在占用该文件的程序（如 Excel），或更换保存位置后重试。"
        return "无法访问所需文件或文件夹，请查看详细信息中的路径并检查访问权限。"

    if any(
        err_token in raw_text
        for err_token in ("No such file", "WinError 2", "WinError 3")
    ):
        if ".dat" in raw_text.lower():
            return "找不到指定的 DAT 数据文件，请确认文件未被移动或删除，并重新设置该测点的数据文件。"
        return "找不到所需的文件或文件夹，请查看详细信息中的路径后重新选择。"

    if any(
        err_token in raw_text
        for err_token in (
            "有效观测",
            "未找到观测",
            "没有通过校验的OBS观测数据",
            "未找到有效的 OBS 观测数据",
        )
    ):
        return "文件中未读取到有效的 OBS 观测记录，请检查 DAT 文件内容是否完整或更换数据文件。"

    if any(
        err_token in raw_text
        for err_token in ("后台", "Traceback", "Error", "Exception", "核查快照")
    ):
        return "处理未能完成，请展开运行日志查看具体原因后重试。"

    return raw_text
