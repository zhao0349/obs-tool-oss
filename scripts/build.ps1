# 使用 Nuitka 构建应用；参数控制编译模式、工具链与输出目录。
param(
    [switch]$Console,
    [ValidateSet('zig', 'msvc')]
    [string]$Compiler = 'zig',
    [string]$OutputDir = 'build',
    [string]$ReportDir = 'validation',
    [ValidateSet('onefile', 'standalone')]
    [string]$Mode = 'onefile',
    [switch]$DebugChecks,
    [switch]$DisableFreelists
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:TEMP = Join-Path (Get-Location) 'validation/tmp'
$env:TMP = $env:TEMP
$env:NUITKA_CACHE_DIR = Join-Path (Get-Location) 'build/cache'
# Zig 0.16 的 #embed 常量对象缓存会复用旧内容，每次构建独立缓存。
$buildCacheId = [guid]::NewGuid().ToString('N')
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path (Get-Location) "$OutputDir/compiler-cache/$buildCacheId/global"
$env:ZIG_LOCAL_CACHE_DIR = Join-Path (Get-Location) "$OutputDir/compiler-cache/$buildCacheId/local"
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$python = Join-Path (Get-Location) '.venv-obs-release/Scripts/python.exe'
if ($Compiler -eq 'zig') {
    # 编译器也是锁定依赖，不能依赖历史构建缓存中的隐式下载。
    $zigDir = & $python -c 'import pathlib, ziglang; print(pathlib.Path(ziglang.__file__).parent)'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $zigDir 'zig.exe'))) {
        throw '缺少 Zig 编译器，请先在 .venv-obs-release 中安装 requirements.lock'
    }
    $env:PATH = "$zigDir;$env:PATH"
}
New-Item -ItemType Directory -Force -Path validation | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir, $ReportDir | Out-Null
$version = & $python -c 'from obs_tool import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0) {
    throw '无法读取版本号'
}
$fileName = "OBS 数据处理工具-$version-preview.exe"
$argsList = @(
    '-m'
    'nuitka'
    "--mode=$Mode"
    '--update-check=never'
    '--experimental=force-dependencies-pefile'
    '--enable-plugin=pyside6'
    '--include-package=obs_tool'
    '--include-data-dir=src/obs_tool/assets=obs_tool/assets'
    '--include-qt-plugins=platforms'
    "--output-dir=$OutputDir"
    "--output-filename=$fileName"
    '--windows-icon-from-ico=assets/branding/app-icon-v1/windows/obs-tool.ico'
    "--file-version=$version.0"
    "--product-version=$version.0"
    '--company-name=zhao0349'
    '--product-name=OBS 数据处理工具'
    '--file-description=OBS 数据处理工具 预览版'
    "--report=$ReportDir/nuitka-report.xml"
    '--assume-yes-for-downloads'
    'scripts/launch.py'
)
if ($Mode -eq 'onefile') {
    $argsList += "--onefile-tempdir-spec={TEMP}/OBSDataTool-$version-{PID}-{TIME_US}",
        '--onefile-cache-mode=temporary'
}
if ($Compiler -eq 'zig') {
    $argsList += '--zig'
} else {
    $argsList += '--msvc=latest'
}
$argsList += '--noinclude-qt-plugins=imageformats,tls'
$argsList += '--disable-ccache'
if (-not $Console) {
    $argsList += '--windows-console-mode=disable'
}
if ($DebugChecks) {
    $argsList += '--debug', '--no-debug-c-warnings', '--no-debug-immortal-assumptions'
}
if ($DisableFreelists) {
    $argsList += '--experimental=disable-freelist-all'
}
& $python @argsList 2>&1 | Tee-Object -FilePath "$ReportDir/build.log"
if ($LASTEXITCODE -ne 0) {
    throw "构建失败，查看 $ReportDir/build.log"
}
& $python scripts/audit_release.py "$OutputDir/launch.dist" "$ReportDir/payload-audit.json"
if ($LASTEXITCODE -ne 0) {
    throw '发行物审计未通过'
}
if ($Mode -eq 'onefile') {
    & $python scripts/audit_release.py "$OutputDir/$fileName" "$ReportDir/onefile-audit.json"
    if ($LASTEXITCODE -ne 0) {
        throw '单文件产物审计未通过'
    }
}
