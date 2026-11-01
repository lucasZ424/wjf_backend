@echo off
rem Point sheet tool: drop the filled point sheet on this file (or double-click to use the one beside it);
rem writes production.json beside this file, checked by the backend's own rules.
"%~dp0runtime\python.exe" "%~dp0launch.py" convert %1
echo.
pause
