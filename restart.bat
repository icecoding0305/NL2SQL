@echo off
chcp 65001 >nul
echo ============================================
echo   NL2SQL 一键重启(后端 8000 + 前端 5173)
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" restart
echo.
echo 完成后按任意键关闭窗口...
pause >nul
