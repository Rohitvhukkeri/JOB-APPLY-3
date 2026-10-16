@echo off
REM Sends LinkedIn connection requests to people whose hiring posts match your skills.
REM Max 15 per run, one request per person ever (connect_log.csv). Add --dry-run to preview.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" linkedin_connect.py %*
) else (
    py linkedin_connect.py %*
)
pause
