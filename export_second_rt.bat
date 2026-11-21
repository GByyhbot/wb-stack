@echo off
rem Save a snapshot of the CURRENTLY logged-in WorkBuddy account (auth file + refresh token).
rem Run this while the target account is logged in on the desktop client.
chcp 65001 >nul
echo ============================================
echo  WorkBuddy account snapshot
echo ============================================
set /p NAME=给当前登录的账号起个代号（如 主号/备用1）:
python "%~dp0wb-rotator\account_switch.py" snapshot --name "%NAME%"
echo.
echo Done. 可关闭本窗口并切换/继续使用客户端。
pause
