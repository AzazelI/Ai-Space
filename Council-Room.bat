@echo off
REM Council Room launcher — double-click from inside the repo (portable).
cd /d "%~dp0"
where py >nul 2>nul && (py app.py) || (python app.py)
if errorlevel 1 pause
