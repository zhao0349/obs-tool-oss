"""Nuitka 独立可执行文件编译入口脚本：直接委托 `obs_tool.entrypoint.main` 统一分派 GUI 主窗口与内部后台子进程。"""

from obs_tool.entrypoint import main

raise SystemExit(main())
