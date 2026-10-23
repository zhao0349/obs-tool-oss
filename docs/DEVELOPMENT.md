# 开发指南

本文说明 Windows 源码开发、自动化测试和 Nuitka 构建。项目脚本使用 PowerShell 7；终端宿主可自行选择。

## 目录

| 路径 | 用途 |
|---|---|
| `src/obs_tool/` | 当前应用源码及运行所需图标资源 |
| `tests/` | 可重复执行的合成数据测试 |
| `scripts/` | 开发入口、构建、发行物检查、结果对照和依赖锁定 |
| `examples/` | 配置格式示例，输入路径需按实际数据修改 |
| `assets/` | 图标母版、派生文件和导出脚本 |
| `docs/` | 使用、算法、维护、开发和第三方资料 |
| `.venv-obs-release/` | 现有启动、测试和构建脚本使用的虚拟环境路径，不纳入版本控制 |

## 生成物与提交范围

`build/`、`dist/`、`validation/`、虚拟环境和 Python 缓存不纳入源码版本控制。仓库提交保留源码、可重复合成测试、必要资源、依赖声明、构建工具和有效文档；日志、截图及样本在提交前检查敏感信息。

测试与诊断建议使用独立输出目录，避免覆盖原始 DAT、用户配置、规则库和实际结果。原始数据及时间校正备份属于使用者的数据，不作为生成物删除。开发者可按自身排错需要保留本地构建和诊断材料；PR 应提供足以复核结论的版本、步骤及脱敏结果。

## 从零准备环境

项目声明支持 Python 3.11–3.12；源码测试工作流使用 Python 3.12，预览构建工作流固定为 Windows x64、Python 3.12.10。PySide6 6.8.3、Nuitka 4.3 和 Zig 0.16.0 等依赖固定在 `requirements.lock`。以下命令使用 PowerShell 7。

已有可用环境时不必重装。新电脑先安装 Python 3.12 x64，再执行（若未安装 Python Launcher，使用对应 python.exe 的完整路径替代首行的 `py -3.12`）：

```powershell
py -3.12 -m venv .venv-obs-release
& ./.venv-obs-release/Scripts/python.exe -m pip install -r requirements.lock
& ./.venv-obs-release/Scripts/python.exe -m pip install --no-deps --no-build-isolation -e .
& ./.venv-obs-release/Scripts/python.exe -m pip check
```

`.venv-obs-release` 是现有 PowerShell 脚本使用的环境路径。其他满足依赖的环境也可设置 `PYTHONPATH=src` 后执行 `python -m obs_tool` 或 `python -m unittest discover -s tests -v`。仓库脚本会自动设置 `PYTHONPATH=src`。图标已随源码提供，正常构建不需要 Pillow；只有重新导出图标时才需要它。

## 运行与测试

### 面向维护者的代码风格与模块职责

采用清晰分段、中文 docstring 和明确类型定义。现行源码、测试、开发脚本和素材导出脚本都应避免分号串接、多步骤单行及多层三元表达式。排版遵循 `pyproject.toml` 中的 Black 配置；格式化只能处理排版，不能替代变量命名、状态语义和注释准确性审查。

`src/obs_tool/data_types.py` 描述核心字典字段（如 `Observation`、`Segment`、`LayerSelection`、`HourRow`、`PointProcessResult`、`DepthEstimate`、`CorrectionEntry`、`CurveContext`、`BatchRunReport` 等 `TypedDict`），`models.py` 保留带验证的配置对象（`Point`、`Rules`、`Batch`）。核心观测使用 `datetime`，磁盘 JSON 使用文本；显示层仍须验证读取结构。说明优先记录单位、输入前提、返回值、异常、何时修改草稿或提交结果，以及取消和备份边界。注释与实现不一致时先按实现核实，不能只润色错误说明。Qt 事件方法沿用框架名称，持久化键和兼容导出不得仅为命名美观而改动。

#### `src/obs_tool/` 核心模块分层导览

