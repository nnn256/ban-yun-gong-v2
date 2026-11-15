@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-workbench.ps1"
if errorlevel 1 (
  echo.
  echo 搬运工V2 启动失败。请检查 runtime\logs 和 runtime\AstrBot\data\logs 中的错误日志。
  pause
  exit /b 1
)
endlocal
