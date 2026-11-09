@echo off
rem HTSG: put a new production.json to use; the databases and the history are not touched.
rem Asks for administrator rights, then runs scripts\change_config.py with the Python of this folder.
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
"%~dp0runtime\python.exe" "%~dp0scripts\change_config.py"
echo.
pause
