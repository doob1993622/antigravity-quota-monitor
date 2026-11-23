@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "LAUNCHER=%SCRIPT_DIR%..\sidecars\quota_floating\launch.mjs"
set "WIDGET=%SCRIPT_DIR%quota_floating_widget.pyw"

where node >nul 2>nul
if %ERRORLEVEL%==0 (
  node "%LAUNCHER%" --detached
  exit /b 0
)

where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
  start "" pythonw "%WIDGET%"
  exit /b 0
)

where pyw >nul 2>nul
if %ERRORLEVEL%==0 (
  start "" pyw "%WIDGET%"
  exit /b 0
)

echo [quota-monitor] 未找到 Node.js 或 Python (PyQt5)。请先安装 Python 与 PyQt5: pip install PyQt5
pause
