"""OBS 数据处理工具包级元数据定义模块。

定义内容：
- `__version__`：当前软件发行版本号；
- `APP_NAME`：软件主窗口与关于信息统一使用的中文全称；
- `AUTHOR`、`AUTHOR_URL`、`AUTHOR_EMAIL`：公开作者署名、主页与联系邮箱；
- `PROJECT_URL`、`ISSUES_URL`：项目与问题反馈入口；
- `ALGORITHM_VERSION`：写入导出 CSV 诊断清单（manifest）中的核心分层提取算法版本标识。
"""

__version__ = "0.6.0"
APP_NAME = "OBS 数据处理工具"
AUTHOR = "zhao0349"
AUTHOR_URL = "https://github.com/zhao0349"
AUTHOR_EMAIL = "zxy0349@qq.com"
PROJECT_URL = "https://github.com/zhao0349/obs-tool-oss"
ISSUES_URL = f"{PROJECT_URL}/issues"
ALGORITHM_VERSION = "custom-layers-3"
