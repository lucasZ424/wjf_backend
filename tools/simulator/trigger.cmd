@echo off
rem Simulated field: pick one scenario from a numbered list and trigger it.
"%~dp0runtime\python.exe" "%~dp0sim\sim.py" menu
echo.
pause
