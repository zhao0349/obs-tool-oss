@echo off
cd /d "%~dp0"
pwsh -NoProfile -File "%~dp0scripts\dev.ps1" gui
if errorlevel 1 pause
