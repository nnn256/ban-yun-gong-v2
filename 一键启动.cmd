@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
rem Native launcher verifies update-and-start.ps1 and runs services without a console.
if not exist "%~dp0mover-v2-launcher.exe" exit /b 2
start "" /b "%~dp0mover-v2-launcher.exe" --background %*
exit /b %errorlevel%
