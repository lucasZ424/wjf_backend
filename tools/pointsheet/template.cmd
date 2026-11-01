@echo off
rem Point sheet tool: write the empty point sheet beside this file (never over a filled one).
"%~dp0runtime\python.exe" "%~dp0launch.py" template
echo.
pause
