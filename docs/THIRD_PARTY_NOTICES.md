# 第三方组件与分发说明

自有代码、脚本、测试和文档：作者 [zhao0349](https://github.com/zhao0349)，GPL-3.0-or-later，正文见根目录 [LICENSE](../LICENSE)。自有图标单独采用 CC BY-NC-SA 4.0，范围见 [ASSET_LICENSE.md](../ASSET_LICENSE.md)。第三方组件及保存的上游资料保留原始版权和许可，不由本项目重新授权。

## 运行时与构建工具

依赖版本固定在 [requirements.lock](../requirements.lock)。安装某个 wheel 不等于其全部模块都进入最终 EXE；发行时以载荷审计和实际构建为准。

| 组件 | 版本/角色 | 许可与来源 |
|---|---|---|
| CPython | Python 3.11–3.12；云端构建固定 3.12.10 | PSF 及其所含第三方许可；[Python 许可](https://docs.python.org/3/license.html)。发行时记录实际版本。 |
| PySide6 / shiboken6 | 6.8.3，运行时 | wheel 元数据声明 LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only；具体文件/模块和内含第三方代码按实际许可。[官方许可](https://doc.qt.io/qtforpython-6.8/licenses.html)、[6.8.3 源码](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.8.3-src/)。 |
| Qt | 6.8.3，随 PySide6 提供 | 不同模块可能适用 LGPLv3、GPLv3 或第三方许可，不能笼统将所有模块称为 LGPL。[Qt 6.8 许可](https://doc.qt.io/qt-6.8/licensing.html)、[源码](https://download.qt.io/archive/qt/6.8/6.8.3/submodules/)。 |
| OpenSSL / libffi | 随实际 Python 运行库 | Apache-2.0 / MIT；[OpenSSL](https://github.com/openssl/openssl)、[libffi](https://github.com/libffi/libffi)。 |
| Nuitka | 4.3，编译工具及部分运行时 | AGPLv3；适用文件附 Nuitka Runtime Library Exception，允许符合例外的编译输出采用自有程序许可。[固定版本例外正文](https://github.com/Nuitka/Nuitka/blob/4.3/LICENSE-RUNTIME.txt)。例外不改变编译器自身或第三方组件的许可。 |
| zstandard | 0.25.0，构建/压缩支撑 | Python 绑定 BSD-3-Clause；底层 zstd 及实际编入部分按各自许可。 |
| ordered-set | 4.1.0，构建工具依赖 | MIT；[上游](https://github.com/rspeer/ordered-set)。 |
| ziglang | 0.16.0，构建编译器 | Zig 主体 MIT；工具链所含 LLVM、C 库等有各自许可，不能概括为全部 MIT。[上游](https://github.com/ziglang/zig)。 |
| setuptools | 84.0.0，构建后端 | MIT；[上游](https://github.com/pypa/setuptools)。 |
| Microsoft 运行库 | 视实际发行载荷 | 按微软再分发条款处理；不是本项目 GPL 代码。 |

## 保留的许可资料

`docs/licenses/` 保存上游原始正文、版权和来源，Qt 子目录保留上游路径；其中包含某项声明不代表该组件一定进入程序。不要为了精简文档删除许可正文或将第三方资料统一改成 GPL。`SOURCES.txt` 中的校验值用于辨认既有上游归档，不代替发行物对应源码核验。

`verify_preview.py` 会把根目录 LICENSE、ASSET_LICENSE.md、本说明、`docs/licenses/` 和构建环境的 Python 许可整理到通知 ZIP。此步骤只整理许可资料，不证明已提供全部对应源码，也不证明图标与代码组合分发已符合全部条件。

## 二进制发行前的要求

1. 记录精确应用提交、Python/OpenSSL、PySide6/Qt、Nuitka 和实际运行库版本；保留构建脚本与依赖锁，公开取得与二进制一致的自有源码和构建所需材料。
2. 选择并落实实际组件的许可路径。若依 GPLv3 分发组合物，按其第 6 节提供相应源码；若保留 LGPL 组件路径，还须核实 onefile 形态下修改库、重新链接/替换及调试修改库所需安排。
3. 核实对应源码是否覆盖必要的库和构建材料、获取位置与版本是否准确。仅提供项目首页、通用上游链接或自动源码 ZIP 不足以自动认定符合要求。依据：[GPLv3 正文](https://www.gnu.org/licenses/gpl-3.0.html)、[Qt 官方义务说明](https://www.qt.io/development/open-source-lgpl-obligations)。
4. 自有图标的非商业许可不能附加到 GPL 代码。保留图标的发行包同时涉及受限素材，不能宣称整体全部 GPL；需要可商用的完整包时替换图标或取得额外授权。最终组合物的可分离性及分发条件须单独核实。

当前待办集中在仓库的 [ROADMAP](https://github.com/zhao0349/obs-tool-oss/blob/main/docs/ROADMAP.md)。本页说明已识别的许可与交付要求，不把源码许可声明等同于最终二进制许可验收。
