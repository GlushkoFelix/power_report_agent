@echo off
rem ==========================================================================
rem  智电析报 · 停止服务（薄启动器；逻辑在 stop.ps1）
rem  用法：stop.bat [--no-pause]
rem  说明：只停"本项目"的后端(8000)与前端端口，带祖先链保护与进程白名单，
rem        不会误杀你的终端/IDE，也不影响 03.ppt 的预览服务。
rem ==========================================================================
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
  echo %* | findstr /i "no-pause" >nul || pause
)
endlocal
