@echo off
setlocal
rem Voice_to_Text launcher for cmd.exe / drag-and-drop / SendTo.
rem Drop audio or video files onto this file, or:  stt.cmd "file.mp3" [options]

set "ROOT=%~dp0"
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
set "PY=%ROOT%.venv\Scripts\python.exe"

set "PYTHONIOENCODING=utf-8"

if not exist "%PY%" (
    echo [Voice_to_Text] venv missing - see README.md
    pause
    exit /b 1
)

"%PY%" -X utf8 "%ROOT%transcribe.py" %*
set "RC=%ERRORLEVEL%"

rem keep the window open when launched by double-click / drag-drop
echo(
echo [Voice_to_Text] done (exit %RC%)
if "%~1"=="" pause
if not defined PROMPT pause
exit /b %RC%
