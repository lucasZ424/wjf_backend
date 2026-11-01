@echo off
rem HTSG one-click install: asks for administrator rights, then runs setup\install.py
rem with the Python bundled in this package.  Safe to run again: finished steps are skipped.
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
"%~dp0runtime\python.exe" "%~dp0setup\install.py" %*
echo.
pause
