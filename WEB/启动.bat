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

rem Load the local API key. key.bat is git-ignored (template: key.bat.example).
rem If you have not created it, A6_API_KEY stays unset and app.py says so.
if exist key.bat call key.bat

"%PY%" app.py
pause