| 分层 | 模块文件 | 核心职责说明 |
|---|---|---|
| **类型与配置模型** | `data_types.py`、`models.py`、`config.py`、`rule_store.py`、`input_policy.py` | 定义全项目 `TypedDict` 字典结构、数据类配置校验、批次 JSON 相对路径解析、自定义规则库原子持久化与输入容量边界。 |
| **读取与核心算法** | `readers.py`、`depth.py`、`algorithm.py`、`processing.py`、`exporters.py`、`record_store.py` | 流式解析 `.dat` 观测文件、智能估算参考水深、切分观测片段（含尾部 4 点浅水状态机）、六点法/自定义分层就近选点、CSV 与归档 JSON 原子导出。 |
| **时间校正与曲线抽稀** | `correction_actions.py`、`time_correction.py`、`curve_context.py`、`result_projection.py`、`editing.py` | 识别完整下放—回收观测动作、规划与落盘整点/已知关系时间校正（含 `.bak.dat` 备份与事务回滚）、构建整点选点双时间双水深投影及过程线极值保真抽稀。 |
| **子进程与系统桥接** | `entrypoint.py`、`runner.py`、`worker_process.py`、`analysis_worker.py`、`analysis_process.py`、`correction_worker.py`、`curve_loader.py`、`ipc_reader.py`、`native_shell.py`、`native_picker.py` | 统一分发 GUI 与内部后台子进程、无阻塞 NDJSON IPC 管道通信、协作式取消以及 Windows 原生 `IFileOpenDialog` COM 文件选择器。 |
| **桌面界面与交互组件** | `desktop.py`、`workspace_view.py`、`result_view.py`、`curve_canvas.py`、`point_editors.py`、`bulk_dialog.py`、`depth_review.py`、`dialogs.py`、`rule_dialogs.py`、`layer_editor.py`、`tools_dialog.py`、`datetime_input.py`、`check_header.py`、`help_dialog.py`、`theme.py`、`branding.py`、`user_messages.py`、`ui_verification.py` | 双页签主窗口（草稿/运行/已完成三态隔离）、结果复核工作台、自绘水深时间曲线画布、专项业务弹窗、Fluent 风格主题及 `--verify-ui` 真实桌面自动化自检。 |

排版工具与应用运行依赖分开管理。可在独立工具环境安装 `black==24.10.0`，从仓库根目录执行 `python -m black --check src tests scripts assets/branding/app-icon-v1/export_icons.py`；去掉 `--check` 会修改文件。格式化帮助模块时核对文本内容，保持内嵌帮助与 `docs/help_inline.txt` 一致。

可读性整理应分开审查纯排版、说明/类型和等价控制流修改；对纯排版可比较 AST，对其余修改使用相关回归测试与源码差异检查。云端回归和本机界面验证的边界仍按本指南执行。

```powershell
pwsh -File scripts/dev.ps1 gui
pwsh -File scripts/dev.ps1 test
```

测试使用合成数据，临时输出放在 `validation/`。

## 从空目录构建

安装上述依赖后即可自行构建，下面使用独立输出目录：

```powershell
pwsh -File scripts/build.ps1 -Mode onefile -OutputDir build/current -ReportDir validation/build
```

输出为 `build/current/OBS 数据处理工具-<版本号>-preview.exe`（文件名随源码版本变化）。脚本从 `scripts/launch.py` 编译 `src/obs_tool`，读取运行资源及 `assets/branding/app-icon-v1/windows/obs-tool.ico`，并检查最终载荷。Zig 编译器由 requirements.lock 固定并安装在当前虚拟环境，构建脚本显式选用它；不再依赖历史缓存或未锁定的自动下载。首次安装依赖及必要辅助工具需要网络；下载失败时不能以旧 EXE 充当成功结果。默认 Zig，无需另装 Visual Studio；MSVC 为可选路线。

构建日志和审计报告写入 `ReportDir`。需验证源码与编译版数值一致时，使用合成批次和独立输出目录：

```powershell
$env:PYTHONPATH = 'src'
$env:OBS_RECORDS_DIR = Join-Path (Get-Location) 'validation/records'
& ./.venv-obs-release/Scripts/python.exe scripts/compare_preview.py <批次JSON> <EXE路径> <验证输出目录>
```

