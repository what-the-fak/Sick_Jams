@echo off
rem Double-click to regenerate your Sick Jams mixes in iTunes (Windows).
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m sick_jams %*) else (python -m sick_jams %*)
echo.
pause
