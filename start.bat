@echo off
chcp 65001 >nul

:waitloop
ping -n 1 -w 1000 www.baidu.com >nul 2>&1
if errorlevel 1 (
    timeout /t 5 /nobreak >nul
    goto waitloop
)

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo 未找到虚拟环境 .venv
    echo 请先在当前目录执行：
    echo   python -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
start "" pythonw app.py