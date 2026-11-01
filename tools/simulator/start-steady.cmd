@echo off
rem Simulated field, steady pace (long tests): keep this window open (Ctrl+C or closing it stops the field).
"%~dp0runtime\python.exe" "%~dp0sim\sim.py" start --steady
echo.
pause
