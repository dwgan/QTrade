@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo QTrade 虚拟环境不存在。
    echo 请先按照 README.md 完成安装。
    pause
    exit /b 1
)

echo 正在启动 QTrade 本地操作界面...
echo 浏览器将自动打开。关闭本窗口或按 Ctrl+C 可停止服务。
echo.
".venv\Scripts\python.exe" -m qtrade ui

if errorlevel 1 (
    echo.
    echo QTrade 启动失败，请查看上方错误信息。
    pause
)
