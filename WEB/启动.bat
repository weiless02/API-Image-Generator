@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  NOTE: keep this file pure ASCII.
rem  cmd.exe reads .bat files byte-by-byte using the system OEM code
rem  page, so non-ASCII characters can desync the parser and turn a
rem  comment line into a bogus command. Chinese notes live in the
rem  .md file next to this one instead.
rem ---------------------------------------------------------------

set PY=C:\Users\Think\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python

rem Uncomment the next line to fake image generation locally
rem (instant, no API calls). Useful when tweaking the UI.
rem set A6_MOCK=1

rem API key and output dir now live in settings.json, edited from the web UI
rem (the gear button at the bottom-left of the page).
rem
rem The two conditions below are for a ONE-TIME migration only: on the very
rem first run there is no settings.json yet, so an old key.bat (if present)
rem is loaded once and app.py moves the key into settings.json. After that
rem the first condition is false forever and key.bat is never read again.
if not exist settings.json if exist key.bat call key.bat

"%PY%" app.py
pause
