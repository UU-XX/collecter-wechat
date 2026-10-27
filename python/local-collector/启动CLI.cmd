@echo off
chcp 65001 >nul
rem 使用现有控制台 Python 打开本地读取菜单，不自动开始任务。
cd /d "%~dp0"
where python.exe >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python，请使用完整的 Collecter CLI 便携包。
  pause
  exit /b 1
)
python.exe -B "%~dp0cli.py" %*
set "collector_cli_exit=%errorlevel%"
if not "%collector_cli_exit%"=="0" pause
exit /b %collector_cli_exit%
