@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Keep this file pure ASCII, see the note in the launcher .bat

set PY=C:\Users\Think\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python

echo Using interpreter: %PY%
"%PY%" -m pip install -r requirements.txt
echo.
pause
