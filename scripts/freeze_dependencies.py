"""将当前 Python 环境中运行与 Nuitka 构建依赖的精确版本号导出至 `requirements.lock` 锁文件。

使用约束：
    仅在明确升级或调整依赖版本并完成全套回归与打包验证时手动执行；
    日常业务逻辑开发与代码注释整理绝不重新冻结环境锁文件。
"""

from importlib.metadata import version
from pathlib import Path

LOCKED_PACKAGES = [
    "Nuitka",
    "ordered-set",
    "PySide6",
    "PySide6_Addons",
    "PySide6_Essentials",
    "shiboken6",
    "zstandard",
    "setuptools",
    "ziglang",
]

Path("requirements.lock").write_text(
    "\n".join(f"{pkg}=={version(pkg)}" for pkg in LOCKED_PACKAGES) + "\n",
    encoding="utf-8",
)
