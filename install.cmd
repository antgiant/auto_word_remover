@echo off
setlocal
rem Install launcher for cmd.exe / double-click.
rem Sets up voice_to_text + profanity_filter's requirements. Extra flags pass
rem straight through, e.g.:  install.cmd -IncludeStemmer -IncludeOcr

set "ROOT=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%install.ps1" %*
set "RC=%ERRORLEVEL%"

echo(
echo [install] done (exit %RC%)
pause
exit /b %RC%
