@echo off
rem ============================================================================
rem 源码模式的开机自启动入口（打包版不用这个文件，注册表直接指向 exe）。
rem
rem 为什么不能直接写 `pyw app.py`：
rem   README 让用户把依赖装进项目的 .venv，而 `pyw` 用的是**系统** Python。
rem   系统 Python 里没有 webview/playwright -> ModuleNotFoundError；
rem   而 pyw 没有控制台、start 又立刻返回 -> 用户看不到任何错误，
rem   表现为"开机后什么都没发生"。
rem 所以优先用项目内的 .venv，找不到才退回系统 Python。
rem ============================================================================
setlocal
cd /d "%~dp0"

set "HW_PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%HW_PYW%" set "HW_PYW=pyw"

start "" "%HW_PYW%" "%~dp0app.py"
endlocal
