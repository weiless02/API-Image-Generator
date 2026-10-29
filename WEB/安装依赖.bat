@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Keep this file pure ASCII, see the note in the launcher .bat

rem Use the "python" found on PATH. See the note in the launcher .bat.
set PY=python

echo Using interpreter: %PY%
"%PY%" -m pip install -r requirements.txt
echo.
pause
