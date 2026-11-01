@echo off
rem Simulated installation: remove it and everything it made (asks for administrator rights; lists first, asks to confirm).
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
"%~dp0runtime\python.exe" "%~dp0sim\cleanup.py" %*
echo.
pause
