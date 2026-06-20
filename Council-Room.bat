@echo off
REM Council Room launcher — double-click to open the app.
REM Absolute path so it works from the Desktop or anywhere.
cd /d "C:\Users\User\.gemini\antigravity\scratch\Ai-Space"
where py >nul 2>nul && (py app.py) || (python app.py)
REM Keep the window open only if the app exited with an error.
if errorlevel 1 pause
