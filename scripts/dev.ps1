# 使用当前锁定环境启动源码 GUI 或合成数据测试。
param(
    [ValidateSet('gui', 'test')]
    [string]$Action = 'gui',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ToolArgs
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:TEMP = Join-Path (Get-Location) 'validation/tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$python = Join-Path (Get-Location) '.venv-obs-release/Scripts/python.exe'
if ($Action -eq 'test') {
    $env:OBS_RECORDS_DIR = Join-Path (Get-Location) 'validation/test-records'
    & $python -m unittest discover -s tests -v
} else {
    & $python -m obs_tool
}
exit $LASTEXITCODE