不要把实际业务输出目录设为验证目录。第三方许可正文位于 `docs/licenses/`；发行资料要求见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 脚本职责

- `dev.ps1`：启动 GUI 或单元测试。
- `build.ps1`、`launch.py`：编译参数及应用入口。
- `audit_release.py`：检查编译产物是否夹带源码、调查数据或调试中间文件。
- `compare_preview.py`：同一批次的源码/EXE 结果对照，三个路径参数都必须明确指定。
- `freeze_dependencies.py`：有意调整依赖并验证后更新锁文件，日常不要运行。

## 当前源码的输入与工具入口

`python -m obs_tool` 无参数启动 GUI；`--worker`、`--analyse`、`--curve-context`、`--native-picker`、`--correction` 是 GUI 管理的内部进程入口，不是公开批处理 CLI。程序不导入 CSV 配置或自动搜寻 DAT，输入必须显式列出完整文件集合。任务、结果 JSON 与规则库使用应用格式；兼容字段 `legacy_export` 映射为 `complete_rows_only`，`input_dir` 不触发文件搜索。

容量策略集中于 `input_policy.InputLimits`，DAT 使用流式有界读取，JSON 解析前检查字节与嵌套深度。64MiB 单文件、128MiB 单点、256MiB 整批是保守内存预算，不是仪器时长限制；观测还会构建排序/去重结构，因此不按原始文件大小估算唯一内存开销。调整容量边界需评估实际资源开销，并覆盖超限拒绝与取消路径。测试通过注入小预算及模拟超大文件元数据检查拒绝，无须生成压力数据。详细数值见 USER_GUIDE.md。

时间校正只对明确选择的一份 DAT 操作，使用独立 QProcess；保存阶段不强制杀进程，提交后校验失败执行回滚。开发测试只能用合成文件。`test_dat_inputs.py`、`test_time_correction.py`、`test_correction_ui.py` 分别覆盖格式/容量、业务方案/事务、真实控件/后台任务；`--verify-ui` 使用合成 DAT 验证本机 Windows 主流程。截图、原生对话框验证输出默认放在 validation 下，不纳入源码提交；Qt 尺寸模拟不能替代 Win10、真实系统 DPI 或 OneDrive 在线占位验收。

## GitHub 协作

仓库：https://github.com/zhao0349/obs-tool-oss 。`main` 保存已审核版本；从最新 `main` 建立短期分支，通过 PR 说明变更、验证和剩余事项。源码测试通过后由维护者评审合并；官方发布按下文流程验证具体交付物。

