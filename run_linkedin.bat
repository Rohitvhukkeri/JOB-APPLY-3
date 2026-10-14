@echo off
REM Runs the LinkedIn Easy Apply auto-apply from this folder using the project's virtualenv.
REM If you're not logged in yet, a browser opens for you to log in and the run then starts by itself.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" linkedin_apply.py
) else (
    py linkedin_apply.py
)
pause
