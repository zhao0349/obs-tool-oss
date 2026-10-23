"""支持 `python -m obs_tool` 命令行直接启动主界面或内部后台子进程的分派入口。"""

from .entrypoint import main

raise SystemExit(main())