提交 PR 后检查评审讨论与 Actions 结果；具体合并条件以仓库的 [分支规则](https://github.com/zhao0349/obs-tool-oss/rules) 为准。改变 job 名称时同步核对所需检查。现有 Actions 使用仓库本地及 GitHub 官方来源，固定完整 SHA，工作流令牌只读。

新机器先安装 Git、Python 3.12 x64 和 PowerShell 7，再克隆并按上文建立环境。以下命令适用于有仓库写入权限的维护者；其他贡献者先 fork，将克隆地址换成自己的 fork，并向本仓库的 main 提交 PR：

```powershell
git clone https://github.com/zhao0349/obs-tool-oss.git
Set-Location obs-tool-oss
git config pull.ff only
git config fetch.prune true
git switch main
git pull --ff-only
git switch -c feature/short-description
# 完成修改与验证后，检查 git diff 和 git status
git add <本次需要提交的文件>
git commit -m "说明本次变更"
git push -u origin feature/short-description
```

在 GitHub 创建 PR，检查差异与测试结果后由维护者合并。使用自己的提交身份，可选择 GitHub noreply 邮箱；凭据由 Git Credential Manager/平台安全存储，不写入地址、代码、日志或文档。新电脑独立登录，不复制他人凭据。

Actions 保持最小读取权限、固定完整提交 SHA 和 `persist-credentials: false`。不使用 `pull_request_target` 执行外部 PR 代码，不把凭据传给贡献者代码，不自动批准 PR 或合并。仓库安全配置以 GitHub 实际设置为准。

`.github/workflows/test.yml` 在 `main` 推送、Pull Request 和手动触发时，以 Windows runner、Python 3.12 与锁定的 PySide6 依赖运行合成数据测试。工作流仅有 `contents: read` 权限，第三方 Actions 固定到提交，不自动打包、发布或上传测试产物。云端无界面测试不能代替真实 GUI 外观、Windows 10/11 无 Python 环境、发行物和许可验收。维护者通过 `build-preview.yml` 手动安排官方预览版构建。

参考：[GitHub Python 测试工作流](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)。CI 只安装测试所需运行依赖；本地从零构建仍按 `requirements.lock` 安装完整构建依赖。

仓库保留当前源码、可重复测试、图标素材、第三方许可、开发脚本和有效文档。虚拟环境、真实 DAT、用户结果、密钥、缓存和构建产物不提交。新加数据文件不能仅依赖 `.gitignore`，提交前必须检查暂存内容。不要提交本机绝对数据路径或敏感业务信息。


同一测试工作流另设 Ubuntu 24.04 的 `POSIX atomic write regression`，仅运行 `test_curve_context.py` 的 7 项标准库测试，不安装 GUI 或构建依赖。Windows 临时文件锁通过 exporters 模块内的 os 代理模拟，不修改全局 os.name；验证前两次占用后第 3 次替换成功。此任务验证测试可移植性，不代表支持 Linux 桌面版。

### 自动化验证与记录

源码测试在 PR 中运行，也可在本地复现。引用 CI 结果时注明具体提交和工作流；界面外观、系统对话框、DPI 及无 Python 目标电脑仍需相应环境验证。完整测试结果可复用，但修改涉及的行为应有对应证据。

Issues 与 PR 记录问题、评审和验证结果，ROADMAP 集中维护产品边界和待验收事项。公开日志、截图和附件只使用合成或经确认可分享的材料，不包含真实业务数据、用户配置或凭据。

源码测试工作流不上传 artifacts；预览构建的交付及失败诊断 artifacts 当前保留 14 天，日志遵循平台与仓库设置。正式 Release 按版本提供交付文件和对应源码，不受临时 artifacts 到期策略影响。新增工作流应说明权限、资源需求、超时及产物保留期。

## 预览版发布流程

维护者确定发布版本后，通过 PR 同步 `pyproject.toml`、`src/obs_tool/__init__.py` 中的版本号及 CHANGELOG。测试通过并合并后，手动触发 `build-preview.yml`，在 Windows runner 安装锁定依赖，执行 onefile 构建、载荷审计、自有模块编译检查、3 个合成测点的源码/EXE 对照及编译版时间校正备份检查。工作流只有读取权限，不创建 Release；上传交付文件及必要验证 JSON，保留 14 天。

下载本次产物核对 SHA256，并在本机执行最终 EXE 的 `--verify-ui`。无 Python 目标电脑等未覆盖事项如实保留。通过检查后，将精确构建提交标记为 `v<版本>`，创建 draft prerelease，上传 EXE、第三方通知 ZIP 和 SHA256SUMS.txt，核对资产完整后发布；不使用 latest，不覆盖既有标签或资产。Release 资产长期保留；公开前按第三方说明核实对应源码、图标许可和最终组合物的分发条件。

云端构建固定使用 Python 3.12.10，其余依赖沿用 requirements.lock；关于页显示实际 Python/OpenSSL 版本。验证报告记录运行时版本。Nuitka 报告内的 XML 1.0 禁用控制字符仅在解析副本中去除并记录数量，其他 XML 错误及非编译模块仍拒绝；失败时保留最小构建诊断 artifact 14 天，不上传未验证 EXE。

GitHub 会规范化中文资产名，`verify_preview.py` 在交付整理阶段使用 `OBS-Data-Tool-<版本>-preview.exe` 并据此生成校验清单；构建目录内中文名称及程序显示名称不变。

许可范围见根目录 LICENSE、ASSET_LICENSE.md 和第三方说明。GPL 代码允许商业使用；图标有单独的非商业限制。

贡献者也可依各文件许可证自行构建、修改和分发；上述官方发布流程不替代或缩减许可证授予的权利。
