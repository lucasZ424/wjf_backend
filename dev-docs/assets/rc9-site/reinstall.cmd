@echo off
rem HTSG: remove the installed program, its databases and ALL its data, then install again from this package.
rem Asks for administrator rights, then runs setup\reinstall.py with the Python of this package.
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
"%~dp0runtime\python.exe" "%~dp0setup\reinstall.py" %*
echo.
pause
