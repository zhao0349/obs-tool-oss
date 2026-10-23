# OBS 应用图标

图标由蓝色磨砂水面与水下探头组成，采用圆角外形、透明背景、青蓝渐变、白色探头与竖直缆线。

- `source/approved-original.png`：1254×1254 原始母版。
- `windows/obs-tool.ico`：供 EXE 使用的多尺寸图标。
- `png/`：透明 PNG 派生文件；运行时副本在 `src/obs_tool/assets/branding/`。
- `export_icons.py`：从母版再导出的脚本，需要 Pillow，正常构建无需运行。
- `preview-light-dark.png`：素材在浅色和深色背景下的预览。

修改图标时先保留母版，核对最小尺寸及透明度；不要把 PNG 包进 SVG 冒充矢量图。导出脚本产生的 ZIP 不纳入源码版本控制。运行窗口采用 PNG，EXE 使用 ICO，以现有资源路径和构建脚本为准。

图标及其派生图片由 [zhao0349](https://github.com/zhao0349) 按 [CC BY-NC-SA 4.0](../../../ASSET_LICENSE.md) 提供；导出脚本仍为 GPL-3.0-or-later。
