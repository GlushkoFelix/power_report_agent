@echo off
rem ==========================================================================
rem  智电析报 · 一键启动（薄启动器；逻辑在 start.ps1）
rem  用法：start.bat [--no-open] [--no-reload] [--no-pause]
rem  说明：前端端口会**自动选**（避开 Windows 保留端口段，见 start.ps1 顶部注释）
rem ==========================================================================
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
  echo %* | findstr /i "no-pause" >nul || pause
)
endlocal
